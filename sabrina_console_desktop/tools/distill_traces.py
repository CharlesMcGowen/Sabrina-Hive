"""distill_traces.py -- generate multi-class pentest REASONING traces by distilling a strong teacher
(Qwen) into training data. This is the long pole for building a "thinking" model: the SFT corpus was 5
SQLi scenarios; this produces many DISTINCT scenarios across many vuln classes, each with a
<think>...</think> reasoning block + exactly one <action>, in the same schema train_sft.py consumes.

Distillation, not "from scratch": a teacher model invents diverse scenarios and solves them with
structured reasoning; a smaller student later learns from these traces. Two phases per record:
  1. INVENT  -- teacher proposes a NOVEL, concrete, in-scope scenario for a vuln class, told to differ
     from everything already generated (diversity pressure -> unique scenarios, not augmentation).
  2. SOLVE   -- teacher produces the <think> reasoning + one <action> for that scenario.

Every record is validated (well-formed <think>+<action>; code actions must compile) and deduped by
scenario hash. Provenance (teacher, phase prompts hash, timestamp) is stamped so the corpus is
auditable -- the experts flagged the old "verified traces" as unauditable; this fixes that.

Teacher endpoint is OpenAI-compatible (vLLM's server) or ollama /api/chat, selected by env:
  DISTILL_TEACHER_URL   (default http://localhost:8000/v1  -> vLLM OpenAI server)
  DISTILL_TEACHER_MODEL (default Qwen/Qwen2.5-72B-Instruct)
  DISTILL_TEACHER_KIND  (openai | ollama; default openai)

CLI:
  python tools/distill_traces.py gen --classes sqli,rce,xss --per-class 20 --out data/distill_sft.jsonl
  python tools/distill_traces.py gen --smoke                       # 1 per class, quick sanity
  python tools/distill_traces.py validate data/distill_sft.jsonl   # re-check an existing corpus
  python tools/distill_traces.py classes                           # list supported vuln classes
"""
import argparse
import hashlib
import json
import os
import re
import sys
import time
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# ── Vuln classes: label + what a scenario for it looks like. Aligned with Charles's targets and the
# 20-class HackerOne label set. `modality` is the default action shape the teacher should emit. ──────
VULN_CLASSES = {
    "sqli":            ("SQL Injection", "payload",
                        "a web/API endpoint that concatenates user input into a SQL query"),
    "rce":             ("Command Injection / RCE", "payload",
                        "an endpoint that passes user input into a shell command or eval"),
    "xss":             ("Cross-site Scripting", "payload",
                        "a field whose value is reflected/stored into HTML without encoding"),
    "ssrf":            ("Server-Side Request Forgery", "payload",
                        "a feature that fetches a user-supplied URL server-side"),
    "idor":            ("Insecure Direct Object Reference", "analysis",
                        "an endpoint that returns a record by id without checking ownership"),
    "path_traversal":  ("Path Traversal", "payload",
                        "a file/download endpoint that builds a path from user input"),
    "ssti":            ("Server-Side Template Injection", "payload",
                        "user input rendered inside a server-side template"),
    "open_redirect":   ("Open Redirect", "payload",
                        "a redirect endpoint that trusts a user-supplied next/return URL"),
    "http_smuggling":  ("HTTP Request Smuggling", "analysis",
                        "a front-end/back-end pair that may disagree on request boundaries (CL.TE/TE.CL)"),
    "xxe":             ("XML External Entity", "payload",
                        "an endpoint that parses user-supplied XML with external entities enabled"),
    "auth_bypass":     ("Improper Authentication", "analysis",
                        "an auth flow with a logic flaw (token, reset, or session handling)"),
    "python_exploit":  ("Python PoC coding", "code",
                        "a described vulnerability for which to write a self-contained Python PoC script"),
}

SYSTEM_PROMPT = (
    "You are a senior web-application penetration tester writing a training example for a junior "
    "analyst. For the given scenario: reason step by step INSIDE a single <think>...</think> block "
    "(observation, where the flaw is, the mechanism, why your approach works), then give EXACTLY ONE "
    "<action>...</action>. The action is the concrete next step: a payload, a Python PoC, or a precise "
    "analysis verdict, as specified. Stay within an authorized lab engagement: no destructive "
    "statements (no DROP/DELETE/UPDATE/INSERT/stacked writes), do not damage or alter the target, and "
    "do not exfiltrate real third-party data. Be technically correct and specific."
)

INVENT_PROMPT = (
    "Invent ONE novel, concrete, realistic {label} scenario: {shape}.\n"
    "It must be materially DIFFERENT from every scenario in this list of already-used titles:\n{used}\n\n"
    "Give a short JSON object ONLY: {{\"title\": \"<=8-word slug\", \"setup\": \"2-4 sentences: the "
    "endpoint/app, the exact observed behavior a tester sees, and (white-box) the backend detail that "
    "makes it vulnerable\"}}. Make the setup concrete enough to act on. Diversity index {idx}."
)

SOLVE_PROMPT = (
    "Scenario ({label}):\n{setup}\n\n"
    "Produce the training example now: a <think>...</think> reasoning block, then exactly one "
    "<action>...</action>. For modality '{modality}': "
    "'payload' -> the action is the single injection/attack string (plus a one-line where-it-goes); "
    "'code' -> the action is a complete runnable Python PoC in a ```python fenced block; "
    "'analysis' -> the action is a precise verdict naming the flaw, the confirming signal, and the safe "
    "next probe. Output ONLY the <think> block followed by the <action> block."
)


# ── teacher call ─────────────────────────────────────────────────────────────────────────────────
def _cfg():
    return (os.environ.get("DISTILL_TEACHER_URL", "http://localhost:8000/v1").rstrip("/"),
            os.environ.get("DISTILL_TEACHER_MODEL", "Qwen/Qwen2.5-72B-Instruct"),
            os.environ.get("DISTILL_TEACHER_KIND", "openai"))


def call_teacher(messages, temperature=0.7, max_tokens=1200, timeout=300):
    """Hit an OpenAI-compatible (/v1/chat/completions) or ollama (/api/chat) endpoint. Non-streamed;
    for a localhost teacher there is no proxy timeout to dodge."""
    url, model, kind = _cfg()
    if kind == "ollama":
        endpoint = url + "/api/chat"
        body = {"model": model, "messages": messages, "stream": False,
                "options": {"temperature": temperature, "num_predict": max_tokens}}
    else:
        endpoint = url + "/chat/completions"
        body = {"model": model, "messages": messages, "temperature": temperature,
                "max_tokens": max_tokens, "stream": False}
    req = urllib.request.Request(endpoint, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "User-Agent": "Sabrina"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read().decode("utf-8", "replace"))
    if kind == "ollama":
        return (data.get("message", {}) or {}).get("content", "")
    return data["choices"][0]["message"]["content"]


# ── parsing / validation ───────────────────────────────────────────────────────────────────────
def _norm(s):
    return re.sub(r"\s+", " ", str(s or "")).strip().lower()


def extract_think_action(text):
    """Return (think, action) or (None, None) if not well-formed."""
    tm = re.search(r"<think>(.*?)</think>", text or "", re.DOTALL)
    am = re.search(r"<action>(.*?)</action>", text or "", re.DOTALL)
    if not tm or not am:
        return None, None
    think, action = tm.group(1).strip(), am.group(1).strip()
    if len(think) < 20 or len(action) < 3:
        return None, None
    return think, action


def code_compiles(action):
    """Best-effort: if the action carries a ```python block, does it compile?"""
    m = re.search(r"```python\s*(.*?)```", action, re.DOTALL)
    src = m.group(1) if m else action
    try:
        compile(src, "<distill_action>", "exec")
        return True
    except SyntaxError:
        return False


def validate_record(rec):
    """Return (ok, reason)."""
    msgs = rec.get("messages", [])
    if len(msgs) != 3 or [m["role"] for m in msgs] != ["system", "user", "assistant"]:
        return False, "bad message structure"
    think, action = extract_think_action(msgs[2]["content"])
    if think is None:
        return False, "missing/short <think> or <action>"
    if rec.get("modality") == "code" and not code_compiles(action):
        return False, "python action does not compile"
    return True, "ok"


# ── generation ───────────────────────────────────────────────────────────────────────────────────
def _title_hash(title):
    return hashlib.sha1(_norm(title).encode()).hexdigest()[:8]


def invent_scenario(cls, used_titles, idx):
    label, _, shape = VULN_CLASSES[cls]
    used = "\n".join(f"- {t}" for t in used_titles[-40:]) or "- (none yet)"
    raw = call_teacher(
        [{"role": "system", "content": "You invent concise, diverse security testing scenarios."},
         {"role": "user", "content": INVENT_PROMPT.format(label=label, shape=shape, used=used, idx=idx)}],
        temperature=0.9, max_tokens=400)
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
        if obj.get("title") and obj.get("setup"):
            return {"title": obj["title"].strip(), "setup": obj["setup"].strip()}
    except Exception:
        pass
    return None


def solve_scenario(cls, scenario):
    label, modality, _ = VULN_CLASSES[cls]
    user = SOLVE_PROMPT.format(label=label, setup=scenario["setup"], modality=modality)
    completion = call_teacher(
        [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user}],
        temperature=0.5, max_tokens=1200)
    return modality, user, completion


def gen_corpus(classes, per_class, out_path, teacher_name, verbose=True):
    """per_class is an int (uniform) OR a dict {class: target_count} -- the latter is what an RSI-forged
    plan (tools/rsi_distill.py) produces, so round N+1's allocation is decided by round N's outcomes."""
    def _target(cls):
        return per_class[cls] if isinstance(per_class, dict) else per_class
    seen_titles = {}     # per-class list of used titles (diversity pressure)
    seen_hashes = set()  # global dedup
    records, stats = [], {"attempted": 0, "kept": 0, "rejected": 0, "per_class": {}}
    for cls in classes:
        seen_titles.setdefault(cls, [])
        target = _target(cls)
        made = 0
        attempts = 0
        while made < target and attempts < target * 3:
            attempts += 1
            stats["attempted"] += 1
            try:
                scenario = invent_scenario(cls, seen_titles[cls], made)
                if not scenario:
                    stats["rejected"] += 1
                    continue
                h = _title_hash(scenario["title"] + scenario["setup"][:60])
                if h in seen_hashes:
                    stats["rejected"] += 1
                    continue
                modality, user, completion = solve_scenario(cls, scenario)
                rec = {
                    "task_id": f"{cls}_{h}",
                    "vuln_class": cls,
                    "modality": modality,
                    "scenario_title": scenario["title"],
                    "messages": [
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": f"{scenario['setup']}"},
                        {"role": "assistant", "content": completion.strip()},
                    ],
                    "teacher": teacher_name,
                    "provenance": {"phase": "invent+solve", "ts": int(time.time())},
                }
                ok, reason = validate_record(rec)
                if not ok:
                    stats["rejected"] += 1
                    if verbose:
                        print(f"  [{cls}] reject ({reason}): {scenario['title']}", flush=True)
                    continue
                seen_titles[cls].append(scenario["title"])
                seen_hashes.add(h)
                records.append(rec)
                made += 1
                stats["kept"] += 1
                if verbose:
                    print(f"  [{cls}] kept {made}/{per_class}: {scenario['title']}", flush=True)
            except Exception as e:
                stats["rejected"] += 1
                if verbose:
                    print(f"  [{cls}] error: {e}", flush=True)
        stats["per_class"][cls] = {"attempts": attempts, "kept": made}
        if verbose:
            print(f"[{cls}] done: {made} kept / {attempts} attempts", flush=True)
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")
    # Auto-record per-class outcomes next to the corpus so the RSI dream (tools/rsi_distill.py) can
    # forge the next round's plan from this round's real results -- no hand-written observed file.
    obs_path = str(Path(out_path).with_suffix("")) + ".observed.json"
    Path(obs_path).write_text(json.dumps(stats["per_class"], indent=2), encoding="utf-8")
    return records, stats


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("gen", help="generate a trace corpus")
    g.add_argument("--classes", default=",".join(VULN_CLASSES.keys()))
    g.add_argument("--per-class", type=int, default=20)
    g.add_argument("--plan", default="", help="path to an RSI-forged plan (rsi_distill current_plan.json); "
                   "its next_round_counts override --per-class, closing the dream->generate loop")
    g.add_argument("--out", default=str(REPO_ROOT / "data" / "distill_sft.jsonl"))
    g.add_argument("--smoke", action="store_true", help="1 per class, quick sanity check")
    v = sub.add_parser("validate", help="re-validate an existing corpus")
    v.add_argument("path")
    sub.add_parser("classes", help="list supported vuln classes")
    a = ap.parse_args()

    if a.cmd == "classes":
        for k, (label, modality, shape) in VULN_CLASSES.items():
            print(f"  {k:16s} {label:34s} [{modality}]  {shape}")
        return

    if a.cmd == "validate":
        recs = [json.loads(l) for l in open(a.path, encoding="utf-8") if l.strip()]
        ok = sum(validate_record(r)[0] for r in recs)
        from collections import Counter
        byc = Counter(r.get("vuln_class") for r in recs)
        uniq = len({r.get("task_id") for r in recs})
        print(f"records={len(recs)} valid={ok} unique_task_ids={uniq}")
        print("per class:", dict(byc))
        return

    if a.cmd == "gen":
        _, teacher_name, _ = _cfg()
        classes = [c.strip() for c in a.classes.split(",") if c.strip() in VULN_CLASSES]
        if a.plan:
            plan = json.loads(Path(a.plan).read_text(encoding="utf-8"))
            per = plan["next_round_counts"]                       # RSI-forged per-class allocation
            classes = [c for c in classes if c in per]
            print(f"[distill] RSI plan '{plan.get('deployed_policy')}' drives allocation "
                  f"(V={plan.get('V')} vs pi0 {plan.get('pi0_V')})", flush=True)
        else:
            per = 1 if a.smoke else a.per_class
        print(f"[distill] teacher={teacher_name} classes={classes} alloc={per} -> {a.out}", flush=True)
        recs, stats = gen_corpus(classes, per, a.out, teacher_name)
        print(f"\n[distill] kept={stats['kept']} rejected={stats['rejected']} "
              f"attempted={stats['attempted']} -> {a.out}", flush=True)


if __name__ == "__main__":
    main()
