#!/usr/bin/env python3
"""
tool_wrapper.py
===============
Sabrina-facing tool interface for the coder-agent pipeline. Three functions:

    train_coder(base, data_path, epochs, run_name)         -> {run_id, status, log_path}
    check_coder_training(run_id)                           -> {status, last_loss, step, log_tail}
    eval_coder(run_id_or_checkpoint_path, humaneval_limit) -> {humaneval_passat1, mbpp_passat1, ...}

Design decisions:
  - Training is a DETACHED subprocess. `train_coder` returns immediately with a run_id.
    Sabrina polls with `check_coder_training`. This prevents her main loop from blocking for
    hours during a real training run.
  - Every run lives under `coder_ft_runs/<run_id>/` with `train.log`, `status.json`, and the
    checkpoint directory once training finishes.
  - Everything is JSON-in / JSON-out so it plugs directly into `sabrina_gemini_loop.execute_tool`.
  - Path resolution: run_id can be given as either a bare `<run_id>` (looked up under
    coder_ft_runs/) or a direct filesystem path to a checkpoint.
"""
import argparse
import datetime
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Optional

_HERE = Path(__file__).resolve().parent
RUNS_ROOT = Path(os.environ.get("CODER_RUNS_ROOT",
                                _HERE.parent.parent / "coder_ft_runs")).resolve()


def _new_run_id(run_name: str) -> str:
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in run_name)
    return f"{ts}_{safe}"


def _run_dir(run_id: str) -> Path:
    return RUNS_ROOT / run_id


def _resolve_checkpoint(run_id_or_path: str) -> Path:
    """Accept either a run_id (looked up under RUNS_ROOT) or an absolute path to a checkpoint."""
    p = Path(run_id_or_path)
    if p.is_absolute() and p.exists():
        return p
    # try RUNS_ROOT/<run_id>/final, then RUNS_ROOT/<run_id>/smoke
    for name in ("final", "smoke"):
        cand = _run_dir(run_id_or_path) / name
        if cand.exists():
            return cand
    # if the raw path is a directory that exists, use it
    if p.exists():
        return p.resolve()
    raise FileNotFoundError(f"no checkpoint for '{run_id_or_path}' "
                            f"(looked under {_run_dir(run_id_or_path)}/{{final,smoke}})")


# ── train_coder ────────────────────────────────────────────────────────────────

def train_coder(base: str = "Qwen/Qwen2.5-Coder-1.5B-Instruct",
                data_path: Optional[str] = None,
                epochs: float = 2.0,
                run_name: str = "coder-ft",
                smoke: bool = False,
                extra_args: Optional[list] = None) -> Dict[str, Any]:
    """Kick off a detached full-FT training run. Returns immediately with a run_id."""
    if not data_path or not os.path.exists(data_path):
        return {"ok": False, "error": f"data_path missing or nonexistent: {data_path!r}"}

    run_id = _new_run_id(run_name)
    rd = _run_dir(run_id)
    rd.mkdir(parents=True, exist_ok=True)

    log_path = rd / "train.log"
    status_path = rd / "status.json"
    status = {
        "run_id": run_id, "status": "starting", "started_at": datetime.datetime.now().isoformat(),
        "base": base, "data_path": data_path, "epochs": epochs, "smoke": smoke,
        "cmd": None,
    }

    train_py = _HERE / "train.py"
    cmd = [sys.executable, "-u", str(train_py),
           "--model", base, "--data", data_path,
           "--out", str(rd), "--epochs", str(epochs)]
    if smoke:
        cmd.append("--smoke")
    if extra_args:
        cmd.extend(extra_args)
    status["cmd"] = cmd

    with open(log_path, "w") as lf:
        proc = subprocess.Popen(cmd, stdout=lf, stderr=subprocess.STDOUT,
                                start_new_session=True)
    status["pid"] = proc.pid
    status["status"] = "running"
    status_path.write_text(json.dumps(status, indent=2))
    return {"ok": True, "run_id": run_id, "pid": proc.pid,
            "log_path": str(log_path), "run_dir": str(rd)}


# ── check_coder_training ────────────────────────────────────────────────────────

def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def check_coder_training(run_id: str) -> Dict[str, Any]:
    """Poll a training run. Returns status + last-loss + last-step + short log tail."""
    rd = _run_dir(run_id)
    if not rd.exists():
        return {"ok": False, "error": f"unknown run_id: {run_id!r}"}
    status_path = rd / "status.json"
    log_path = rd / "train.log"
    status = json.loads(status_path.read_text()) if status_path.exists() else {}

    # log tail
    tail = ""
    if log_path.exists():
        try:
            with open(log_path, "rb") as f:
                f.seek(0, 2); size = f.tell()
                f.seek(max(0, size - 4000))
                tail = f.read().decode("utf-8", errors="replace")
        except Exception:
            tail = ""

    # infer last-loss and last-step from log
    last_loss, last_step = None, None
    import re
    for m in re.finditer(r"'loss':\s*'?([-0-9.eE+]+)'?", tail):
        try: last_loss = float(m.group(1))
        except Exception: pass
    for m in re.finditer(r"(\d+)/\d+ \[", tail):
        try: last_step = int(m.group(1))
        except Exception: pass

    # alive?
    pid = status.get("pid")
    alive = _pid_alive(pid) if isinstance(pid, int) else False
    done = "[train] done." in tail
    checkpoint_exists = (rd / "final").exists() or (rd / "smoke").exists()

    if done or checkpoint_exists:
        current_status = "done"
    elif alive:
        current_status = "running"
    else:
        current_status = "exited"

    if status.get("status") != current_status:
        status["status"] = current_status
        status["last_check"] = datetime.datetime.now().isoformat()
        status_path.write_text(json.dumps(status, indent=2))

    return {"ok": True, "run_id": run_id, "status": current_status,
            "alive": alive, "last_loss": last_loss, "last_step": last_step,
            "checkpoint_exists": checkpoint_exists, "log_tail": tail[-1200:]}


# ── eval_coder ────────────────────────────────────────────────────────────────

def eval_coder(run_id_or_checkpoint: str,
               base: str = "Qwen/Qwen2.5-Coder-1.5B-Instruct",
               humaneval_limit: Optional[int] = None,
               mbpp_limit: int = 20,
               out_name: str = "coder_eval.json") -> Dict[str, Any]:
    """Run HumanEval + MBPP-20 eval on a trained checkpoint (blocking; returns pass@1 numbers)."""
    ckpt = _resolve_checkpoint(run_id_or_checkpoint)
    rd = ckpt.parent if ckpt.parent != RUNS_ROOT else ckpt
    out_path = rd / out_name

    eval_py = _HERE / "eval.py"
    cmd = [sys.executable, "-u", str(eval_py),
           "--base", base, "--checkpoint", str(ckpt),
           "--mbpp-limit", str(mbpp_limit),
           "--out", str(out_path)]
    if humaneval_limit is not None:
        cmd.extend(["--humaneval-limit", str(humaneval_limit)])

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        return {"ok": False, "error": "eval subprocess failed",
                "stdout_tail": result.stdout[-1500:], "stderr_tail": result.stderr[-1500:]}

    if not out_path.exists():
        return {"ok": False, "error": "eval finished but no result JSON was written",
                "stdout_tail": result.stdout[-1500:]}
    data = json.loads(out_path.read_text())
    return {"ok": True, "run_id_or_checkpoint": run_id_or_checkpoint,
            "humaneval_passat1": data["humaneval"]["pass_at_1"],
            "mbpp_passat1": data["mbpp"]["pass_at_1"],
            "humaneval_solves": f"{data['humaneval']['solves']}/{data['humaneval']['n']}",
            "mbpp_solves": f"{data['mbpp']['solves']}/{data['mbpp']['n']}",
            "results_file": str(out_path)}


# ── CLI for manual testing ─────────────────────────────────────────────────────

def _cli():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    tr = sub.add_parser("train")
    tr.add_argument("--base", default="Qwen/Qwen2.5-Coder-1.5B-Instruct")
    tr.add_argument("--data", required=True)
    tr.add_argument("--epochs", type=float, default=2.0)
    tr.add_argument("--run-name", default="coder-ft")
    tr.add_argument("--smoke", action="store_true")

    ck = sub.add_parser("check")
    ck.add_argument("--run-id", required=True)

    ev = sub.add_parser("eval")
    ev.add_argument("--run-id", required=True)
    ev.add_argument("--base", default="Qwen/Qwen2.5-Coder-1.5B-Instruct")
    ev.add_argument("--humaneval-limit", type=int, default=None)
    ev.add_argument("--mbpp-limit", type=int, default=20)

    a = ap.parse_args()
    if a.cmd == "train":
        print(json.dumps(train_coder(base=a.base, data_path=a.data, epochs=a.epochs,
                                     run_name=a.run_name, smoke=a.smoke), indent=2))
    elif a.cmd == "check":
        print(json.dumps(check_coder_training(a.run_id), indent=2))
    elif a.cmd == "eval":
        print(json.dumps(eval_coder(a.run_id, base=a.base,
                                    humaneval_limit=a.humaneval_limit,
                                    mbpp_limit=a.mbpp_limit), indent=2))


if __name__ == "__main__":
    _cli()
