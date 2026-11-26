"""Direct lab HTTP with a cached resolved address; no local or cloud fallback."""
import json
import socket
import urllib.error
import urllib.request
from urllib.parse import urlsplit
import time

class LabHTTP:
    def __init__(self,base,managed=True):
        self.base=base.rstrip('/');self.cached=None
        self.managed=managed
        self.opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def request(self,path,payload=None,timeout=150):
        if self.managed and payload is not None and path in {'/api/chat','/api/generate','/api/embed','/api/embeddings'}:
            from tools.control_client import configured
            coordinator = configured(self.base)
            if coordinator:
                return coordinator.infer(path,payload,timeout)
        parsed=urlsplit(self.base)
        candidates=[self.cached] if self.cached else []
        error=None
        if not candidates:
            # mDNS occasionally fails even between /api/ps and /api/chat. Resolve once,
            # then retain a working address for this client without changing system DNS.
            for _ in range(3):
                try:
                    addresses=socket.getaddrinfo(parsed.hostname,parsed.port or 11434,type=socket.SOCK_STREAM)
                    break
                except OSError as exc:error=exc
            else:raise error
            if parsed.scheme=='http':
                for family,_,_,_,address in addresses:
                    host='['+address[0]+']' if family==socket.AF_INET6 else address[0]
                    candidate='http://'+host+':'+str(parsed.port or 11434)+parsed.path.rstrip('/')
                    if candidate not in candidates:candidates.append(candidate)
            else:candidates=[self.base]  # Preserve certificate hostname for TLS endpoints.
        for base in candidates:
            t0 = time.time()
            headers = {'Host': parsed.netloc, 'Content-Type': 'application/json'}
            try:
                data = json.dumps(payload).encode() if payload is not None else None
                request = urllib.request.Request(base + path, data=data, headers=headers)
                with self.opener.open(request, timeout=timeout) as response:
                    status_code = getattr(response, 'status', 200)
                    result = json.load(response)
                latency_ms = (time.time() - t0) * 1000.0
                self.cached = base
                try:
                    from tools.network_flow_inspector import FLOW_INSPECTOR
                    FLOW_INSPECTOR.record_flow(
                        target_url=base + path,
                        method='POST' if payload is not None else 'GET',
                        status_code=status_code,
                        latency_ms=latency_ms,
                        worker_id='lab_http',
                        request_headers=headers,
                        request_body=payload,
                        response_body=result,
                    )
                except Exception:
                    pass
                return result
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                latency_ms = (time.time() - t0) * 1000.0
                error = exc
                status_code = getattr(exc, 'code', 0) if hasattr(exc, 'code') else 0
                try:
                    from tools.network_flow_inspector import FLOW_INSPECTOR
                    FLOW_INSPECTOR.record_flow(
                        target_url=base + path,
                        method='POST' if payload is not None else 'GET',
                        status_code=status_code,
                        latency_ms=latency_ms,
                        worker_id='lab_http',
                        request_headers=headers,
                        request_body=payload,
                        error_message=str(exc),
                    )
                except Exception:
                    pass
                if payload is not None:
                    # The server may have accepted a POST even if its response was lost.
                    self.cached = None
                    raise
        self.cached = None
        raise error
