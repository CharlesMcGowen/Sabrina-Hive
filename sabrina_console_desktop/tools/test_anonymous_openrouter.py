import urllib.request
import json
import time

url = "https://openrouter.ai/api/v1/chat/completions"
payload = json.dumps({
    "model": "liquid/lfm-3-1.7b:free",
    "messages": [{"role": "user", "content": "Respond with: RECRUIT_ACK_PASSED"}]
}).encode("utf-8")

req = urllib.request.Request(
    url,
    data=payload,
    headers={
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/sabrina-assistant",
        "X-Title": "Sabrina Swarm",
    }
)

t0 = time.time()
try:
    with urllib.request.urlopen(req, timeout=10) as resp:
        body = resp.read().decode("utf-8")
        print("Status: 200 OK")
        print(body[:300])
except urllib.error.HTTPError as exc:
    print(f"HTTP Error {exc.code}: {exc.read().decode('utf-8')[:300]}")
except Exception as e:
    print(f"Error: {e}")
