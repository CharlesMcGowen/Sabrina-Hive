#!/usr/bin/env python3
"""
tools/juiceshop_agent/generate.py
=================================
STaR "generate" step: run a TEACHER model through multi-step best-of-N rollouts against
live Juice Shop, keep ONLY scoreboard-confirmed solved trajectories as ChatML SFT rows.

FORMAT ALIGNMENT (2026-09-27): the action rides Qwen's OWN pretrained tool-calling grammar
instead of a custom <action> tag. Verified from qwen2.5:3b's live chat template: the model
was trained to read tool signatures from <tools>...</tools> in the system message, emit
calls as <tool_call>{"name","arguments"}</tool_call>, and consume results from
<tool_response>...</tool_response>. Reusing that grammar (rather than a novel <action> tag)
means the SFT reinforces a capability the base already has instead of fighting it, and it
avoids the format-drift bugs a custom tag invites. Our NET-NEW security layer -- <think>
reasoning and <hypothesis vuln="CLASS"> -- lives in the assistant CONTENT, which Qwen emits
right before the tool_call. That is exactly the split: Qwen supplies the transport, we
augment the security semantics on top.

Teacher-agnostic (--teacher). Default is the ABLATED huihui_ai/qwen3-abliterated:14b (won't
refuse exploit generation -- abliteration matters on the TEACHER writing attack traces, not
the student, which the probe showed doesn't refuse). `think:false` is sent so qwen3-class
reasoning models emit content directly instead of burning the budget in a hidden thinking
field (verified failure mode).

Ground truth is reward.py (Juice Shop's own scoreboard). Kept traces go through
trace_recorder.py (JWT-scrubbed, deduped). Requires a freshly-reset target (reset.py) so the
scoreboard starts clean -- sticky solved flags otherwise skip challenges.
"""
import argparse
import json
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

_SABRINA = Path(__file__).resolve().parent.parent.parent
if str(_SABRINA) not in sys.path:
    sys.path.insert(0, str(_SABRINA))

from tools.juiceshop_agent import reward as R          # noqa: E402
from tools.juiceshop_agent import challenges as C       # noqa: E402
from tools.juiceshop_agent import trace_recorder as TR  # noqa: E402
from tools.juiceshop_agent import reset as RS           # noqa: E402

OLLAMA_CHAT = "http://localhost:11434/api/chat"
DEFAULT_TARGET = "http://localhost:3000"
DEFAULT_TEACHER = "huihui_ai/qwen3-abliterated:14b"

# Controlled vuln vocabulary for the <hypothesis vuln="..."> attribute -- the routing key
# that (later) selects the payload arsenal via get_payloads(vuln=...). Matches the engine set.
VULN_CLASSES = [
    "sqli", "xss", "idor", "lfi", "ssrf", "ssti", "open_redirect", "file_upload",
    "csrf", "xxe", "auth_bypass", "rce", "info_disclosure", "misconfig",
]

# The http_request function signature, in the exact JSON-in-<tools> shape Qwen's template
# expects. We inline <tools> in the system message (rather than passing ollama's `tools`
# param) so we control parsing and store the literal native-format text for SFT.
_HTTP_REQUEST_TOOL = {
    "type": "function",
    "function": {
        "name": "http_request",
        "description": "Send ONE HTTP request to the target to attempt the challenge.",
        "parameters": {
            "type": "object",
            "properties": {
                "method": {"type": "string", "enum": ["GET", "POST", "PUT", "DELETE", "PATCH"]},
                "path": {"type": "string", "description": "relative path starting with /"},
                "headers": {"type": "object"},
                "body": {"description": "request body: object, string, or null"},
            },
            "required": ["method", "path"],
        },
    },
}

SYSTEM = (
    "You are an authorized penetration tester solving challenges on OWASP Juice Shop (an "
    "Angular + Express/Node REST app) at {target}. Its REST API is under /rest and /api.\n\n"
    "# Tools\n"
    "You may call one or more functions to assist with the user query.\n"
    "You are provided with function signatures within <tools></tools> XML tags:\n"
    "<tools>\n{tools}\n</tools>\n"
    "For each function call, return a json object with function name and arguments within "
    "<tool_call></tool_call> XML tags:\n"
    '<tool_call>\n{{"name": <function-name>, "arguments": <args-json-object>}}\n</tool_call>\n\n'
    "# How to respond each step\n"
    "Before the tool call, write:\n"
    "  <think>your reasoning about what to try and why</think>\n"
    '  <hypothesis vuln="CLASS">one line naming the vulnerability class you are testing and why</hypothesis>\n'
    f"where CLASS is one of: {', '.join(VULN_CLASSES)}.\n"
    "Then emit exactly one <tool_call> for http_request. After it runs you will see a "
    "<tool_response>. Keep going until the challenge is solved. Paths are relative (start with /)."
)

USER = "CHALLENGE: {name}\nGOAL: {desc}\n\nSolve it. First step now."

_TOOLCALL_RE = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.DOTALL)


def _system_message(target: str) -> str:
    tools_json = json.dumps({"type": "function", "function": _HTTP_REQUEST_TOOL["function"]})
    return SYSTEM.format(target=target, tools=tools_json)


def teacher_chat(model: str, messages: List[Dict[str, Any]], temperature: float,
                 timeout: int = 240) -> str:
    """One teacher generation. `think:false` keeps reasoning models emitting content. Returns
    the raw assistant text (which should contain <think>/<hypothesis>/<tool_call>)."""
    try:
        r = requests.post(OLLAMA_CHAT, json={
            "model": model, "messages": messages, "stream": False, "think": False,
            "options": {"temperature": temperature, "num_predict": 700},
        }, timeout=timeout)
        if r.status_code != 200:
            return ""
        msg = r.json().get("message", {}) or {}
        text = (msg.get("content") or msg.get("thinking") or "").strip()
        # If ollama parsed a structured tool_call (because the model emitted one cleanly),
        # render it back into the literal <tool_call> text so downstream parsing + storage
        # see one consistent native form.
        if "<tool_call>" not in text and msg.get("tool_calls"):
            for tc in msg["tool_calls"]:
                fn = tc.get("function", {})
                args = fn.get("arguments", {})
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except json.JSONDecodeError:
                        pass
                text += ("\n<tool_call>" + json.dumps({"name": fn.get("name"), "arguments": args})
                         + "</tool_call>")
        return text
    except Exception:
        return ""


def parse_tool_call(text: str) -> Optional[Dict[str, Any]]:
    """Extract the http_request arguments from a <tool_call> block. Returns the arguments
    dict (method/path/headers/body), or None."""
    m = _TOOLCALL_RE.search(text)
    if not m:
        return None
    try:
        call = json.loads(m.group(1))
    except json.JSONDecodeError:
        return None
    if call.get("name") != "http_request":
        return None
    args = call.get("arguments")
    return args if isinstance(args, dict) else None


def execute_http_request(args: Dict[str, Any], target: str) -> Tuple[Optional[int], str]:
    method = str(args.get("method") or "GET").upper()
    path = str(args.get("path") or "/")
    if not path.startswith("/"):
        path = "/" + path
    headers = args.get("headers") if isinstance(args.get("headers"), dict) else {}
    body = args.get("body")
    kwargs: Dict[str, Any] = {"headers": headers, "timeout": 10}
    if body is not None and method in ("POST", "PUT", "PATCH", "DELETE"):
        if isinstance(body, (dict, list)):
            kwargs["json"] = body
        else:
            kwargs["data"] = str(body)
    try:
        resp = requests.request(method, f"{target}{path}", **kwargs)
        return resp.status_code, resp.text[:300]
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


def run_episode(model: str, ch: Dict[str, str], target: str,
                max_steps: int, temperature: float,
                detector_out: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """One teacher rollout. Returns {'solved':bool,'messages':[...]} or None if target down.

    messages use native roles: system, user, assistant (content carries
    <think>/<hypothesis>/<tool_call>), and user turns carrying <tool_response>.

    When detector_out is set, EVERY executed step also emits a detector example
    (request+response -> verdict) -- the second projection of the shared rollout stream:
    the step that flips the flag is a positive, every other executed step is a negative."""
    baseline = R.solved_set(target)
    if baseline is None:
        return None
    if ch["key"] in baseline:
        return {"solved": False, "skip": "already_solved", "messages": []}
    messages: List[Dict[str, Any]] = [
        {"role": "system", "content": _system_message(target)},
        {"role": "user", "content": USER.format(name=ch["name"], desc=ch["desc"])},
    ]
    solved_seen = set(baseline)  # cumulative solved set; delta per step attributes flips to requests
    for _ in range(max_steps):
        gen = teacher_chat(model, messages, temperature)
        if not gen:
            break
        messages.append({"role": "assistant", "content": gen})
        args = parse_tool_call(gen)
        if not args:
            messages.append({"role": "user",
                             "content": "No valid <tool_call> for http_request found. Emit exactly one."})
            continue
        status, body = execute_http_request(args, target)
        now = R.newly_solved(target, solved_seen)  # challenges flipped by THIS request
        if now is None:
            return None  # target down mid-episode
        new_this_step = set(now)
        solved_seen |= new_this_step
        solved_now = ch["key"] in new_this_step
        if detector_out:
            # Detector projection, correctly attributed: a request is POSITIVE if it flipped
            # ANY challenge this step (incidental solves included), labeled with that
            # challenge's ground-truth class; otherwise a NEGATIVE. Fixes the earlier
            # mislabel where an incidental solve was recorded as not-vulnerable.
            resp = {"status": status, "body": body}
            if new_this_step:
                for k in new_this_step:
                    vc = C.BY_KEY.get(k, {}).get("vuln_class")
                    TR.record_detection_example(detector_out, request=args, response=resp,
                                                vulnerable=True, vuln_class=vc)
            else:
                TR.record_detection_example(detector_out, request=args, response=resp,
                                            vulnerable=False, vuln_class=None)
        if solved_now:
            return {"solved": True, "messages": messages}
        # Tool result carried in a USER turn (not a "tool" role) so the SFT chat template
        # never has to render role=="tool" -- the <tool_response> text still teaches the
        # observe->refine loop, without depending on tool-role template support.
        messages.append({"role": "user",
                         "content": f"<tool_response>status={status}\n{body}</tool_response>\nNot solved yet; try another approach."})
    return {"solved": False, "messages": messages}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--teacher", default=DEFAULT_TEACHER)
    ap.add_argument("--target", default=DEFAULT_TARGET)
    ap.add_argument("--n", type=int, default=4, help="rollouts per challenge (best-of-N)")
    ap.add_argument("--max-steps", type=int, default=6)
    ap.add_argument("--out", default="juiceshop_solver.jsonl", help="solver corpus (goal->request)")
    ap.add_argument("--detector-out", default="juiceshop_detector.jsonl",
                    help="detector corpus (request+response->verdict); shared rollout stream")
    ap.add_argument("--band", choices=["http_only", "all"], default="all")
    ap.add_argument("--limit", type=int, default=0, help="cap number of challenges (0=all)")
    ap.add_argument("--reset-each", action="store_true",
                    help="recreate the container before each challenge (clean baseline; avoids "
                         "incidental-solve contamination of the target). ~30s per challenge.")
    a = ap.parse_args()

    chset = C.http_only() if a.band == "http_only" else C.all_challenges()
    if a.limit:
        chset = chset[:a.limit]
    temps = [0.3, 0.6, 0.9, 1.1]
    print(f"[gen] teacher={a.teacher} target={a.target} N={a.n} max_steps={a.max_steps} "
          f"challenges={len(chset)} (native tool_call format)", flush=True)

    if R.solved_set(a.target) is None:
        print("[gen] FATAL: target scoreboard unreachable -- is Juice Shop up?", flush=True)
        sys.exit(2)

    solved_challenges = 0
    for ch in chset:
        if a.reset_each:
            if not RS.reset_container(wait=True):
                print(f"[gen] {ch['name']}: reset failed, skipping", flush=True)
                continue
        got = False
        for i in range(a.n):
            ep = run_episode(a.teacher, ch, a.target, a.max_steps, temps[i % len(temps)],
                             detector_out=a.detector_out)
            if ep is None:
                print(f"[gen] {ch['name']}: target down, dropping", flush=True)
                continue
            if ep.get("skip") == "already_solved":
                print(f"[gen] {ch['name']}: already solved in baseline (needs reset), skipping", flush=True)
                got = True
                break
            if ep.get("solved"):
                res = TR.record_solved_conversation(a.out, ch["key"], ep["messages"])
                steps = sum(1 for m in ep["messages"] if m["role"] == "assistant")
                print(f"[gen] {ch['name']}: SOLVED in {steps} step(s) rollout {i} "
                      f"({'recorded' if res['written'] else res['reason']})", flush=True)
                got = True
                break
        if not got:
            print(f"[gen] {ch['name']}: not solved in {a.n} rollouts", flush=True)
        else:
            solved_challenges += 1

    stats = TR.corpus_stats(a.out)
    dstats = TR.detector_stats(a.detector_out)
    print("\n[gen] ===== SUMMARY =====", flush=True)
    print(f"[gen] challenges with a solved trace: {solved_challenges}/{len(chset)}", flush=True)
    print(f"[gen] SOLVER corpus rows: {stats['rows']}  per_challenge: {stats['per_challenge']}", flush=True)
    print(f"[gen] DETECTOR corpus rows: {dstats['rows']} (pos={dstats['positive']} neg={dstats['negative']}) "
          f"per_class: {dstats['per_class']}", flush=True)
    print(f"[gen] solver -> {a.out}   detector -> {a.detector_out}", flush=True)


if __name__ == "__main__":
    main()
