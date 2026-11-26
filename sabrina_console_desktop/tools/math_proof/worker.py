"""Durable ego worker, resumable at every candidate and panel boundary."""
import time
import subprocess
import sys
import os
import socket

from .common import digest, load, save, compute
from .compiler import build
from .evaluate import evaluate
from .policy import export


def recover_own_reservation(client):
    leases = client.request("/v1/status").get("leases", [])
    prior = next((x for x in leases if x["resource"] == "compute:lab"
                  and x["state"] == "uncertain" and x["owner"].startswith("sabrina-math-proof:")), None)
    if prior is None:
        return
    pid = (prior.get("executor") or {}).get("pid")
    if (prior.get("executor") or {}).get("host", "").casefold() != socket.gethostname().casefold():
        raise RuntimeError("Previous executor belongs to another host; reservation retained")
    if not isinstance(pid, int) or pid <= 0:
        raise RuntimeError("Uncertain reservation lacks executor evidence")
    try:
        os.kill(pid, 0)
        raise RuntimeError("Previous executor still exists; reservation retained")
    except ProcessLookupError:
        pass
    names = subprocess.check_output(["docker", "ps", "--format", "{{.Names}}"], text=True)
    if any(name.startswith(("math-proof-", "math-print-")) for name in names.splitlines()):
        raise RuntimeError("Previous proof container still exists; reservation retained")
    current = next((x for x in client.request("/v1/status").get("leases", [])
                    if x["resource"] == "compute:lab"), None)
    if current and (current["owner"], current["state"]) == (prior["owner"], "uncertain"):
        client.request("/v1/leases/reconcile", dict(resource="compute:lab", confirmed_stopped=True))


def work(run, client):
    if (run / "completed.json").exists():
        return
    import torch
    torch.set_num_threads(1)
    recover_own_reservation(client)
    while not (run / "completed.json").exists():
        for path in sorted((run / "submissions").glob("*/generator.py")):
            key = path.parent.name
            outcome = run / "outcomes" / f"{key}.json"
            if outcome.exists():
                continue
            source = path.read_text(encoding="utf-8")
            candidate = run / "candidates" / key
            save(run / "worker.json", dict(state="compiling", candidate=key, at=time.time()))
            try:
                if (candidate / "manifest.json").exists():
                    if load(candidate / "manifest.json")["provenance"]["generator_sha256"] != digest(source):
                        raise ValueError("Submission changed after checkpoint compilation")
                if not (candidate / "manifest.json").exists():
                    with compute(client):
                        export(build(source), candidate, dict(generator_sha256=digest(source),
                               submission=load(path.with_suffix(".json"))))
                    (candidate / "generator.py").write_text(source, encoding="utf-8")
                save(run / "worker.json", dict(state="development", candidate=key, at=time.time()))
                summary = evaluate(run, candidate, "development", client)
                save(outcome, dict(state="evaluated", candidate=key, summary=summary))
            except (ValueError, SyntaxError, TypeError, KeyError) as exc:
                save(outcome, dict(state="rejected", candidate=key, diagnostic=str(exc)[:1500]))
        if (run / "dispatch-closed.json").exists():
            viable = [(load(p), p.parent) for p in (run / "candidates").glob("*/development-summary.json")]
            if not viable:
                save(run / "completed.json", dict(state="no_valid_candidates"))
                return
            if not (run / "finalist.json").exists():
                _, best = min(viable, key=lambda item: (-item[0]["metrics"]["policy"]["solved"],
                     item[0]["metrics"]["policy"]["median_attempts"], item[1].name))
                save(run / "finalist.json", dict(candidate=best.name,
                     weight_hash=digest(list((best / "policy.safetensors").read_bytes()))))
            finalist = run / "candidates" / load(run / "finalist.json")["candidate"]
            save(run / "worker.json", dict(state="holdout", candidate=finalist.name, at=time.time()))
            summary = evaluate(run, finalist, "holdout", client)
            # Run a separate interpreter, which loads the checkpoint again.
            sample_path = run / "replay-goal.json"
            save(sample_path, load(run / "holdout.json")["problems"][0]["goal"])
            replay = subprocess.run([sys.executable, "-B", "-m", "tools.math_proof", "prove",
                  "--run", str(run), "--candidate", finalist.name, "--goal", str(sample_path)],
                  capture_output=True, text=True, timeout=1500)
            replay_receipt = dict(exit_code=replay.returncode, output=replay.stdout[:6000],
                                  diagnostic=replay.stderr[:2000])
            save(run / "completed.json", dict(state="qualified" if summary["passes"] and replay.returncode == 0 else "experimental",
                 finalist=finalist.name, summary=summary, fresh_reload=replay_receipt, training_steps=0))
            return
        save(run / "worker.json", dict(state="waiting_submissions", at=time.time()))
        time.sleep(15)
