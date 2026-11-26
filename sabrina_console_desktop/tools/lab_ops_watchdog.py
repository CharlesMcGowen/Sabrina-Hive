# -*- coding: utf-8 -*-
"""
tools.lab_ops_watchdog
========================
Self-healing infrastructure watchdog for the lab host (192.168.2.69). Built 2026-09-18 after a
full night of manually rescuing the same three failure classes over and over -- the whole point
is that an autonomous research loop (framing-vs-Juice-Shop-score trials, evolve_framing, etc.)
should be able to run unattended without a human noticing these and stepping in by hand.

1. Stale/duplicate processes competing for the same shared target (Juice Shop) or resources.
   Live-confirmed repeatedly tonight: orphaned trial runners, a sabrina_gemini_loop.py instance
   left running for hours after its own work stalled, dead wait-loops still polling.
2. Ollama pulls that stall (zero real progress) due to slow/dead registry bandwidth.
   Live-confirmed: qwen2.5:72b stalled twice near the finish line; qwen2.5-coder pulls hit the
   same ~250-800 KB/s ceiling. Nothing previously noticed a pull that stopped moving -- it just
   sat there silently for hours until a human happened to check.

Run as its own long-lived process: nohup python3 tools/lab_ops_watchdog.py &
Meant to run ON the lab host (192.168.2.69) where the actual processes/pulls/Juice Shop live --
the dashboard health-check gap (item 3 discussed alongside this) is handled separately in
tools/sabrina_supervisor.py, which already runs on the Windows side where the dashboard lives.
"""

import subprocess
import time
import os
import sys

# Process patterns considered stale once older than MAX_PROCESS_AGE_S. Deliberately narrow --
# only patterns for SHORT-LIVED, single-purpose runs (a trial, a wait-loop). Long-running,
# intentional things (ollama serve, the dashboard itself) are never in this list.
STALE_PATTERNS = [
    "run_framing_juiceshop_trial",
    "trial_batch",
    "refusal_batch",
]
MAX_PROCESS_AGE_S = 2 * 3600  # 2 hours
SWEEP_INTERVAL_S = 300  # 5 min


def _sh(cmd: str) -> str:
    return subprocess.run(cmd, shell=True, capture_output=True, text=True).stdout.strip()


def sweep_stale_processes(dry_run: bool = False) -> list:
    """
    Kills processes matching STALE_PATTERNS older than MAX_PROCESS_AGE_S. Returns the list of
    (pid, age_seconds, cmd_preview) killed, so a caller (or a log line) can see exactly what
    was cleaned up -- silent process-killing with no record is how tonight's confusion (was a
    result contaminated by something else running?) happened in the first place.
    """
    killed = []
    out = _sh("ps -eo pid,etimes,cmd --no-headers")
    for line in out.split("\n"):
        parts = line.strip().split(None, 2)
        if len(parts) < 3:
            continue
        pid, etimes_s, cmd = parts
        try:
            etimes = int(etimes_s)
        except ValueError:
            continue
        if any(p in cmd for p in STALE_PATTERNS) and etimes > MAX_PROCESS_AGE_S:
            preview = cmd[:120]
            print(f"[watchdog] {'[DRY RUN] would kill' if dry_run else 'killing'} stale pid={pid} age={etimes}s: {preview}", flush=True)
            if not dry_run:
                _sh(f"kill {pid}")
            killed.append((pid, etimes, preview))
    return killed


def pull_model_with_stall_recovery(model_name: str, log_path: str, stall_timeout_s: int = 900, max_retries: int = 3) -> bool:
    """
    Launches `ollama pull <model_name>`, monitoring the log file's mtime -- not byte counts --
    for a real stall. Ollama's progress bar redraws constantly even at very low bandwidth, so a
    log file that's genuinely stopped changing means the download has actually died (network
    drop, registry issue), not just gone slow. Kills and retries up to max_retries on a detected
    stall. This is the function any future pull (watchdog-initiated or agent-initiated) should
    go through instead of a bare `ollama pull`, so stall-protection is built in from the start
    rather than needing retroactive detection of an ad-hoc process.
    """
    for attempt in range(1, max_retries + 1):
        print(f"[watchdog] pull attempt {attempt}/{max_retries} for {model_name}", flush=True)
        proc = subprocess.Popen(f"ollama pull {model_name} > {log_path} 2>&1", shell=True)
        while proc.poll() is None:
            time.sleep(30)
            if os.path.exists(log_path):
                mtime = os.path.getmtime(log_path)
                if time.time() - mtime > stall_timeout_s:
                    print(f"[watchdog] {model_name} pull STALLED (no log activity for {stall_timeout_s}s) -- killing and retrying", flush=True)
                    proc.kill()
                    _sh(f"pkill -f 'ollama pull {model_name}'")
                    time.sleep(3)
                    break
        else:
            if proc.returncode == 0:
                print(f"[watchdog] {model_name} pull completed successfully (attempt {attempt})", flush=True)
                return True
            print(f"[watchdog] {model_name} pull exited with code {proc.returncode} (not a stall) -- treating as failed attempt", flush=True)
    print(f"[watchdog] {model_name} pull FAILED after {max_retries} attempts -- giving up, needs human attention", flush=True)
    return False


def main():
    print(f"[watchdog] starting, sweep interval {SWEEP_INTERVAL_S}s, stale threshold {MAX_PROCESS_AGE_S}s", flush=True)
    while True:
        try:
            sweep_stale_processes()
        except Exception as e:
            print(f"[watchdog] sweep error (non-fatal, continuing): {e}", flush=True)
        time.sleep(SWEEP_INTERVAL_S)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--dry-run":
        print(sweep_stale_processes(dry_run=True))
    else:
        main()
