import urllib.request
import json

data = json.loads(urllib.request.urlopen("https://openrouter.ai/api/v1/models", timeout=8).read())
models = data.get("data", [])
free_models = [m["id"] for m in models if ":free" in m.get("id", "") or "free" in m.get("id", "")]
print(f"Total OpenRouter models online: {len(models)}")
print(f"Free models online: {len(free_models)}")
for m in free_models[:15]:
    print(f" - {m}")
