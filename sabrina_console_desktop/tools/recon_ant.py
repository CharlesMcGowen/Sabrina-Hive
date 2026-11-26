import urllib.request
import urllib.parse
import json
import re
from dataclasses import dataclass, field
from typing import List

@dataclass
class DiscoveredEndpoint:
    path: str
    method: str = 'GET'
    params: list = field(default_factory=list)
    param_type: str = 'unknown'
    source: str = 'unknown'

    def __hash__(self):
        return hash((self.path, self.method))

    def __eq__(self, other):
        return isinstance(other, DiscoveredEndpoint) and self.path == other.path and self.method == other.method


# Fast deterministic regex — no LLM needed for JS bundles
_API_RE = re.compile(
    r'[`"\']('
    r'(?:/?rest|/?api|/?auth|/?user|/?admin|/?v\d+|/?login|/?register|/?logout|/?account|/?profile|/?feedback|/?basket|/?challenge|/?metrics|/?file|/?ftp)[a-zA-Z0-9_/\-{}:.?=&]*'
    r')[`"\']',
    re.IGNORECASE,
)

# Catches human-readable, TypeScript typed, and minified method calls:
# this.http.post<Feedback>('/api/Feedbacks', ...) OR n.post(`api/Feedbacks`, ...) OR r.get("/path")
_METHOD_HINT_RE = re.compile(
    r'\b(?:this\.)?(?:http\.)?'
    r'(get|post|put|delete|patch)'
    r'(?:\s*<[^>]+>)?'
    r'\s*\(\s*[`"\']'
    r'([a-zA-Z0-9_/\-{}:.?=&]*)'
    r'[`"\']',
    re.IGNORECASE,
)

# Angular router: path: 'login', path: "user/:id"
_ROUTER_RE = re.compile(
    r'path\s*:\s*[`"\']([a-zA-Z0-9_/\-:{}][a-zA-Z0-9_/\-:{}.?=&]*)[`"\']',
    re.IGNORECASE,
)

# Form fields / JSON keys that hint at parameters
_PARAM_RE = re.compile(
    r'\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}|'  # path var {id}
    r'[?&]([a-zA-Z_][a-zA-Z0-9_]*)=|'       # query ?q=
    r'"([a-zA-Z_][a-zA-Z0-9_]*)"\s*:',       # json key
)


def _regex_extract(text: str, source: str = "regex") -> List[DiscoveredEndpoint]:
    """Fast deterministic extraction — no LLM call, no network latency."""
    found: dict = {}
    static_exts = ('.js', '.css', '.png', '.jpg', '.jpeg', '.gif', '.svg', '.woff', '.woff2', '.ttf', '.html', '.ico', '.map')

    # Method + path hints (most reliable — works on TypeScript & minified code)
    for m in _METHOD_HINT_RE.finditer(text):
        method = m.group(1).upper()
        raw_path = m.group(2).split('?')[0].strip()
        if not raw_path or any(raw_path.endswith(ext) for ext in static_exts):
            continue
        path = "/" + raw_path.lstrip("/")
        # Infer param_type and params from path
        path_params = re.findall(r'\{([^}]+)\}', path)
        pt = "path_variable" if path_params else "unknown"
        key = f"{method}:{path}"
        if key not in found:
            found[key] = DiscoveredEndpoint(
                path=path, method=method,
                params=path_params, param_type=pt, source=source
            )

    # Plain API path strings
    for m in _API_RE.finditer(text):
        raw_path = m.group(1).split('?')[0].strip()   # strip inline query strings
        if not raw_path or any(raw_path.endswith(ext) for ext in static_exts):
            continue
        path = "/" + raw_path.lstrip("/")
        # Guess method from context (look at 25 chars before the match)
        before = text[max(0, m.start()-25):m.start()]
        method = "GET"
        for verb in ("post", "put", "delete", "patch"):
            if verb in before.lower():
                method = verb.upper()
                break
        key = f"{method}:{path}"
        if key not in found:
            path_params = re.findall(r'\{([^}]+)\}', path)
            pt = "path_variable" if path_params else "unknown"
            found[key] = DiscoveredEndpoint(
                path=path, method=method,
                params=path_params, param_type=pt, source=source
            )

    # Angular router routes — prefix with / and mark as frontend
    for m in _ROUTER_RE.finditer(text):
        raw = m.group(1).strip()
        if not raw or raw in ("", "**", "''", "``"):  # skip wildcards
            continue
        path = "/" + raw.lstrip("/")
        # Angular routes with :param are path variables
        params = re.findall(r':([a-zA-Z_][a-zA-Z0-9_]*)', path)
        # Normalise Angular :id → {id} convention
        norm_path = re.sub(r':([a-zA-Z_][a-zA-Z0-9_]*)', r'{\1}', path)
        key = f"GET:{norm_path}"
        if key not in found:
            found[key] = DiscoveredEndpoint(
                path=norm_path, method="GET",
                params=params,
                param_type="path_variable" if params else "unknown",
                source=f"{source}:angular_router"
            )

    return list(found.values())


def extract_endpoints_from_text(text: str, use_llm: bool = True) -> List[DiscoveredEndpoint]:
    """
    Stage 1: fast regex scan.
    Stage 2 (optional): ask qwen2.5:7b to find anything regex missed.
    """
    results = _regex_extract(text)

    if use_llm and len(text) < 30000:   # only LLM on small enough chunks
        import os
        worker_model = os.environ.get("WORKER_MODEL_NAME", "qwen2.5:7b")
        payload = json.dumps({
            "model": worker_model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are a security recon parser. Analyze the given code snippet "
                        "and extract all API/web endpoints. "
                        "Output ONLY a valid JSON array of objects with "
                        "keys: path (str), method (str), params (list of strings), "
                        "param_type (one of: query, body_json, path_variable, unknown). "
                        "No markdown, no explanation, just the JSON array."
                    )
                },
                {"role": "user", "content": text[:8000]}   # trim for speed
            ],
            "stream": False,
            "options": {"temperature": 0.1, "num_predict": 512}
        }).encode()
        req = urllib.request.Request(
            "http://127.0.0.1:11434/api/chat",
            data=payload,
            headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=None) as resp:
                reply = json.loads(resp.read().decode())["message"]["content"]
            reply = reply.strip()
            if reply.startswith("```"):
                reply = "\n".join(l for l in reply.splitlines()
                                  if not l.strip().startswith("```"))
            parsed = json.loads(reply)
            seen = {(ep.path, ep.method) for ep in results}
            for item in parsed:
                p = item.get("path", "/")
                m = item.get("method", "GET").upper()
                if (p, m) not in seen:
                    results.append(DiscoveredEndpoint(
                        path=p, method=m,
                        params=item.get("params", []),
                        param_type=item.get("param_type", "unknown"),
                        source="llm_parse"
                    ))
                    seen.add((p, m))
        except Exception as e:
            print(f"[recon_ant] LLM pass skipped: {e}")

    return results


def _resolve_url(base_url: str, href: str) -> str:
    """Safely join a base URL with a possibly-relative script src."""
    if href.startswith("http://") or href.startswith("https://"):
        return href
    if href.startswith("//"):
        scheme = base_url.split("://")[0]
        return f"{scheme}:{href}"
    base = base_url.rstrip("/")
    if href.startswith("/"):
        return base + href
    return base + "/" + href


def _safe_fetch(url: str, timeout: int = 10) -> str:
    """Fetch URL and return text body; return '' on any error."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            ct = r.headers.get("Content-Type", "")
            # Skip binary/image responses
            if any(x in ct for x in ("image/", "font/", "audio/", "video/")):
                return ""
            return r.read(512 * 1024).decode("utf-8", errors="replace")  # cap at 512 KB
    except Exception:
        return ""


def _parse_swagger(text: str, source: str = "swagger") -> List[DiscoveredEndpoint]:
    """Parse an OpenAPI/Swagger JSON spec into endpoints."""
    results = []
    try:
        spec = json.loads(text)
        paths = spec.get("paths", {})
        for path, methods in paths.items():
            for method, details in methods.items():
                if method.upper() in ("GET", "POST", "PUT", "DELETE", "PATCH"):
                    params = []
                    param_type = "unknown"
                    for p in details.get("parameters", []):
                        params.append(p.get("name", ""))
                        if p.get("in") == "query":
                            param_type = "query"
                        elif p.get("in") == "body":
                            param_type = "body_json"
                        elif p.get("in") == "path":
                            param_type = "path_variable"
                    results.append(DiscoveredEndpoint(
                        path=path, method=method.upper(),
                        params=params, param_type=param_type, source=source
                    ))
    except Exception:
        pass
    return results


def crawl_js_bundles(base_url: str) -> List[DiscoveredEndpoint]:
    """
    1. Fetch index HTML, extract <script src> bundle URLs
    2. Regex + LLM scan each JS bundle for API paths
    3. Check /api-docs, /swagger.json, /openapi.json for machine-readable specs
    4. Return deduplicated list of DiscoveredEndpoint
    """
    seen_keys: set = set()
    results: List[DiscoveredEndpoint] = []

    def _add(ep: DiscoveredEndpoint):
        k = (ep.method.upper(), ep.path)
        if k not in seen_keys:
            seen_keys.add(k)
            results.append(ep)

    # --- Step 1: fetch HTML and find script tags + modulepreload links ---
    html = _safe_fetch(base_url)
    if not html:
        print(f"[recon_ant] Could not fetch {base_url}")
        return []

    script_srcs = re.findall(r'<script[^\>]+src=["\']([^"\']+)["\']', html, re.IGNORECASE)
    # Also capture Angular/Vite modulepreload chunks — these contain the real API service code
    preload_srcs = re.findall(r'<link[^\>]+href=["\']([^"\']+\.js)["\']', html, re.IGNORECASE)
    script_srcs = list(dict.fromkeys(script_srcs + preload_srcs))  # deduplicate, preserve order
    print(f"[recon_ant] Found {len(script_srcs)} JS bundles (script + modulepreload) in {base_url}")

    # --- Step 2: scan each same-origin JS bundle and referenced lazy chunks ---
    scanned_srcs = set()
    queue = list(script_srcs)
    max_js_to_crawl = 30

    while queue and len(scanned_srcs) < max_js_to_crawl:
        src = queue.pop(0)
        if src in scanned_srcs:
            continue
        scanned_srcs.add(src)

        js_url = _resolve_url(base_url, src)
        # Skip cross-origin
        base_host = urllib.parse.urlparse(base_url).netloc
        js_host   = urllib.parse.urlparse(js_url).netloc
        if js_host and js_host != base_host:
            continue

        js_text = _safe_fetch(js_url, timeout=15)
        if not js_text:
            continue

        for ep in extract_endpoints_from_text(js_text, use_llm=True):
            _add(ep)

        # Discover lazy-loaded Angular/Webpack chunks referenced in the JS code
        chunk_refs = re.findall(
            r'["\']([a-zA-Z0-9_.-]+\.(?:chunk\.)?js|\d+\.[a-f0-9]+\.js)[\'"]',
            js_text,
            re.IGNORECASE
        )
        for chunk in chunk_refs:
            if chunk not in scanned_srcs and chunk not in queue and not chunk.startswith("http"):
                if "/" in src and not chunk.startswith("/"):
                    chunk_path = src.rsplit("/", 1)[0] + "/" + chunk
                else:
                    chunk_path = chunk
                queue.append(chunk_path)

    # --- Step 3: check spec endpoints ---
    for spec_path in ("/api-docs", "/swagger.json", "/openapi.json"):
        spec_url  = base_url.rstrip("/") + spec_path
        spec_text = _safe_fetch(spec_url)
        if spec_text and spec_text.strip().startswith("{"):
            for ep in _parse_swagger(spec_text, source=spec_path.lstrip("/")):
                _add(ep)
        elif spec_text:
            # HTML response — regex scan it too
            for ep in _regex_extract(spec_text, source=spec_path.lstrip("/")):
                _add(ep)

    return results


if __name__ == '__main__':
    import sys
    target = sys.argv[1] if len(sys.argv) > 1 else 'http://127.0.0.1:3000'
    print(f"Crawling {target} ...")
    endpoints = crawl_js_bundles(target)
    print(f"\nDiscovered {len(endpoints)} endpoint(s):")
    for ep in sorted(endpoints, key=lambda e: e.path):
        print(f"  {ep.method:6s} {ep.path}  params={ep.params}  source={ep.source}")