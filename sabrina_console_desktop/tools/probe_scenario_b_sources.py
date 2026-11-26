#!/usr/bin/env python3
import json
import re
import urllib.error
import urllib.request

env_text = open("/home/ego/bot/Sabrina-windows-sync/.env", encoding="utf-8", errors="ignore").read()
gkey = re.search(r"GEMINI_API_KEY=([^\s#]+)", env_text).group(1).strip()
akey_m = re.search(r"ANTHROPIC_API_KEY=([^\s#]+)", env_text)
akey = akey_m.group(1).strip() if akey_m else ""
rkey_m = re.search(r"RUNPOD_API_KEY=([^\s#]+)", env_text)
rkey = rkey_m.group(1).strip() if rkey_m else ""

print("Anthropic key prefix:", akey[:12] if akey else "None")
print("Runpod key prefix:", rkey[:12] if rkey else "None")

# Test Anthropic
if akey:
    try:
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/models",
            headers={"x-api-key": akey, "anthropic-version": "2023-06-01"},
        )
        resp = urllib.request.urlopen(req, timeout=10)
        print("Anthropic models:", resp.read().decode()[:300])
    except urllib.error.HTTPError as e:
        print("Anthropic HTTP err:", e.code, e.read().decode()[:200])
    except Exception as e:
        print("Anthropic err:", e)

# Test all text/omni/flash/pro/gemma models on Google Cloud to see which ones respond HTTP 200
req = urllib.request.urlopen(f"https://generativelanguage.googleapis.com/v1beta/models?key={gkey}", timeout=10)
gmodels = [
    x["name"].replace("models/", "")
    for x in json.loads(req.read().decode()).get("models", [])
    if "generateContent" in x.get("supportedGenerationMethods", [])
]

working_google = []
for m in gmodels:
    if any(skip in m for skip in ["tts", "image", "lyria", "transcribe", "robotics", "computer-use"]):
        continue
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{m}:generateContent?key={gkey}"
    payload = json.dumps({
        "contents": [{"parts": [{"text": "Reply with OK"}]}],
        "generationConfig": {"maxOutputTokens": 256, "temperature": 0.0},
    }).encode("utf-8")
    try:
        r = urllib.request.urlopen(
            urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"}, method="POST"),
            timeout=12,
        )
        data = json.loads(r.read().decode())
        cands = data.get("candidates", [])
        parts = cands[0].get("content", {}).get("parts", []) if cands else []
        print(f"  [OK] {m}: {len(parts)} parts -> {[list(p.keys()) for p in parts]}")
        working_google.append(m)
    except urllib.error.HTTPError as e:
        err_body = e.read().decode()[:120].replace("\n", " ")
        print(f"  [HTTP {e.code}] {m}: {err_body}")
    except Exception as e:
        print(f"  [ERR] {m}: {e}")

print(f"Total working Google Cloud models: {len(working_google)} -> {working_google}")
