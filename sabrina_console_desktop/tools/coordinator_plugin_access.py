"""One-shot coordinator grants for operator-scoped plugin operations."""
from __future__ import annotations
import hashlib
import json
import re
import threading
import time
import uuid

from tools.plugin_console import discover, invoke


from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MARKER = re.compile(r"\[plugins:([a-z0-9_.,]+)\]")
CLASSIFICATIONS = frozenset({"public", "local", "private", "financial"})


def load_policy(root=None):
    path = Path(root or ROOT) / "data/control/coordinator_plugin_policy.json"
    try:
        policy = json.loads(path.read_text(encoding="utf-8"))
        if policy.get("version") != 1 or not isinstance(policy.get("plugins"), dict):
            return {}
        return policy["plugins"]
    except (OSError, ValueError, TypeError, AttributeError):
        return {}


def scope_from_goal(goal, *, root=None):
    matches = MARKER.findall(goal or "")
    if len(matches) != 1:
        return frozenset()
    declared = matches[0].split(",")
    policy = load_policy(root)
    caps = {f"{name}.{op}" for name, spec in policy.items()
            if isinstance(spec, dict) and isinstance(spec.get("operations"), dict)
            for op, item in spec["operations"].items()
            if isinstance(item, dict) and item.get("classification") in CLASSIFICATIONS}
    if len(set(declared)) != len(declared) or any(cap not in caps for cap in declared):
        return frozenset()
    return frozenset(declared)


def scope_from_capabilities(capabilities, *, root=None):
    if not isinstance(capabilities, (list, tuple)) or any(not isinstance(cap, str) for cap in capabilities):
        raise ValueError("Plugin capabilities must be a list of names")
    if not capabilities:
        return frozenset()
    scope = scope_from_goal("[plugins:" + ",".join(capabilities) + "]", root=root)
    if len(scope) != len(capabilities):
        raise ValueError("Unknown, duplicate, or unapproved plugin capability")
    return scope


def validate_action(plugin, action, policy):
    if not isinstance(plugin, str) or not isinstance(action, dict):
        return False
    op = action.get("operation")
    if not isinstance(op, str):
        return False
    spec = policy.get(plugin)
    if not isinstance(spec, dict) or not isinstance(spec.get("operations"), dict):
        return False
    rule = spec["operations"].get(op)
    if not isinstance(rule, dict) or rule.get("classification") not in CLASSIFICATIONS:
        return False
    fields = rule.get("fields", {})
    required = rule.get("required", ["operation"])
    if (not isinstance(fields, dict) or not isinstance(required, list)
            or any(not isinstance(field, str) for field in required)):
        return False
    if any(not isinstance(k, str) for k in action) or not set(required) <= set(action):
        return False
    if set(action) - (set(fields) | {"operation"}):
        return False
    for field, value in action.items():
        if field == "operation":
            continue
        definition = fields.get(field, {})
        if not isinstance(definition, dict):
            return False
        if definition.get("type") == "integer":
            lower, upper = definition.get("min"), definition.get("max")
            if type(value) is not int or type(lower) is not int or type(upper) is not int or not lower <= value <= upper:
                return False
        elif definition.get("type") == "string":
            pattern = definition.get("pattern")
            if not isinstance(value, str) or len(value) > 256 or not isinstance(pattern, str) or len(pattern) > 256:
                return False
            try:
                if not re.fullmatch(pattern, value):
                    return False
            except re.error:
                return False
        else:
            return False
    return True


class CoordinatorPluginAccess:
    def __init__(self, mission_id, scope, *, root=None):
        self.mission_id = mission_id
        self.scope = frozenset(scope)
        self.root = root
        self._policy = load_policy(root)
        self._policy_hash = hashlib.sha256(json.dumps(self._policy, sort_keys=True).encode()).hexdigest()
        self._requests = {}
        self._financial_handoffs = {}
        self._lock = threading.Lock()
        self._cancelled = False

    def _current_policy(self):
        policy = load_policy(self.root)
        digest = hashlib.sha256(json.dumps(policy, sort_keys=True).encode()).hexdigest()
        return policy if digest == self._policy_hash else {}

    def operation_requirements(self, plugin, operation):
        policy = self._current_policy()
        spec = policy.get(plugin, {})
        rule = spec.get("operations", {}).get(operation, {}) if isinstance(spec, dict) else {}
        if not isinstance(rule, dict) or rule.get("classification") not in CLASSIFICATIONS:
            return None
        requirements = rule.get("requires", [] if rule["classification"] == "local" else ["network"])
        if not isinstance(requirements, list) or any(x not in {"network", "read"} for x in requirements):
            return None
        return tuple(requirements)

    def request(self, mission_id, worker_id, plugin, action):
        if self._cancelled or mission_id != self.mission_id or not worker_id:
            return {"ok": False, "status": "denied", "reason": "mission_or_worker_mismatch"}
        policy = self._current_policy()
        if not validate_action(plugin, action, policy):
            return {"ok": False, "status": "denied", "reason": "unsupported_action"}
        capability = plugin + "." + action["operation"]
        if capability not in self.scope:
            return {"ok": False, "status": "denied", "reason": "outside_mission_scope"}
        plugins, _ = discover(self.root) if self.root else discover()
        source = plugins.get(plugin)
        if not source or source["sha256"] != policy[plugin].get("sha256"):
            return {"ok": False, "status": "denied", "reason": "source_changed_or_unavailable"}
        token = uuid.uuid4().hex
        payload = json.dumps(action, sort_keys=True, separators=(",", ":"), allow_nan=False)
        with self._lock:
            if self._cancelled:
                return {"ok": False, "status": "denied", "reason": "cancelled"}
            self._requests[token] = (worker_id, plugin, payload, source["sha256"], time.monotonic() + 30, False)
        return {"ok": True, "status": "pending", "request_id": token, "capability": capability,
                "source_sha256": source["sha256"]}

    def decide(self, mission_id, request_id, permit=True):
        """Coordinator policy decides independently of the worker request."""
        with self._lock:
            item = self._requests.get(request_id)
            if self._cancelled or mission_id != self.mission_id or not item:
                return {"ok": False, "status": "denied", "reason": "unknown_request"}
            worker, plugin, payload, digest, expiry, _ = item
            action = json.loads(payload)
            policy = self._current_policy()
            if (not permit or time.monotonic() > expiry or not validate_action(plugin, action, policy)
                    or policy.get(plugin, {}).get("sha256") != digest
                    or plugin + "." + action["operation"] not in self.scope):
                self._requests.pop(request_id, None)
                return {"ok": False, "status": "denied", "reason": "coordinator_policy"}
            self._requests[request_id] = (worker, plugin, payload, digest, expiry, True)
            return {"ok": True, "status": "granted", "grant_id": request_id,
                    "decision": "deterministic_coordinator_policy"}

    def execute(self, mission_id, worker_id, grant_id, plugin, action):
        with self._lock:
            entry = self._requests.pop(grant_id, None)
            cancelled = self._cancelled
        if cancelled or not entry or mission_id != self.mission_id:
            return {"ok": False, "status": "denied", "reason": "cancelled_or_unknown_grant"}
        owner, name, payload, digest, expiry, granted = entry
        if (not granted or owner != worker_id or name != plugin or time.monotonic() > expiry
                or json.dumps(action, sort_keys=True, separators=(",", ":"), allow_nan=False) != payload):
            return {"ok": False, "status": "denied", "reason": "grant_scope_mismatch_or_expired"}
        plugins, _ = discover(self.root) if self.root else discover()
        source = plugins.get(plugin)
        policy = self._current_policy()
        if (not source or source["sha256"] != digest or policy.get(plugin, {}).get("sha256") != digest
                or not validate_action(plugin, action, policy) or plugin + "." + action["operation"] not in self.scope):
            return {"ok": False, "status": "denied", "reason": "source_changed_or_unavailable"}
        try:
            rule = policy[plugin]["operations"][action["operation"]]
            if rule["classification"] == "financial":
                if (plugin, action["operation"]) != ("kraken_console", "queue_entry"):
                    return {"ok": False, "status": "denied", "reason": "financial_route_unavailable"}
                return self._handoff_entry(grant_id, worker_id, action)
            value = invoke(source, action)
            if isinstance(value, str):
                value = json.loads(value)
            result = {"ok": not (isinstance(value, dict) and value.get("ok") is False),
                      "status": "executed", "result": value}
            if len(json.dumps(result, allow_nan=False)) > 60000:
                return {"ok": False, "status": "denied", "reason": "result_too_large"}
            return result
        except Exception as exc:
            return {"ok": False, "status": "failed", "reason": type(exc).__name__}

    def _handoff_entry(self, grant_id, worker_id, action):
        from tools.kraken_order_handoff import KrakenOrderHandoff

        client = KrakenOrderHandoff.configured()
        if client is None:
            return {"ok": False, "status": "denied", "reason": "ego_order_handoff_disabled"}
        with self._lock:
            if self._cancelled:
                return {"ok": False, "status": "denied", "reason": "cancelled"}
            self._financial_handoffs[grant_id] = client
        receipt = client.submit(grant_id, self.mission_id, worker_id, action)
        state = receipt.get("order_handoff_state")
        if state or receipt.get("error") == "order_account_scope_mismatch":
            with self._lock:
                self._financial_handoffs.pop(grant_id, None)
        return {"ok": bool(receipt.get("ok")), "status": "order_handoff", "result": receipt}

    def cancel(self):
        with self._lock:
            self._cancelled = True
            self._requests.clear()
            handoffs = tuple(self._financial_handoffs.items())
            self._financial_handoffs.clear()
        for grant_id, client in handoffs:
            threading.Thread(target=client.revoke, args=(grant_id,), daemon=True).start()
