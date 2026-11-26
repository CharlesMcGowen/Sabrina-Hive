#!/usr/bin/env python3
import concurrent.futures
import json
import os
import re
import subprocess
import urllib.error
import urllib.request

env_text = open("/home/ego/bot/Sabrina-windows-sync/.env", encoding="utf-8", errors="ignore").read()
gkey = re.search(r"GEMINI_API_KEY=([^\s#]+)", env_text).group(1).strip()

print("=== 1. Searching /home/ego for any other API keys in .env files ===")
try:
    out = subprocess.check_output(
        "find /home/ego -maxdepth 3 -name '.env*' -o -name '*config*.json' 2>/dev/null",
        shell=True,
        text=True,
    )
    for path in out.splitlines():
        if not os.path.isfile(path):
            continue
        try:
            txt = open(path, encoding="utf-8", errors="ignore").read()
            found = re.findall(r"^([A-Z0-9_]*(?:KEY|TOKEN|SECRET)[A-Z0-9_]*)\s*=", txt, re.M)
            if found:
                print(f"  {path}: {found}")
        except Exception:
            pass
except Exception as e:
    print("Find err:", e)

print("\n=== 2. Testing Google Interactions API on antigravity-preview models ===")
for ag_model in ["antigravity-preview-latest", "antigravity-preview-09-2026", "antigravity-preview-05-2026"]:
    url = f"https://generativelanguage.googleapis.com/v1beta/interactions?key={gkey}"
    payload = json.dumps({
        "model": f"models/{ag_model}",
        "input": "Reply with OK",
    }).encode("utf-8")
    try:
        r = urllib.request.urlopen(
            urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"}, method="POST"),
            timeout=15,
        )
        body = r.read().decode()
        print(f"  [OK] {ag_model} via Interactions API: {body[:200]}")
    except urllib.error.HTTPError as e:
        print(f"  [HTTP {e.code}] {ag_model}: {e.read().decode()[:160].replace(chr(10), ' ')}")
    except Exception as e:
        print(f"  [ERR] {ag_model}: {e}")

print("\n=== 3. Scanning HuggingFace Live Gradio LLM Chat Spaces in Parallel ===")
queries = ["qwen", "deepseek", "llama", "chat", "mistral", "gemma", "coder", "gpt", "claude", "glm"]
candidate_spaces = {}
for q in queries:
    try:
        u = f"https://huggingface.co/api/spaces?search={q}&filter=gradio&sort=likes&direction=-1&limit=40"
        req = urllib.request.urlopen(urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0"}), timeout=10)
        for s in json.loads(req.read().decode()):
            sid = s.get("id", "")
            if sid and sid not in candidate_spaces:
                candidate_spaces[sid] = s.get("likes", 0)
    except Exception as e:
        print(f"  Query {q} err: {e}")

print(f"Collected {len(candidate_spaces)} unique candidate Gradio spaces across 10 LLM search queries.")


def check_gradio_space(space_id: str):
    slug = space_id.replace("/", "-").lower()
    base = f"https://{slug}.hf.space"
    for info_path in ["/gradio_api/info", "/info"]:
        try:
            req = urllib.request.urlopen(
                urllib.request.Request(base + info_path, headers={"User-Agent": "Mozilla/5.0"}),
                timeout=6,
            )
            data = json.loads(req.read().decode())
            named = data.get("named_endpoints", {})
            if named:
                return {
                    "space_id": space_id,
                    "base_url": base,
                    "info_path": info_path,
                    "endpoints": list(named.keys())[:6],
                }
        except Exception:
            pass
    return None


live_gradio_endpoints = []
with concurrent.futures.ThreadPoolExecutor(max_workers=24) as ex:
    futs = {ex.submit(check_gradio_space, sid): sid for sid in list(candidate_spaces.keys())[:150]}
    for f in concurrent.futures.as_completed(futs):
        res = f.result()
        if res:
            live_gradio_endpoints.append(res)

print(f"Found {len(live_gradio_endpoints)} LIVE Gradio Spaces with exposed API endpoints!")
for item in live_gradio_endpoints[:25]:
    print(f"  [LIVE GRADIO] {item['space_id']} ({item['base_url']}{item['info_path']}) -> {item['endpoints']}")
