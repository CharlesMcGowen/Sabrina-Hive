import os
import re
import json
import py_compile
import asyncio
import concurrent.futures
from typing import Optional

from tools.model_cpu_registry import load_cpu_registry, recommend_cpu_skill

class Colors:
    BLUE = '\033[94m'      # Gemini Architect
    MAGENTA = '\033[95m'   # Sabrina Worker
    CYAN = '\033[96m'      # System messages
    GREEN = '\033[92m'     # Success/Completion
    YELLOW = '\033[93m'    # Warnings
    RED = '\033[91m'       # Errors
    RESET = '\033[0m'

def print_system(msg):
    print(f"{Colors.CYAN}[SYSTEM] {msg}{Colors.RESET}")

def query_sabrina_api(prompt, task_type="worker"):
    """Queries Sabrina through her designated multi-node API router (Resident Worker/Queen)."""
    try:
        from sabrina_coven_orchestrator_LAB import MultiNodeLLMRouter
        router = MultiNodeLLMRouter()
        # System prompt wrapper is handled by the caller, so we pass empty system
        result = router.route(system="", user=prompt, task_type=task_type, max_tokens=4096)
        return result[0] if isinstance(result, tuple) else result
    except ImportError:
        return "Error: Could not import Sabrina API router (MultiNodeLLMRouter)."
    except Exception as e:
        return f"Error communicating with Sabrina API: {str(e)}"

# A fixed keyword list proved too brittle -- verified live, two different refusals from the
# SAME model used different phrasing ("I cannot fulfill your request to write functional
# vulnerability scanning scripts" vs "I cannot generate Python scripts, plugins, or actionable
# tools designed to scan for..."), and the second one didn't match the first list at all. Refusals
# vary in exact wording but essentially always OPEN with a declining statement before any (absent)
# code -- a real code response starts with an explanation or a code block, never "I cannot ...".
_REFUSAL_OPENERS = (
    "i cannot", "i can't", "i'm not able", "i am not able", "i'm unable", "i am unable",
    "sorry, i", "i'm sorry, but", "i must decline", "i won't", "i will not",
)


def _looks_like_refusal(text: str) -> bool:
    stripped = (text or "").strip().lstrip("*#> \n").lower()
    if any(stripped.startswith(opener) for opener in _REFUSAL_OPENERS):
        return True
    # Also check the first ~200 chars for a declining phrase even if it's not the literal
    # opening word (e.g. a one-sentence preamble before the refusal) -- but require it to
    # actually be near the start, not just appear anywhere in a long, otherwise-fine response.
    head = stripped[:200]
    return any(opener in head for opener in _REFUSAL_OPENERS)


def _looks_like_echoed_prompt(result: str, source_text: str, window: int = 80) -> bool:
    """True if a large chunk of the task's OWN description/backstory shows up verbatim in
    the result -- a second real CrewAI failure mode, distinct from refusal: a confused or
    weak model parrots its own instructions back instead of doing the task. Verified live,
    2026-09-15: a decide_action round's 'reason' turned out to be the raw task description
    text, not a real answer, with nothing catching it before it reached Sabrina's context.

    Checks for a long shared substring via sliding windows over the source text rather than
    hardcoded phrases, so this generalizes across whichever task template called it (build
    vs debug engine, navigation tasks, etc.) without a separate keyword list per template.
    """
    result_norm = " ".join((result or "").split())
    source_norm = " ".join((source_text or "").split())
    if len(source_norm) < window or len(result_norm) < window:
        return False
    for i in range(0, len(source_norm) - window, window):
        if source_norm[i:i + window] in result_norm:
            return True
    return False


def _looks_like_empty_result(result: str) -> bool:
    """True if the crew nominally reported 'Completed' but produced no real content -- a
    THIRD distinct CrewAI failure mode, different from refusal or echo. Live-confirmed
    2026-09-16 building a subdomain-recon engine: steps 1-2 of a 6-step native-planning run
    (PlanningConfig, added 2026-09-15) made real tool calls and real progress, then step 3
    onward hit gemini-3.1-flash-lite's PER-MINUTE quota (15 req/min -- planning's own
    execute+observe calls per step burn through this far faster than the old single-shot
    ReAct loop did). CrewAI's own reasoning_handler caught that 429 and silently logged
    'Defaulting to conservative replan', marking the step SUCCEEDED with empty content
    instead of surfacing a real failure -- the final synthesized output was literally
    'Step 1: (no result)' through 'Step 6: (no result)', with zero code and no loud
    failure signal. Worse than an honest crash: 'Crew Execution Completed' reads as success.
    """
    stripped = (result or "").strip()
    if not stripped:
        return True
    return stripped.count("(no result)") >= 2


def _looks_like_missing_code(result: str) -> bool:
    """True if a code-generation task's result contains no ```python fenced block at all -- a
    FOURTH distinct CrewAI failure mode, different from refusal, echo, or empty-result.
    Live-confirmed 2026-09-16 building a subdomain-recon engine: 'Crew Execution Completed'
    with a confident, detailed PROSE paragraph describing exactly what the script does and how
    it works ('I have designed the script to be fully reusable and robust... Final Answer: The
    passive DNS resolution logic is implemented within the subdomain_recon.py script') -- with
    zero actual code, despite the task's PLUGIN REQUIREMENTS explicitly demanding the final
    answer be pure Python wrapped in ```python fences. extract_and_validate_code() would find
    nothing to save here. query_sabrina_crewai() is always a code-producing task by design (the
    QA Test Automation Engineer persona building/refining a checker script), so this check
    applies unconditionally, unlike a general-purpose task where prose could be a valid answer.
    """
    return "```python" not in (result or "")


def _looks_like_incomplete_plugin(result: str) -> bool:
    """True if a ```python block IS present but doesn't satisfy the plugin contract every
    create_build_engine_task() call requires (TOOL_NAME, TOOL_PROMPT, def execute(...)) -- a
    FIFTH distinct CrewAI failure mode, different from refusal/echo/empty/missing-code because
    extract_and_validate_code() finds real, syntactically-valid code and happily reports "Valid
    Python syntax!" while saving a useless fragment.

    Live-confirmed 2026-09-17 building subdomain_takeover_engine.py: gemini-3.1-flash-lite hit
    Google's own 503 "high demand" errors on 5 of 6 native-planning steps AND every one of 13
    execute_python_sandbox self-verification attempts failed (Docker not reachable locally,
    see FRAMEWORK.md) -- under that double failure the agent's step-6 "Observation" self-judged
    "Goal Achieved Early" and returned only the last helper function it had been mid-drafting
    (check_takeover_with_nxdomain, calling an undefined get_dns_status()) instead of the full
    required plugin. py_compile passed because one syntactically-valid function is still valid
    Python -- syntax validity is not the same as satisfying the task.
    """
    matches = re.findall(r"```python\s*(.*?)\s*```", result or "", re.DOTALL)
    if not matches:
        return False  # _looks_like_missing_code already covers the no-code-block case
    code = matches[-1]
    return not ("TOOL_NAME" in code and "TOOL_PROMPT" in code and "def execute(" in code)


def _validate_crewai_result(result_str: str, model_name: str, task_description: str, instruction: str, vuln_type: str, exclude_tiers: set):
    """Runs the five known CrewAI failure-mode checks (refusal/echo/empty/missing-code/
    incomplete-plugin) against a result string and either returns it accepted (wrapped with the
    model tag) or recurses into query_sabrina_crewai with the offending tier excluded.

    Factored out so BOTH the normal crew.kickoff() success path and the except-fallback path
    (query_sabrina_api via MultiNodeLLMRouter) get the same quality gate. Live-confirmed
    2026-09-17 this gap was real, not theoretical: a "Crew Execution Failed" exception (the same
    unexplained CrewAI status quirk flagged earlier this session, where kickoff() raises despite
    real usable content existing) sent a subdomain_takeover build through the except branch,
    which returned Gemini Flash's raw answer completely unchecked -- good CNAME/fingerprint/HTTP
    logic, but missing the required TOOL_NAME/TOOL_PROMPT/execute() plugin wrapper, saved as
    "Valid Python syntax!" by extract_and_validate_code (py_compile doesn't know what a plugin
    contract is) with nothing catching that it can't actually be loaded as a Sabrina tool.
    """
    if _looks_like_refusal(result_str) and len(exclude_tiers) < 2:
        refused_tier = "gemini" if "gemini" in model_name else ("claude" if "claude" in model_name else "unknown")
        print_system(f"[REFUSAL DETECTED] '{model_name}' appears to have refused the task (not a connectivity failure) -- retrying with '{refused_tier}' excluded, next tier up.")
        return query_sabrina_crewai(instruction, vuln_type=vuln_type, _exclude_tiers=exclude_tiers | {refused_tier})

    if _looks_like_echoed_prompt(result_str, task_description) and len(exclude_tiers) < 2:
        refused_tier = "gemini" if "gemini" in model_name else ("claude" if "claude" in model_name else "unknown")
        print_system(f"[ECHOED PROMPT DETECTED] '{model_name}' returned its own task description instead of real output -- retrying with '{refused_tier}' excluded, next tier up.")
        return query_sabrina_crewai(instruction, vuln_type=vuln_type, _exclude_tiers=exclude_tiers | {refused_tier})

    if _looks_like_empty_result(result_str) and len(exclude_tiers) < 2:
        refused_tier = "gemini" if "gemini" in model_name else ("claude" if "claude" in model_name else "unknown")
        print_system(f"[EMPTY RESULT DETECTED] '{model_name}' reported completion but produced no real content (likely a mid-task rate-limit swallowed by the planning pipeline) -- retrying with '{refused_tier}' excluded, next tier up.")
        return query_sabrina_crewai(instruction, vuln_type=vuln_type, _exclude_tiers=exclude_tiers | {refused_tier})

    if _looks_like_missing_code(result_str) and len(exclude_tiers) < 2:
        refused_tier = "gemini" if "gemini" in model_name else ("claude" if "claude" in model_name else "unknown")
        print_system(f"[MISSING CODE DETECTED] '{model_name}' described the solution in prose instead of delivering a ```python code block -- retrying with '{refused_tier}' excluded, next tier up.")
        return query_sabrina_crewai(instruction, vuln_type=vuln_type, _exclude_tiers=exclude_tiers | {refused_tier})

    if _looks_like_incomplete_plugin(result_str) and len(exclude_tiers) < 2:
        refused_tier = "gemini" if "gemini" in model_name else ("claude" if "claude" in model_name else "unknown")
        print_system(f"[INCOMPLETE PLUGIN DETECTED] '{model_name}' returned a code block missing TOOL_NAME/TOOL_PROMPT/execute() -- retrying with '{refused_tier}' excluded, next tier up.")
        return query_sabrina_crewai(instruction, vuln_type=vuln_type, _exclude_tiers=exclude_tiers | {refused_tier})

    return f"[CrewAI Model: {model_name}]\n{result_str}"


def query_sabrina_crewai(instruction: str, vuln_type: str = "custom_engine", _exclude_tiers: set = None) -> str:
    """
    Invokes Sabrina configured as a QA test-automation engineer in CrewAI with RAG search.

    Retries once with the next LLM tier excluded if the first attempt looks like a safety
    refusal rather than a real failure or a genuine code-quality miss. Verified live this is a
    real, distinct failure mode: Gemini passes its pre-flight connectivity check every time, then
    refuses the actual task content outright ("Sorry, I cannot fulfill your request...") even
    after the whole agent persona and task template were reworded to neutral QA-engineering
    language -- pre-flight checks connectivity, not whether THIS task will be refused. Without
    this retry, a refusal silently became "no valid Python code block" and the whole attempt was
    wasted instead of falling through to Claude/local, which is exactly the "should try Claude
    next" behavior this was missing.
    """
    exclude_tiers = set(_exclude_tiers or [])
    try:
        from ai_systems.agents.tool_developer import create_tool_developer
        from ai_systems.tasks.engine_tasks import create_build_engine_task
        from crewai import Crew

        agent = create_tool_developer(use_rag=True, exclude_llm_tiers=exclude_tiers)
        task = create_build_engine_task(agent, vuln_type, instruction)
        crew = Crew(agents=[agent], tasks=[task], verbose=True)

        model_name = "unknown"
        if hasattr(agent, "llm") and agent.llm:
            model_name = getattr(agent.llm, "model", "unknown")

        print_system(f"Launching CrewAI Agent with RAG reasoning for [{vuln_type}] using model: {model_name}...")
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            result = executor.submit(crew.kickoff).result()
        result_str = str(result)

        return _validate_crewai_result(result_str, model_name, getattr(task, "description", ""), instruction, vuln_type, exclude_tiers)
    except Exception as e:
        # Live-confirmed 2026-09-17: crew.kickoff() can raise ("Crew Execution Failed", the same
        # unexplained status quirk flagged elsewhere this session) even when a full, valid answer
        # was never at risk of existing -- this branch has no result to lose, it just needs a
        # real fallback. That fallback's output used to skip every quality gate above entirely;
        # now it goes through the same _validate_crewai_result() check so a fallback answer that's
        # a refusal/empty/missing-code/incomplete-plugin gets caught instead of silently saved.
        print_system(f"CrewAI execution encountered error: {e}. Falling back to MultiNode router.")
        fallback_result = query_sabrina_api(instruction, task_type="queen")
        return _validate_crewai_result(fallback_result, "multinode_fallback", "", instruction, vuln_type, exclude_tiers)

def query_sabrina_web_navigator(
    instruction: str,
    target_url: str = None,
    cdp_url: str = "http://localhost:9222",
    credentials: dict = None
) -> str:
    """Invokes Sabrina configured as an Elite Web Security Navigator in CrewAI with Playwright/CDP."""
    try:
        from ai_systems.agents.web_navigator import create_web_navigator
        from ai_systems.tasks.navigation_tasks import create_navigation_task
        from crewai import Crew

        agent = create_web_navigator(cdp_url=cdp_url)
        task = create_navigation_task(
            agent=agent,
            goal=instruction,
            target_url=target_url,
            credentials=credentials,
            cdp_url=cdp_url
        )
        crew = Crew(agents=[agent], tasks=[task], verbose=True)
        
        model_name = "unknown"
        if hasattr(agent, "llm") and agent.llm:
            model_name = getattr(agent.llm, "model", "unknown")
            
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            result = executor.submit(crew.kickoff).result()
        return f"[CrewAI Web Navigator Model: {model_name}]\n{str(result)}"
    except Exception as e:
        print_system(f"CrewAI Web Navigator encountered error: {e}")
        return f"Error executing Web Navigator: {e}"


def _attempt_deterministic_syntax_repair(code: str) -> Optional[str]:
    """
    A small set of SAFE, deterministic (no LLM call, no randomness) fixes for common LLM
    code-generation syntax mistakes, grounded in three real errors observed in this project's
    own juiceshop-framing-trial batch (2026-09-18, qwen2.5:7b): a stray space splitting a
    function name, unescaped nested double-quotes inside a string literal, and an unmatched
    closing bracket in a truncated literal. Each candidate fix is only accepted if the result
    actually re-parses via ast.parse() -- never applied blindly, never guessed at semantically.
    Returns the repaired code, or None if nothing in this small set fixed it.
    """
    import ast
    lines = code.split("\n")

    # Fix 1: "def name1 name2(" -> "def name1name2(" -- a space here is never valid Python,
    # always safe to join. Real example: "def check_for_success Signal(response):"
    def_fix = re.sub(r"(\bdef\s+\w+)\s+(\w+\s*\()", r"\1\2", code)
    if def_fix != code:
        try:
            ast.parse(def_fix)
            return def_fix
        except SyntaxError:
            pass

    # Fix 2: unescaped nested "..." inside a "..." literal -- widen the OUTER delimiters on
    # the offending line to triple-quotes, which tolerate a single embedded " without escaping.
    # Real example: success_signal = "<script>alert("XSS Test")</script>"
    for i, line in enumerate(lines):
        if line.count('"') >= 4:  # at least two full pairs -- a plausible nested-quote line
            first, last = line.find('"'), line.rfind('"')
            if first != -1 and last != -1 and first != last:
                candidate_line = line[:first] + '"""' + line[first + 1:last] + '"""' + line[last + 1:]
                candidate = "\n".join(lines[:i] + [candidate_line] + lines[i + 1:])
                try:
                    ast.parse(candidate)
                    return candidate
                except SyntaxError:
                    continue

    # Fix 3: a single unmatched closing bracket/brace/paren on one line -- try dropping the
    # LAST one on that line, a common pattern for a truncated/malformed literal. Real example:
    # expected_success_signal = '{"status":"error","message":"Incorrect'}
    pairs = {"}": "{", ")": "(", "]": "["}
    for closer, opener in pairs.items():
        for i, line in enumerate(lines):
            if line.count(closer) > line.count(opener):
                idx = line.rfind(closer)
                candidate_line = line[:idx] + line[idx + 1:]
                candidate = "\n".join(lines[:i] + [candidate_line] + lines[i + 1:])
                try:
                    ast.parse(candidate)
                    return candidate
                except SyntaxError:
                    continue

    # Fix 4: a trailing stray bracket that raw counting misses because a matching bracket
    # CHARACTER happens to also sit inside an adjacent string literal's own content (so the
    # per-line count looks balanced even though the trailing one is genuinely dangling). Real
    # example: expected_success_signal = '{"status":"error","message":"Incorrect'}  -- the "{"
    # inside the string content balances the count against the real, stray "}" at the end.
    # Only tries the line's LAST character, and only when the trimmed line still parses on its
    # own -- same safety net (ast.parse of the whole candidate) as every other fix here.
    for i, line in enumerate(lines):
        stripped = line.rstrip()
        if stripped and stripped[-1] in pairs:
            candidate_line = line[:len(stripped) - 1] + line[len(stripped):]
            candidate = "\n".join(lines[:i] + [candidate_line] + lines[i + 1:])
            try:
                ast.parse(candidate)
                return candidate
            except SyntaxError:
                continue

    return None


def query_sabrina_crewai_general(instruction: str, target_path: str, _exclude_tiers: set = None) -> str:
    """Build a GENERAL standalone Python module (not a vuln-scanner engine) via the same CrewAI
    engineer agent, but with the general module task template. Added 2026-09-20 so delegate_to_engineer
    can produce a correct standalone tool (e.g. an offline DB-analysis script) instead of forcing the
    web-scanner format. Returns the raw crew output; the caller extracts + validates + RUNS it.
    """
    exclude_tiers = set(_exclude_tiers or [])
    try:
        from ai_systems.agents.tool_developer import create_tool_developer
        from ai_systems.tasks.engine_tasks import create_build_module_task
        from crewai import Crew

        agent = create_tool_developer(use_rag=True, exclude_llm_tiers=exclude_tiers)
        task = create_build_module_task(agent, instruction, target_path)
        crew = Crew(agents=[agent], tasks=[task], verbose=True)
        model_name = getattr(getattr(agent, "llm", None), "model", "unknown")
        print_system(f"Launching CrewAI Agent (GENERAL module build) for {target_path} using model: {model_name}...")
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            result = executor.submit(crew.kickoff).result()
        # NB: deliberately skip _validate_crewai_result here -- its checks are vuln-plugin-specific
        # (e.g. "missing TOOL_NAME/execute()") and would wrongly flag a correct general module.
        return str(result)
    except Exception as e:
        return f"[general module build error] {e}"


def extract_and_validate_code(response_text: str, save_path: str = None):
    """Extracts python code from ```python ... ``` fences and verifies syntax."""
    matches = re.findall(r"```python\s*(.*?)\s*```", response_text, re.DOTALL)
    if not matches:
        return None, "No python code block found"

    code = matches[-1].strip()
    if save_path:
        os.makedirs(os.path.dirname(save_path), exist_ok=True)
        with open(save_path, "w", encoding="utf-8") as f:
            f.write(code)
        try:
            py_compile.compile(save_path, doraise=True)
            return code, f"Valid Python syntax! Saved to {save_path}"
        except Exception as e:
            repaired = _attempt_deterministic_syntax_repair(code)
            if repaired:
                with open(save_path, "w", encoding="utf-8") as f:
                    f.write(repaired)
                return repaired, f"Syntax auto-repaired deterministically (was: {e}). Saved to {save_path}"
            return code, f"Syntax validation failed: {e}"
    return code, "Code extracted"


def discover_bounty_programs(platform: str = "bugcrowd", api_response_body: str = "") -> str:
    """
    Parse a bounty platform's API/HTML response and extract enrolled programs.
    Returns JSON list of program skeletons suitable for scope-file authoring.

    Usage by Sabrina: call this after api_request to fetch Bugcrowd's /api/v1/researcher/engagements.
    The response gets auto-parsed into program names, URLs, and basic metadata. Sabrina then
    surfaces the list via ask_human with a pointer to config/engagements/TEMPLATE.json, so the
    human can review each program's actual policy and author a real scope file.
    """
    try:
        from tools.bounty_discovery import discover_bugcrowd_programs, discover_hackerone_programs

        platform = platform.lower().strip()
        if platform in ("bugcrowd", ""):
            programs = discover_bugcrowd_programs(api_response_body)
        elif platform == "hackerone":
            programs = discover_hackerone_programs(api_response_body)
        else:
            return f"Unknown platform: {platform}. Supported: bugcrowd, hackerone."

        if not programs:
            return f"No programs found in the response for platform '{platform}'."

        return json.dumps(programs, indent=2)
    except Exception as e:
        return f"Error parsing {platform} discovery response: {e}"
