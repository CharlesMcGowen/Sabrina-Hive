#!/usr/bin/env python3
"""Swarm Expansion — discovery stage (candidates only, never authority).

What this does:
1. Pages through Hugging Face Spaces search results (Gradio SDK only) across a
   set of chat/agent/LLM search tags, following the API's cursor `Link` header.
2. Optionally asks local Ollama models to suggest extra URLs (errors are logged,
   never swallowed). Those URLs get the same strict check as everything else.
3. Strict, zero-auth, inquiry-only reachability check:
   - Gradio Spaces: GET https://<subdomain>.hf.space/gradio_api/info must return
     HTTP 200 + a JSON schema exposing at least one named endpoint with a text
     input. Only these count as `schema_verified` candidates.
   - Any 401/403 (auth/paywall) is recorded as `auth_required` and disqualified.
4. Writes results to the STAGING table `swarm_discovery_candidates` and to
   artifacts/collective-repair-20261108/swarm_discovery_candidates.json.

What this deliberately does NOT do:
- It never writes to `assistant_external_portals`. A candidate only becomes a
  swarm member after the sanctioned path (PortalPolicy.register + access review
  + Evaluator benchmark + jury) qualifies it.
- It does not ingest OpenRouter model pages or HF model cards: neither is a
  zero-auth inference endpoint.

History: an earlier version of this script synced web-page "reachability" into
the authority table with auth_mode='zero_auth'; those 123 rows were removed on
2026-11-10 (backup: data/sabrina_assistant_fresh.backup_pre_cleanup_20261110_2148.sqlite3).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional



REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.portal_policy import DEFAULT_DB, PortalPolicy, AdmissionError
from tools.portal_transport import NoRedirect, MAX_BYTES, classify, retry_seconds

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434/api/generate")
OUT_JSON = REPO_ROOT / "artifacts" / "collective-repair-20261108" / "swarm_discovery_candidates.json"
OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
DB_PATH = DEFAULT_DB
STAGING_TABLE = "swarm_discovery_candidates"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) SabrinaDiscovery/2.0 (inquiry-only)"
HF_SPACES_API = "https://huggingface.co/api/spaces"
DEFAULT_TAGS = [
    "chatbot", "chat", "llm", "assistant", "rag", "agent", "qwen", "llama",
    "mistral", "deepseek", "gemma", "phi", "coder", "tutor", "gpt", "instruct",
]

STAGING_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS {STAGING_TABLE} (
    portal_id TEXT PRIMARY KEY,
    space_id TEXT,
    url TEXT NOT NULL,
    info_url TEXT,
    source TEXT NOT NULL,
    search_tag TEXT,
    probe_status TEXT NOT NULL,
    http_status INTEGER,
    latency_ms REAL,
    text_endpoints_json TEXT NOT NULL DEFAULT '[]',
    error TEXT,
    state TEXT NOT NULL DEFAULT 'staged',
    first_seen REAL NOT NULL,
    last_probed REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS {STAGING_TABLE}_status ON {STAGING_TABLE}(probe_status, state);
"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _log(msg: str) -> None:
    print(msg, flush=True)


class DiscoverySession:
    """One bounded campaign, sharing durable request admission with live routing."""
    def __init__(self, policy, max_requests=20, wait=True):
        self.policy, self.max_requests, self.wait = policy, max_requests, wait
        self.requests = 0
        with policy.db() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS swarm_discovery_pages
 (url TEXT PRIMARY KEY, headers_json TEXT NOT NULL, body BLOB NOT NULL, fetched REAL NOT NULL)''')

    def get(self, url, timeout, portal_id=None):
        host = urllib.parse.urlsplit(url).hostname or ''
        if host == 'huggingface.co':
            with self.policy.db() as db:
                cached = db.execute('SELECT * FROM swarm_discovery_pages WHERE url=? AND fetched>?',
                                    (url, self.policy.clock()-86400)).fetchone()
            if cached:
                return 200, json.loads(cached['headers_json']), cached['body']
        if self.requests >= self.max_requests:
            raise AdmissionError('campaign_budget_exhausted')
        path=urllib.parse.urlsplit(url).path
        metadata_space=re.fullmatch(r'/api/spaces/([\w.-]+/[\w.-]+)',path) if host=='huggingface.co' else None
        portal_id = portal_id or (portal_id_for_space(metadata_space.group(1)) if metadata_space else
            ('hf-discovery-catalog' if host == 'huggingface.co' else 'hf-gradio-' + host.removesuffix('.hf.space')))
        while True:
            try:
                rid = self.policy.admit_discovery(portal_id, url)
                break
            except AdmissionError as exc:
                if self.wait and str(exc) == 'provider_cooldown':
                    time.sleep(1)
                    continue
                raise
        self.requests += 1
        with self.policy.db() as db:
            db.execute('INSERT INTO portal_audit(created,kind,detail) VALUES (?,?,?)',
                (self.policy.clock(),'discovery_request',json.dumps({'request_id':rid,'url':url,'portal_id':portal_id})))
        result = {'delivery': 'missing', 'failure': 'uncertain'}
        try:
            req = urllib.request.Request(url, headers={'User-Agent': UA})
            with urllib.request.build_opener(NoRedirect()).open(req, timeout=timeout) as resp:
                body = resp.read(MAX_BYTES + 1)
                if len(body) > MAX_BYTES:
                    result = {'failure': 'response_too_large'}
                    raise ValueError('response_too_large')
                # A successful metadata document can describe an unavailable
                # Space, or contain example error strings in a schema/model card.
                # Those fields are data, not a quota response to this client.
                document=None
                try:document=json.loads(body)
                except (ValueError,UnicodeError):pass
                valid_document=(resp.status==200 and (
                    (metadata_space and isinstance(document,dict) and document.get('id')==metadata_space.group(1))
                    or (host=='huggingface.co' and path=='/api/spaces' and isinstance(document,list)
                        and all(isinstance(item,dict) and isinstance(item.get('id'),str) for item in document))
                    or (host.endswith('.hf.space') and path=='/gradio_api/info' and isinstance(document,dict)
                        and isinstance(document.get('named_endpoints'),dict))))
                failure = '' if valid_document else classify(body.decode('utf-8', 'replace'), resp.status)
                result = {'delivery': 'observed', 'failure': failure}
                if resp.headers.get('Retry-After'):
                    result['retry_after_seconds'] = retry_seconds(resp.headers['Retry-After'])
                with self.policy.db() as db:
                    db.execute('INSERT INTO portal_audit(created,kind,detail) VALUES (?,?,?)',
                               (self.policy.clock(), 'discovery_protocol', json.dumps({
                                   'request_id': rid, 'url': url, 'status': resp.status,
                                   'raw_response': body.decode('utf-8', 'replace')})))
                if failure:
                    raise AdmissionError(failure)
                if host == 'huggingface.co':
                    with self.policy.db() as db:
                        db.execute('INSERT OR REPLACE INTO swarm_discovery_pages VALUES (?,?,?,?)',
                                   (url, json.dumps({'Link': resp.headers.get('Link', '')}), body, self.policy.clock()))
                return resp.status, resp.headers, body
        except urllib.error.HTTPError as exc:
            with self.policy.db() as db:
                db.execute('INSERT INTO portal_audit(created,kind,detail) VALUES (?,?,?)',
                    (self.policy.clock(),'discovery_protocol',json.dumps({'request_id':rid,'url':url,
                        'status':exc.code,'raw_response':exc.read(MAX_BYTES).decode('utf-8','replace')})))
            result = {'failure': classify('', exc.code) or 'redirect_requires_review',
                      'retry_after_seconds': retry_seconds(exc.headers['Retry-After'])
                      if exc.headers and exc.headers.get('Retry-After') else 0}
            if exc.code in (401, 403, 429) or result['retry_after_seconds']:
                raise AdmissionError(result['failure']) from exc
            raise
        except (urllib.error.URLError, TimeoutError):
            # Read-only metadata requests have no inference or tool side effect.
            result = {'failure': 'unreachable'}
            raise
        finally:
            self.policy.finish(rid, result)


def _http_get(url: str, timeout: float, session: DiscoverySession, portal_id=None):
    return session.get(url, timeout, portal_id=portal_id)


def portal_id_for_space(space_id: str) -> str:
    """Match the existing `hf-gradio-<owner>-<name>` convention."""
    slug = re.sub(r"[^a-z0-9]+", "-", space_id.lower().replace("/", "-")).strip("-")
    return f"hf-gradio-{slug}"


# ---------------------------------------------------------------------------
# Source 1: Hugging Face Spaces (paginated, Gradio only)
# ---------------------------------------------------------------------------
# Measured 2026-11-10 on search=llm (100 results each): default sort -> 2/63 schema-verified
# (most are sleeping, HTTP 503); likes -> 17/68; lastModified -> 22/46. Recent + popular first.
DEFAULT_SORTS = ["lastModified", "likes"]


def iter_hf_gradio_spaces(tags: List[str], per_tag_max: int, page_size: int = 100,
                          sorts: Optional[List[str]] = None, *, session: DiscoverySession) -> Iterator[Dict[str, Any]]:
    for tag, sort in [(t, s) for t in tags for s in (sorts or DEFAULT_SORTS)]:
        url: Optional[str] = (f"{HF_SPACES_API}?search={urllib.parse.quote(tag)}&limit={page_size}"
                              f"&full=true&sort={sort}&direction=-1")
        seen_for_tag = 0
        while url and seen_for_tag < per_tag_max:
            try:
                _, headers, body = _http_get(url, timeout=15, session=session)
                items = json.loads(body.decode("utf-8", "ignore"))
            except AdmissionError:
                raise
            except Exception as exc:
                _log(f"[-] HF Spaces page failed for tag '{tag}': {type(exc).__name__}: {exc}")
                break
            if not items:
                break
            for s in items:
                seen_for_tag += 1
                if s.get("sdk") != "gradio" or s.get("private"):
                    continue
                sid = s.get("id") or ""
                sub = s.get("subdomain") or re.sub(r"[^a-z0-9-]", "-", sid.lower().replace("/", "-"))
                if not sid or not sub:
                    continue
                yield {
                    "portal_id": portal_id_for_space(sid),
                    "space_id": sid,
                    "url": f"https://{sub}.hf.space",
                    "info_url": f"https://{sub}.hf.space/gradio_api/info",
                    "source": "hf_spaces",
                    "search_tag": tag,
                }
            m = re.search(r"<([^>]+)>;\s*rel=\"next\"", headers.get("Link") or "")
            url = m.group(1) if m else None
        _log(f"[*] Tag '{tag}' (sort={sort}): scanned {seen_for_tag} spaces.")


# ---------------------------------------------------------------------------
# Source 2: local model suggestions (low yield; strictly verified like the rest)
# ---------------------------------------------------------------------------
def ask_local_model_for_urls(runner_model: str, specialty: str, timeout: float = 120) -> List[Dict[str, Any]]:
    prompt = (
        f"List up to 5 public Hugging Face Space URLs (https://huggingface.co/spaces/<owner>/<name>) "
        f"hosting a free, no-login chat demo specialised in '{specialty}'. "
        'Return ONLY a JSON list: [{"space_id": "owner/name"}].'
    )
    payload = json.dumps({
        "model": runner_model, "prompt": prompt, "stream": False,
        "options": {"temperature": 0.3, "num_predict": 400},
    }).encode("utf-8")
    req = urllib.request.Request(OLLAMA_URL, data=payload, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            txt = json.loads(resp.read().decode("utf-8")).get("response", "")
    except Exception as exc:
        _log(f"[-] Local model {runner_model} ({specialty}) call failed: {type(exc).__name__}: {exc}")
        return []
    match = re.search(r"\[.*\]", txt, re.DOTALL)
    if not match:
        _log(f"[-] Local model {runner_model} ({specialty}) returned no JSON list. First 120 chars: {txt[:120]!r}")
        return []
    try:
        items = json.loads(match.group(0))
    except Exception as exc:
        _log(f"[-] Local model {runner_model} ({specialty}) JSON parse failed: {exc}")
        return []
    out = []
    for it in items if isinstance(items, list) else []:
        sid = str((it or {}).get("space_id") or "").strip().strip("/")
        sid = re.sub(r"^https?://huggingface\.co/spaces/", "", sid)
        if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", sid):
            sub = re.sub(r"[^a-z0-9-]", "-", sid.lower().replace("/", "-"))
            out.append({
                "portal_id": portal_id_for_space(sid), "space_id": sid,
                "url": f"https://{sub}.hf.space", "info_url": f"https://{sub}.hf.space/gradio_api/info",
                "source": f"local_model:{runner_model}", "search_tag": specialty,
            })
    return out


# ---------------------------------------------------------------------------
# Strict zero-auth probe
# ---------------------------------------------------------------------------
def _text_endpoints(info: Dict[str, Any]) -> List[str]:
    eps = []
    for name, spec in (info.get("named_endpoints") or {}).items():
        for p in spec.get("parameters") or []:
            if (p.get("type") or {}).get("type") == "string" or p.get("component") in ("Textbox", "Chatbot", "MultimodalTextbox"):
                eps.append(name)
                break
    return eps


def strict_probe(cand: Dict[str, Any], timeout: float = 8.0, *, session: DiscoverySession) -> Dict[str, Any]:
    t0 = time.monotonic()
    res: Dict[str, Any] = {"probe_status": "unreachable", "http_status": 0, "text_endpoints": [], "error": None}
    try:
        code, _, body = _http_get(cand["info_url"], timeout, session, portal_id=cand['portal_id'])
        res["http_status"] = code
        info = json.loads(body.decode("utf-8", "ignore"))
        eps = _text_endpoints(info) if isinstance(info, dict) else []
        res["text_endpoints"] = eps
        res["probe_status"] = "schema_verified" if (code == 200 and eps) else "no_text_endpoint"
    except AdmissionError:
        raise
    except urllib.error.HTTPError as exc:
        res["http_status"] = exc.code
        res["probe_status"] = "auth_required" if exc.code in (401, 403) else f"http_{exc.code}"
    except json.JSONDecodeError:
        res["probe_status"] = "non_json_schema"
    except Exception as exc:
        res["error"] = f"{type(exc).__name__}: {str(exc)[:160]}"
    res["latency_ms"] = round((time.monotonic() - t0) * 1000.0, 1)
    return res


# ---------------------------------------------------------------------------
# Staging persistence (never the authority table)
# ---------------------------------------------------------------------------
def stage_candidates(rows: List[Dict[str, Any]], db_path: Path) -> int:
    now = time.time()
    with sqlite3.connect(str(db_path), timeout=15) as db:
        db.executescript(STAGING_SCHEMA)
        for r in rows:
            db.execute(
                f"""
                INSERT INTO {STAGING_TABLE} (portal_id, space_id, url, info_url, source, search_tag,
                    probe_status, http_status, latency_ms, text_endpoints_json, error, state, first_seen, last_probed)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'staged', ?, ?)
                ON CONFLICT(portal_id) DO UPDATE SET
                    probe_status=excluded.probe_status, http_status=excluded.http_status,
                    latency_ms=excluded.latency_ms, text_endpoints_json=excluded.text_endpoints_json,
                    error=excluded.error, last_probed=excluded.last_probed
                """,
                (r["portal_id"], r.get("space_id"), r["url"], r.get("info_url"), r["source"], r.get("search_tag"),
                 r["probe_status"], r.get("http_status"), r.get("latency_ms"), json.dumps(r.get("text_endpoints", [])),
                 r.get("error"), now, now),
            )
        db.commit()
    return len(rows)


def validate_database(db_path: Path) -> None:
    # Never create a new empty authority database because a path was mistyped.
    with sqlite3.connect(db_path.resolve().as_uri() + '?mode=ro', uri=True) as db:
        if db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise ValueError('Invalid authority database')
        db.execute('SELECT portal_id FROM assistant_external_portals LIMIT 1')


def already_member_ids(db_path: Path) -> set:
    validate_database(db_path)
    with sqlite3.connect(str(db_path), timeout=10) as db:
        return {r[0] for r in db.execute('SELECT portal_id FROM assistant_external_portals')}


def run_discovery(target_verified: int, tags: List[str], per_tag_max: int, workers: int,
                  use_local_models: bool, write_db: bool, db_path: Path,
                  max_requests: int = 20, wait: bool = True,
                  new_verified_target: int = 0, retry_after_seconds: int = 86400) -> dict:
    if (target_verified < 1 or max_requests < 0 or per_tag_max < 1
            or new_verified_target < 0 or retry_after_seconds < 0):
        raise ValueError('Positive target/page limits and nonnegative request budget required')
    if workers != 1:
        raise ValueError('Hugging Face discovery permits one concurrent request; use --workers 1')
    start = time.time()
    existing = already_member_ids(db_path)
    policy = PortalPolicy(db_path)
    with policy.db() as db:
        db.executescript(STAGING_SCHEMA)
        staged = {r['portal_id']: dict(r) for r in db.execute(f'SELECT * FROM {STAGING_TABLE}')}
    verified = sum(r['probe_status'] == 'schema_verified' for r in staged.values())
    _log(f'[*] {verified} historical schema-verified candidates already staged; target {target_verified}.')
    _log('[*] Schema evidence is not live availability or role qualification.')
    session = DiscoverySession(policy, max_requests=max_requests, wait=wait)
    results = []
    incremental = new_verified_target > 0
    stop_reason = ('sources_exhausted' if incremental else
                   'target_already_staged' if verified >= target_verified else 'sources_exhausted')
    new_verified = 0

    def candidates():
        if use_local_models:
            for runner, spec in [('qwen2.5-coder:14b', 'code generation'), ('llama3.1:8b', 'research assistant')]:
                yield from ask_local_model_for_urls(runner, spec)
        yield from iter_hf_gradio_spaces(tags, per_tag_max, session=session)

    # Incremental campaigns revisit transient failures after a cooling period.
    # Auth barriers and successful schemas remain excluded; neither is silently
    # retried just because the operator asked for more candidates.
    retryable = {'http_500', 'http_502', 'http_503', 'unreachable', 'non_json_schema', 'no_text_endpoint'}
    now = policy.clock()
    seen = existing | {pid for pid, row in staged.items()
                       if not incremental or row['probe_status'] not in retryable
                       or now - row['last_probed'] < retry_after_seconds}
    try:
        if incremental or verified < target_verified:
            for candidate in candidates():
                pid = candidate['portal_id']
                if pid in seen:
                    continue
                seen.add(pid)
                row = {**candidate, **strict_probe(candidate, session=session)}
                results.append(row)
                if write_db:
                    stage_candidates([row], db_path)  # Durable after every probe.
                if row['probe_status'] == 'schema_verified':
                    verified += 1
                    new_verified += 1
                _log(f"    {session.requests}/{max_requests} campaign requests | staged schema evidence: {verified}")
                if (new_verified >= new_verified_target if incremental else verified >= target_verified):
                    stop_reason = 'new_target_reached' if incremental else 'target_reached'
                    break
    except AdmissionError as exc:
        stop_reason = str(exc)
        _log(f'[*] Paused: {stop_reason}. Existing results are preserved; no automatic access retry.')
    except KeyboardInterrupt:
        stop_reason = 'cancelled'
        _log('[*] Cancelled; completed probe results are preserved.')

    by_status = {}
    for row in results:
        by_status[row['probe_status']] = by_status.get(row['probe_status'], 0) + 1
    report = {'campaign': 'swarm_discovery_v3_bounded', 'generated_at': _now_iso(),
              'elapsed_seconds': round(time.time()-start, 1), 'target': target_verified,
              'historical_schema_verified': verified - new_verified,
              'new_verified': new_verified, 'new_verified_target': new_verified_target,
              'requests': session.requests,
              'stop_reason': stop_reason, 'probe_status_counts': by_status, 'candidates': results}
    OUT_JSON.write_text(json.dumps(report, indent=2), encoding='utf-8')
    _log(f"[DONE] {len(results)} new probes, {session.requests} requests; {stop_reason}.")
    _log('[*] Authority membership unchanged. Candidates require access review and role evaluation.')
    return report


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Swarm discovery stage: strict zero-auth Gradio candidate staging.")
    ap.add_argument("--target", type=int, default=1000, help="Stop after this many schema-verified candidates (default 1000)")
    ap.add_argument("--new-verified", type=int, default=0, help="Find this many additional verified schemas even when the historical target is met")
    ap.add_argument("--tags", type=str, default=",".join(DEFAULT_TAGS), help="Comma-separated HF Spaces search tags")
    ap.add_argument("--per-tag-max", type=int, default=500, help="Max spaces scanned per tag (default 500)")
    ap.add_argument("--workers", type=int, default=1, help="Must be 1: shared provider concurrency limit")
    ap.add_argument("--max-requests", type=int, default=20, help="Campaign request cap, including catalog pages (default 20)")
    ap.add_argument("--wait", action=argparse.BooleanOptionalAction, default=True, help="Wait for ordinary provider spacing (default); access blocks and budgets still stop")
    ap.add_argument("--local-models", action="store_true", help="Also ask local Ollama models for suggestions")
    ap.add_argument("--no-db", action="store_true", help="Only write the JSON artifact, not the staging table")
    ap.add_argument("--db-path", type=str, default="", help="SQLite path (default: live authority DB file)")
    a = ap.parse_args()
    run_discovery(
        max_requests=a.max_requests, wait=a.wait,
        new_verified_target=a.new_verified,
        target_verified=a.target,
        tags=[t.strip() for t in a.tags.split(",") if t.strip()],
        per_tag_max=a.per_tag_max,
        workers=a.workers,
        use_local_models=a.local_models,
        write_db=not a.no_db,
        db_path=Path(a.db_path) if a.db_path else DB_PATH,
    )
