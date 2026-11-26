import json
from threading import Lock
from dataclasses import dataclass

@dataclass
class DiscoveredEndpoint:
    path: str
    method: str
    details: dict

class Blackboard:
    def __init__(self, persist_path: str = None):
        self.attack_surface = {}
        self.lock = Lock()
        import os
        if persist_path is None:
            project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            persist_path = os.path.join(project_root, "data", "attack_surface.json")
        self.persist_path = persist_path
        if os.path.exists(self.persist_path):
            try:
                with open(self.persist_path, "r", encoding="utf-8") as f:
                    self.attack_surface = json.load(f)
            except Exception:
                pass

    def register(self, endpoint: DiscoveredEndpoint) -> bool:
        key = f"{endpoint.method}:{endpoint.path}"
        with self.lock:
            if key not in self.attack_surface:
                # Ensure 'tested' list always exists so mark_tested never KeyErrors
                details = dict(endpoint.details) if isinstance(endpoint.details, dict) else {
                    "path": endpoint.path,
                    "method": endpoint.method,
                }
                details.setdefault("tested", [])
                self.attack_surface[key] = details
                return True
            return False

    def get_untested(self, vuln_type: str) -> list[dict]:
        untested = []
        for key, details in self.attack_surface.items():
            if vuln_type not in details.get("tested", []):
                untested.append(details)
        return untested

    def mark_tested(self, path: str, method: str, vuln_type: str):
        with self.lock:
            key = f"{method}:{path}"
            if key in self.attack_surface:
                self.attack_surface[key].setdefault("tested", [])
                if vuln_type not in self.attack_surface[key]["tested"]:
                    self.attack_surface[key]["tested"].append(vuln_type)

    def to_json(self) -> str:
        return json.dumps(self.attack_surface, indent=4)

    def summary(self) -> str:
        """
        Compact, prompt-ready rendering of the current attack surface. Was
        previously never called from anywhere -- record_endpoint wrote to
        this structure every round but nothing ever read it back into
        decide_action's prompt, so the model had zero visibility into
        endpoints it had already discovered/tested in earlier rounds.
        """
        if not self.attack_surface:
            return "(no endpoints recorded yet)"
        lines = []
        with self.lock:
            for key, details in self.attack_surface.items():
                method, path = key.split(":", 1)
                params = details.get("params")
                tested = details.get("tested", [])
                params_str = f" (params: {params})" if params else ""
                tested_str = f"tested: {tested}" if tested else "UNTESTED"
                lines.append(f"- {method} {path}{params_str} | {tested_str}")
        return "\n".join(lines)

    def load_json(self, data: str):
        with self.lock:
            self.attack_surface = json.loads(data)

BLACKBOARD = Blackboard()

if __name__ == "__main__":
    endpoint1 = DiscoveredEndpoint(path="/login", method="POST",
                                   details={"params": ["email", "password"], "param_type": "body_json"})
    endpoint2 = DiscoveredEndpoint(path="/admin", method="GET",
                                   details={"params": [], "param_type": "query"})

    BLACKBOARD.register(endpoint1)
    BLACKBOARD.register(endpoint2)

    print(BLACKBOARD.get_untested("sqli"))

    BLACKBOARD.mark_tested("/login", "POST", "sqli")
    print(BLACKBOARD.get_untested("sqli"))

    print(BLACKBOARD.to_json())