import os
import json
import time
from pathlib import Path

# Use the same data directory as blackboard
DATA_DIR = Path("data")
QA_TRACKER_FILE = DATA_DIR / "qa_tested_models.json"

def _load_tracker() -> dict:
    if QA_TRACKER_FILE.exists():
        try:
            with open(QA_TRACKER_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def _save_tracker(data: dict):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(QA_TRACKER_FILE, "w") as f:
        json.dump(data, f, indent=2)

def mark_model_tested(model_name: str):
    """Mark a model as tested right now."""
    data = _load_tracker()
    data[model_name] = time.time()
    _save_tracker(data)

def get_qa_queue() -> dict:
    """
    Retrieves all available models from the mesh and sorts them
    so that the least recently tested models appear first.
    """
    try:
        from tools.model_assignment_api import get_all_assignments
        mesh_data = get_all_assignments()
    except Exception as e:
        return {"error": f"Failed to get mesh data: {e}"}
        
    all_models = set()
    
    # Extract from assignments
    for role, assignment in mesh_data.get("assignments", {}).items():
        if assignment and assignment.get("model"):
            all_models.add(assignment["model"])
            
    # Extract from mesh discovery
    for node, info in mesh_data.get("mesh_nodes", {}).items():
        if info.get("status") == "online":
            for m in info.get("models", []):
                all_models.add(m)
                
    tracker = _load_tracker()
    
    queue = []
    for model in all_models:
        last_tested = tracker.get(model, 0)
        queue.append({
            "model_name": model,
            "last_tested_ts": last_tested,
            "last_tested_str": time.ctime(last_tested) if last_tested > 0 else "Never"
        })
        
    # Sort by timestamp (oldest first)
    queue.sort(key=lambda x: x["last_tested_ts"])
    
    return {
        "status": "success",
        "queue": queue
    }
