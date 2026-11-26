"""Standing engagement runner -- the single command you start so 'prompt Sabrina -> she runs it' works.

    python tools/run_engagement.py serve            # work the whole queue, oldest first
    python tools/run_engagement.py run --slug acme  # run one engagement now
    python tools/run_engagement.py add  --slug acme --scope "*.acme.com,api.acme.com"   # enqueue a program

Each engagement is a config/engagements/<slug>.json scope file (tools/engagement_tracker). For each, this
launches the real Sabrina loop (sabrina_gemini_loop.py) with that engagement's --scope-file, so the
EngagementGate hard-limits every request to the program's in-scope hosts. This phase is RECON ONLY and
uses only SCOPE-GATED tools (browser_navigate + api_request, both gated by EngagementGate). Active exploit
engines are NOT invoked here -- active testing waits on the scope-gate hardening for run_shell engines.

Model/browser are decoupled: any oracle drives the CDP browser. Defaults to a free lab-local model so a
recon pass costs no pod time; override with --model / --ollama-url (e.g. the pod's 72B).
"""

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
from tools.engagement_tracker import (get_next_engagement, mark_engagement, list_engagements,
                                      add_engagement, engagement_status)  # noqa: E402

# POD-STRICT policy (Charles, 2026-09-22 "if the pods are down just do nothing without fallbacks"):
# Sabrina runs on the real model (the pod's 72B) or NOT AT ALL. No silent degradation to a weak
# local model, ever. The default endpoint is the pod; a preflight refuses to launch if the pod/model
# isn't actually resident, and SABRINA_SMART_TIER_STRICT=1 stops the loop's own oracle from falling
# back mid-run. See memory: sabrina-no-fallbacks.
try:
    from ai_systems.framework.lab_config import RUNPOD_OLLAMA_URLS
    _DEFAULT_OLLAMA_URL = RUNPOD_OLLAMA_URLS[0] if RUNPOD_OLLAMA_URLS else ""
except Exception:
    _DEFAULT_OLLAMA_URL = ""
_DEFAULT_MODEL = "qwen2.5:72b"


_DEFAULT_POD_ID = os.environ.get("SABRINA_RUNPOD_POD_ID", "3dp51xy0gpvyor")


def pod_ready_or_nothing(model, ollama_url, verbose=True, pod_id=None):
    """Bring the pod UP for a task, then confirm `model` is resident. Returns True if ready.

    Policy (Charles, 2026-09-22): pods should be DOWN when idle but come UP automatically when there
    is work -- he must never have to hand-restart them. So when a task calls this we actively
    ensure_running (start the pod + ollama if needed) and pull the model if missing. We return False
    (do NOTHING, no weak-local fallback) ONLY if the pod genuinely can't be brought up -- e.g. no
    RUNPOD_API_KEY, or it won't boot. See memory: sabrina-no-fallbacks.
    """
    from tools.pod_control import model_present, ensure_running, ensure_model
    if ollama_url and model_present(ollama_url, model):
        return True  # already up + model resident
    if not ollama_url:
        if verbose:
            print("[pod-strict] no pod ollama URL configured -- doing nothing (no local fallback).", flush=True)
        return False
    pod_id = pod_id or _DEFAULT_POD_ID
    if verbose:
        print(f"[pod-strict] pod not ready -- auto-starting pod {pod_id} for this task...", flush=True)
    ok, detail = ensure_running(pod_id, ollama_url)
    if not ok:
        if verbose:
            print(f"[pod-strict] could NOT bring the pod up ({detail}). Doing nothing -- no local "
                  f"fallback. (Set RUNPOD_API_KEY so tasks can auto-start the pod.)", flush=True)
        return False
    m_ok, m_detail = ensure_model(ollama_url, model)
    if not m_ok:
        if verbose:
            print(f"[pod-strict] pod is up but model '{model}' unavailable ({m_detail}). Doing nothing.", flush=True)
        return False
    if verbose:
        print(f"[pod-strict] pod ready ({detail}); model '{model}' present. Proceeding on the 72B.", flush=True)
    return True

DISCOVERY_SCOPE = {
    "platform": "discovery",
    "program": "discovery-read-only",
    "in_scope": ["bugcrowd.com", "tracker.bugcrowd.com", "identity.bugcrowd.com",
                 "duckduckgo.com", "html.duckduckgo.com", "lite.duckduckgo.com"],
    "out_of_scope": [],
    "automated_testing_allowed": False,
    "rate_limit_rps": 0.5,
    "notes": "READ ONLY: browse my Bugcrowd account + search engines to discover engagements. No target testing.",
}

DISCOVER_GOAL = (
    "RECON / READ-ONLY discovery of MY bug-bounty engagements. You may ONLY browse bugcrowd.com, "
    "tracker.bugcrowd.com and duckduckgo.com (the scope gate blocks everything else). Do NOT test any "
    "target and do NOT join/enroll in any program.\n"
    "Use these tools: open_authenticated_site, list_bugcrowd_engagements, browser_navigate, browser_inspect, "
    "write_file, save_knowledge, completed.\n\n"
    "PART 1 - my existing engagements:\n"
    "STEP 1: your FIRST action MUST be exactly: {\"tool\": \"open_authenticated_site\", \"site\": \"bugcrowd\"}  "
    "-- the argument key is `site` and its value is the literal string \"bugcrowd\". Do NOT leave site empty; "
    "do NOT pass a url. This opens my logged-in Bugcrowd engagements page.\n"
    "STEP 2: look at the result. If it looks like a login / sign-in page (URL contains /login or /sign-in), "
    "STOP and call completed with note 'BUGCROWD AUTH EXPIRED'.\n"
    "STEP 3: otherwise call {\"tool\": \"list_bugcrowd_engagements\"} to extract the programs listed on the "
    "current page, then browser_inspect if you need more detail.\n"
    "STEP 4: for EACH program I am on, browser_navigate to its brief (bugcrowd.com only), browser_inspect it, "
    "and collect: program name, program URL, in-scope assets, out-of-scope assets, whether automated testing "
    "is allowed, rate limit. Keep them in a running list -- do NOT write files yet.\n\n"
    "PART 2 - find more: browser_navigate to https://bugcrowd.com/programs (and/or duckduckgo.com) to find "
    "PUBLIC programs open to join. For up to 15, collect name, platform, URL, one-line scope summary, payout "
    "range. Do NOT enroll.\n\n"
    "FINAL STEP: write_file EVERYTHING you collected to data/discovered_engagements.md as ONE markdown table "
    "with columns: Program | Mine-or-Public | URL | In-scope | Out-of-scope | Automated-allowed | Rate-limit | "
    "Notes. IMPORTANT: do NOT write to config/engagements/ -- that directory is write-protected (the operator "
    "promotes chosen programs from your list). Only write the single data/discovered_engagements.md file.\n\n"
    "When finished, call completed stating how many of my programs and how many public programs you listed."
)


def _ensure_discovery_scope():
    import json as _json
    d = REPO_ROOT / "config" / "engagements" / "_discovery.json"
    d.parent.mkdir(parents=True, exist_ok=True)
    d.write_text(_json.dumps(DISCOVERY_SCOPE, indent=2), encoding="utf-8")
    return d


def run_discover(minutes, model, ollama_url, cdp_url, verbose=True):
    if not pod_ready_or_nothing(model, ollama_url, verbose=verbose):
        return
    scope_file = _ensure_discovery_scope()
    max_seconds = max(300, minutes * 60)
    log_dir = REPO_ROOT / "logs" / "engagements"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "discover.log"
    env = {**os.environ, "SABRINA_RUNPOD_OLLAMA_URLS": ollama_url,
           "SABRINA_FORCE_LOCAL_MODEL": model, "SABRINA_SMART_TIER_STRICT": "1",
           "SABRINA_UNATTENDED": "1"}
    cmd = [sys.executable, "-u", "sabrina_gemini_loop.py",
           "--goal", DISCOVER_GOAL, "--scope-file", str(scope_file.relative_to(REPO_ROOT)).replace(os.sep, "/"),
           "--cdp-url", cdp_url, "--oracle-preference", "local", "--max-seconds", str(max_seconds), "--mode", "yolo"]
    if verbose:
        print(f"[discover] reading my Bugcrowd engagements + searching for more; model={model}, "
              f"{minutes}m -> {log_path}", flush=True)
    with open(log_path, "a", encoding="utf-8", errors="replace") as lf:
        subprocess.run(cmd, cwd=str(REPO_ROOT), env=env, stdout=lf, stderr=subprocess.STDOUT)
    tail = ""
    try:
        tail = "\n".join(log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-20:])
    except Exception:
        pass
    if "AUTH EXPIRED" in tail.upper():
        print("[discover] BUGCROWD AUTH EXPIRED -- Sabrina's stored Bugcrowd login lapsed; re-auth needed.", flush=True)
    print(f"[discover] done. New engagements -> config/engagements/ ; program list -> data/discovered_programs.md", flush=True)


RECON_GOAL = (
    "AUTHORIZED BUG-BOUNTY RECON for program '{slug}'. The EngagementGate (--scope-file) HARD-LIMITS you "
    "to this program's in-scope hosts: {scope}. RECON ONLY this phase -- do NOT run active exploit engines "
    "or send attack payloads; only map the attack surface.\n"
    "Use ONLY scope-gated tools: browser_navigate / browser_inspect to load in-scope pages, and "
    "api_request with method GET for endpoints (both are blocked by the gate off-scope). For each in-scope "
    "host: discover pages, forms, parameters, API endpoints, JS bundles, and any /swagger.json /openapi.json "
    "/api-docs. Call record_endpoint for each endpoint you find (feeds the attack-surface map). Respect the "
    "engagement's rate limit. Never navigate a host that is not in scope.\n"
    "When the surface is mapped (or you are blocked), call completed with a summary: hosts covered, count of "
    "endpoints/params recorded, notable tech/versions, and the top 5 leads worth testing next (with which "
    "vuln class each suggests). Save key leads with save_knowledge."
)


def run_one(eng, minutes, model, ollama_url, cdp_url, verbose=True):
    slug, scope_file = eng["slug"], eng["file"]
    if not pod_ready_or_nothing(model, ollama_url, verbose=verbose):
        mark_engagement(slug, "recon", "skipped_pod_down", "pod/model unavailable -- no fallback")
        return "pod_down"
    scope = ", ".join(eng["in_scope"][:12]) + (" ..." if eng["in_scope_count"] > 12 else "")
    goal = RECON_GOAL.format(slug=slug, scope=scope)
    max_seconds = max(120, minutes * 60)
    log_dir = REPO_ROOT / "logs" / "engagements"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{slug}.recon.log"
    env = {**os.environ,
           "SABRINA_RUNPOD_OLLAMA_URLS": ollama_url,   # reuse this var to point the oracle at any ollama
           "SABRINA_FORCE_LOCAL_MODEL": model,
           "SABRINA_SMART_TIER_STRICT": "1",   # no in-loop degradation to a weak local model
           "SABRINA_UNATTENDED": "1"}
    cmd = [sys.executable, "-u", "sabrina_gemini_loop.py",
           "--goal", goal, "--scope-file", scope_file, "--cdp-url", cdp_url,
           "--oracle-preference", "local", "--max-seconds", str(max_seconds), "--mode", "yolo"]
    if verbose:
        print(f"[engagement] {slug}: recon, model={model}, scope={eng['in_scope_count']} hosts, "
              f"{minutes}m -> {log_path}", flush=True)
    mark_engagement(slug, "recon", "running", f"model={model}")
    with open(log_path, "a", encoding="utf-8", errors="replace") as lf:
        proc = subprocess.run(cmd, cwd=str(REPO_ROOT), env=env, stdout=lf, stderr=subprocess.STDOUT)
    status = "done" if proc.returncode == 0 else f"exit_{proc.returncode}"
    # surface auth-expired / obvious failure from the log tail
    tail = ""
    try:
        tail = "\n".join(log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-15:])
        if "AUTH EXPIRED" in tail.upper():
            status = "auth_expired"
    except Exception:
        pass
    mark_engagement(slug, "recon", status, tail[-400:])
    if verbose:
        print(f"[engagement] {slug}: recon {status}", flush=True)
    return status


ACTIVE_GOAL = (
    "AUTHORIZED ACTIVE bug-bounty testing for program '{slug}'. automated_testing_allowed is TRUE for "
    "this program. The EngagementGate (--scope-file) AND the trusted scope channel (SABRINA_ACTIVE_SCOPE_FILE, "
    "which the loop exports) HARD-LIMIT every request to this program's in-scope hosts: {scope}. Every exploit "
    "engine is fail-closed scope-gated -- an out-of-scope URL is refused and no request is sent.\n"
    "TARGETS: use the attack surface mapped during recon (record_endpoint output / saved leads) as your "
    "targets -- do not re-crawl from scratch.\n"
    "ENGINES: run the scope-gated exploit engines via run_shell against IN-SCOPE hosts only -- e.g. "
    "tools/engines/{{sqli,xss,ssrf,idor,lfi,xxe,open_redirect,ssti,file_upload,clickjacking,csrf}}_engine.py, "
    "each invoked with the engagement's --scope-file. Pick engines by the lead's suggested vuln class.\n"
    "SAFETY (non-negotiable, this program's policy): DETECTION-FIRST and LOW-VOLUME. Respect the engagement's "
    "rate limit. Do NOT run high-volume / brute-force / DoS. For HTTP request smuggling use TIMING-ONLY "
    "detection -- never response-splitting against a live target. Treat any engine signal as a LEAD requiring "
    "careful manual verification, NOT a confirmed finding. STOP IMMEDIATELY and call completed with a "
    "'STOP: accessed data that isn't mine' note if any probe returns another user's/tenant's data.\n"
    "For each real lead: save_knowledge with the evidence. When the leads are worked (or you hit a STOP "
    "condition or the rate budget), call completed with: hosts tested, engines run, confirmed-vs-leads counts, "
    "and any STOP conditions hit."
)


def run_active(eng, minutes, model, ollama_url, cdp_url, verbose=True):
    """ACTIVE testing phase -- only for an engagement whose scope has automated_testing_allowed=True.
    Refuses (recon-only) otherwise: active exploit traffic requires that explicit, per-program flag, which
    the operator sets only after confirming the program's policy permits automated active testing. Same
    POD-STRICT posture as run_one: runs on the pod's 72B or does nothing (no weak-local fallback)."""
    slug, scope_file = eng["slug"], eng["file"]
    if not eng.get("automated_testing_allowed"):
        msg = (f"'{slug}' is recon-only (automated_testing_allowed=False). Refusing active testing. Set "
               f"automated_testing_allowed=true in its scope file only after confirming the program permits "
               f"automated active testing.")
        if verbose:
            print(f"[engagement] {slug}: ACTIVE BLOCKED -- {msg}", flush=True)
        mark_engagement(slug, "active", "blocked_recon_only", msg)
        return "blocked_recon_only"
    if not pod_ready_or_nothing(model, ollama_url, verbose=verbose):
        mark_engagement(slug, "active", "skipped_pod_down", "pod/model unavailable -- no fallback")
        return "pod_down"
    scope = ", ".join(eng["in_scope"][:12]) + (" ..." if eng["in_scope_count"] > 12 else "")
    goal = ACTIVE_GOAL.format(slug=slug, scope=scope)
    max_seconds = max(120, minutes * 60)
    log_dir = REPO_ROOT / "logs" / "engagements"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{slug}.active.log"
    env = {**os.environ,
           "SABRINA_RUNPOD_OLLAMA_URLS": ollama_url,
           "SABRINA_FORCE_LOCAL_MODEL": model,
           "SABRINA_SMART_TIER_STRICT": "1",   # no in-loop degradation to a weak local model
           "SABRINA_UNATTENDED": "1"}
    # --scope-file makes the loop export SABRINA_ACTIVE_SCOPE_FILE (the trusted channel), so every gated
    # engine subprocess launched via run_shell inherits it and is hard-limited to this program's hosts.
    cmd = [sys.executable, "-u", "sabrina_gemini_loop.py",
           "--goal", goal, "--scope-file", scope_file, "--cdp-url", cdp_url,
           "--oracle-preference", "local", "--max-seconds", str(max_seconds), "--mode", "yolo"]
    if verbose:
        print(f"[engagement] {slug}: ACTIVE testing (detection-first), model={model}, "
              f"scope={eng['in_scope_count']} hosts, {minutes}m -> {log_path}", flush=True)
    mark_engagement(slug, "active", "running", f"model={model}")
    with open(log_path, "a", encoding="utf-8", errors="replace") as lf:
        proc = subprocess.run(cmd, cwd=str(REPO_ROOT), env=env, stdout=lf, stderr=subprocess.STDOUT)
    status = "done" if proc.returncode == 0 else f"exit_{proc.returncode}"
    tail = ""
    try:
        tail = "\n".join(log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-15:])
        if "STOP:" in tail:
            status = "stopped_policy"
    except Exception:
        pass
    mark_engagement(slug, "active", status, tail[-400:])
    if verbose:
        print(f"[engagement] {slug}: active {status}", flush=True)
    return status


IMPROVE_GOAL = (
    "SELF-IMPROVEMENT SESSION (no external target -- this is pure engineering on your OWN codebase). "
    "Work your improvement backlog using your real engineering tools:\n"
    "1. Call get_improvement_queue to see what needs work. Pick the single highest-value target.\n"
    "2. Understand it: discover_code / read_file / describe_module on the file(s) involved -- build "
    "against the REAL API, never a guessed one.\n"
    "3. Improve it: for a normal module edit it in place with edit_file (or write_file for a new file). "
    "PREFER writing/verifying the code YOURSELF -- do NOT rely on delegate_to_engineer (CrewAI may be "
    "unavailable here); only use it for a genuine <vuln_type>_engine.py HTTP scanner.\n"
    "4. VERIFY with verify_module (path=..., expect=[...]) until it returns [VERIFIED]. Exit code 0 is "
    "NOT success -- read the semantic verdict.\n"
    "5. Record it: mark_improved(target, action, outcome) and save_knowledge for anything reusable.\n"
    "A HIGH-VALUE target if the queue is thin: harden the engagement scope gate so ALL engines enforce "
    "EngagementGate (several bypass it today) -- but only touch tools/ files you are allowed to write; "
    "never your orchestration core. When you've completed one solid improvement (verified), call completed "
    "with a summary of what you changed and how you verified it."
)


def _run_improvement_pass(minutes, model, ollama_url, cdp_url, verbose=True):
    """One self-improvement pass through SABRINA'S OWN LOOP (not idle_improver -- its MultiNodeLLMRouter
    ignores the pod env and falls back to Gemini). The loop's UnifiedOracleHub honors SABRINA_FORCE_
    LOCAL_MODEL + SABRINA_RUNPOD_OLLAMA_URLS + SABRINA_SMART_TIER_STRICT, so this runs on the pod's 72B
    or escalates cleanly -- never a weak-local fallback. Pure code/RSI work, NO third-party target."""
    max_seconds = max(300, minutes * 60)
    env = {**os.environ,
           "SABRINA_RUNPOD_OLLAMA_URLS": ollama_url,
           "SABRINA_FORCE_LOCAL_MODEL": model,
           "SABRINA_SMART_TIER_STRICT": "1",
           "SABRINA_IMPROVEMENT_MODE": "1",
           "SABRINA_UNATTENDED": "1"}
    log_path = REPO_ROOT / "logs" / "engagements" / "self_improve.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, "-u", "sabrina_gemini_loop.py",
           "--goal", IMPROVE_GOAL, "--cdp-url", cdp_url,
           "--oracle-preference", "local", "--max-seconds", str(max_seconds), "--mode", "yolo"]
    if verbose:
        print(f"[busy] no authorized engagement queued -- self-improvement pass on {model} "
              f"({minutes}m) -> {log_path}", flush=True)
    with open(log_path, "a", encoding="utf-8", errors="replace") as lf:
        subprocess.run(cmd, cwd=str(REPO_ROOT), env=env, stdout=lf, stderr=subprocess.STDOUT)


def busy(minutes, model, ollama_url, cdp_url, mode="improve", cycles=0):
    """Autonomous 'keep herself busy' scheduler. Each cycle: bring the pod up (auto-start), do the
    highest-value available work, repeat. Priority: an authorized engagement recon (mode=all) else a
    self-improvement pass. Pod-strict + no fallback throughout; runs forever unless cycles>0.

    mode='improve' (default): self-improvement only -- no third-party target, safe to run unattended.
    mode='all': also work the engagement recon queue when a runnable engagement exists.

    LIVE CONTROL: the --mode arg only sets the INITIAL state; thereafter each cycle reads
    data/busy_control.json (see tools/busy_control.py / tools/busy_panel.py) so you can flip
    improve<->all or pause the loop from a web button / CLI WITHOUT relaunching.
    """
    from tools.busy_control import read_control, write_control
    write_control(mode=mode)  # seed the control file from the launch flag
    n = 0
    while cycles <= 0 or n < cycles:
        n += 1
        ctl = read_control()
        if ctl["paused"]:
            print("[busy] paused (data/busy_control.json) -- idling this cycle.", flush=True)
            time.sleep(15)
            continue
        if not pod_ready_or_nothing(model, ollama_url):
            print("[busy] pod unavailable and could not be started -- stopping (no fallback).", flush=True)
            return
        did_recon = False
        if ctl["mode"] == "all":
            eng = get_next_engagement("recon")
            if eng:
                st = (engagement_status(eng["slug"]).get("recon", {}) or {}).get("status")
                if st not in ("done", "auth_expired"):
                    run_one(eng, minutes, model, ollama_url, cdp_url)
                    did_recon = True
        if not did_recon:
            _run_improvement_pass(minutes, model, ollama_url, cdp_url)
        time.sleep(5)
    print(f"[busy] completed {n} cycle(s).", flush=True)


def serve(minutes, model, ollama_url, cdp_url, max_engagements=50):
    if not pod_ready_or_nothing(model, ollama_url):
        return  # pod down -> do nothing, don't churn the queue
    ran = 0
    while ran < max_engagements:
        eng = get_next_engagement("recon")
        if not eng:
            print("[serve] queue empty -- nothing to run. Add an engagement to continue.", flush=True)
            break
        # skip ones already done recently this serve pass (avoid tight re-loop on a 1-item queue)
        st = (engagement_status(eng["slug"]).get("recon", {}) or {}).get("status")
        if st in ("done", "auth_expired") and ran > 0:
            print(f"[serve] next up '{eng['slug']}' already {st}; stopping (re-add or --slug to force).", flush=True)
            break
        run_one(eng, minutes, model, ollama_url, cdp_url)
        ran += 1
        time.sleep(2)
    print(f"[serve] worked {ran} engagement(s).", flush=True)


def main():
    ap = argparse.ArgumentParser(description="Standing engagement runner.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    common = dict()
    for name in ("serve", "run", "discover", "busy", "active"):
        p = sub.add_parser(name)
        p.add_argument("--minutes", type=int, default=20)
        p.add_argument("--model", default=_DEFAULT_MODEL, help="Oracle model. Defaults to the pod's 72B -- POD-STRICT: if it's not resident, the runner does nothing (no local fallback).")
        p.add_argument("--ollama-url", default=_DEFAULT_OLLAMA_URL, help="Oracle ollama endpoint. Defaults to the RunPod pod proxy (the big brain). A down pod = no run.")
        p.add_argument("--cdp-url", default="http://localhost:9222", help="Sabrina's CDP browser endpoint.")
        if name in ("run", "active"):
            p.add_argument("--slug", required=True)
        if name == "busy":
            p.add_argument("--mode", choices=["improve", "all"], default="improve",
                           help="improve = self-improvement only (no target, safe unattended); all = also work the recon queue.")
            p.add_argument("--cycles", type=int, default=0, help="0 = run forever; N = stop after N cycles.")
    a_ = sub.add_parser("add")
    a_.add_argument("--slug", required=True)
    a_.add_argument("--scope", required=True)
    a_.add_argument("--platform", default="manual")
    a_.add_argument("--url", default="")
    a_.add_argument("--allow-automated", action="store_true")
    a_.add_argument("--rps", type=float, default=1.0)
    a_.add_argument("--notes", default="")
    sub.add_parser("list")
    a = ap.parse_args()

    if a.cmd == "list":
        for e in list_engagements(include_meta=True):
            print(f"  {'RUN' if e['runnable'] else '---'}  {e['slug']:28s} {e['in_scope_count']:>3} in-scope  "
                  f"{'AUTO' if e['automated_testing_allowed'] else 'recon'}")
        return 0
    if a.cmd == "add":
        import pprint
        pprint.pprint(add_engagement(a.slug, a.scope, platform=a.platform, program_url=a.url,
                                     automated_testing_allowed=a.allow_automated, rate_limit_rps=a.rps, notes=a.notes))
        return 0
    if a.cmd == "run":
        eng = next((e for e in list_engagements() if e["slug"] == a.slug and e["runnable"]), None)
        if not eng:
            print(f"No runnable engagement '{a.slug}'. Use: run_engagement.py list", flush=True)
            return 2
        run_one(eng, a.minutes, a.model, a.ollama_url, a.cdp_url)
        return 0
    if a.cmd == "active":
        eng = next((e for e in list_engagements() if e["slug"] == a.slug and e["runnable"]), None)
        if not eng:
            print(f"No runnable engagement '{a.slug}'. Use: run_engagement.py list", flush=True)
            return 2
        status = run_active(eng, a.minutes, a.model, a.ollama_url, a.cdp_url)
        return 0 if status not in ("blocked_recon_only",) else 3
    if a.cmd == "discover":
        run_discover(a.minutes, a.model, a.ollama_url, a.cdp_url)
        return 0
    if a.cmd == "serve":
        serve(a.minutes, a.model, a.ollama_url, a.cdp_url)
        return 0
    if a.cmd == "busy":
        busy(a.minutes, a.model, a.ollama_url, a.cdp_url, mode=a.mode, cycles=a.cycles)
        return 0


if __name__ == "__main__":
    sys.exit(main())
