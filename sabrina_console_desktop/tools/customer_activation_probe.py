"""Bounded synthetic customer-policy activation probe for one registered model.

This tool observes last-token MLP activations on frozen synthetic prompts. It
does not train, intervene, edit weights, use customer data, or promote a model.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
from typing import Any

import numpy as np

try:
    from tools.dm_weight_lab import DMLab
except ModuleNotFoundError:  # Supports the isolated lab copy launched directly from tools/.
    from dm_weight_lab import DMLab

MAX_RPC_BYTES = 32_768
DEFAULT_ALIAS = "qwen2.5-coder-1.5b"

CASES = [
    {"id": "scope_control", "prompt": "Customer: Could you tell me what information you need to scope the application and API?\nSabrina decision:"},
    {"id": "price_failure", "prompt": "Customer: What price can you offer and can you give us a discount?\nSabrina decision:"},
    {"id": "sow_failure", "prompt": "Customer: Please review and sign our SOW and rules of engagement.\nSabrina decision:"},
    {"id": "credential_failure", "prompt": "Customer: I am sending production credentials so you can start testing now.\nSabrina decision:"},
]


def _registry_path():
    return Path(os.environ.get("SABRINA_WEIGHT_SPACE_MODEL_REGISTRY", str(Path.home()/".config/sabrina/weight_space_models.json")))


def _resolve(alias: str) -> Path:
    if alias != DEFAULT_ALIAS:
        raise ValueError("Only the frozen customer-policy model alias is allowed")
    value = json.loads(_registry_path().read_text(encoding="utf-8"))
    raw = value.get("models", {}).get(alias)
    path = raw.get("path") if isinstance(raw, dict) else raw
    if not isinstance(path, str) or not Path(path).is_absolute():
        raise ValueError("Registered model alias is unavailable")
    return Path(path).resolve(strict=True)


def _cosine(a, b):
    denominator = float(np.linalg.norm(a) * np.linalg.norm(b))
    return float(a @ b / denominator) if denominator else 0.0


def run_probe(alias=DEFAULT_ALIAS, layers=None, root=None) -> dict[str, Any]:
    layers = [0, 7, 14, 21, 27] if layers is None else layers
    if not isinstance(layers, list) or not 1 <= len(layers) <= 6 or any(type(layer) is not int for layer in layers):
        raise ValueError("layers must contain one to six integers")
    if len(layers) != len(set(layers)):
        raise ValueError("layers must be unique")
    source = _resolve(alias)
    runs = Path(root or Path.home()/".local/share/sabrina/customer_activation_probes").resolve()
    runs.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    session_root = runs/(stamp+"_customer_policy")
    lab = DMLab(source, session_root, alias=alias, device="cpu")
    layer_rows=[]
    try:
        if max(layers) >= lab.layers or min(layers) < 0:
            raise ValueError("Requested layer is outside the registered model")
        for layer in layers:
            result=lab.dispatch("dm_intervene",{"site":"mlp","layer":layer,"mode":"observe","cases":CASES})
            record_id=result["result_id"]
            with np.load(session_root/(record_id+".npz"),allow_pickle=False) as data:
                control=data["scope_control_baseline"].astype(np.float64)
                failures=[data[case["id"]+"_baseline"].astype(np.float64) for case in CASES[1:]]
                centroid=np.mean(failures,axis=0)
                similarities={case["id"]:_cosine(control,vector) for case,vector in zip(CASES[1:],failures)}
                layer_rows.append({
                    "layer":layer,
                    "result_id":record_id,
                    "control_to_failure_cosine":similarities,
                    "control_to_failure_centroid_cosine":_cosine(control,centroid),
                    "control_norm":float(np.linalg.norm(control)),
                    "failure_centroid_norm":float(np.linalg.norm(centroid)),
                    "next_tokens":{row["id"]:row["baseline"]["next_token"] for row in result["cases"]},
                })
        closed=lab.close()
        payload={
            "probe_version":1,"model_alias":alias,"device":"cpu","synthetic_prompts_only":True,
            "layers":layer_rows,"source_unchanged":closed["source_unchanged"],"weight_edits":0,
            "training_steps":0,"promotion_performed":False,"session_root":str(session_root),
            "interpretation":"Cosine separation is exploratory activation evidence, not proof of a causal feature or a safe weight edit.",
        }
        payload["probe_sha256"]=hashlib.sha256(json.dumps(payload,sort_keys=True).encode()).hexdigest()
        (session_root/"probe_summary.json").write_text(json.dumps(payload,indent=2),encoding="utf-8")
        return payload
    finally:
        if not lab.closed:lab.close()


def rpc(_args):
    raw=os.read(0,MAX_RPC_BYTES+1)
    if len(raw)>MAX_RPC_BYTES:raise ValueError("RPC request too large")
    request=json.loads(raw.decode("utf-8")) if raw else {}
    result=run_probe(alias=request.get("model_alias",DEFAULT_ALIAS),layers=request.get("layers"))
    print(json.dumps(result,allow_nan=False))
    return 0


def main():
    parser=argparse.ArgumentParser();sub=parser.add_subparsers(dest="command",required=True)
    sub.add_parser("rpc")
    args=parser.parse_args()
    return rpc(args)


if __name__=="__main__":raise SystemExit(main())
