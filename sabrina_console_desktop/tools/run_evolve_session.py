"""Launch a CODE-EVOLUTION RSI session: Sabrina (the code agent) evolves a real target, gated by a fast
benchmark via tools/rsi_evolve (accept-or-revert, auto-revert on no-gain/test-failure). Pod-strict --
runs on the 72B or does nothing (no weak-local fallback), same policy as run_engagement.

    python tools/run_evolve_session.py --target vuln_prompt --rounds 3
    python tools/run_evolve_session.py --list

Each target = {path, score, test, goal}. `score` and `test` run in a FRESH subprocess so they always
read the CURRENT on-disk code (a same-process import would score Sabrina's edit against a stale cached
module). `goal` is what Sabrina is told to improve. She writes the candidate; the gate keeps it only if
`score` rises and `test` stays green.
"""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
from tools.rsi_evolve import evolve_loop  # noqa: E402

_DEFAULT_MODEL = os.environ.get("SABRINA_FORCE_LOCAL_MODEL", "qwen2.5:72b")
try:
    from ai_systems.framework.lab_config import RUNPOD_OLLAMA_URLS
    _DEFAULT_OLLAMA = RUNPOD_OLLAMA_URLS[0] if RUNPOD_OLLAMA_URLS else ""
except Exception:
    _DEFAULT_OLLAMA = ""


def _subprocess_score(expr, timeout=240):
    """A score_fn that runs `expr` in a FRESH python process and reads the last stdout line as a float.
    Fresh process = it imports the CURRENT on-disk target, so it measures Sabrina's edit, not a cache."""
    def score():
        try:
            out = subprocess.run([sys.executable, "-c", expr], cwd=str(REPO_ROOT),
                                 capture_output=True, text=True, timeout=timeout,
                                 env={**os.environ, "SABRINA_FORCE_LOCAL_MODEL": _DEFAULT_MODEL})
            return float(out.stdout.strip().splitlines()[-1])
        except Exception as e:
            print(f"[evolve-score] failed ({e}); scoring 0.0", flush=True)
            return 0.0
    return score


def _pytest_ok(path, timeout=300):
    def test():
        r = subprocess.run([sys.executable, "-m", "pytest", path, "-q"], cwd=str(REPO_ROOT),
                           capture_output=True, text=True, timeout=timeout)
        return r.returncode == 0
    return test


def _subprocess_text(expr, timeout=300):
    """Like _subprocess_score but returns full stdout (for multi-line FEEDBACK the code agent reads)."""
    def run():
        try:
            out = subprocess.run([sys.executable, "-c", expr], cwd=str(REPO_ROOT),
                                 capture_output=True, text=True, timeout=timeout,
                                 env={**os.environ, "SABRINA_FORCE_LOCAL_MODEL": _DEFAULT_MODEL})
            return out.stdout.strip()
        except Exception as e:
            return f"(feedback unavailable: {e})"
    return run


def _read(target):
    try:
        return (REPO_ROOT / target).read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""


def _diff(before, after, target):
    """Compact unified diff of the target file across Sabrina's editing pass -- logged to the discovery
    tree so a node records the ACTUAL change tried, not just 'an editing pass'."""
    if before == after:
        return f"no change to {target}"
    import difflib
    d = "".join(difflib.unified_diff(before.splitlines(True), after.splitlines(True),
                                     fromfile=target + " (before)", tofile=target + " (after)"))
    return d[:1500] or f"changed {target}"


# --- Target registry. Add a target = pick something with real headroom + a FAST score. ---------------
TARGETS = {
    # Real headroom (72B baseline ~63.8%): evolve the classifier PROMPT/scaffold to raise accuracy on a
    # fast held-out sample. Sabrina edits build_prompt() in vuln_classification_bench.py; scored on 40
    # tasks against the pod.
    "vuln_prompt": {
        "path": "tools/vuln_classification_bench.py",
        "score": _subprocess_score(
            "import tools.vuln_classification_bench as v; "
            "print(v.score(tasks=v.load_tasks(limit=40), verbose=False)['accuracy'])"),
        "test": _pytest_ok("tests/unit/test_vuln_classification_bench.py"),
        "feedback": _subprocess_text(
            "import tools.vuln_classification_bench as v; print(v.feedback_summary(sample=40))"),
        "goal": ("RSI CODE EVOLUTION (edit ONLY tools/vuln_classification_bench.py). Raise the 72B's "
                 "vuln-classification accuracy by improving build_prompt() -- clearer instructions, better "
                 "handling of the confusable classes (XSS variants; Improper Access Control vs IDOR vs "
                 "Privilege Escalation). Do NOT change the task set, the grading, or parse_prediction to "
                 "make it easier -- only the prompt. Keep tests/unit/test_vuln_classification_bench.py green. "
                 "When done, call completed."),
    },
    # Fast, no-pod score (~30s) -- good for proving the loop end-to-end. NOTE: the engine already scores
    # 1.0 on smuggle_sim, so expect reverts (no headroom) -- it demonstrates the gate correctly REJECTS
    # non-improving edits rather than a climb.
    "smuggling": {
        "path": "tools/engines/http_smuggling_engine.py",
        "score": _subprocess_score(
            "import tools.smuggle_sim as s; print(s.score_detector(vuln_delay=6.0, verbose=False)['accuracy'])"),
        "test": _pytest_ok("tests/unit/test_smuggle_sim.py"),
        "goal": ("RSI CODE EVOLUTION (edit ONLY tools/engines/http_smuggling_engine.py). Raise "
                 "tools/smuggle_sim.score_detector accuracy toward 1.0 by refining the desync probe "
                 "variants. Keep safe/safe_slow clean (no false positives). Do NOT edit the sim or tests."),
    },
}


def sabrina_candidate(goal, minutes, model, ollama_url, cdp_url="http://localhost:9222", feedback_fn=None):
    """Return a candidate_fn(target) that launches Sabrina's loop to write the code change. POD-STRICT:
    runs on the 72B or does nothing. Blocks until she finishes one bounded editing pass. If `feedback_fn`
    is given, its output (the model's CURRENT failures) is injected into her goal so her edit is
    informed, not blind. Returns the REAL unified diff of the target so the tree records the change."""
    def candidate(target):
        from tools.pod_control import model_present, ensure_running
        if ollama_url and not model_present(ollama_url, model):
            ok, detail = ensure_running(os.environ.get("SABRINA_RUNPOD_POD_ID", "3dp51xy0gpvyor"), ollama_url)
            if not ok:
                raise RuntimeError(f"pod not ready ({detail}) -- no fallback")
        g = goal
        if feedback_fn is not None:
            try:
                fb = feedback_fn()
                if fb:
                    g = goal + "\n\nCURRENT FAILURES (target these specific confusions):\n" + fb
                    print("[evolve] injected current-failure feedback into the goal", flush=True)
            except Exception:
                pass
        before = _read(target)
        log_dir = REPO_ROOT / "logs" / "evolve"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / (target.replace("/", "_") + ".log")
        env = {**os.environ, "SABRINA_RUNPOD_OLLAMA_URLS": ollama_url,
               "SABRINA_FORCE_LOCAL_MODEL": model, "SABRINA_SMART_TIER_STRICT": "1",
               "SABRINA_IMPROVEMENT_MODE": "1", "SABRINA_UNATTENDED": "1"}
        cmd = [sys.executable, "-u", "sabrina_gemini_loop.py", "--goal", g, "--no-crewai",
               "--oracle-preference", "local", "--max-seconds", str(max(120, minutes * 60)), "--mode", "yolo"]
        with open(log_path, "a", encoding="utf-8", errors="replace") as lf:
            subprocess.run(cmd, cwd=str(REPO_ROOT), env=env, stdout=lf, stderr=subprocess.STDOUT)
        return _diff(before, _read(target), target)
    return candidate


def main():
    ap = argparse.ArgumentParser(description="Code-evolution RSI session (Sabrina evolves a target, gated).")
    ap.add_argument("--target", choices=list(TARGETS), help="which target to evolve")
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--minutes", type=int, default=15, help="per-round budget for Sabrina's editing pass")
    ap.add_argument("--model", default=_DEFAULT_MODEL)
    ap.add_argument("--ollama-url", default=_DEFAULT_OLLAMA)
    ap.add_argument("--list", action="store_true", help="list targets and exit")
    a = ap.parse_args()
    if a.list or not a.target:
        for name, t in TARGETS.items():
            print(f"  {name:12s} -> {t['path']}")
        return 0
    t = TARGETS[a.target]
    cand = sabrina_candidate(t["goal"], a.minutes, a.model, a.ollama_url, feedback_fn=t.get("feedback"))
    summary = evolve_loop(t["path"], t["score"], cand, rounds=a.rounds, test_fn=t["test"])
    print("\n[evolve session] " + json.dumps({k: summary[k] for k in
          ("target", "rounds_run", "accepted", "final_score", "tree")}, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
