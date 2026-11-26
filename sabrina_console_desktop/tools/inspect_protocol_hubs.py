#!/usr/bin/env python3
import json
import re
import urllib.request

env_text = open("/home/ego/bot/Sabrina-windows-sync/.env", encoding="utf-8", errors="ignore").read()
keys = [line.split("=")[0].strip() for line in env_text.splitlines() if "=" in line and not line.strip().startswith("#")]
print("ENV KEYS:", keys)

m = re.search(r"GEMINI_API_KEY=([^\s#]+)", env_text)
gkey = m.group(1).strip() if m else ""
req = urllib.request.urlopen(f"https://generativelanguage.googleapis.com/v1beta/models?key={gkey}", timeout=10)
gmodels = [
    x["name"].replace("models/", "")
    for x in json.loads(req.read().decode()).get("models", [])
    if "generateContent" in x.get("supportedGenerationMethods", [])
]
print(f"GOOGLE GENERATE_CONTENT MODELS ({len(gmodels)}):", gmodels)

try:
    req2 = urllib.request.urlopen(
        urllib.request.Request("https://text.pollinations.ai/models", headers={"User-Agent": "Mozilla/5.0"}),
        timeout=10,
    )
    pmodels = json.loads(req2.read().decode())
    print(f"POLLINATIONS MODELS ({len(pmodels)}):", [x.get("name") for x in pmodels])
except Exception as e:
    print("POLLINATIONS ERR:", e)

try:
    req3 = urllib.request.urlopen(
        urllib.request.Request("https://openrouter.ai/api/v1/models", headers={"User-Agent": "Mozilla/5.0"}),
        timeout=10,
    )
    ormodels = json.loads(req3.read().decode()).get("data", [])
    free_or = [
        x["id"]
        for x in ormodels
        if x.get("id", "").endswith(":free") or str(x.get("pricing", {}).get("prompt", "1")) in ("0", "0.0")
    ]
    print(f"OPENROUTER TOTAL ({len(ormodels)}), FREE ({len(free_or)}):", free_or[:30])
except Exception as e:
    print("OPENROUTER ERR:", e)

try:
    req4 = urllib.request.urlopen(
        urllib.request.Request(
            "https://huggingface.co/api/spaces?filter=gradio&sort=likes&direction=-1&limit=50",
            headers={"User-Agent": "Mozilla/5.0"},
        ),
        timeout=10,
    )
    spaces = json.loads(req4.read().decode())
    print(f"HF TOP GRADIO SPACES ({len(spaces)}):", [s.get("id") for s in spaces[:20]])
except Exception as e:
    print("HF SPACES ERR:", e)
