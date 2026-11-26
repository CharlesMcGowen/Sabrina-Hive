"""Client for the shared lab coordinator. No direct fallback when enabled."""
from contextlib import contextmanager
import contextvars
import http.client
import json
import os
from pathlib import Path
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit
import uuid

from tools.worker_identity import DependencyUnavailable, executor_identity

ROOT = Path(__file__).resolve().parents[1]
CALLER = contextvars.ContextVar('sabrina_control_caller', default=None)
INFERENCE_DEADLINE = contextvars.ContextVar('sabrina_inference_deadline', default=None)
_owned_resources = threading.local()


def transient_transport_error(exc):
    """HTTP denials and invalid payloads are not connectivity failures."""
    return not isinstance(exc, urllib.error.HTTPError) and isinstance(
        exc, (urllib.error.URLError, ConnectionError, TimeoutError,
              http.client.RemoteDisconnected, http.client.IncompleteRead))


def settings():
    path = Path(os.environ.get('SABRINA_CONTROL_CONFIG', ROOT / 'config/lab_control_client.json'))
    value = json.loads(path.read_text(encoding='utf-8-sig')) if path.exists() else {}
    if os.environ.get('SABRINA_CONTROL_URL'):
        value.update(enabled=True, url=os.environ['SABRINA_CONTROL_URL'])
    return value


def configured(base=None):
    cfg = settings()
    if not cfg.get('enabled'):
        return None
    if base:
        url = urlsplit(base)
        if url.hostname not in cfg.get('managed_hosts', ['ego.local', '127.0.0.1', 'localhost', '192.168.1.133']):
            return None
        if url.port not in (None, 11434):
            return None
    return ControlClient(cfg)


class ControlClient:
    def __init__(self, config, client=None, project=None):
        self.base = config['url'].rstrip('/')
        token = os.environ.get('SABRINA_CONTROL_TOKEN')
        if not token:
            path = Path(config.get('token_file', 'data/control/client.token'))
            token = (path if path.is_absolute() else ROOT / path).read_text().strip()
        self.token = token
        caller = CALLER.get() or {}
        self.client = client or caller.get('client') or os.environ.get('SABRINA_CLIENT_ID', 'legacy-client')
        self.project = project or caller.get('project') or os.environ.get('SABRINA_PROJECT_ID', 'sabrina')
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def request(self, path, body=None, method=None):
        req = urllib.request.Request(self.base + path,
            data=None if body is None else json.dumps(body).encode(),
            headers={'Authorization': 'Bearer ' + self.token, 'Content-Type': 'application/json'},
            method=method)
        verb = req.get_method()
        safe = verb in ('GET', 'HEAD') or (
            verb == 'POST' and isinstance(body, dict) and (
                (path in ('/v1/inference', '/v1/objectives') and bool(body.get('operation_id')))
                or (path == '/v1/leases/renew' and all(body.get(k) for k in ('resource','owner','token')))))
        # Read retries and durable operation IDs never replay a browser action.
        # Acquire/release and all unkeyed writes are deliberately excluded.
        for attempt in range(3 if safe else 1):
            try:
                with self.opener.open(req, timeout=10) as response:
                    return json.load(response)
            except Exception as exc:
                if not safe or attempt == 2 or not transient_transport_error(exc):
                    raise
                time.sleep(.5 * (2 ** attempt))

    def infer(self, path, payload, timeout=150, operation_id=None, device=None, task_label=''):
        deadline = INFERENCE_DEADLINE.get()
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError('Inference wait budget exhausted before submission')
        key = operation_id or uuid.uuid4().hex
        body = {'client': self.client, 'project': self.project, 'operation_id': key,
                'path': path, 'payload': payload, 'request_seconds': min(300, max(10, timeout))}
        if device is not None:
            if device not in {'auto', 'cpu', 'gpu'}:
                raise ValueError('Invalid inference device')
            body['device'] = device
        if task_label:
            body['task_label'] = str(task_label)[:200]
        # A lost submission response is safe to retry with the same key. Inference is not replayed.
        t0 = time.time()
        receipt = None
        res = None
        failure = None
        try:
            receipt = self.request('/v1/inference', body)
            res = self.wait(receipt['id'], seconds=max(900, timeout + 60))
        except Exception as exc:
            failure = exc
        latency_ms = (time.time() - t0) * 1000.0
        try:
            from tools.network_flow_inspector import FLOW_INSPECTOR
            FLOW_INSPECTOR.record_flow(
                target_url=self.base + '/v1/inference',
                method='POST',
                status_code=200 if failure is None else 0,
                latency_ms=latency_ms,
                worker_id=f"coordinator_{self.client}",
                operation_id=key,
                request_headers={'Authorization': 'Bearer ' + self.token, 'Content-Type': 'application/json'},
                request_body=body,
                response_body=res if isinstance(res, dict) else {'result': str(res)[:500]},
                error_message='' if failure is None else (
                    f'{type(failure).__name__}: {failure}; '
                    f'coordinator_request_id={receipt["id"] if receipt else "unconfirmed"}'),
            )
        except Exception:
            pass
        if failure is not None:
            raise failure
        return res

    def wait(self, request_id, seconds=900):
        deadline = time.monotonic() + seconds
        scoped_deadline = INFERENCE_DEADLINE.get()
        if scoped_deadline is not None:
            deadline = min(deadline, scoped_deadline)
        while True:
            record = self.request('/v1/inference/' + request_id)
            if record['state'] == 'completed':
                return record['result']
            if record['state'] in {'failed', 'cancelled', 'uncertain'}:
                raise OSError(f"Lab request {request_id}: {record['state']}: {record.get('error', '')}")
            if time.monotonic() >= deadline:
                # Cancellation leaves running compute reserved until execution actually returns.
                self.request('/v1/inference/' + request_id + '/cancel', {})
                raise TimeoutError(f'Lab wait expired; retained request {request_id}')
            time.sleep(0.3)

    def submit(self, goal, context='', operation_id=None, tier='fast', device='auto', **extra):
        return self.request('/v1/objectives', {'client': self.client, 'project': self.project,
            'operation_id': operation_id or uuid.uuid4().hex, 'goal': goal, 'context': context,
            'tier': tier, 'device': device, **extra})

    def status(self, task_id):
        return self.request('/v1/objectives/' + task_id)

    def cancel(self, task_id):
        return self.request('/v1/objectives/' + task_id + '/cancel', {})

    @contextmanager
    def lease(self, resource, wait_seconds=90, *, queue_owner=None):
        """An optional stable owner polls once and retains its expiring native FIFO ticket."""
        owner = queue_owner or self.client + ':' + uuid.uuid4().hex
        deadline = time.monotonic() + wait_seconds
        token = None
        while token is None:
            try:
                reply = self.request('/v1/leases/acquire', {
                    'resource': resource, 'owner': owner, 'executor': executor_identity()})
            except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
                raise DependencyUnavailable('Coordinator unavailable before resource acquisition') from exc
            token = reply.get('token')
            if token is None:
                if queue_owner is not None:
                    yield None
                    return
                if time.monotonic() >= deadline:
                    self.request('/v1/leases/withdraw', {'resource': resource, 'owner': owner})
                    raise DependencyUnavailable('Waiting for resource: ' + resource)
                time.sleep(0.5)
        stop, lost = threading.Event(), threading.Event()
        identity = {'resource': resource, 'owner': owner, 'token': token}
        def heartbeat():
            while not stop.wait(20):
                try:
                    self.request('/v1/leases/renew', identity)
                except Exception:
                    lost.set()
                    return
        worker = threading.Thread(target=heartbeat, daemon=True)
        worker.start()
        try:
            yield identity
            if lost.is_set():
                raise OSError('Resource heartbeat lost; resource retained for reconciliation')
        except BaseException:
            try:
                self.request('/v1/leases/quarantine', identity)
            except Exception:
                pass
            raise
        else:
            try:
                self.request('/v1/leases/release', identity)
            except Exception as exc:
                if not transient_transport_error(exc):
                    raise
                # The release may have committed before the response was lost.
                held = next((r for r in self.request('/v1/status').get('leases', [])
                             if r['resource'] == resource), None)
                if held and held['owner'] == owner:
                    if held['state'] != 'held':
                        raise OSError('Uncertain lease retained for reconciliation') from exc
                    self.request('/v1/leases/release', identity)
        finally:
            stop.set()
            worker.join(1)


@contextmanager
def inference_wait_budget(seconds):
    """Bound all inference waits in this context; leave other callers unchanged."""
    if not 0 < seconds <= 86400:
        raise ValueError('Inference wait budget must be between 0 and 86400 seconds')
    deadline = time.monotonic() + seconds
    previous = INFERENCE_DEADLINE.get()
    token = INFERENCE_DEADLINE.set(min(previous, deadline) if previous is not None else deadline)
    try:
        yield
    finally:
        INFERENCE_DEADLINE.reset(token)


@contextmanager
def caller(client, project='sabrina'):
    token = CALLER.set({'client': client, 'project': project})
    try:
        yield
    finally:
        CALLER.reset(token)


@contextmanager
def resource_lease(resource, wait_seconds=90):
    owned = getattr(_owned_resources, 'names', set())
    if resource in owned:
        yield
        return
    client = configured()
    if client:
        with client.lease(resource, wait_seconds=wait_seconds):
            _owned_resources.names = owned | {resource}
            try:
                yield
            finally:
                _owned_resources.names = owned
    else:
        yield
