import os
import json
import urllib.request
import time
from typing import Dict, Any

class SwarmWorkerManager:
    """
    Autonomous Swarm Capacity Management Tool for Sabrina.
    Evaluates cluster topology and dynamically scales the distributed worker swarm
    when the task backlog exceeds the processing capacity of the local node.
    """

    def __init__(self, blackboard, router):
        self.blackboard = blackboard
        self.router = router
        self.node_status = router.nodes_status

    def get_telemetry(self) -> Dict[str, Any]:
        """Summarize active nodes, queue sizes, and capacity limits."""
        return {
            "queue_size": len(self.blackboard._queue),
            "unvalidated_size": len(self.blackboard._unvalidated),
            "findings_count": len(self.blackboard._findings),
            "nodes": self.node_status
        }

    def evaluate_and_scale(self) -> Dict[str, Any]:
        """
        Assess current task backlog and topology.
        If backlog > 5 and a remote Windows Workstation is linked,
        dispatch a signal to start/allocate tasks across the distributed nodes.
        """
        telemetry = self.get_telemetry()
        q_size = telemetry["queue_size"]
        win_info = telemetry["nodes"].get("Windows Workstation (Tunnel)", {})
        lab_info = telemetry["nodes"].get("Lab Host (GPU)", {})

        result = {
            "status": "idle",
            "action": "none",
            "message": "Swarm capacity is sufficient.",
            "queue_size": q_size
        }

        if q_size >= 5:
            if win_info.get("alive"):
                # Autonomous Decision: Scale to Windows Workstation
                result["status"] = "scaled"
                result["action"] = "scale_out"
                result["message"] = f"Task backlog ({q_size}) exceeds threshold. Scaling out to Windows Workstation."
                
                # Signal cluster to engage distributed workers via Redis control channel
                if self.blackboard.redis_broker and self.blackboard.redis_broker.is_connected:
                    try:
                        self.blackboard.redis_broker.client.publish("sabrina:cluster:control", json.dumps({"command": "scale_up", "target": "windows"}))
                        result["message"] += " Dispatched cluster control signal."
                    except Exception as e:
                        result["message"] += f" Failed to dispatch signal: {e}"
            else:
                result["status"] = "constrained"
                result["message"] = f"Task backlog ({q_size}) is high, but no remote nodes are available for scaling."
        
        # Log to blackboard
        self.blackboard.write_log("SwarmWorkerManager", result["message"])
        return result

    def execute(self, params: Dict[str, Any]) -> str:
        """Tool entrypoint for Sabrina's autonomous execution."""
        action = params.get("action", "evaluate")
        if action == "telemetry":
            return json.dumps(self.get_telemetry())
        elif action == "evaluate_and_scale":
            res = self.evaluate_and_scale()
            return json.dumps(res)
        else:
            return json.dumps({"error": f"Unknown action: {action}"})
