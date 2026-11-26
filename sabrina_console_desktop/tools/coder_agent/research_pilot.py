"""Native-tool pilot backend for the existing MRI GPU queue and model archive.

This module owns experiment artifacts only. The GPU queue owns scheduling and job
state; model_archive owns source downloads, validation and registry identity.
"""
import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import uuid

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

WORKFLOW = "native_tool_pilot"


def digest(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for part in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            value.update(part)
    return value.hexdigest()


def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def save(path, value):
    from tools.model_archive import save_json
    save_json(Path(path), value)


def normalize_spec(spec):
    if spec.get("workflow") != WORKFLOW:
        raise ValueError("Unknown model research workflow")
    result = {"workflow": WORKFLOW, "topic": "model_research", "task_type": "causal_lm", "target_host": "remote_gpu"}
    for key in ("source_id", "experiment_id"):
        value = spec.get(key)
        if not isinstance(value, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,79}", value):
            raise ValueError(key + " requires a configured identifier")
        result[key] = value
    steps = spec.get("max_steps", 20)
    if type(steps) is not int or not 1 <= steps <= 20:
        raise ValueError("Pilot max_steps must be 1..20")
    result["max_steps"] = steps
    episode = spec.get("episode_id")
    if episode is not None:
        if not isinstance(episode, str) or not re.fullmatch(r"[A-Za-z0-9_:.-]{1,160}", episode):
            raise ValueError("Invalid research episode_id")
        result["episode_id"] = episode
    return result


@contextlib.contextmanager
def locked(path):
    import fcntl
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def available_gpu():
    completed = subprocess.run(["nvidia-smi", "--query-gpu=index,memory.free,utilization.gpu",
        "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=15, check=True)
    rows = []
    for line in completed.stdout.strip().splitlines():
        index, free, util = [int(part.strip()) for part in line.split(",")]
        rows.append({"index": index, "free_gib": free / 1024, "utilization": util})
    eligible = [row for row in rows if row["free_gib"] >= 14 and row["utilization"] <= 10]
    return (eligible[0]["index"] if eligible else None), rows


def prepare(folder, recipe):
    from tools import model_archive as archive
    from tools.coder_agent.tool_curriculum import training_records, render_record
    receipt = folder / "preparation.json"
    if receipt.exists():
        data = load(receipt)
        source = Path(data["source_path"])
        if data["source"] == recipe["source"] and all(
            (source / name).is_file() and (source / name).stat().st_size == row["bytes"] and
            (source / name).stat().st_mtime_ns == row["mtime_ns"] for name, row in data["files"].items()):
            if digest(folder / "train.jsonl") == load(folder / "tokenization.json")["dataset_sha256"]:
                return source
    # Same archive lock used by its native RPC, so repairs cannot race one another.
    with locked(archive.wsp._config_home() / "sabrina/.model_archive.lock"):
        report = archive.handle_request({"action": "resume_download", "source_id": recipe["source_id"]})
    if report["validation"]["status"] != "valid":
        raise ValueError("Model archive validation failed")
    source = Path(report["destination"])
    configured = recipe["source"]
    if configured.get("model_alias"):
        cfg = archive.config()
        root_name = configured.get("root", "managed")
        with locked(archive.wsp._config_home() / "sabrina/.model_archive.lock"):
            archive.handle_request({"action": "register", "root": root_name,
                "relative_path": str(source.relative_to(archive.root_for(cfg, root_name))),
                "model_alias": configured["model_alias"]})
    from transformers import AutoTokenizer
    tokenizer = AutoTokenizer.from_pretrained(str(source), local_files_only=True, trust_remote_code=False)
    records = [render_record(tokenizer, row) for row in training_records(recipe["train_seed"])]
    lengths = [len(tokenizer.encode(row["prompt"] + row["completion"], add_special_tokens=False)) for row in records]
    if max(lengths) > recipe["max_length"]:
        raise ValueError("Curriculum exceeds context budget")
    (folder / "train.jsonl").write_text("".join(json.dumps(row) + "\n" for row in records), encoding="utf-8")
    save(folder / "tokenization.json", {"records": len(records), "maximum_tokens": max(lengths),
         "dataset_sha256": digest(folder / "train.jsonl"), "loss": "assistant completion only"})
    save(receipt, {"source": recipe["source"], "source_path": str(source), "archive_report": report["report_path"],
         "files": {path.name: {"bytes": path.stat().st_size, "mtime_ns": path.stat().st_mtime_ns}
                   for path in source.iterdir() if path.is_file()}})
    return source


def run_request(request):
    from tools import model_archive as archive
    spec = normalize_spec(request)
    cfg = archive.config()
    source, _, _ = archive.source_layout(cfg, spec["source_id"])
    if not source.get("sha256"):
        raise ValueError("Research source requires configured weight SHA256 provenance")
    runs = Path(cfg.get("research_run_root", Path(cfg["report_root"]).parent / "runs")).resolve()
    if runs == REPO or runs.is_relative_to(REPO):
        raise ValueError("Research runs must be outside the source tree")
    folder = runs / spec["experiment_id"]
    folder.mkdir(parents=True, exist_ok=True)
    recipe = {"version": 1, **spec, "source": source, "method": "qlora", "train_seed": 731101,
              "evaluation_seed": 940003, "lora_rank": 8, "learning_rate": 0.0001,
              "max_length": 2048, "gradient_accumulation_steps": 4, "curriculum": "tool-interop-pilot-1"}
    with locked(folder / "experiment.lock"):
        if (folder / "recipe.json").exists() and load(folder / "recipe.json") != recipe:
            raise ValueError("Experiment already exists with a different recipe")
        save(folder / "recipe.json", recipe)
        if not (folder / "evaluation-summary.json").exists():
            checkpoint = prepare(folder, recipe)
            with locked(runs / ".gpu-pilot.lock"):
                gpu, resources = available_gpu()
                if gpu is None:
                    return {"status": "deferred", "reason": "waiting_for_gpu", "retry_after_seconds": 300,
                            "metrics": {"resources": resources, "training_started": False},
                            "artifact_paths": {"run": str(folder), "preparation": str(folder / "preparation.json")}}
                environment = dict(os.environ, CUDA_VISIBLE_DEVICES=str(gpu))
                implementation = {name: digest(Path(__file__).with_name(name)) for name in
                    ("research_pilot.py", "qlora_pilot.py", "tool_curriculum.py", "code_eval.py", "tool_creation.py")}
                manifest = folder / "implementation.json"
                if manifest.exists() and load(manifest) != implementation:
                    raise ValueError("Experiment implementation changed; use a new experiment_id")
                save(manifest, implementation)
                subprocess.run([sys.executable, str(Path(__file__).with_name("qlora_pilot.py")), str(folder), str(checkpoint)],
                               check=True, timeout=7200, env=environment)
        return {"status": "completed", "metrics": load(folder / "evaluation-summary.json"),
                "artifact_paths": {"run": str(folder), "evaluation": str(folder / "evaluation-summary.json"),
                                   "training": str(folder / "training.json")}}


def run_remote(spec):
    from tools.weight_space_lab import run_remote as rpc
    from tools.control_client import resource_lease
    started = False
    try:
        with resource_lease('compute:lab'):
            started = True
            return rpc(normalize_spec(spec), helper="coder_agent/research_pilot")
    except TimeoutError:
        if started:
            raise
        return {'status':'deferred', 'reason':'waiting_for_shared_compute', 'retry_after_seconds':60}


if __name__ == "__main__":
    try:
        with contextlib.redirect_stdout(sys.stderr):
            result = run_request(json.load(sys.stdin))
        print(json.dumps(result))
    except Exception as error:
        print(json.dumps({"status": "error", "error": type(error).__name__ + ": " + str(error)}))
        raise SystemExit(2)
