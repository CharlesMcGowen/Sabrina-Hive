"""
tools/sabrina_supervisor.py
============================
Process supervisor for Sabrina so a human doesn't have to manually
Stop-Process/relaunch every time a run crashes or silently stalls.

What it does automatically:
  - Keeps ai_systems/training/probe_api.py (the dashboard) running.
  - Launches one sabrina_gemini_loop.py process per enabled task in TASKS
    below, with -u (unbuffered) so log-file mtime is a real staleness signal
    instead of hiding behind Python's stdout buffering.
  - Restarts a task's process, with the SAME goal/flags, if it exits, or if
    its log file hasn't grown in that task's stall_minutes.
  - Caps restarts per task (default 8) with a backoff, so a task that's
    fundamentally broken (bad goal, missing scope file) doesn't crash-loop
    forever burning API calls -- it gives up and reports why instead.
  - Reports live status (pid, alive, restart_count, last action/reason) to
    probe_api.py's /api/supervisor/status once per check cycle, so it's
    visible through the existing dashboard/API instead of a separate thing
    to check on. Sabrina herself can read this (read-only) via the
    get_supervisor_status tool in sabrina_gemini_loop.py.

What it deliberately does NOT do:
  - Never edits or creates config/engagements/*.json scope files, and never
    flips automated_testing_allowed. That stays a human decision, same as
    the rest of this project's design (see tools/engagement_scope.py).
  - Never adds/removes/enables tasks on its own -- the TASKS list below is
    hand-edited (by a human, or a human directing an assistant), not
    self-modified by the supervisor or by Sabrina herself. There is
    deliberately no external config file for this -- everything a human
    needs to change lives in this one file, not split across a script plus
    a JSON config that can drift out of sync with each other.

Usage:
  python tools/sabrina_supervisor.py                  # run until Ctrl+C
  python tools/sabrina_supervisor.py --check-interval 20
"""
import argparse
import json
import os
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = REPO_ROOT / "logs" / "supervisor"
PROBE_API_DIR = REPO_ROOT / "ai_systems" / "training"
STATUS_API_URL = "http://127.0.0.1:8086/api/supervisor/status"
CHAT_API_URL = "http://127.0.0.1:8086/api/chat"

MAX_RESTARTS_PER_TASK = 8

# ask_human blocks a process indefinitely waiting for a real answer (verified live:
# sabrina_gemini_loop.py's ask_human handler polls forever, no timeout of its own).
# That looks EXACTLY like a stall to log-mtime-based detection -- and killing/
# restarting a process that's correctly paused is actively harmful, not just
# wasteful: verified live this session, hostgator-latam-hunt hit the built-in
# "15 rounds, should I continue?" safety-net check, sat waiting for a human nobody
# answered, and got killed+restarted 8 times by log-staleness alone until the
# supervisor gave up and disabled it -- destroying all progress (a real CVE lead,
# a diagnosed request-format bug) each time, for a question that was never a real
# stall at all.
WAITING_FOR_HUMAN_MARKERS = ("is asking for your input", "WAITING_FOR_HUMAN:")
# The generic "N rounds without completing, should she continue?" safety-net gate
# (sabrina_gemini_loop.py's own periodic check-in, not a substantive question) is
# specifically safe to auto-continue: it's a runaway-loop governor, not something
# requiring domain knowledge or a judgment call only the human can make -- and
# the entire point of an unattended supervisor is not needing to click "continue"
# every 15 rounds. A real ask_human question (e.g. "which email should I use?")
# does NOT match this and is left waiting for an actual human, as designed.
SAFETY_NET_CONTINUE_MARKER = "Should she continue?"

# Everything the supervisor manages lives right here -- one file, not a script
# plus a separate JSON config to keep in sync. Edit this list directly to
# add/remove/pause a task (flip "enabled": False rather than deleting it).
    # Real-testing-authorized programs for the rotating hunter below. Each entry is a scope
    # file under config/engagements/ that Charles has personally reviewed and set
    # "automated_testing_allowed": true on for real api_request testing (see
    # hostgator-latam-bb.json's _automated_testing_allowed_NOTE for what that review actually
    # covers -- exact in-scope hosts, rate limit, disallowed techniques). Per-user request
    # 2026-09-18: only add a program here after that same real review -- this hub trusts
    # whatever the scope file says, so a fabricated "automated_testing_allowed": true would send
    # real api_request calls at a company's live infrastructure with no real authorization behind
    # it. Only one program is reviewed so far; the rotation is a no-op list-of-one until more are
    # added.
BOUNTY_ROTATION_SCOPE_FILES = [
    "config/engagements/hostgator-latam-bb.json",
]
# Persists which program is next, so restarts (crash, stall, or a clean max-rounds finish) pick
# up the NEXT program instead of either resetting to index 0 or endlessly re-running the one
# that just stalled -- previously this task was hardcoded to one program forever, and because
# each restart is a fresh process, it also had no memory of prior rounds, so a repeated stall on
# early recon looked like it was "doing gator stuff endlessly" with nothing to show for it.
ROTATION_STATE_FILE = Path(__file__).resolve().parent.parent / "config" / "engagements" / ".rotation_state.json"


def _next_rotation_scope_file() -> str:
    idx = 0
    try:
        idx = json.loads(ROTATION_STATE_FILE.read_text()).get("index", 0)
    except Exception:
        idx = 0
    scope_file = BOUNTY_ROTATION_SCOPE_FILES[idx % len(BOUNTY_ROTATION_SCOPE_FILES)]
    try:
        ROTATION_STATE_FILE.write_text(json.dumps({"index": idx + 1}))
    except Exception:
        pass
    return scope_file


def _build_bounty_hunt_goal(scope_file: str) -> str:
    """Generalized version of the old hardcoded HostGator-only prose -- pulls the program name,
    in/out of scope, rate limit, disallowed techniques, and focus list straight out of the
    program's own reviewed scope file rather than repeating one program's specifics in code."""
    data = json.loads((Path(__file__).resolve().parent.parent / scope_file).read_text())
    in_scope = "; ".join(data.get("in_scope", [])) or "(see scope file)"
    out_of_scope = "; ".join(data.get("out_of_scope", [])) or "(see scope file)"
    disallowed = ", ".join(data.get("disallowed_techniques", [])) or "(see scope file)"
    focus = ", ".join(data.get("focus", [])) or "(see scope file)"
    rate = data.get("rate_limit_rps", 1)
    program = data.get("program", scope_file)
    return (
        f"Authorized n-day vulnerability hunt against the real '{program}' Bugcrowd program "
        f"({scope_file}, automated_testing_allowed={data.get('automated_testing_allowed')} -- see that file's "
        f"_automated_testing_allowed_NOTE for exactly what Charles McGowen authorized and when). "
        f"This is the ONLY program to review this run -- do not ask which program, it is already selected. "
        f"In-scope targets: {in_scope}. "
        f"Steps: (1) Use api_request (single, low-rate GET requests, respect rate_limit_rps={rate} -- do NOT "
        f"brute force or hammer any host) against the in-scope targets to collect real tech-stack fingerprint "
        f"text (response headers, body snippets, any X-Powered-By/framework/CMS signatures). (2) Call "
        f"browser_inspect (after navigating there) to discover real forms, links, and API endpoints/paths/"
        f"parameters -- do not skip straight to CVE matching with only a homepage GET. (3) Call check_recent_cves "
        f"with fingerprint_text set to the ACTUAL headers/body text you collected (not a URL -- check_recent_cves "
        f"does not accept a url parameter and ignores it if you pass one) to find candidate n-day CVEs matching "
        f"the detected stack, focusing on: {focus}. (4) For each candidate match, manually verify with ONE "
        f"targeted, non-destructive api_request -- do not assume a keyword match means the vuln is present. If a "
        f"candidate CVE is disallowed for REPORTING under a disclosure-age exclusion, that blocks submission only "
        f"-- still validate it technically now (confirm the actual version/config is affected, don't skip it just "
        f"because you can't report it yet), and save_knowledge the CVE id, its publication date, and the exact "
        f"date it becomes reportable, so a future run on this program knows to check back instead of "
        f"re-discovering it from scratch. (5) Stay strictly within in_scope; NEVER touch out-of-scope assets: "
        f"{out_of_scope}. Disallowed techniques for this program: {disallowed}. (6) Do not create real accounts "
        f"with anything but an @bugcrowdninja.com address if signup is required; do not interact with real "
        f"customer data or real customers. (7) If you find a genuinely confirmed vulnerability, use ask_human "
        f"before drafting/submitting any Bugcrowd report. (8) If you've spent more than 15 rounds without a "
        f"genuine lead, wrap up with save_knowledge and end the run rather than grinding further -- the next "
        f"restart will pick up the next program in rotation. (9) If blocked or confused on something only a "
        f"human would know, use ask_human -- but do NOT ask which program to review, that is already decided."
    )


TASKS = [
    {
        "name": "bounty-hunt-rotation",
        # Paused 2026-09-19 per user request: with only HostGator authorized so far, every
        # restart just re-tests the same program with nothing new to show for it. Re-enable
        # once more programs are added to BOUNTY_ROTATION_SCOPE_FILES, or flip back to True to
        # keep running HostGator alone in the meantime.
        "enabled": False,
        "rotate_scope_files": True,
        "max_rounds": 40,
        # "claude" pins to ONLY the Claude CLI subscription tier with zero fallback to Gemini/
        # local -- live-confirmed 2026-09-15 this produces a genuine no-inference loop whenever
        # that CLI tier is unreliable (not logged in, or -- under this task's real concurrent
        # multi-worker load -- hanging the full 120s with no result at all, a failure mode not
        # reproducible in isolated single-call testing). "auto" restores real fallback (Google
        # Gemini free tier -> local Ollama fast -> Claude CLI subscription), never the metered
        # API either way (UnifiedOracleHub.query() excludes it by policy regardless of preferred).
        "oracle_preference": "auto",
        "stall_minutes": 6,
    },
    {
        "name": "credential-handling-survey",
        # Paused 2026-09-19 per Charles's "as many models working on the RSI dream" focus: this
        # Disabled again 2026-09-20: paused so the paper-to-code run owns the mesh; also it points
        # at the OLD (dead) pod, so it was degrading to LAN/cloud anyway. Re-enable to resume.
        "enabled": False,
        "scope_file": None,
        "max_rounds": 60,
        # See the identical note on "hostgator-latam-hunt" above.
        "oracle_preference": "auto",
        "stall_minutes": 6,
        "goal": (
            "Survey a batch of real public Bugcrowd programs and, for each one, decide for yourself whether the right "
            "next step is recon-only or something deeper, based on what that program's own published policy actually "
            "says. No scope file is loaded for this run, so api_request calls to any of these programs' actual "
            "customer infrastructure are hard-blocked by EngagementGate automatically -- stay within browser-based "
            "recon and account/dashboard interaction only, never call api_request against a real target host this "
            "run. The 12 real Bugcrowd program slugs to work through, one at a time: city-of-vienna-mbb-og, verisign, "
            "justeattakeaway, underarmour-corp, underarmour, octopus-deploy, gearset-mbb, viator, tripadvisor-bb-og, "
            "chipotle-mbb-og, ultramobile-mobile-apps, myfitnesspal-mbb. For EACH slug: (1) browser_navigate or "
            "open_authenticated_site to https://bugcrowd.com/engagements/<slug>, then browser_inspect/read the "
            "rendered page -- look at its Access/Credentials section, its Program Rules, and its overall posture. "
            "(2) Use your own judgment: some programs will just need a recon note (what they require, whether they "
            "even allow automated testing, whether signup is needed at all); others may genuinely invite you to set "
            "up a researcher account first. Where the policy does describe account creation and you have a real "
            "email to use for it, you have the create_researcher_credential tool available, which generates a fresh "
            "unique credential and stores it in the OS credential store -- the raw password is never exposed back to "
            "you or logged anywhere. Do not invent an email address yourself; if you don't know the correct one to "
            "use, ask_human once and reuse that answer for the rest of the batch. (3) If a signup form requires "
            "solving a CAPTCHA, don't attempt to bypass it -- note that and move on. (4) After each program, call "
            "save_knowledge recording the slug, what you found, and what you decided to do and why -- this is the "
            "report Charles will review. (5) Don't spend more than 2-3 rounds struggling on any single program -- "
            "note the issue and move to the next slug; breadth across all 12 and good judgment about when recon is "
            "enough matters more than forcing the same action everywhere."
        ),
    },
    {
        "name": "model-diagnostic-qa-pipeline",
        # Disabled 2026-09-19 per Charles ("stop live hunt, redundant"): this was the only live
        # loop, and it competes with the RSI-dream loop for the same A100 models. Pausing it frees
        # the whole mesh to focus on the dream-RSI work. Flip back to True to resume model QA.
        "enabled": False,
        "scope_file": None,
        "max_rounds": 50,
        "oracle_preference": "local",
        "stall_minutes": 10,
        "run_continuously": True,
        "goal": (
            "Autonomous AI model research and capability benchmarking. "
            "Your task is to agentically test and map out the cybersecurity domain knowledge of models in the compute mesh. "
            "Use 'get_qa_queue' to see which models haven't been tested recently, and systematically round-robin through them. "
            "DO NOT just run a batch diagnostic. Instead, use the 'interact_with_model' tool to ask the model specific technical questions, "
            "read its Chain of Thought (CoT) and final answer, and formulate follow-up questions based on the results to fully map out its capabilities and safety guardrails. "
            "You can use 'diagnose_model_domain' if you want a broad sweep, but focus on deep, interactive questioning. "
            "Record your synthesized findings and failure modes using 'save_knowledge'."
        ),
    },
    {
        "name": "dream-rsi-self-improvement",
        # Disabled 2026-09-20: the replay simulator it was meant to build is DONE
        # (tools/replay_simulator.py, working, verified on real data) and the loop now consults it
        # every round via read_environment. Leaving this build-task enabled just re-clobbers the
        # working file with the model's crude write_file stub. Re-enable only to iterate on the
        # BUILD again; for normal runs the consultation wiring is global and needs no task here.
        "enabled": False,
        "scope_file": None,
        "max_rounds": 60,
        # 72b brain 2026-09-19: the 32B decision models sit on the page and never synthesize, and
        # claude_cli (`claude -p`) hangs/timeouts under this loop's load. So step the reasoning up
        # to the strongest LOCAL model instead -- qwen2.5:72b, already resident on the A100, free,
        # and reliable (plain local HTTP, no CLI subprocess to hang, no metered $). "escalate"
        # routes reasoning to the local smart tier, and the SABRINA_FORCE_LOCAL_MODEL=qwen2.5:72b
        # env pin (set in the dreamer block below) forces every decide_action/reflect onto the
        # 72b. Engineering stays on local qwen3-coder. Open question whether a 72B can synthesize
        # where the 32Bs couldn't -- this is the free test before paying for the metered API.
        "oracle_preference": "local",
        "stall_minutes": 12,
        # run_continuously added 2026-09-19 per Charles: the RSI-dream loop should run as long as
        # possible, not stop after one max_rounds batch. Without this, exiting cleanly at round 60
        # made the supervisor mark it "finished" and never relaunch (that's why it died earlier).
        # With it, each clean finish is immediately relaunched -> an effectively endless dreamer
        # that keeps researching + refining its own RSI ability across restarts (prior findings
        # persist in save_knowledge / probe_runs.db, now surfaced back via read_environment's
        # self-model, so each new batch builds on the last rather than starting cold).
        # BOUNDED for the Claude test 2026-09-19: run_continuously off so this does ONE 60-round
        # batch on Claude quota and stops, rather than looping on the subscription forever before
        # we've confirmed it actually produces the deliverable. Flip back to True once proven.
        "run_continuously": False,
        "goal": (
            "Engineering + BUILD task, revised 2026-09-19: do NOT browse the web or re-read the paper -- its "
            "full mechanism is written out for you below, and re-fetching it just wastes rounds. Do NOT call "
            "browser_navigate or browser_inspect at all for this task. Skip straight to the real work: analyze "
            "your OWN decision history with execute_python, then delegate_to_engineer to BUILD the replay "
            "simulator, then run it and apply it. The mechanism you already have (use it, don't go re-read it):\n\n"
            "PAPER: 'Dream-RSI' -- recursive self-improvement for AI agent exploration policies. Problem: an "
            "agent's exploration policy (how it picks what to try next) stays fixed even as it accumulates real "
            "discovery experience, because testing a NEW policy normally requires expensive live re-runs to see "
            "if it's actually better. Core idea: treat the agent's own PAST discovery trace (every branch "
            "explored, every outcome, every score) as a 'replay simulator' -- a cheap, offline, deterministic "
            "pool you can test a CANDIDATE policy against (does it pick the branches that, in hindsight, actually "
            "paid off?) without spending a single new live inference. Only a policy that wins on replay ever gets "
            "redeployed online. Concretely: (1) log every exploration decision as a node in a tree/graph "
            "(action taken, cost, outcome, score); (2) build a replay interface that, given that history, can "
            "answer 'what would policy X have picked at each decision point, and how would that have scored' "
            "purely from already-recorded outcomes; (3) score candidate policies via "
            "quality - cost_penalty + parallelism_bonus over the SAME historical decision points, so policies are "
            "compared apples-to-apples on identical data; (4) only promote a candidate policy online if it beats "
            "the current one on replay; (5) classify failures as hard (permanently stop retrying) vs repairable "
            "(eligible for one retry, but never enough to starve out genuinely successful branches) -- this is "
            "what stops the agent from either giving up too early or looping forever on a dead end.\n\n"
            "YOUR TASK -- start here immediately, using the mechanism above (no browsing): "
            "(A) Use execute_python (not read_file -- data/probe_runs.db is a SQLite database, read_file will "
            "tell you this if you forget) to study your own decision loop's real historical data: inference_traces "
            "and live_thoughts in that database (every past decide_action/reflect round, what tool was called, "
            "what it returned, whether PLAN_STEP_WORKED), and tools/lab_knowledge.py's save_knowledge/fetch_all "
            "(your longer-term findings). This IS your equivalent of the paper's 'discovery tree' -- you already "
            "have the raw material, it's just never been used as a replay simulator before. "
            "(B) Design what a 'replay simulator' over YOUR OWN history would look like: given a past round's "
            "real context, could a CANDIDATE decide_action strategy be scored against what ACTUALLY happened next "
            "-- without re-running any live oracle calls? "
            "(C) tools/replay_simulator.py is NOT protected (only your orchestration core is). Do NOT use "
            "delegate_to_engineer for this -- that tool ONLY builds web vulnerability-SCANNER engine plugins "
            "(the target_url/endpoint/method HTTP format) and it will produce the WRONG thing. It already did "
            "exactly that: it wrote a web scanner that sends HTTP requests instead of a tool that reads "
            "probe_runs.db. WRITE THE REPLAY SIMULATOR YOURSELF: use write_file to create tools/replay_simulator.py "
            "-- a plain standalone Python script that opens data/probe_runs.db read-only, reads the inference_traces "
            "table (real columns: id, timestamp, round_num, task_type, preferred, model_used, system_prompt, "
            "user_prompt, response, thinking), and implements the scoring method over that recorded history. NO "
            "network, NO HTTP, NO target_url -- this is pure offline analysis of your OWN data. "
            "(D) CRITICAL: a file that has never been executed is not a deliverable. Immediately after write_file, "
            "use execute_python to actually RUN tools/replay_simulator.py against your real data and look at the "
            "real output. If it errors or produces nonsense, that is real signal -- fix it with write_file/edit_file "
            "on tools/replay_simulator.py and run it again, the same way you'd debug any other code, until it "
            "genuinely works. "
            "(E) Prototype it against real data: pick 2-3 genuinely different candidate strategies (e.g. 'always "
            "escalate to smart-tier on stagnation' vs 'the current one-repeat-then-replan approach') and show, "
            "using only already-recorded history and the ACTUAL execution output you just observed, which would "
            "have scored better and why. "
            "(F) 'Connecting yourself to the algorithm to work on it some more' (Charles's phrase for this step): "
            "once it genuinely runs, don't just file it away -- note in save_knowledge specifically how YOU would "
            "call this tool on your own very next decision (e.g. 'before escalating to smart-tier next time I "
            "stall, I would first check replay_simulator's verdict on whether escalating actually helped in "
            "similar past situations') and then, on your next round, actually DO that -- use the tool you just "
            "built as part of your real decision-making at least once before this run ends, not just describe it "
            "hypothetically. "
            "(G) save_knowledge your design writeup, the real code, and the real execution output together -- "
            "this is a deliverable for Charles to review, not something to silently swap into production as your "
            "default behavior going forward. "
            "(H) If you get stuck on what 'scoring' should mean for security-research rounds specifically "
            "(vulnerability hunting doesn't have as clean a reward signal as the paper's math/GPU-kernel domains), "
            "or if you couldn't actually access the paper/coverage after genuinely trying multiple approaches, use "
            "ask_human rather than guessing."
        ),
    },
]

# --- Multi-dreamer swarm (added 2026-09-19 per Charles) ------------------------------------------
# "As many models as possible working on the RSI dream, communicating, dreaming about improving
# their RSI abilities." Rather than one dreamer, run TWO in parallel on the A100, each PINNED to a
# different decision model via SABRINA_FORCE_LOCAL_MODEL (the per-process pin added to
# LocalOllamaOracle). They share one goal, one /workspace, one probe_runs.db and one knowledge
# base -- so each sees the other's engines + save_knowledge findings through read_environment's
# self-model. That shared state IS the communication channel. Model pair chosen for a clean 80GB
# fit with no swap: qwq:32b (~29GB) + qwen2.5:32b (~26GB) + the shared qwen3-coder:30b engineer
# (~24GB, loaded on demand) ~= 79GB. The A100's ollama must keep >=3 models resident for this
# (OLLAMA_MAX_LOADED_MODELS>=3, set in /workspace/start_ollama.sh) or it will swap.
_dreamer_a = next(t for t in TASKS if t["name"] == "dream-rsi-self-improvement")
# SABRINA_NO_BROWSER hard-blocks browsing (the 72b ignores the polite "don't browse" and keeps
# re-reading the paper instead of following its own DB-analysis plan) so it's forced onto
# execute_python + delegate_to_engineer -- straight to the build.
_dreamer_a["env"] = {"SABRINA_FORCE_LOCAL_MODEL": "qwen2.5:72b", "SABRINA_NO_BROWSER": "1"}
_dreamer_b = dict(_dreamer_a)  # same goal / max_rounds / run_continuously / oracle_preference
_dreamer_b["name"] = "dream-rsi-swarm-b"
_dreamer_b["env"] = {"SABRINA_FORCE_LOCAL_MODEL": "qwen2.5:32b"}
# Stood down 2026-09-19: the local-model dreamers can't synthesize (they sit on the page), so
# running them just burns GPU with no output. Focus on the one Claude-driven dreamer. The env
# pin also does nothing now that dreamer A reasons via claude_cli, not a local model. Re-enable
# these (and flip A back to a local model / continuous) to resume the local swarm experiment.
_dreamer_b["enabled"] = False
TASKS.append(_dreamer_b)

# Third dreamer added 2026-09-19 per Charles ("add more models / expound the swarm"): a genuinely
# different strong reasoner (deepseek-r1) for diverse "dreaming", rather than a weak 7B math model
# that would flail on this tool-calling task. VRAM note: three 32B dreamers (~79GB) leave no room
# for the shared qwen3-coder engineer, so ollama (OLLAMA_MAX_LOADED_MODELS=4) will evict the
# least-recently-used dreamer to load the engineer during delegate_to_engineer, then reload it --
# some swap thrash during engineering is expected. Drop back to two dreamers if that hurts.
_dreamer_c = dict(_dreamer_a)
_dreamer_c["name"] = "dream-rsi-swarm-c"
_dreamer_c["env"] = {"SABRINA_FORCE_LOCAL_MODEL": "deepseek-r1:32b"}
_dreamer_c["enabled"] = False  # stood down alongside dreamer B (see note above)
TASKS.append(_dreamer_c)

# --- Paper -> Python conversion run (added 2026-09-20) --------------------------------------------
# Reads the Dream-RSI paper's algorithms from a LOCAL file (no browsing -- that was the worst
# failure mode) and converts each into a working Python module under tools/dream_rsi/. Uses the
# fixed general-build path (write_file + execute_python to verify; NOT the vuln-scanner delegate).
# enabled=False on purpose: wired and ready, launched only on Charles's go after tweaks.
TASKS.append({
    "name": "dream-rsi-paper-to-code",
    "enabled": False,  # done (all 8 algorithms verified); paused so the improvement session owns the A100
    "scope_file": None,
    # Re-scoped 2026-09-20 (CAPSTONE): all 8 Dream-RSI algorithms are now built + verify_module-VERIFIED
    # in tools/dream_rsi/ (tree, policy, simulator, rollout, replay, improve -- the last 3 built by the
    # manager after the loop hit its ceiling on the subtle shared-root semantics). All 6 are now write-
    # PROTECTED (is_finished_module in sabrina_gemini_loop.py). This run's job is the end-to-end capstone:
    # compose them into one pipeline demo. It plays to the loop's now-working strengths (describe_module
    # to read APIs, verify_module to gate) without needing it to re-derive the hard algorithm internals.
    # NOTE: pinned to qwen2.5:72b on the A100 -- the A100 pod must be STARTED before enabling this,
    # or it will crash-loop against a stopped pod.
    "max_rounds": 100,
    "oracle_preference": "local",
    "stall_minutes": 15,
    "run_continuously": True,
    "env": {"SABRINA_FORCE_LOCAL_MODEL": "qwen2.5:72b",
            "SABRINA_RUNPOD_OLLAMA_URLS": "https://45m90jfro2yzjn-11434.proxy.runpod.net"},
    "goal": (
        "CAPSTONE: build ONE end-to-end Dream-RSI pipeline demo. All eight algorithms are ALREADY built and "
        "verified as separate modules in tools/dream_rsi/ -- tree.py (Discovery Tree + Node + Child Revelation), "
        "policy.py (BatchSelectionPolicy), simulator.py (History-as-Simulator), rollout.py (Online Rollout), "
        "replay.py (Offline Replay Evaluation + Replay Objective), improve.py (Policy Improvement Loop). ALL SIX "
        "ARE FINISHED AND WRITE-PROTECTED: do NOT modify, overwrite, or 'improve' any of them -- write_file/edit_file "
        "to them is BLOCKED. Your ONLY job is to create ONE new file, tools/dream_rsi/pipeline.py, that imports and "
        "runs them together to demonstrate the full loop. "
        "STEP 1 -- introspect: call describe_module on tools/dream_rsi/rollout.py, tools/dream_rsi/improve.py, "
        "tools/dream_rsi/tree.py and tools/dream_rsi/policy.py to see their EXACT public functions/classes (do not "
        "guess signatures). rollout.py exposes online_rollout(frozen_tree, policy, max_rounds) -> "
        "(observed_tree, trajectory, revealed_count, max_score); improve.py exposes "
        "improve_policy(frozen_tree, candidate_worker_counts, max_rounds, cost=0.1) -> (results, best). "
        "STEP 2 -- write tools/dream_rsi/pipeline.py using plain absolute sibling imports (from tree import Node, Tree; "
        "from rollout import online_rollout; from improve import improve_policy -- NOT relative 'from .tree import'). "
        "In its __main__: build ONE frozen Tree (root with a couple of children and grandchildren, each Node given a "
        "score), then (a) run online_rollout once with a BatchSelectionPolicy(worker_count=2) and print the trajectory "
        "and max score, and (b) call improve_policy with candidate_worker_counts=[1,2,3] and print each setting's V and "
        "the chosen best. Add a one-line header before each stage so the output reads as a pipeline. "
        "STEP 3 -- verify: run {\"tool\": \"verify_module\", \"path\": \"tools/dream_rsi/pipeline.py\", "
        "\"expect\": [\"trajectory\", \"Best policy\"]}. A build is DONE only when verify_module returns [VERIFIED] -- "
        "exit code 0 is NOT proof (it fails a file that prints nothing or prints a traceback). If NOT VERIFIED, "
        "describe_module/read_file the dependency, fix pipeline.py (never the dependencies), and verify_module again "
        "until [VERIFIED]. Then save_knowledge a one-line note that the Dream-RSI pipeline demo is complete, and call "
        "the completed tool. This is NOT a web vulnerability scanner: do not use the target_url/endpoint format and do "
        "not use delegate_to_engineer. If genuinely stuck after describe_module + read_file, use ask_human."
    ),
})

# Standing codebase-improvement session 2026-09-20 (Charles: "I have a lot of code for Sabrina to
# improve" + "I want to see the pod being used"). Runs the snapshot-isolated launcher
# (tools/run_improvement_session.py) -- LOCAL ONLY, no git (this repo never leaves the box). The loop
# pulls targets from get_improvement_queue, adds tests / fixes bugs / adds docs IN PLACE, gated by
# run_tests; the launcher snapshots before + writes a REVIEW.md diff bundle after for human review.
# FIXED SESSION LENGTH via session_minutes -> the loop's --max-seconds; run_continuously:False so it
# STOPS after and does not relaunch. enabled:False until launched (disable the capstone first so the
# A100 is uncontended, then enable this + restart the supervisor).
TASKS.append({
    "name": "codebase-self-improvement",
    # Customer work is now the only default supervised workload. This broad
    # improvement swarm can still be opted into explicitly after the customer
    # job is no longer the operational priority.
    "enabled": False,
    "improvement_session": True,
    "session_minutes": 60,
    "session_workers": 3,        # swarm: 3 parallel loops on disjoint queue slices -> fills the A100's GPU gaps
    "oracle_preference": "local",
    "stall_minutes": 30,         # > session_minutes so the stall detector never kills a healthy timed session
    "run_continuously": False,
    "env": {"SABRINA_FORCE_LOCAL_MODEL": "qwen2.5:72b",
            "SABRINA_RUNPOD_OLLAMA_URLS": "https://45m90jfro2yzjn-11434.proxy.runpod.net",
            # Cost control: start the pod before the session (best-effort) + STOP it after -> zero idle
            # billing between sessions. Needs RUNPOD_API_KEY in the secret store (stop no-ops+warns if absent).
            "SABRINA_POD_AUTOSTOP": "1",
            "RUNPOD_POD_ID": "45m90jfro2yzjn"},
})

# CORE-RSI session 2026-09-20 (Charles: "proceed ... i dont want catastrophic loss"). The loop may edit
# its OWN brain (loop/oracle/prompts/skills), incl. refactors. Safe via the recovery layer: the launcher
# takes a full-system known-good checkpoint first (--core), each marked target is gated by a full
# self-check (auto-reverts to known-good if the core breaks), and this task is core_mode -> the supervisor
# restores known-good if the loop crash-loops. Single worker. Enable this + disable codebase-self-improvement
# (one A100 improvement task at a time), then restart the supervisor.
TASKS.append({
    "name": "codebase-core-rsi",
    "enabled": False,  # OFF for the lab deploy until recovery is verified ON THE LAB HOST (proven on Windows only). Flip True + disable codebase-self-improvement + restart to run a core session.
    "improvement_session": True,
    "core_mode": True,
    "session_minutes": 20,
    "session_workers": 1,
    "oracle_preference": "local",
    "stall_minutes": 30,
    "run_continuously": False,
    "env": {"SABRINA_FORCE_LOCAL_MODEL": "qwen2.5:72b",
            "SABRINA_RUNPOD_OLLAMA_URLS": "https://45m90jfro2yzjn-11434.proxy.runpod.net",
            "SABRINA_POD_AUTOSTOP": "1", "RUNPOD_POD_ID": "45m90jfro2yzjn"},
})

# Manager-assigned task 2026-09-20 (Charles wants the replay-simulator idea applied to bug bounty).
# Reuses dreamer A's good config -- 72b pin + SABRINA_NO_BROWSER env, local routing, stall_minutes --
# only the name/goal/enabled differ. Sabrina builds it; the manager (Claude) verifies she actually
# does, and does NOT accept a stub. Writes a NEW file (tools/bounty_simulator.py); the finished
# tools/replay_simulator.py is off-limits.
_bounty_sim = dict(_dreamer_a)
_bounty_sim["name"] = "bounty-simulator-build"
# Disabled 2026-09-20: Sabrina failed to build it autonomously (launched, produced an empty log, no
# tools/bounty_simulator.py) -- the documented pattern (RSI_PROCESS.md sec.4). And the A100 pod is now
# STOPPED to save cost, so a local-72b-pinned task would only crash-loop against a dead pod. Re-enable
# only after the pod is started AND you're ready to build it the leader/worker way, not autonomously.
_bounty_sim["enabled"] = False
_bounty_sim["run_continuously"] = False
_bounty_sim["goal"] = (
    "BUILD task -- do NOT browse (browsing is disabled), and do NOT use delegate_to_engineer (it ONLY "
    "builds web vulnerability-SCANNER engine plugins with a target_url/endpoint format and will produce "
    "the WRONG thing). Your job: build tools/bounty_simulator.py, an OFFLINE simulator that scores "
    "bug-bounty hunting TACTICS against your own recorded history -- the same idea tools/replay_simulator.py "
    "already implements for oracle routing. "
    "(A) FIRST use read_file on tools/replay_simulator.py to see the exact WORKING pattern and COPY its "
    "structure: it opens data/probe_runs.db read-only, reads inference_traces (parsing the PLAN_STEP_WORKED:<n> "
    "progress signal out of each response's text), scores each candidate policy, and exposes a guidance_line() "
    "function returning a one-line verdict. "
    "(B) Your version scores TOOL-CHOICE tactics instead of routing: for each candidate 'first tool to reach "
    "for' when working a target (e.g. api_request vs browser_inspect vs check_recent_cves vs execute_python), "
    "compute the fraction of rounds that used that tool where PLAN_STEP_WORKED>0, using data/probe_runs.db's "
    "inference_traces and crew_tasks (tool_name, status). Higher progress-rate = better tactic. "
    "(C) WRITE it yourself with write_file to tools/bounty_simulator.py -- a plain standalone Python module, "
    "read-only on the DB, NO http/requests/target_url, pure offline analysis. "
    "(D) RUN it with execute_python against the real data and look at the real output. If it errors or prints "
    "nothing, that is real signal -- fix it with edit_file/write_file on tools/bounty_simulator.py and run it "
    "again until it genuinely works and prints a real per-tactic score table. A file that has never run is NOT "
    "a deliverable. "
    "(E) Add a guidance_line() function like tools/replay_simulator.py's, returning a one-line verdict string. "
    "(F) save_knowledge your short writeup plus the REAL execution output you observed. "
    "(G) Do NOT modify tools/replay_simulator.py -- it is finished and protected. If you get genuinely stuck "
    "after real attempts, use ask_human rather than guessing or writing a stub."
)
TASKS.append(_bounty_sim)

# The default runtime is one deterministic dispatcher, not a free-roaming
# reasoning loop. It owns the canonical config in config/job_scheduler.json and
# its customer_email handler has no LinkedIn or general-browser capability.
TASKS.append({
    "name": "customer-job-scheduler",
    "enabled": True,
    "customer_scheduler": True,
    "run_continuously": True,
    "stall_minutes": None,
})


def log(msg: str):
    print(f"[supervisor {time.strftime('%H:%M:%S')}] {msg}", flush=True)


class ManagedProcess:
    def __init__(self, name: str, cmd: list, cwd: Path, log_path: Path, stall_minutes: float = None, run_continuously: bool = False, cmd_builder=None, env: dict = None, core_mode: bool = False):
        self.name = name
        self.cmd = cmd
        self.cmd_builder = cmd_builder  # if set, called fresh in start() instead of reusing `cmd` -- lets a
                                         # rotating task (see BOUNTY_ROTATION_SCOPE_FILES) pick a new program
                                         # on every (re)start rather than repeating the same static command.
        self.cwd = cwd
        self.log_path = log_path
        self.stall_minutes = stall_minutes
        self.run_continuously = run_continuously
        # Per-task extra environment (added 2026-09-19 for the multi-dreamer swarm): merged over
        # os.environ at launch. Used to pin each dreamer instance to a different local model via
        # SABRINA_FORCE_LOCAL_MODEL, so several dreamers run in parallel on distinct models.
        self.env = env or {}
        self.core_mode = core_mode  # core-RSI task: crash-loop triggers known-good auto-restore
        self.proc: subprocess.Popen = None
        self.restart_count = 0
        self.disabled = False  # set True once MAX_RESTARTS_PER_TASK is exceeded
        self.last_action = "starting"
        self.last_action_reason = "initial launch"
        self.started_at: float = 0.0

    def start(self):
        if self.cmd_builder:
            self.cmd = self.cmd_builder()
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        log_file = open(self.log_path, "a", encoding="utf-8", errors="replace")
        log(f"starting '{self.name}': {' '.join(self.cmd[:4])}... (log: {self.log_path})")
        # Every supervised child is unattended by definition -- there is no human at its stdin. Set
        # SABRINA_UNATTENDED=1 for all of them so ask_human self-answers immediately instead of
        # blocking on a keystroke that will never come (Charles, 2026-09-20). A per-task env can
        # still override it if ever needed.
        self.proc = subprocess.Popen(
            self.cmd, cwd=str(self.cwd), stdout=log_file, stderr=subprocess.STDOUT,
            env={**os.environ, "SABRINA_UNATTENDED": "1", **(self.env or {})}
        )
        self.last_action = "started"
        # Live-confirmed 2026-09-17: is_stalled() below used to judge staleness purely from the
        # log FILE's mtime -- but that file is opened in APPEND mode and shared across every
        # restart of this task, so a freshly-started process inherits whatever staleness the
        # PREVIOUS, just-killed instance left behind. If the old process died already stale (say
        # 5.5 minutes since its last write) and the new one takes even 30-40s to reach its own
        # first print (RAG sync, an oracle call fighting through real provider 503s -- both
        # observed live today), the inherited age can already exceed stall_minutes before the new
        # process ever writes a single byte -- killing it again, immediately, before it had any
        # real chance. Observed live: restarts every 30-50s, all logged as "no log output for
        # over 6 minutes", which is definitionally impossible at that cadence -- proof the check
        # was measuring stale, inherited file age, not this process's own elapsed runtime.
        self.started_at = time.time()

    def is_alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def log_tail(self, n_bytes: int = 4000) -> str:
        if not self.log_path.exists():
            return ""
        try:
            with open(self.log_path, "rb") as f:
                f.seek(0, 2)
                size = f.tell()
                f.seek(max(0, size - n_bytes))
                return f.read().decode("utf-8", errors="replace")
        except Exception:
            return ""

    def is_waiting_for_human(self) -> bool:
        """True if the last thing this process did was block on ask_human --
        not a stall, a correct pause. Checked via the tail of its own log, not
        the dashboard's shared is_waiting_for_human flag, since that's a single
        global slot two concurrently-running tasks would race over."""
        tail = self.log_tail()
        return any(marker in tail for marker in WAITING_FOR_HUMAN_MARKERS)

    def is_safety_net_continue_prompt(self) -> bool:
        """True specifically for the generic periodic 'N rounds, should she
        continue?' governor -- safe to auto-answer, unlike a real substantive
        ask_human question."""
        return SAFETY_NET_CONTINUE_MARKER in self.log_tail()

    def is_stalled(self) -> bool:
        if self.stall_minutes is None or not self.log_path.exists():
            return False
        if self.is_waiting_for_human():
            return False  # correctly paused, not stuck -- never kill for this
        # Anchor staleness to whichever is MORE RECENT: the log file's last write, or this
        # process's own start time. Without the second term, a fresh process inherits whatever
        # staleness the file already had from the instance it replaced (see the note in start())
        # -- this guarantees every incarnation gets a real stall_minutes-long grace window of its
        # own, measured from when IT started, not from whenever the file was last touched by a
        # predecessor that may already be dead.
        reference_ts = max(self.log_path.stat().st_mtime, self.started_at)
        age_minutes = (time.time() - reference_ts) / 60.0
        return age_minutes > self.stall_minutes

    def stop(self):
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=10)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass

    def restart(self, reason: str):
        self.restart_count += 1
        self.last_action_reason = reason
        if self.restart_count > MAX_RESTARTS_PER_TASK:
            log(f"'{self.name}' exceeded {MAX_RESTARTS_PER_TASK} restarts -- giving up, "
                f"marking disabled. Check {self.log_path} for the real underlying problem.")
            self.disabled = True
            self.last_action = "disabled"
            return
        log(f"restarting '{self.name}' (attempt {self.restart_count}/{MAX_RESTARTS_PER_TASK}) -- reason: {reason}")
        self.last_action = "restarting"
        self.stop()
        # Small backoff, growing with restart count, capped at 60s.
        time.sleep(min(5 * self.restart_count, 60))
        # CORE-RSI catastrophic backstop: if a core session crash-loops, the loop likely broke its own
        # brain. Restore the last known-good full-system checkpoint before restarting, so a bricked
        # core self-heals instead of dying at the restart cap. Fires once at a low threshold.
        if self.core_mode and self.restart_count == 3:
            try:
                sys.path.insert(0, str(REPO_ROOT))
                from tools.system_checkpoint import restore as _restore, latest_known_good as _lkg
                g = _lkg()
                if g:
                    _restore(g.name)
                    log(f"[core-restore] '{self.name}' crash-looped {self.restart_count}x -- restored "
                        f"known-good {g.name} before restart (a self-edit likely broke the core).")
                else:
                    log(f"[core-restore] '{self.name}' crash-looped but NO known-good checkpoint exists.")
            except Exception as e:
                log(f"[core-restore] restore failed: {e}")
        self.start()

    def status_dict(self) -> dict:
        return {
            "task_name": self.name,
            "pid": self.proc.pid if self.proc else None,
            "alive": self.is_alive(),
            "restart_count": self.restart_count,
            "disabled": self.disabled,
            "last_action": self.last_action,
            "last_action_reason": self.last_action_reason,
        }


def try_auto_continue_safety_net(managed: dict):
    """If exactly one managed task is sitting on the generic 'should she
    continue?' safety-net prompt, answer it so she doesn't wait forever for a
    human who isn't watching. is_waiting_for_human/pending_human_response on
    the dashboard side are single global values shared across every
    concurrently-running task -- answering while more than one task could be
    waiting risks routing the answer to the wrong process, so this
    deliberately does nothing in that ambiguous case rather than guess."""
    waiting = [mp for mp in managed.values() if mp.name != "dashboard" and mp.is_waiting_for_human()]
    if len(waiting) != 1:
        if len(waiting) > 1:
            log(f"{len(waiting)} tasks waiting for human input at once -- ambiguous which the dashboard's "
                f"single response slot belongs to, skipping auto-continue for all of them this cycle.")
        return
    mp = waiting[0]
    if not mp.is_safety_net_continue_prompt():
        return  # a real substantive ask_human question -- leave it for an actual human
    log(f"'{mp.name}' is only waiting on the generic safety-net continue prompt -- auto-answering "
        f"'Yes, continue' so it doesn't sit blocked with no human watching.")
    body = json.dumps({"goal": "Yes, continue"}).encode("utf-8")
    req = urllib.request.Request(CHAT_API_URL, data=body, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=5):
            pass
    except Exception as e:
        log(f"(non-fatal) couldn't auto-continue '{mp.name}': {e}")


def report_status(managed: dict):
    """POST current state of every managed process to probe_api.py's
    /api/supervisor/status so it's visible through the dashboard/API instead
    of only in this process's own stdout. Best-effort -- if the dashboard
    itself is down (e.g. mid-restart), this just skips a beat rather than
    crashing the supervisor over a status-reporting failure."""
    body = json.dumps({"tasks": [mp.status_dict() for mp in managed.values()]}).encode("utf-8")
    req = urllib.request.Request(STATUS_API_URL, data=body, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=5):
            pass
    except Exception as e:
        log(f"(non-fatal) couldn't report status to dashboard: {e}")


def build_task_process(task: dict) -> ManagedProcess:
    name = task["name"]
    log_path = LOG_DIR / f"{name}.log"
    if task.get("customer_scheduler"):
        cmd = [sys.executable, "-u", "-m", "tools.customer_job_scheduler", "run"]
        return ManagedProcess(
            name,
            cmd,
            REPO_ROOT,
            log_path,
            stall_minutes=None,
            run_continuously=True,
        )
    if task.get("improvement_session"):
        # Standing codebase-improvement session: run the snapshot-isolated launcher (LOCAL only, no
        # git -- this repo never leaves the box). It snapshots in-scope source, runs the loop with a
        # wall-clock cap, then writes a REVIEW.md diff bundle for the human. run_continuously:False so
        # a clean timed exit STOPS (does not relaunch) -- that's the "fixed session length" stop policy.
        cmd = [sys.executable, "-u", os.path.join("tools", "run_improvement_session.py"),
               "--minutes", str(task.get("session_minutes", 45)),
               "--workers", str(task.get("session_workers", 1)),
               "--oracle-preference", task.get("oracle_preference", "local")]
        if task.get("core_mode"):
            cmd.append("--core")   # unlock brain edits + full-system checkpoint protection
        return ManagedProcess(name, cmd, REPO_ROOT, log_path,
                              stall_minutes=task.get("stall_minutes", 20),
                              run_continuously=task.get("run_continuously", False),
                              env=task.get("env"), core_mode=bool(task.get("core_mode")))
    if task.get("rotate_scope_files"):
        def cmd_builder():
            scope_file = _next_rotation_scope_file()
            goal = _build_bounty_hunt_goal(scope_file)
            return [sys.executable, "-u", "sabrina_gemini_loop.py",
                    "--max-rounds", str(task.get("max_rounds", 40)),
                    "--oracle-preference", task.get("oracle_preference", "claude"),
                    "--goal", goal,
                    "--scope-file", scope_file]
        return ManagedProcess(name, None, REPO_ROOT, log_path, stall_minutes=task.get("stall_minutes", 6),
                               run_continuously=task.get("run_continuously", False), cmd_builder=cmd_builder)
    cmd = [sys.executable, "-u", "sabrina_gemini_loop.py",
           "--max-rounds", str(task.get("max_rounds", 40)),
           "--oracle-preference", task.get("oracle_preference", "claude"),
           "--goal", task["goal"]]
    if task.get("scope_file"):
        cmd += ["--scope-file", task["scope_file"]]
    return ManagedProcess(name, cmd, REPO_ROOT, log_path, stall_minutes=task.get("stall_minutes", 6), run_continuously=task.get("run_continuously", False), env=task.get("env"))


def build_dashboard_process() -> ManagedProcess:
    cmd = [sys.executable, "-u", "probe_api.py"]
    log_path = LOG_DIR / "dashboard.log"
    return ManagedProcess("dashboard", cmd, PROBE_API_DIR, log_path, stall_minutes=None, run_continuously=True)


def build_curator_process() -> ManagedProcess:
    cmd = [sys.executable, "-u", "autonomous_hf_curator.py"]
    log_path = LOG_DIR / "hf_curator.log"
    return ManagedProcess("hf_curator", cmd, PROBE_API_DIR, log_path, stall_minutes=None, run_continuously=True)


def load_tasks() -> list:
    enabled = [t for t in TASKS if t.get("enabled", True)]
    if os.environ.get("SABRINA_ENABLE_LEGACY_TASKS") == "1":
        return enabled
    return [t for t in enabled if t.get("customer_scheduler")]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-interval", type=int, default=30, help="Seconds between health checks")
    args = parser.parse_args()

    managed = {
        "dashboard": build_dashboard_process(),
        # hf_curator disabled 2026-09-19: it hammers Google Gemini in a tight loop and, on
        # cooldown, falls to the local SMART_LADDER (qwq:32b) -- which kept evicting the pinned
        # 72b dream-rsi brain from the A100 and stalling it. Re-enable once the dreamer work is
        # done, or pin it off the A100. (Dashboard stays -- it makes no oracle calls.)
        # "hf_curator": build_curator_process(),
    }
    for task in load_tasks():
        managed[task["name"]] = build_task_process(task)

    def shutdown(*_):
        log("shutting down -- stopping all managed processes...")
        for mp in managed.values():
            mp.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    try:
        signal.signal(signal.SIGTERM, shutdown)
    except Exception:
        pass  # SIGTERM not available on some platforms

    for mp in managed.values():
        mp.start()

    log(f"supervising {len(managed)} process(es): {', '.join(managed.keys())}. "
        f"Checking every {args.check_interval}s. Ctrl+C to stop everything cleanly.")

    # Give the dashboard a moment to actually bind before the first status report
    # (verified this session: probe_api.py takes ~10-15s from launch to serving).
    time.sleep(12)
    report_status(managed)

    while True:
        time.sleep(args.check_interval)
        for name, mp in managed.items():
            if mp.disabled:
                continue
            if not mp.is_alive():
                exit_code = mp.proc.returncode if mp.proc else None
                # Previously restarted on ANY exit, with no distinction between a real crash
                # and a clean finish -- verified live, 2026-09-15: hostgator-latam-hunt hit its
                # own round limit (or could have self-reported "completed"), the process exited
                # 0, and the supervisor relaunched the identical goal from round 1 anyway. Since
                # sabrina_gemini_loop.py has no sys.exit() in either the round-limit or
                # "completed" path, both fall through to Python's default exit 0 -- an unhandled
                # crash (the Playwright EPIPE crash seen earlier this session) exits non-zero.
                # exit code 0 on a TASK (not the always-persistent dashboard) means "finished on
                # its own", not "died" -- stop supervising it instead of looping it forever.
                if mp.name not in ["dashboard", "hf_curator"] and exit_code == 0 and not mp.run_continuously:
                    log(f"'{mp.name}' exited cleanly (code 0) -- finished on its own (round limit "
                        f"reached, or it called the 'completed' tool). NOT restarting; flip "
                        f"'enabled' back on in TASKS to run it again. Check {mp.log_path} for how it ended.")
                    mp.disabled = True
                    mp.last_action = "finished"
                    mp.last_action_reason = "exited cleanly (code 0) -- not restarted"
                else:
                    if exit_code == 0 and mp.run_continuously:
                        mp.restart(f"process exited cleanly but is set to run continuously")
                    else:
                        mp.restart(f"process exited (code {exit_code})")
            elif mp.is_stalled():
                mp.restart(f"no log output for over {mp.stall_minutes} minutes")
        try_auto_continue_safety_net(managed)
        report_status(managed)


if __name__ == "__main__":
    main()
