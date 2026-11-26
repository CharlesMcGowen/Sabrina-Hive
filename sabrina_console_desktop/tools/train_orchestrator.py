#!/usr/bin/env python3
"""
train_orchestrator.py
=====================
Resource-aware training orchestrator -- the scheduling layer of Sabrina's self-training flywheel.

It (1) detects idle GPU/CPU capacity, (2) places queued jobs only where there is genuinely free
capacity (so it coexists with jobs a human launched by hand -- it never stacks a GPU job onto a busy
GPU), and (3) exports each finished job's artifacts (adapters, scores, logs) to a durable directory
so an ephemeral rented pod's work is preserved as it goes.

Runs ON the pod (reads local `nvidia-smi`, launches local subprocesses). Sabrina/CodeAgent interact
with it by writing the job queue and reading the status file; a human launches the `serve` loop.

Design decisions:
  * Idle-only placement: a GPU counts as free only if util <= util_max AND free VRAM >= the job's
    min_free_gb. So a human's hand-launched run makes that GPU unavailable and the orchestrator
    quietly schedules elsewhere (other GPU, or CPU) -- no collisions.
  * Durable export ("save as we go"): on completion, artifact globs are copied to ARTIFACTS_DIR
    (on the persistent /workspace volume) with a manifest. Nothing is model-exfiltration; these are
    our own training outputs, preserved against pod teardown.
  * Crash-safe: job state persists to STATUS_FILE; `serve` resumes tracking across restarts by pid.

CLI:
    python train_orchestrator.py snapshot                 # print current resources (idle detection)
    python train_orchestrator.py add --id sqlmap_grpo --needs gpu --gpus 2 \
        --cmd "cd /workspace/rl && python -u train_grpo_sqlmap.py --use-vllm ..." \
        --artifact "/workspace/rl/grpo_best/**" --artifact "/workspace/rl/grpo_out/*.json"
    python train_orchestrator.py serve --interval 20      # place + reap + export loop
    python train_orchestrator.py status                   # print the status file
"""
import argparse
import contextlib
import glob as globmod
import json
import os
import shutil
import subprocess
import time
from datetime import datetime, timezone

BASE = os.environ.get("ORCH_BASE", "/workspace/rl/orchestrator")
JOBS_FILE = os.path.join(BASE, "jobs.json")
STATUS_FILE = os.path.join(BASE, "status.json")
ARTIFACTS_DIR = os.path.join(BASE, "artifacts")
LOG_DIR = os.path.join(BASE, "logs")
for d in (BASE, ARTIFACTS_DIR, LOG_DIR):
    os.makedirs(d, exist_ok=True)


LOCK_FILE = os.path.join(BASE, ".lock")


@contextlib.contextmanager
def _lock():
    """Serialize read-modify-write of jobs.json across the `add` CLI and the `serve` loop.

    Without this, an `add` and a `serve` cycle race on jobs.json and one clobbers the other. Uses
    POSIX flock on the pod; degrades to a no-op where fcntl is unavailable (e.g. Windows dev box).
    """
    try:
        import fcntl
    except ImportError:
        yield
        return
    f = open(LOCK_FILE, "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(f, fcntl.LOCK_UN)
        finally:
            f.close()


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ── resource detection ───────────────────────────────────────────────────────────
def gpu_status():
    """Return [{index, util, mem_used_gb, mem_total_gb, mem_free_gb}] via nvidia-smi."""
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=index,utilization.gpu,memory.used,memory.total",
             "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=15).stdout
    except Exception as e:
        return [{"error": str(e)}]
    gpus = []
    for line in out.strip().splitlines():
        idx, util, used, total = [x.strip() for x in line.split(",")]
        used_gb, total_gb = float(used) / 1024, float(total) / 1024
        gpus.append({"index": int(idx), "util": int(float(util)),
                     "mem_used_gb": round(used_gb, 1), "mem_total_gb": round(total_gb, 1),
                     "mem_free_gb": round(total_gb - used_gb, 1)})
    return gpus


def cpu_status():
    try:
        load1 = os.getloadavg()[0]
    except Exception:
        load1 = -1.0
    ncpu = os.cpu_count() or 1
    ram_used = ram_total = -1.0
    try:
        with open("/proc/meminfo") as f:
            mem = {l.split(":")[0]: int(l.split()[1]) for l in f}
        ram_total = mem["MemTotal"] / 1024 / 1024
        ram_used = (mem["MemTotal"] - mem["MemAvailable"]) / 1024 / 1024
    except Exception:
        pass
    return {"ncpu": ncpu, "load1": round(load1, 2), "load_per_cpu": round(load1 / ncpu, 2),
            "ram_used_gb": round(ram_used, 1), "ram_total_gb": round(ram_total, 1)}


def idle_gpus(util_max=15, min_free_gb=30.0):
    return [g["index"] for g in gpu_status()
            if "error" not in g and g["util"] <= util_max and g["mem_free_gb"] >= min_free_gb]


def resource_snapshot():
    return {"ts": _now(), "gpus": gpu_status(), "cpu": cpu_status(),
            "idle_gpus": idle_gpus()}


# ── job queue ─────────────────────────────────────────────────────────────────────
def _load_jobs():
    try:
        return json.load(open(JOBS_FILE))
    except Exception:
        return []


def _save_jobs(jobs):
    tmp = JOBS_FILE + ".tmp"
    json.dump(jobs, open(tmp, "w"), indent=2)
    os.replace(tmp, JOBS_FILE)


def job_may_pollute(cmd):
    """True if a job command pip-installs without an isolated venv -- i.e. it could upgrade shared
    core deps in the base training env. This ACTUALLY happened once (a job's `pip install
    sentence-transformers` yanked transformers 4.46.3 -> 5.17 and broke the SFT/GRPO stack). A job that
    needs extra packages must create a venv (`python -m venv`), use `--target`, etc. -- never the base."""
    low = (cmd or "").lower()
    has_pip = "pip install" in low or "pip3 install" in low
    isolated = any(t in low for t in ("venv", "virtualenv", "--target", "conda create", "uv venv", "uv pip"))
    return has_pip and not isolated


def add_job(spec):
    if job_may_pollute(spec.get("cmd", "")) and not spec.get("allow_env_change"):
        raise SystemExit(
            f"[orch] REFUSED job {spec.get('id')!r}: it pip-installs into the SHARED training env, "
            f"which can upgrade core deps and break training (this broke transformers 4.46.3->5.17 "
            f"once). Use an isolated venv / --target, or pass allow_env_change=true to override.")
    with _lock():
        jobs = _load_jobs()
        if any(j["id"] == spec["id"] for j in jobs):
            raise SystemExit(f"job id {spec['id']!r} already queued")
        spec.setdefault("needs", "gpu")
        spec.setdefault("gpus", 1)
        spec.setdefault("min_free_gb", 30.0)
        spec.setdefault("artifacts", [])
        spec.setdefault("state", "pending")
        jobs.append(spec)
        _save_jobs(jobs)
    print(f"[orch] queued {spec['id']} (needs={spec['needs']} gpus={spec['gpus']})")


def _pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except Exception:
        return False


# ── placement / reaping / export ───────────────────────────────────────────────────
def _launch(job, gpu_ids):
    log_path = os.path.join(LOG_DIR, f"{job['id']}.log")
    rc_path = os.path.join(LOG_DIR, f"{job['id']}.rc")
    env_prefix = ""
    if gpu_ids:
        env_prefix = f"CUDA_VISIBLE_DEVICES={','.join(map(str, gpu_ids))} "
    # wrap so we always capture an exit code, even on crash
    wrapped = f"{env_prefix}bash -c '({job['cmd']}) ; echo $? > {rc_path}'"
    with open(log_path, "a") as lf:
        lf.write(f"\n===== launch {_now()} gpus={gpu_ids} =====\n{job['cmd']}\n")
    proc = subprocess.Popen(wrapped, shell=True, stdout=open(log_path, "a"),
                            stderr=subprocess.STDOUT, start_new_session=True)
    job.update(state="running", pid=proc.pid, gpu_ids=gpu_ids,
               started=_now(), log=log_path, rc_file=rc_path)
    print(f"[orch] launched {job['id']} pid={proc.pid} on gpus={gpu_ids}")


def _export_artifacts(job):
    dest = os.path.join(ARTIFACTS_DIR, job["id"])
    os.makedirs(dest, exist_ok=True)
    exported = []
    for pattern in job.get("artifacts", []):
        for src in globmod.glob(pattern, recursive=True):
            if not os.path.exists(src):
                continue
            name = os.path.basename(src.rstrip("/")) or "artifact"
            target = os.path.join(dest, name)
            try:
                if os.path.isdir(src):
                    shutil.copytree(src, target, dirs_exist_ok=True)
                else:
                    shutil.copy2(src, target)
                exported.append(target)
            except Exception as e:
                print(f"[orch] export failed {src}: {e}")
    manifest = {"job": job["id"], "ts": _now(), "rc": job.get("rc"),
                "files": exported}
    json.dump(manifest, open(os.path.join(dest, "manifest.json"), "w"), indent=2)
    print(f"[orch] exported {len(exported)} artifact(s) for {job['id']} -> {dest}")
    return exported


def reap(jobs):
    for job in jobs:
        if job.get("state") != "running":
            continue
        if _pid_alive(job["pid"]):
            continue
        rc = None
        try:
            rc = int(open(job["rc_file"]).read().strip())
        except Exception:
            pass
        job["rc"] = rc
        job["state"] = "done" if rc == 0 else "failed"
        job["ended"] = _now()
        _export_artifacts(job)
        print(f"[orch] {job['id']} -> {job['state']} (rc={rc})")


def place(jobs, max_cpu_jobs=4):
    running_cpu = sum(1 for j in jobs if j.get("state") == "running" and j.get("needs") == "cpu")
    for job in jobs:
        if job.get("state") != "pending":
            continue
        if job["needs"] == "cpu":
            if running_cpu < max_cpu_jobs:
                _launch(job, gpu_ids=[])
                running_cpu += 1
            continue
        free = idle_gpus(min_free_gb=job.get("min_free_gb", 30.0))
        if len(free) >= job.get("gpus", 1):
            _launch(job, gpu_ids=free[:job["gpus"]])


def write_status(jobs):
    status = {"ts": _now(), "resources": resource_snapshot(),
              "jobs": [{k: j.get(k) for k in ("id", "state", "needs", "gpus", "gpu_ids",
                                              "pid", "rc", "started", "ended")} for j in jobs]}
    tmp = STATUS_FILE + ".tmp"
    json.dump(status, open(tmp, "w"), indent=2)
    os.replace(tmp, STATUS_FILE)
    return status


# ── CLI ─────────────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    # dest must NOT be "cmd" -- the `add` subcommand has a --cmd flag that would clobber it.
    sub = ap.add_subparsers(dest="subcmd", required=True)
    sub.add_parser("snapshot")
    sub.add_parser("status")
    a_add = sub.add_parser("add")
    a_add.add_argument("--id", required=True)
    a_add.add_argument("--cmd", required=True)
    a_add.add_argument("--needs", choices=["gpu", "cpu"], default="gpu")
    a_add.add_argument("--gpus", type=int, default=1)
    a_add.add_argument("--min-free-gb", type=float, default=30.0)
    a_add.add_argument("--artifact", action="append", default=[], dest="artifacts")
    a_add.add_argument("--allow-env-change", action="store_true",
                       help="override the guard that refuses jobs pip-installing into the shared env")
    a_serve = sub.add_parser("serve")
    a_serve.add_argument("--interval", type=int, default=20)
    a_serve.add_argument("--forever", action="store_true",
                         help="keep looping even when the queue is drained (wait for new jobs)")
    a = ap.parse_args()

    if a.subcmd == "snapshot":
        print(json.dumps(resource_snapshot(), indent=2))
    elif a.subcmd == "status":
        try:
            print(open(STATUS_FILE).read())
        except Exception:
            print("{}  (no status yet)")
    elif a.subcmd == "add":
        add_job({"id": a.id, "cmd": a.cmd, "needs": a.needs, "gpus": a.gpus,
                 "min_free_gb": a.min_free_gb, "artifacts": a.artifacts,
                 "allow_env_change": a.allow_env_change})
    elif a.subcmd == "serve":
        print(f"[orch] serve loop, interval={a.interval}s, base={BASE}", flush=True)
        while True:
            with _lock():
                jobs = _load_jobs()
                reap(jobs)
                place(jobs)
                _save_jobs(jobs)
            st = write_status(jobs)
            pend = sum(1 for j in jobs if j["state"] == "pending")
            run = sum(1 for j in jobs if j["state"] == "running")
            print(f"[orch] {st['ts']} idle_gpus={st['resources']['idle_gpus']} "
                  f"pending={pend} running={run}", flush=True)
            if not a.forever and pend == 0 and run == 0 and jobs:
                print("[orch] queue drained; exiting (use --forever to keep waiting).", flush=True)
                break
            time.sleep(a.interval)


if __name__ == "__main__":
    main()
