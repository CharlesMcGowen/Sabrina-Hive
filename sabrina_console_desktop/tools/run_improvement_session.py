"""Launcher for a standing codebase-improvement session (LOCAL-ONLY, no git, nothing leaves the box).

Charles's constraints: this repo never goes to GitHub and the code can't leave the environment; source
is deliberately kept out of git. So review/rollback here is done with LOCAL FILE SNAPSHOTS, not git:

  1. Snapshot every in-scope source file to  analysis/improvement_sessions/<ts>/baseline/  (a few MB
     of .py only -- never the 6.7 GB of vendored/generated trees; scope from improvement_tracker).
  2. Run sabrina_gemini_loop.py with the improvement goal + a wall-clock --max-seconds (session length).
     The loop edits IN PLACE (fast) -- the snapshot is the safety net.
  3. At the end, diff baseline vs current and write REVIEW.md (added / modified / deleted + unified
     diffs) so Charles can review exactly what changed. baseline/ stays for rollback.

Rollback:  python tools/run_improvement_session.py --rollback analysis/improvement_sessions/<ts>
           (restores every in-scope file to its pre-session content and deletes files the session added)

Run:       python tools/run_improvement_session.py --minutes 20
Supervised: the `codebase-self-improvement` task in sabrina_supervisor.py invokes this.
"""

import argparse
import datetime as _dt
import difflib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SESSIONS_DIR = REPO_ROOT / "analysis" / "improvement_sessions"

sys.path.insert(0, str(REPO_ROOT))
from tools.improvement_tracker import list_in_scope_files, is_in_scope  # noqa: E402

DEFAULT_GOAL = (
    "STANDING CODEBASE-IMPROVEMENT SESSION. You are improving Sabrina's OWN repository. Every file you "
    "change is snapshotted first, so a human can review the diff and roll back -- work in small, "
    "verifiable steps.\n"
    "Each round: (1) call get_improvement_queue and take the TOP item -- do NOT invent your own targets. "
    "(2) EXPLORE it like an engineer: search_code to find where things are defined / who calls them, "
    "find_files to locate modules, read_lines to zoom into a hit, describe_module for a module's API, "
    "read_file for the whole thing -- never assume a method/attribute exists. (3) Do ONE in-scope improvement "
    "matching the item's 'action':\n"
    "   - add_tests: create/extend the target test file under tests/ with real, ASSERTING pytest tests "
    "that characterize the source module's ACTUAL behavior (import it, call real functions, assert "
    "concrete outputs). Do NOT edit the source module. If a test reveals a genuine bug, save_knowledge a "
    "one-line note so a later bugfix target can address it -- do not weaken the test to hide it.\n"
    "   - retrofit_engagement_gate: add the EngagementGate scope-check to the engine (copy the pattern "
    "from an already-gated engine) WITHOUT changing its detection logic.\n"
    "   - add_docstrings / add_type_hints: add docstrings/type hints only; do not change behavior.\n"
    "(4) Verify: run_tests with `path` set to the SPECIFIC test file you wrote/edited (fast, isolated) -- "
    "it must return [TESTS GREEN]. Do NOT run the whole suite as your gate (some pre-existing tests do "
    "real network I/O and hang). If the target is a runnable module, also verify_module it. If not green, "
    "fix YOUR file and re-run -- NEVER delete/weaken a test to pass, and never edit a protected/out-of-scope "
    "file. (5) mark_improved(target, action_taken, outcome). Then take the next target.\n"
    "HARD RULES: only edit files get_improvement_queue hands you (in-scope + writable); NEVER edit the "
    "protected core or the finished dream_rsi modules (writes are BLOCKED). Prefer edit_file for existing "
    "files, write_file for new ones. Keep each change small and independently reviewable. If the queue is "
    "empty or you are truly blocked, save_knowledge a note and call completed."
)


CORE_GOAL = (
    "CORE SELF-IMPROVEMENT (RSI) SESSION. You are improving Sabrina's OWN brain -- the reasoning loop, "
    "oracle routing, prompts, skills. This is a checkpointed session: every change is snapshotted and "
    "AUTO-REVERTED if it breaks the self-check, so improve carefully and boldly.\n"
    "Each round: (1) get_improvement_queue -> take the TOP item (core modules come first; action "
    "'improve'). (2) EXPLORE deeply before changing anything: search_code to find every caller/user of "
    "what you'll touch (so you don't break them), find_files/read_lines/describe_module/read_file to "
    "understand it fully -- never assume. (3) Make ONE focused improvement to that core module: clearer code, "
    "accurate docstrings/type hints, a real bug fix, or a SAFE refactor that PRESERVES external behavior "
    "and the module's public API (other core modules import it). Prefer edit_file for surgical changes. "
    "(4) mark_improved(target, action_taken='improve', outcome). The system runs a FULL self-check on "
    "mark; if it replies AUTO-REVERTED, your change broke the core and was undone -- read the self-check "
    "tail, understand the breakage, and try a smaller/safer change. "
    "HARD RULES: NEVER touch the recovery layer (sabrina_supervisor, system_checkpoint, pod_control, "
    "run_improvement_session, improvement_tracker) or the finished dream_rsi modules -- writes are BLOCKED. "
    "ONE module per round; small, behavior-preserving changes; when unsure prefer docs/type-hints over "
    "risky logic edits. If truly blocked, save_knowledge a note and call completed."
)


def _read(path):
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None


def _snapshot_baseline(session_dir):
    files = list_in_scope_files(exts=(".py",))
    baseline = session_dir / "baseline"
    for rel in files:
        src = REPO_ROOT / rel
        if not src.is_file():
            continue
        dst = baseline / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copy2(src, dst)
        except Exception as e:
            print(f"[launcher] warn: could not snapshot {rel}: {e}", flush=True)
    (session_dir / "manifest.json").write_text(json.dumps({"files": files}, indent=2), encoding="utf-8")
    print(f"[launcher] snapshotted {len(files)} in-scope files -> {baseline}", flush=True)
    return files


def _build_review(session_dir, baseline_files):
    baseline = session_dir / "baseline"
    current = set(list_in_scope_files(exts=(".py",)))
    base_set = set(baseline_files)
    added, modified, deleted, diffs = [], [], [], []
    for rel in sorted(current | base_set):
        base_txt = _read(baseline / rel) if (baseline / rel).is_file() else None
        cur_txt = _read(REPO_ROOT / rel) if (REPO_ROOT / rel).is_file() else None
        if base_txt is None and cur_txt is not None:
            added.append(rel)
            diffs.append(f"### ADDED  {rel}\n\n```python\n{cur_txt[:8000]}\n```\n")
        elif base_txt is not None and cur_txt is None:
            deleted.append(rel)
            diffs.append(f"### DELETED  {rel}\n")
        elif base_txt is not None and cur_txt is not None and base_txt != cur_txt:
            modified.append(rel)
            ud = "".join(difflib.unified_diff(
                base_txt.splitlines(keepends=True), cur_txt.splitlines(keepends=True),
                fromfile=f"baseline/{rel}", tofile=rel))
            diffs.append(f"### MODIFIED  {rel}\n\n```diff\n{ud[:8000]}\n```\n")
    review = session_dir / "REVIEW.md"
    lines = [
        f"# Improvement session review — {session_dir.name}",
        "",
        f"- Added: {len(added)}  |  Modified: {len(modified)}  |  Deleted: {len(deleted)}",
        "",
        "## Added files", *([f"- {p}" for p in added] or ["- (none)"]),
        "", "## Modified files", *([f"- {p}" for p in modified] or ["- (none)"]),
        "", "## Deleted files", *([f"- {p}" for p in deleted] or ["- (none)"]),
        "", "---", "", *diffs,
        "", "---",
        f"Rollback everything: python tools/run_improvement_session.py --rollback {session_dir.as_posix()}",
    ]
    review.write_text("\n".join(lines), encoding="utf-8")
    return added, modified, deleted, review


def _verify_marked_targets(session_start_ts):
    """Reliability gate: re-check every target the loop MARKED done during this session. For a test
    file, actually re-run pytest on it (GREEN/RED); otherwise confirm it exists (EXISTS/PHANTOM). This
    catches the loop over-claiming ("tests green" when the file is missing or red) without a human
    having to spot-check. Returns a list of {target, action, verdict, detail}."""
    ledger_path = REPO_ROOT / "data" / "improvement_ledger.json"
    try:
        ledger = json.loads(ledger_path.read_text(encoding="utf-8")) if ledger_path.exists() else {}
    except Exception:
        ledger = {}
    rows = []
    for target, rec in ledger.items():
        try:
            if float(rec.get("last_improved_ts", 0)) < session_start_ts:
                continue  # not touched this session
        except Exception:
            continue
        action = rec.get("action", "")
        tgt = REPO_ROOT / target
        if not tgt.is_file():
            rows.append({"target": target, "action": action, "verdict": "PHANTOM", "detail": "file missing on disk"})
            continue
        is_test = target.startswith("tests/") or Path(target).name.startswith("test_")
        if is_test:
            try:
                p = subprocess.run([sys.executable, "-X", "utf8", "-m", "pytest", "-q", "--no-header",
                                    "-p", "no:cacheprovider", target],
                                   cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=180,
                                   env={**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"})
                last = [ln for ln in (p.stdout or "").strip().splitlines() if ln.strip()]
                detail = last[-1] if last else f"exit {p.returncode}"
                rows.append({"target": target, "action": action,
                             "verdict": "GREEN" if p.returncode == 0 else "RED", "detail": detail})
            except Exception as e:
                rows.append({"target": target, "action": action, "verdict": "ERROR", "detail": str(e)[:120]})
        else:
            rows.append({"target": target, "action": action, "verdict": "EXISTS", "detail": "non-test target present"})
    return rows


def _append_verification(review_path, rows, tampered):
    lines = ["", "", "## Verification (post-session re-check of every 'done' target)", ""]
    if not rows:
        lines.append("- (no targets were marked done this session)")
    else:
        lines.append("| verdict | target | detail |")
        lines.append("|---|---|---|")
        for r in rows:
            lines.append(f"| {r['verdict']} | {r['target']} | {r['detail'][:80].replace('|','/')} |")
    if tampered:
        lines += ["", "### ⚠ Possible test tampering (a baseline test file was deleted or shrank):",
                  *[f"- {t}" for t in tampered]]
    try:
        with open(review_path, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    except Exception:
        pass
    return rows


def _rollback(session_dir):
    session_dir = Path(session_dir)
    baseline = session_dir / "baseline"
    manifest = json.loads((session_dir / "manifest.json").read_text(encoding="utf-8"))
    base_files = set(manifest.get("files", []))
    restored, removed = 0, 0
    # Restore every snapshotted file to its pre-session content.
    for rel in base_files:
        src = baseline / rel
        if src.is_file():
            dst = REPO_ROOT / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            restored += 1
    # Delete in-scope files the session ADDED (present now, absent from the baseline manifest).
    for rel in list_in_scope_files(exts=(".py",)):
        if rel not in base_files and is_in_scope(rel):
            try:
                (REPO_ROOT / rel).unlink()
                removed += 1
            except Exception:
                pass
    print(f"[rollback] restored {restored} files, deleted {removed} session-added files from {session_dir}", flush=True)
    return 0


def main():
    ap = argparse.ArgumentParser(description="Run a snapshot-isolated, local-only codebase-improvement session.")
    ap.add_argument("--minutes", type=int, default=45, help="Session length in minutes (fixed session length).")
    ap.add_argument("--workers", type=int, default=1, help="Number of parallel improvement loops (swarm). Each gets a disjoint slice of the queue. 0 = auto: use the dreamed policy's worker_count (self-tuning parallelism).")
    ap.add_argument("--goal", type=str, default=DEFAULT_GOAL, help="Override the improvement goal.")
    ap.add_argument("--oracle-preference", type=str, default="local")
    ap.add_argument("--core", action="store_true", help="CORE-RSI session: the loop may edit its own brain. Forces 1 worker and full-system checkpoint protection.")
    ap.add_argument("--measure", action="store_true", help="After the session, re-run the capability benchmark on the 72b (the RSI proof). Costs one benchmark run; dream() always runs regardless.")
    ap.add_argument("--rollback", type=str, default=None, help="Restore a session's baseline (undo it) and exit.")
    args = ap.parse_args()

    if args.rollback:
        return _rollback(args.rollback)

    ts = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    session_dir = SESSIONS_DIR / ts
    session_dir.mkdir(parents=True, exist_ok=True)
    max_seconds = max(60, args.minutes * 60)
    if args.core:
        workers = 1  # core-RSI is single-worker (controlled blast radius)
    elif args.workers == 0:
        # Round-5 self-tuning: let the dreamed policy set the swarm width (worker_count it converged on).
        try:
            from tools.rsi_improve import CandidatePolicy
            workers = max(1, CandidatePolicy.load().worker_count)
            print(f"[launcher] --workers 0 -> dreamed policy worker_count={workers}", flush=True)
        except Exception as e:
            workers = 1
            print(f"[launcher] --workers 0 but policy unreadable ({e}); defaulting to 1 worker", flush=True)
    else:
        workers = max(1, args.workers)

    # CORE-RSI: capture a full-system known-good checkpoint BEFORE the loop may touch its own brain.
    # The self-check must pass now (the system provably runs), so we always have a good restore point;
    # the supervisor's auto-restore backstop and the per-target self-check gate both restore to this.
    if args.core:
        try:
            from tools import system_checkpoint as _cp
            sc = subprocess.run([sys.executable, "-X", "utf8", "sabrina_gemini_loop.py", "--self-check"],
                                cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=240)
            ck = _cp.create("pre-core-known-good")
            if sc.returncode == 0:
                _cp.promote(ck.name)
                print(f"[launcher] core-RSI: known-good checkpoint {ck.name} (self-check PASSED)", flush=True)
            else:
                print(f"[launcher] WARNING: pre-core self-check FAILED -- checkpoint {ck.name} created but NOT "
                      f"marked known-good. Fix the core before a core session.", flush=True)
                return 3
        except Exception as e:
            print(f"[launcher] core-RSI checkpoint error (aborting core session): {e}", flush=True)
            return 3

    # Cost control (opt-in): start the pod before the session, stop it after -> zero idle billing
    # between sessions. Enabled when SABRINA_POD_AUTOSTOP is set and RUNPOD_POD_ID is known.
    pod_id = os.environ.get("RUNPOD_POD_ID", "").strip()
    autostop = bool(os.environ.get("SABRINA_POD_AUTOSTOP")) and bool(pod_id)
    ollama_url = (os.environ.get("SABRINA_RUNPOD_OLLAMA_URLS", "").split(",")[0] or "").strip()
    if autostop:
        try:
            from tools.pod_control import ensure_running
            ok, detail = ensure_running(pod_id, ollama_url)
            print(f"[launcher] pod ensure_running -> {ok} ({detail})", flush=True)
            if not ok:
                print("[launcher] WARNING: pod/ollama not confirmed ready -- the loop may fail against "
                      "an unreachable model. Proceeding anyway.", flush=True)
        except Exception as e:
            print(f"[launcher] pod ensure_running error (continuing): {e}", flush=True)

    print(f"[launcher] session {ts}: snapshotting in-scope source (local only, no git, nothing leaves the box)...", flush=True)
    baseline_files = _snapshot_baseline(session_dir)

    goal = CORE_GOAL if (args.core and args.goal == DEFAULT_GOAL) else args.goal
    loop_cmd = [sys.executable, "-u", "sabrina_gemini_loop.py",
                "--goal", goal, "--max-seconds", str(max_seconds),
                "--oracle-preference", args.oracle_preference, "--mode", "yolo"]

    # Spawn the swarm: N parallel loops, each pinned to a disjoint queue slice via SABRINA_WORKER_ID/COUNT.
    # Even sharing one 72b they fill each other's GPU idle gaps, raising A100 utilization. Each worker
    # streams to its own log; the launcher prints a heartbeat so the supervisor's stall detector (which
    # watches THIS process's log) never mistakes a long healthy session for a stall.
    procs, wlogs = [], []
    import time as _time
    log_dir = REPO_ROOT / "logs" / "supervisor"
    log_dir.mkdir(parents=True, exist_ok=True)
    for i in range(workers):
        env = {**os.environ, "SABRINA_MAX_SECONDS": str(max_seconds), "SABRINA_UNATTENDED": "1",
               "SABRINA_WORKER_ID": str(i), "SABRINA_WORKER_COUNT": str(workers),
               "SABRINA_IMPROVEMENT_MODE": "1",   # strip pentest context -> faster rounds
               "SABRINA_NUM_CTX": os.environ.get("SABRINA_NUM_CTX", "12288")}  # smaller KV cache
        if args.core:
            env["SABRINA_CORE_MODE"] = "1"   # unlock brain edits; per-target self-check + auto-revert guard them

        wlog = open(log_dir / f"codebase-self-improvement.w{i}.log", "a", encoding="utf-8", errors="replace")
        wlogs.append(wlog)
        p = subprocess.Popen(loop_cmd, cwd=str(REPO_ROOT), env=env, stdout=wlog, stderr=subprocess.STDOUT)
        procs.append(p)
        print(f"[launcher] spawned worker {i + 1}/{workers} pid={p.pid} (log: codebase-self-improvement.w{i}.log)", flush=True)

    start = _time.time()
    while any(p.poll() is None for p in procs):
        _time.sleep(30)
        alive = sum(1 for p in procs if p.poll() is None)
        print(f"[launcher] heartbeat: {alive}/{workers} workers alive, {int(_time.time() - start)}s elapsed", flush=True)
    for p in procs:
        try:
            p.wait(timeout=10)
        except Exception:
            pass
    for wl in wlogs:
        try:
            wl.close()
        except Exception:
            pass
    print(f"[launcher] all {workers} workers exited; building review...", flush=True)

    added, modified, deleted, review = _build_review(session_dir, baseline_files)

    # Reliability pass: re-verify every target the loop marked done, and flag test-tampering.
    verify_rows = _verify_marked_targets(start)
    tampered = [p for p in deleted if p.startswith("tests/") or Path(p).name.startswith("test_")]
    _append_verification(review, verify_rows, tampered)
    bad = [r for r in verify_rows if r["verdict"] in ("PHANTOM", "RED", "ERROR")]

    print("\n================ SESSION REVIEW ================", flush=True)
    print(f"Added: {len(added)}  Modified: {len(modified)}  Deleted: {len(deleted)}", flush=True)
    for p in added:
        print(f"  + {p}", flush=True)
    for p in modified:
        print(f"  ~ {p}", flush=True)
    for p in deleted:
        print(f"  - {p}", flush=True)
    if verify_rows:
        print(f"\nVerification of marked targets: {len(verify_rows)-len(bad)} good, {len(bad)} FLAGGED", flush=True)
        for r in bad:
            print(f"  !! {r['verdict']}: {r['target']} ({r['detail'][:60]})", flush=True)
    if tampered:
        print(f"  !! possible test tampering (deleted): {tampered}", flush=True)
    print(f"\nReview:   {review}", flush=True)
    print(f"Rollback: python tools/run_improvement_session.py --rollback {session_dir.as_posix()}", flush=True)
    print("===============================================", flush=True)

    # Dream-RSI phase ❸: dream a better target-selection policy over the accumulated real history
    # (the trees this + prior sessions logged). Never ships worse than the current policy.
    try:
        from tools.rsi_improve import dream
        dr = dream()
        if dr.get("status") == "ok":
            print(f"[dream] V0={dr['V0']} -> V_selected={dr['V_selected']} | selected={dr['selected']} "
                  f"| kept_current={dr['kept_current']}", flush=True)
        else:
            print(f"[dream] {dr.get('detail', 'no history yet')}", flush=True)
    except Exception as e:
        print(f"[dream] error: {e}", flush=True)

    # Re-measure capability (the RSI proof). Opt-in (it costs ~one benchmark run on the 72b): set
    # --measure or env SABRINA_RSI_MEASURE=1. dream() above is cheap and always runs.
    if args.measure or os.environ.get("SABRINA_RSI_MEASURE"):
        try:
            from tools.capability_bench import run_benchmark, _log, guidance_line
            print("[measure] scoring capability after this session ...", flush=True)
            score, results = run_benchmark(verbose=False)
            _log(score, results, note=f"post-session {ts}")
            print(f"[measure] {guidance_line()}", flush=True)
        except Exception as e:
            print(f"[measure] error: {e}", flush=True)

        # Second RSI proof track: vuln-classification on real disclosed bug-bounty reports. Separate
        # try so a failure here (or a missing task file) never masks the coding measurement above.
        try:
            from tools.bugbounty_rsi import run_benchmark as bb_bench, solve_with_pod, log_score, TASKS_PATH
            if TASKS_PATH.exists():
                print("[measure] scoring bug-bounty vuln-classification ...", flush=True)
                bb_score, bb_results = bb_bench(solve_with_pod, verbose=False)
                log_score(bb_score, bb_results, note=f"post-session {ts}")
                print(f"[measure] bug-bounty score = {bb_score:.3f} over {len(bb_results)} reports", flush=True)
            else:
                print("[measure] no config/bugbounty_tasks.json -- run `bugbounty_rsi.py build` first; skipping.", flush=True)
        except Exception as e:
            print(f"[measure] bug-bounty error: {e}", flush=True)

    # Cost control: stop the pod now that the session is done, so it doesn't bill while idle.
    if autostop:
        try:
            from tools.pod_control import stop as _pod_stop
            ok, detail = _pod_stop(pod_id)
            print(f"[launcher] pod stop -> {ok} ({detail}); GPU billing halted until the next session.", flush=True)
        except Exception as e:
            print(f"[launcher] pod stop error: {e}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
