#!/usr/bin/env python3
"""
generate_detection_rules.py
============================
Has Sabrina write her own detection_rules.json by:

  1. Querying AkashicMemory (episodic RAG) for all successful payloads
     grouped by vuln_type — this is Sabrina's own lived experience.
  2. Querying SwarmMetaLearner for cross-site pattern confidence scores.
  3. Feeding that evidence into a capable model (default: dolphin-mixtral
     or whatever is available) with a structured prompt.
  4. Parsing the model output and writing it to config/detection_rules.json.

The rules file is then hot-reloadable by the detection engine mid-run.
Sabrina should re-run this after every major scan to update her patterns
from new findings.

Usage:
    python generate_detection_rules.py
    python generate_detection_rules.py --model dolphin-mixtral:latest
    python generate_detection_rules.py --model llama3.3:latest --dry-run
"""

import sys
import json
import re
import os
import argparse
import urllib.request
import urllib.error
import pathlib
import datetime
import paramiko

# ── paths ────────────────────────────────────────────────────────────────────
PROJECT_ROOT  = pathlib.Path(__file__).parent
RULES_OUT     = PROJECT_ROOT / "config" / "detection_rules.json"
OLLAMA_URL    = "http://127.0.0.1:11434/api/chat"

# ── remote config ────────────────────────────────────────────────────────────
LAB_HOST = "192.168.2.69"
LAB_USER = "ego"
LAB_PASS = "ego"
REMOTE_STORAGE_DIR = "/home/ego/sabrina-coven/ai_systems/personalities/pentest/sabrina/swarm/storage"
REMOTE_MEMORY_FILE = f"{REMOTE_STORAGE_DIR}/swarm_memory.jsonl"
REMOTE_META_FILE = f"{REMOTE_STORAGE_DIR}/meta_learner.json"

# ── helpers ──────────────────────────────────────────────────────────────────

def load_akashic_memory() -> list[dict]:
    """Load all episodic entries from remote AkashicMemory JSONL via SSH."""
    entries = []
    print(f"  [SSH] Connecting to {LAB_HOST}...")
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(LAB_HOST, username=LAB_USER, password=LAB_PASS, timeout=10, look_for_keys=False, allow_agent=False)
        sftp = client.open_sftp()
        try:
            with sftp.open(REMOTE_MEMORY_FILE, "r") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            entries.append(json.loads(line))
                        except Exception:
                            pass
            print(f"  [RAG] Loaded {len(entries)} episodic entries from remote AkashicMemory")
        except IOError:
            print(f"  [WARN] No AkashicMemory found at {REMOTE_MEMORY_FILE} on {LAB_HOST}")
        sftp.close()
    except Exception as e:
        print(f"  [ERROR] SSH connection failed: {e}")
    finally:
        client.close()
    return entries


def load_meta_learner() -> dict:
    """Load SwarmMetaLearner pattern bank via SSH."""
    data = {}
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(LAB_HOST, username=LAB_USER, password=LAB_PASS, timeout=10, look_for_keys=False, allow_agent=False)
        sftp = client.open_sftp()
        try:
            with sftp.open(REMOTE_META_FILE, "r") as f:
                data = json.load(f)
            print(f"  [RAG] Loaded MetaLearner with {len(data)} profiles from {LAB_HOST}")
        except IOError:
            print(f"  [WARN] No MetaLearner file found at {REMOTE_META_FILE} on {LAB_HOST}")
        sftp.close()
    except Exception as e:
        print(f"  [ERROR] SSH connection failed: {e}")
    finally:
        client.close()
    return data


def summarise_memory(entries: list[dict]) -> dict:
    """
    Group successful entries by vuln_type.
    Returns: {vuln_type: [{"payload": ..., "evidence": ..., "host": ...}, ...]}
    """
    grouped: dict[str, list[dict]] = {}
    for e in entries:
        if not e.get("success"):
            continue
        vt = e.get("vuln_type", "unknown")
        grouped.setdefault(vt, []).append({
            "payload":  e.get("payload", ""),
            "evidence": e.get("evidence", ""),
            "host":     e.get("host", ""),
            "waf":      e.get("waf", ""),
            "tech":     e.get("tech", {}),
        })
    for vt, items in grouped.items():
        print(f"  [RAG]   {vt}: {len(items)} successful probes in memory")
    return grouped


def build_prompt(grouped: dict, meta: dict) -> str:
    """
    Build the generation prompt Sabrina will reason over.
    Includes her full episodic memory of what worked, plus meta-learner patterns.
    """
    rag_section = ""
    for vt, items in grouped.items():
        rag_section += f"\n### {vt.upper()} ({len(items)} confirmed successes)\n"
        for i, item in enumerate(items[:20]):   # cap at 20 per type for context
            rag_section += (
                f"  [{i+1}] host={item['host']} waf={item['waf'] or 'none'}\n"
                f"       payload={item['payload'][:200]}\n"
                f"       evidence={item['evidence'][:200]}\n"
            )

    meta_section = ""
    if meta:
        meta_section = "\n### CROSS-SITE META-LEARNER PATTERNS\n"
        for profile_key, profile in list(meta.items())[:10]:
            meta_section += f"  Profile: {profile_key}\n"
            patterns = profile.get("patterns", [])
            for p in patterns[:5]:
                meta_section += f"    confidence={p.get('confidence', '?')} pattern={p.get('pattern', '')[:150]}\n"

    prompt = f"""You are Sabrina, an elite autonomous penetration tester with extensive memory of past exploits.

Below is your own episodic memory (AkashicMemory RAG) showing every vulnerability you have confirmed across all prior runs, grouped by vuln type. This is YOUR experience — real payloads you sent, real evidence you received.

{rag_section}
{meta_section}

Your task: Generate a comprehensive detection_rules.json file that covers ALL vuln types in your memory.

For EACH vuln type, create one or more rules. A rule MUST have:
  - id: unique snake_case identifier
  - vuln_type: exact vuln type string
  - endpoint_pattern: regex matching the request path (use \\d+ for ID params)
  - method: GET | POST | PUT | DELETE | ANY
  - severity: critical | high | medium | low | info
  - require_code: list of HTTP status codes that must match (e.g. [200])
  - confirm_any: list of regex patterns where ANY match confirms exploitation
    * Base these on real evidence strings from your memory above
    * Be SPECIFIC — match field names, JSON structure, tokens, not generic words
    * Use proper JSON string escaping (double-escape backslashes: \\\\d+ not \\d+)
  - exclude: list of regex patterns that discard false positives
    * Error messages, encoded output, rate limit responses
  - fallback_to_model: true if ambiguous, false if deterministic
  - notes: one-sentence description of what confirms success

Rules:
1. Do NOT use generic patterns like "email" alone — require structure: "\\"email\\"\\\\s*:\\\\s*\\\"[^\\"]+@[^\\"]+\\\"" 
2. Generate at least 3 confirm_any patterns per rule from real evidence in your memory
3. Generate multiple rules for the same vuln_type if they cover different endpoints
4. Include rules for every vuln_type you see in your memory, even if only 1 success
5. Output ONLY valid JSON. No markdown fences. No explanation. No extra text.

Output format (extend this with ALL your rules):
{{
  "_meta": {{
    "version": "2.0",
    "owner": "sabrina",
    "generated": "{datetime.datetime.now().isoformat()}",
    "generated_from": "AkashicMemory + SwarmMetaLearner",
    "rag_entries_used": {sum(len(v) for v in grouped.values())},
    "fallback_model": "llama3.3:latest",
    "description": "Auto-generated by Sabrina from her episodic RAG memory. Hot-reloadable."
  }},
  "rules": [
    {{
      "id": "example_rule",
      "vuln_type": "sqli",
      "endpoint_pattern": "/rest/user/login",
      "method": "POST",
      "severity": "critical",
      "require_code": [200],
      "confirm_any": ["\\\"token\\\"\\\\s*:", "\\\"authentication\\\"\\\\s*:"],
      "exclude": ["Wrong email", "Invalid"],
      "fallback_to_model": true,
      "notes": "SQLi auth bypass confirmed by JWT token in response body."
    }}
    ... (ADD ALL RULES HERE based on your memory above)
  ]
}}
"""
    return prompt


def call_model(model: str, prompt: str, stream: bool = True) -> str:
    """Call Ollama with streaming output. Returns full content."""
    payload = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are Sabrina, an elite autonomous penetration tester. "
                    "You output ONLY valid JSON. Never markdown. Never explanations. "
                    "Never fences. Pure JSON only."
                )
            },
            {"role": "user", "content": prompt}
        ],
        "stream": stream,
        "options": {"temperature": 0.15, "num_predict": 8192}
    }
    req = urllib.request.Request(
        OLLAMA_URL,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}
    )
    import sys
    sys.stdout.reconfigure(encoding='utf-8')
    full = []
    print(f"\n  [MODEL] Streaming from {model} ...\n" + "-" * 60)
    try:
        with urllib.request.urlopen(req, timeout=600) as r:
            for line in r:
                if not line:
                    continue
                try:
                    chunk = json.loads(line.decode())
                    content = chunk.get("message", {}).get("content", "")
                    if content:
                        print(content, end="", flush=True)
                        full.append(content)
                    if chunk.get("done"):
                        break
                except Exception:
                    pass
    except urllib.error.URLError as e:
        print(f"\n  [ERROR] Ollama unreachable: {e}")
        sys.exit(1)
    print("\n" + "-" * 60)
    return "".join(full)


def parse_rules_json(raw: str) -> dict | None:
    """
    Extract valid JSON from model output.
    Handles accidental markdown fences or leading text.
    """
    # Strip markdown fences
    clean = re.sub(r"```json\s*", "", raw)
    clean = re.sub(r"```\s*", "", clean)
    clean = clean.strip()

    # Try direct parse
    try:
        return json.loads(clean)
    except json.JSONDecodeError:
        pass

    # Try to find JSON object in output
    match = re.search(r'\{[\s\S]+\}', clean)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError as e:
            print(f"\n  [ERROR] JSON parse failed: {e}")
    return None


def list_models() -> list[str]:
    """Query Ollama for available models."""
    try:
        req = urllib.request.Request("http://127.0.0.1:11434/api/tags")
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read().decode())
            return [m["name"] for m in data.get("models", [])]
    except Exception:
        return []


# ── main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Have Sabrina generate detection_rules.json from her RAG memory"
    )
    parser.add_argument("--model", default=None,
                        help="Ollama model name (default: auto-select best available)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print prompt and exit without calling model")
    parser.add_argument("--rules-out", default=str(RULES_OUT),
                        help=f"Output path (default: {RULES_OUT})")
    args = parser.parse_args()

    print("=" * 60)
    print("  Sabrina — Detection Rules Generator")
    print("  Source: AkashicMemory RAG + SwarmMetaLearner")
    print("=" * 60)

    # 1. Load RAG
    entries = load_akashic_memory()
    meta    = load_meta_learner()
    grouped = summarise_memory(entries)

    if not grouped:
        print("\n  [WARN] No successful probes in AkashicMemory yet.")
        print("         Run a live pentest first to populate memory,")
        print("         then re-run this script to generate data-driven rules.")
        print("\n  Falling back to prompt with empty memory...")

    # 2. Build prompt
    prompt = build_prompt(grouped, meta)

    if args.dry_run:
        print("\n[DRY RUN] Prompt that would be sent:\n")
        print(prompt)
        return

    # 3. Pick model
    model = args.model
    if not model:
        available = list_models()
        print(f"\n  [MODELS] Available: {available}")
        # Prefer dolphin for uncensored generation, fall back to llama3.3
        preferred = ["dolphin-mixtral", "dolphin2", "llama3.3", "llama3.3:latest", "qwen2.5:7b"]
        model = next((m for p in preferred for m in available if p in m), None)
        if not model and available:
            model = available[0]
        if not model:
            print("  [ERROR] No Ollama models available")
            sys.exit(1)
        print(f"  [MODEL] Auto-selected: {model}")

    # 4. Generate
    raw = call_model(model, prompt)

    # 5. Parse
    rules_data = parse_rules_json(raw)
    if not rules_data:
        print("\n  [ERROR] Could not parse model output as JSON.")
        dump_path = pathlib.Path(args.rules_out).parent / "detection_rules_raw_output.txt"
        dump_path.write_text(raw, encoding="utf-8")
        print(f"  Raw output saved to: {dump_path}")
        sys.exit(1)

    # 6. Validate structure
    rules = rules_data.get("rules", [])
    print(f"\n  [PARSE] Extracted {len(rules)} rules from model output")
    for r in rules:
        missing = [k for k in ("id", "vuln_type", "endpoint_pattern", "confirm_any") if k not in r]
        if missing:
            print(f"  [WARN] Rule {r.get('id', '?')} missing fields: {missing}")

    # 7. Write
    out_path = pathlib.Path(args.rules_out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(rules_data, indent=2), encoding="utf-8")
    print(f"\n  [DONE] Written {len(rules)} rules → {out_path}")
    print(f"  The detection engine will hot-reload these on next engine.reload_rules() call.")


if __name__ == "__main__":
    main()
