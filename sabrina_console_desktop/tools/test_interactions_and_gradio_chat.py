#!/usr/bin/env python3
import concurrent.futures
import json
import re
import urllib.error
import urllib.request

env_text = open("/home/ego/bot/Sabrina-windows-sync/.env", encoding="utf-8", errors="ignore").read()
gkey = re.search(r"GEMINI_API_KEY=([^\s#]+)", env_text).group(1).strip()

print("=== 1. Testing Google Interactions API with 'agent' field ===")
for ag_name in [
    "antigravity-preview-latest",
    "antigravity-preview-09-2026",
    "antigravity-preview-05-2026",
    "deep-research-max-preview-04-2026",
    "deep-research-preview-04-2026",
    "deep-research-pro-preview-12-2025",
]:
    url = f"https://generativelanguage.googleapis.com/v1beta/interactions?key={gkey}"
    payload = json.dumps({
        "agent": ag_name,
        "input": "Reply with OK",
    }).encode("utf-8")
    try:
        r = urllib.request.urlopen(
            urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"}, method="POST"),
            timeout=15,
        )
        body = r.read().decode()
        print(f"  [OK] {ag_name}: {body[:250]}")
    except urllib.error.HTTPError as e:
        print(f"  [HTTP {e.code}] {ag_name}: {e.read().decode()[:200].replace(chr(10), ' ')}")
    except Exception as e:
        print(f"  [ERR] {ag_name}: {e}")

print("\n=== 2. Discovering Text-Chat Gradio Spaces Across HuggingFace ===")
queries = [
    "chatbot", "chat", "qwen2.5", "qwen3", "deepseek", "llama-3", "mistral",
    "gemma", "coder", "llm", "assistant", "instruct", "yi", "glm", "phi-3", "phi-4",
    "nemotron", "command-r", "smollm", "olmo",
]
candidate_spaces = {}
for q in queries:
    try:
        u = f"https://huggingface.co/api/spaces?search={q}&filter=gradio&sort=likes&direction=-1&limit=50"
        req = urllib.request.urlopen(urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0"}), timeout=10)
        for s in json.loads(req.read().decode()):
            sid = s.get("id", "")
            if sid and sid not in candidate_spaces:
                # Skip obvious image/tts/audio/video spaces
                low = sid.lower()
                if any(bad in low for bad in ["image", "tts", "voice", "audio", "video", "flux", "diffusion", "kolors", "try-on", "portrait", "ocr", "asr", "3d", "music"]):
                    continue
                candidate_spaces[sid] = s.get("likes", 0)
    except Exception:
        pass

print(f"Filtered to {len(candidate_spaces)} text/chat candidate Gradio spaces.")


def inspect_chat_space(space_id: str):
    slug = space_id.replace("/", "-").lower()
    base = f"https://{slug}.hf.space"
    for prefix in ["/gradio_api", ""]:
        try:
            req = urllib.request.urlopen(
                urllib.request.Request(f"{base}{prefix}/info", headers={"User-Agent": "Mozilla/5.0"}),
                timeout=6,
            )
            data = json.loads(req.read().decode())
            named = data.get("named_endpoints", {})
            for ep_name, ep_info in named.items():
                params = ep_info.get("parameters", [])
                if not params:
                    continue
                first_p = params[0]
                ptype = first_p.get("type", {}).get("type", "")
                if ptype == "string" and any(
                    k in ep_name.lower() for k in ["chat", "predict", "respond", "submit", "generate", "ask", "bot", "infer", "model"]
                ):
                    # Build default args for remaining params
                    defaults = []
                    for p in params[1:]:
                        if " parameter_default" in p or "parameter_default" in p:
                            defaults.append(p.get("parameter_default"))
                        else:
                            t = p.get("type", {}).get("type", "")
                            if t == "array":
                                defaults.append([])
                            elif t == "number" or t == "integer":
                                defaults.append(512)
                            elif t == "boolean":
                                defaults.append(False)
                            elif t == "string":
                                defaults.append("")
                            else:
                                defaults.append(None)
                    return {
                        "space_id": space_id,
                        "base_url": base,
                        "prefix": prefix,
                        "ep_name": ep_name,
                        "num_params": len(params),
                        "defaults": defaults,
                    }
        except Exception:
            pass
    return None


chat_gradio_spaces = []
with concurrent.futures.ThreadPoolExecutor(max_workers=32) as ex:
    futs = {ex.submit(inspect_chat_space, sid): sid for sid in list(candidate_spaces.keys())[:300]}
    for f in concurrent.futures.as_completed(futs):
        r = f.result()
        if r:
            chat_gradio_spaces.append(r)

print(f"Found {len(chat_gradio_spaces)} LIVE Text/Chat Gradio Spaces with string-input endpoints!")
for item in chat_gradio_spaces[:35]:
    print(f"  [CHAT GRADIO] {item['space_id']} -> {item['base_url']}{item['prefix']}/call{item['ep_name']} (params={item['num_params']})")

with open("/tmp/sabrina_routing_candidate_workspace/artifacts/collective-repair-20261108/discovered_gradio_chat_spaces.json", "w") as f:
    json.dump(chat_gradio_spaces, f, indent=2)
