#!/usr/bin/env python3
"""
swarm_capacity_planner.py
=========================
Hardware & Capacity Allocator for Sabrina Swarm Orchestration.

Responsibilities:
1. Detect GPU VRAM & compute resources via nvidia-smi.
2. Query Ollama /api/tags and /api/ps for model footprints and currently resident models.
3. Enforce the 16 GB RTX 5060 Ti hardware budget.
4. Block & evict memory-hogging monolithic models (e.g. 70B, 35B) to stop model thrashing.
5. Pin the "Pure Resident Swarm" (Option A):
   - Planner / Orchestrator: llama3.1:8b (~4.58 GB)
   - Worker A (Fast Prober): granite-code:8b (~4.28 GB)
   - Worker B (Code Fixer / Logic): qwen2.5:7b (~4.36 GB)
   Total = ~13.22 GB / 16.3 GB (Leaving ~3.08 GB for KV cache + CUDA context).
6. Enable zero-swap latency and support parallel worker concurrency (--workers 3 or 4).
"""

import os
import sys
import json
import re
import subprocess
import urllib.request
import urllib.parse
from typing import Dict, List, Optional, Tuple, Any


class Colors:
    G  = "\033[92m"; Y  = "\033[93m"; R  = "\033[91m"
    B  = "\033[94m"; M  = "\033[35m"; CN = "\033[96m"
    BD = "\033[1m";  DM = "\033[2m";  X  = "\033[0m"


DEFAULT_RESIDENT_ROLES = {
    "planner":       "llama3.1:8b",
    "worker_prober": "granite-code:8b",
    "worker_fixer":  "qwen2.5:7b",
}

# Maximum allowed size for a single resident model in 16GB VRAM budget
MAX_SINGLE_MODEL_GB = 6.5
TARGET_VRAM_BUDGET_GB = 14.5


class SwarmCapacityPlanner:
    def __init__(self, ollama_url: Optional[str] = None, roles: Optional[Dict[str, str]] = None):
        if not ollama_url:
            ollama_url = os.environ.get("AGENT_OLLAMA_URL", "http://127.0.0.1:11434")
        # Ensure base URL without /api/chat
        if "/api" in ollama_url:
            ollama_url = ollama_url.split("/api")[0]
        self.ollama_url = ollama_url.rstrip("/")
        self.roles = roles or dict(DEFAULT_RESIDENT_ROLES)
        
        self.gpu_info: Optional[Dict[str, Any]] = None
        self.available_models: Dict[str, Dict[str, Any]] = {}
        self.loaded_models: List[Dict[str, Any]] = []

    def detect_gpu(self) -> Dict[str, Any]:
        """Queries nvidia-smi to obtain GPU name, total VRAM, used VRAM, and utilization."""
        info = {
            "name": "Unknown GPU",
            "total_vram_mb": 16384,
            "used_vram_mb": 0,
            "free_vram_mb": 16384,
            "utilization_pct": 0,
            "detected": False
        }
        try:
            cmd = [
                "nvidia-smi",
                "--query-gpu=name,memory.total,memory.used,memory.free,utilization.gpu",
                "--format=csv,noheader,nounits"
            ]
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
            if res.returncode == 0 and res.stdout.strip():
                parts = [p.strip() for p in res.stdout.strip().splitlines()[0].split(",")]
                if len(parts) >= 5:
                    info["name"] = parts[0]
                    info["total_vram_mb"] = float(parts[1])
                    info["used_vram_mb"] = float(parts[2])
                    info["free_vram_mb"] = float(parts[3])
                    info["utilization_pct"] = float(parts[4])
                    info["detected"] = True
        except Exception:
            pass

        self.gpu_info = info
        return info

    def fetch_available_models(self) -> Dict[str, Dict[str, Any]]:
        """Queries /api/tags from Ollama to inspect installed models and sizes."""
        url = f"{self.ollama_url}/api/tags"
        models_map = {}
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Sabrina-Capacity-Planner"})
            with urllib.request.urlopen(req, timeout=8) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                for m in data.get("models", []):
                    name = m.get("name", "")
                    size_bytes = m.get("size", 0)
                    size_gb = size_bytes / (1024 ** 3)
                    details = m.get("details", {})
                    param_size = details.get("parameter_size", "Unknown")
                    quant = details.get("quantization_level", "Unknown")
                    models_map[name] = {
                        "name": name,
                        "size_gb": round(size_gb, 2),
                        "parameter_size": param_size,
                        "quantization": quant,
                        "format": details.get("format", "gguf")
                    }
        except Exception as e:
            pass
        self.available_models = models_map
        return models_map

    def fetch_loaded_models(self) -> List[Dict[str, Any]]:
        """Queries /api/ps to see which models are currently resident in VRAM/RAM."""
        url = f"{self.ollama_url}/api/ps"
        loaded = []
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Sabrina-Capacity-Planner"})
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                for m in data.get("models", []):
                    name = m.get("name", "")
                    size_vram = m.get("size_vram", 0) / (1024 ** 3)
                    size_total = m.get("size", 0) / (1024 ** 3)
                    loaded.append({
                        "name": name,
                        "size_vram_gb": round(size_vram, 2),
                        "size_total_gb": round(size_total, 2),
                        "details": m.get("details", {})
                    })
        except Exception:
            pass
        self.loaded_models = loaded
        return loaded

    def evict_unwanted_models(self, keep_models: Optional[List[str]] = None) -> List[str]:
        """
        Unloads models that exceed MAX_SINGLE_MODEL_GB (like 70B or 35B) or are not
        part of the desired resident swarm, eliminating model thrashing.
        """
        from tools.control_client import configured
        if configured(self.ollama_url):
            return []  # Shared service owns residency; never evict another caller's model.
        if keep_models is None:
            keep_models = list(self.roles.values())

        evicted = []
        self.fetch_loaded_models()
        for lm in self.loaded_models:
            name = lm["name"]
            # Check if model is unwanted or over budget
            is_oversized = lm.get("size_total_gb", 0) > MAX_SINGLE_MODEL_GB or "35b" in name.lower() or "70b" in name.lower()
            is_not_in_keep = name not in keep_models and not any(k in name for k in keep_models)
            
            if is_oversized or is_not_in_keep:
                # Unload via Ollama keep_alive: 0
                try:
                    payload = {"model": name, "keep_alive": 0}
                    req = urllib.request.Request(
                        f"{self.ollama_url}/api/generate",
                        data=json.dumps(payload).encode("utf-8"),
                        headers={"Content-Type": "application/json"}
                    )
                    with urllib.request.urlopen(req, timeout=10) as r:
                        r.read()
                    evicted.append(name)
                except Exception as e:
                    pass
        return evicted

    def validate_and_assign_roles(self) -> Dict[str, str]:
        """
        Validates the desired roles against available models. If a desired model
        is not tagged, it finds the best equivalent that stays under MAX_SINGLE_MODEL_GB.
        """
        self.fetch_available_models()
        assigned = {}

        # 1. Planner / Orchestrator (Target: llama3.1:8b)
        desired_planner = self.roles.get("planner", "llama3.1:8b")
        if desired_planner in self.available_models:
            assigned["planner"] = desired_planner
        elif "llama3.1:latest" in self.available_models:
            assigned["planner"] = "llama3.1:latest"
        elif "llama3.1:8b" in self.available_models:
            assigned["planner"] = "llama3.1:8b"
        else:
            # Pick any capable 7-8B model that isn't 70B
            for m in self.available_models:
                if "llama3" in m.lower() and "70b" not in m.lower():
                    assigned["planner"] = m
                    break
            if "planner" not in assigned:
                assigned["planner"] = "llama3.1:8b"

        # 2. Worker A (Fast Prober, Target: granite-code:8b)
        desired_prober = self.roles.get("worker_prober", "granite-code:8b")
        if desired_prober in self.available_models:
            assigned["worker_prober"] = desired_prober
        elif "granite-code:8b" in self.available_models:
            assigned["worker_prober"] = "granite-code:8b"
        elif "granite:8b" in self.available_models:
            assigned["worker_prober"] = "granite:8b"
        elif "granite-exploit:latest" in self.available_models:
            assigned["worker_prober"] = "granite-exploit:latest"
        else:
            assigned["worker_prober"] = "granite-code:8b"

        # 3. Worker B (Code Fixer & Logic, Target: qwen2.5:7b)
        desired_fixer = self.roles.get("worker_fixer", "qwen2.5:7b")
        if desired_fixer in self.available_models:
            assigned["worker_fixer"] = desired_fixer
        elif "qwen2.5:7b" in self.available_models:
            assigned["worker_fixer"] = "qwen2.5:7b"
        elif "rlhf-aligned-qwen:latest" in self.available_models:
            assigned["worker_fixer"] = "rlhf-aligned-qwen:latest"
        else:
            assigned["worker_fixer"] = "qwen2.5:7b"

        self.roles = assigned
        return assigned

    def get_resident_budget_summary(self) -> Dict[str, Any]:
        """Calculates total projected VRAM usage for the resident swarm."""
        self.fetch_available_models()
        self.detect_gpu()
        
        breakdown = {}
        total_projected_gb = 0.0

        for role, model_name in self.roles.items():
            model_info = self.available_models.get(model_name, {})
            size_gb = model_info.get("size_gb", 4.5)  # estimate ~4.5GB if not found
            breakdown[role] = {
                "model": model_name,
                "size_gb": size_gb,
                "quant": model_info.get("quantization", "Q4"),
                "params": model_info.get("parameter_size", "8B")
            }
            total_projected_gb += size_gb

        gpu_total_gb = self.gpu_info["total_vram_mb"] / 1024.0 if self.gpu_info else 16.0
        headroom_gb = gpu_total_gb - total_projected_gb

        return {
            "breakdown": breakdown,
            "total_projected_gb": round(total_projected_gb, 2),
            "gpu_total_gb": round(gpu_total_gb, 2),
            "headroom_gb": round(headroom_gb, 2),
            "fits_in_vram": total_projected_gb <= TARGET_VRAM_BUDGET_GB
        }

    def prewarm_resident_models(self) -> Dict[str, bool]:
        """
        Sends a keep_alive: -1 warmup request to pin all 3 models in VRAM concurrently.
        """
        from tools.control_client import configured
        if configured(self.ollama_url):
            raise RuntimeError('Shared scheduling loads models on demand; manual pinning is disabled')
        self.validate_and_assign_roles()
        results = {}
        # First evict any legacy 35B / 70B models
        self.evict_unwanted_models()

        for role, model_name in self.roles.items():
            payload = {
                "model": model_name,
                "prompt": "warmup",
                "keep_alive": -1,  # Pin indefinitely in VRAM
                "options": {"num_predict": 1}
            }
            try:
                req = urllib.request.Request(
                    f"{self.ollama_url}/api/generate",
                    data=json.dumps(payload).encode("utf-8"),
                    headers={"Content-Type": "application/json"}
                )
                with urllib.request.urlopen(req, timeout=120) as r:
                    r.read()
                results[model_name] = True
            except Exception as e:
                results[model_name] = False

        return results

    def get_model_for_task(self, task_type: str) -> str:
        """
        Maps a task or workflow phase directly to its dedicated pinned resident model.
        """
        t = task_type.lower()
        if any(k in t for k in ["plan", "queen", "coordinator", "scope", "synthesize", "report"]):
            return self.roles.get("planner", "llama3.1:8b")
        elif any(k in t for k in ["fix", "correct", "retry", "traceback", "syntax", "refactor"]):
            return self.roles.get("worker_fixer", "qwen2.5:7b")
        else:
            # Default to fast prober Worker A
            return self.roles.get("worker_prober", "granite-code:8b")

    def print_status_report(self):
        """Prints a human-readable diagnostic report of the hardware & resident swarm state."""
        self.detect_gpu()
        self.validate_and_assign_roles()
        budget = self.get_resident_budget_summary()
        loaded = self.fetch_loaded_models()

        print(f"\n{Colors.CN}{Colors.BD}=== Sabrina Hardware & Swarm Capacity Status ==={Colors.X}")
        print(f"Ollama Endpoint: {self.ollama_url}")
        
        gpu = self.gpu_info
        if gpu and gpu["detected"]:
            print(f"GPU: {Colors.G}{gpu['name']}{Colors.X} | VRAM: {gpu['used_vram_mb']:.0f} / {gpu['total_vram_mb']:.0f} MB ({gpu['free_vram_mb']:.0f} MB Free) | Util: {gpu['utilization_pct']:.0f}%")
        else:
            print(f"GPU: {Colors.Y}Direct nvidia-smi not detected on this node (Remote host mode){Colors.X}")

        print(f"\n{Colors.BD}[Resident Swarm Role Allocation]{Colors.X}")
        for role, data in budget["breakdown"].items():
            print(f"  • {role:15}: {Colors.B}{data['model']:22}{Colors.X} ({data['size_gb']} GB, {data['quant']}, {data['params']})")

        print(f"\nTotal Swarm Footprint : {Colors.G}{budget['total_projected_gb']} GB{Colors.X} / {budget['gpu_total_gb']} GB")
        print(f"Reserved Headroom     : {Colors.G}{budget['headroom_gb']} GB{Colors.X} (for KV-cache and CUDA context)")
        print(f"Budget Validated      : {Colors.G if budget['fits_in_vram'] else Colors.R}{budget['fits_in_vram']}{Colors.X}")

        print(f"\n{Colors.BD}[Currently Loaded in VRAM (/api/ps)]{Colors.X}")
        if loaded:
            for m in loaded:
                is_pinned = m["name"] in self.roles.values()
                color = Colors.G if is_pinned else Colors.Y
                pin_label = "[PINNED RESIDENT]" if is_pinned else "[UNMANAGED / CANDIDATE FOR EVICTION]"
                print(f"  • {color}{m['name']:25}{Colors.X} | VRAM: {m['size_vram_gb']} GB | Total: {m['size_total_gb']} GB {pin_label}")
        else:
            print(f"  {Colors.DM}No models currently in memory.{Colors.X}")
        print()


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Sabrina Swarm Capacity Planner")
    parser.add_argument("--url", default="http://127.0.0.1:11434", help="Ollama API base URL")
    parser.add_argument("--status", action="store_true", help="Print status report and VRAM budget")
    parser.add_argument("--prewarm", action="store_true", help="Evict unwanted models and pin the 3 resident models")
    parser.add_argument("--evict", action="store_true", help="Evict models >6.5GB (e.g. 35B, 70B)")
    args = parser.parse_args()

    planner = SwarmCapacityPlanner(ollama_url=args.url)

    if args.evict:
        evicted = planner.evict_unwanted_models()
        print(f"Evicted models: {evicted}")

    if args.prewarm:
        print("Prewarming and pinning resident models in VRAM...")
        res = planner.prewarm_resident_models()
        print(f"Warmup status: {res}")

    planner.print_status_report()


if __name__ == "__main__":
    main()
