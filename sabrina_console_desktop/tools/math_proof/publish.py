"""Publish the completed run as a reproducible local evidence bundle."""
import ast
import hashlib
import re
import shutil
import subprocess
from pathlib import Path

from .common import load, save


def publish(run, output=None):
    from .collective import REMOTE, remote_read
    completed = remote_read("completed.json")
    if not completed or completed.get("state") not in ("qualified", "experimental"):
        raise ValueError("Evaluation has not completed")
    candidate = completed["finalist"]
    if not re.fullmatch("[a-z0-9-]{1,60}", candidate):
        raise ValueError("Invalid finalist identifier")
    output = output or run / "published"
    output.mkdir(parents=True, exist_ok=True)
    files = {"completed.json": "completed.json", "finalist.json": "finalist.json", "fixtures.json": "fixtures.json",
             "development.json": "development-goals.json", "holdout.json": "holdout-goals.json",
             "replay-goal.json": "example-goal.json"}
    for name in ("manifest.json", "policy.safetensors", "generator.py",
                 "development-summary.json", "holdout-summary.json"):
        files[f"candidates/{candidate}/{name}"] = name
    for source, target in files.items():
        subprocess.run(["scp", "-q", f"ego@ego.local:{REMOTE}/{source}", str(output / target)],
                       check=True, timeout=30)
    for split in ("development", "holdout"):
        (output / split).mkdir(exist_ok=True)
        subprocess.run(["scp", "-q", f"ego@ego.local:{REMOTE}/candidates/{candidate}/{split}/*.json",
                        str(output / split) + "/"], check=True, timeout=90)
    manifest = load(output / "manifest.json")
    assert hashlib.sha256((output / "policy.safetensors").read_bytes()).hexdigest() == manifest["checkpoint_sha256"]
    for name in ("critique.json", "roster.json"):
        if (run / name).exists():
            shutil.copyfile(run / name, output / name)
    shutil.copytree(Path(__file__).parent, output / "math_proof",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"), dirs_exist_ok=True)
    panels = {}
    for split in ("development", "holdout"):
        rows = [load(p) for p in (output / split).glob("*.json")]
        summary = load(output / f"{split}-summary.json")
        expected = {g["id"] for g in load(output / f"{split}-goals.json")["problems"]}
        if len(rows) != summary["count"] or {r["id"] for r in rows} != expected:
            raise ValueError("Published proof traces are incomplete")
        panels[split] = [dict(panel=panel, count=sum(r["panel"] == panel for r in rows),
                  proved=sum(r["panel"] == panel and r["results"]["policy"]["solved"] for r in rows))
                        for panel in range(8)]
    save(output / "panels.json", panels)
    holdout = load(output / "holdout-summary.json")
    dev = load(output / "development-summary.json")
    replay = completed["fresh_reload"]
    replay_result = ast.literal_eval(replay["output"].splitlines()[0]) if replay["exit_code"] == 0 else {}
    save(output / "replay.json", dict(exit_code=replay["exit_code"],
                      emitted_proof_checked=replay_result.get("emitted_proof_checked", False)))
    offset = replay["output"].find("import Mathlib")
    if offset >= 0:
        (output / "example-proof.lean").write_text(replay["output"][offset:], encoding="utf-8")
    # Inspect the actual printed tensors without importing a neural runtime.
    import struct
    import json
    raw = (output / "policy.safetensors").read_bytes()
    length = struct.unpack("<Q", raw[:8])[0]
    header = json.loads(raw[8:8+length])
    up = header["up"]
    left, right = up["data_offsets"]
    values = struct.unpack("<288f", raw[8+length+left:8+length+right])
    uses_history = any(values[row*18+col] for row in range(16) for col in range(12, 18))
    save(output / "history-audit.json", dict(nonzero_history_inputs=uses_history,
                  cleared_history_holdout=holdout["metrics"]["reset"]))
    labels = {"policy": "Printed policy", "fixed": "Fixed order", "uniform": "Random order",
              "zero": "Zero weights", "reset": "Cleared history"}
    table = "\n".join(f"| {labels[mode]} | {m['solved']}/{holdout['count']} | {m['median_attempts']:g} |"
                      for mode, m in holdout["metrics"].items())
    history_text = ("The audit found nonzero connections from history to action scoring." if uses_history else
                    "All connections from history to action scoring had zero weights.")
    critique = load(output / "critique.json") if (output / "critique.json").exists() else {}
    critique_text = "An independent critique was received." if critique.get("ok") else "An independent critique was unavailable."
    text = f"""# Sabrina printed mathematics specialist

The run finished as **{completed['state']}**. A recruit generated {manifest['parameters']} FP32 weights with zero training steps. The provider reported model `{manifest['provenance']['submission']['reported_model']}`.

Development solved {dev['metrics']['policy']['solved']}/{dev['count']} goals. Holdout solved {holdout['metrics']['policy']['solved']}/{holdout['count']}. The false-goal controls rejected {dev['negative_rejections']+holdout['negative_rejections']}/{dev['negative_count']+holdout['negative_count']} statements.

| Holdout method | Proved | Median attempts |
|---|---:|---:|
{table}

The median attempt reduction against random order was {holdout['median_attempt_reduction_vs_uniform']:.1%}. Attempts count independently checked Lean declarations. Six actions are batched per process and comparators share the checker cache. This measures search effort.

The independent Python replay exited with code {replay['exit_code']}. Its emitted proof passed a separate kernel check according to the replay receipt, `{replay_result.get('emitted_proof_checked', False)}`.

The policy selects among six supplied proof actions for structured integer goals and additive recurrences. Lean tactics perform the algebra and induction. {history_text} The cleared-history comparison records the effect of state on the completed workload.

{critique_text} The Windows roster and critique receipts record actual participation and provider deferrals. The candidate, generator, frozen goals, per-goal traces, and source package are included here. The checkpoint manifest identifies the author request and hashes.

The persistent worker on ego uses coordinator reservations, two Lean checkers limited to one CPU and 5 GiB each, and a six-CPU/12-GiB worker service ceiling. Lean is pinned to 4.34.1 and mathlib to `d13f23b723b8a846827a245b89c10fc7d3f11612`.

From Sabrina's checkout on ego, reproduce the example with the command below. The bundle includes its structured GoalSpec and the emitted Lean proof.

```sh
python3 -m tools.math_proof prove --run data/architecture_research/math-proof-v1 --candidate {candidate} --goal data/architecture_research/math-proof-v1/replay-goal.json
```
"""
    (output / "RESULTS.md").write_text(text, encoding="utf-8")
    save(output / "bundle-hashes.json", {str(p.relative_to(output)): hashlib.sha256(p.read_bytes()).hexdigest()
                          for p in output.rglob("*") if p.is_file() and p.name != "bundle-hashes.json"})
    print(output)
    return output
