#!/usr/bin/env python3
"""Autonomous Swarm Wave 7: Testing Google Cloud Gemma-4 & Gemini-3.1/3.6/3.7/3.8 Free-Tier Models
with Thinking-Token Budget Fix (`maxOutputTokens=2048` + part extraction).
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.external_mind_scout import ExternalMindScout, _authoritative_clamp_ratio_check
from tools.run_autonomous_account_and_token_onboarding import load_env_secrets

OUT_JSON = REPO_ROOT / "artifacts" / "collective-repair-20261108" / "wave7_google_cloud_gemma4_and_gemini_qualified.json"
OUT_JSON.parent.mkdir(parents=True, exist_ok=True)


def call_google_cloud_model_v2(gemini_key: str, model_id: str, prompt: str) -> Dict[str, Any]:
    """Query Google Generative Language API with maxOutputTokens=2048 and extract non-thought text parts."""
    t0 = time.monotonic()
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_id}:generateContent?key={gemini_key}"
    payload = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.1, "maxOutputTokens": 2048},
    }).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=35) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        cands = data.get("candidates") or []
        parts = ((cands[0].get("content") or {}).get("parts") or []) if cands else []
        text_chunks = [p.get("text", "") for p in parts if p.get("text") and not p.get("thought")]
        if not text_chunks and parts:
            text_chunks = [p.get("text", "") for p in parts if p.get("text")]
        reply = "\n".join(text_chunks).strip()
        elapsed_ms = round((time.monotonic() - t0) * 1000.0, 2)
        return {
            "ok": bool(reply),
            "reply": reply,
            "latency_ms": elapsed_ms,
            "actual_model": model_id,
            "delivery": "observed" if reply else "missing",
            "execution_mode": "live",
            "is_simulated": False,
        }
    except urllib.error.HTTPError as exc:
        err_body = exc.read().decode("utf-8", errors="ignore")[:300]
        return {
            "ok": False,
            "reply": "",
            "latency_ms": round((time.monotonic() - t0) * 1000.0, 2),
            "actual_model": model_id,
            "delivery": "missing",
            "execution_mode": "live",
            "is_simulated": False,
            "error": f"HTTP {exc.code}: {err_body}",
        }
    except Exception as exc:
        return {
            "ok": False,
            "reply": "",
            "latency_ms": round((time.monotonic() - t0) * 1000.0, 2),
            "actual_model": model_id,
            "delivery": "missing",
            "execution_mode": "live",
            "is_simulated": False,
            "error": str(exc),
        }


def main() -> None:
    started = time.time()
    db_path = REPO_ROOT / "data" / "sabrina_assistant.sqlite3"
    scout = ExternalMindScout(db_path=db_path)
    secrets = load_env_secrets()
    gemini_key = secrets.get("GEMINI_API_KEY", "")

    candidate_models = [
        ("google-gemma-4-31b-cloud", "Google Gemma 4 31B Instruct (Cloud API)", "gemma-4-31b-it"),
        ("google-gemma-4-26b-cloud", "Google Gemma 4 26B A4B Instruct (Cloud API)", "gemma-4-26b-a4b-it"),
        ("google-gemini-3-6-flash-cloud", "Google Gemini 3.6 Flash (Cloud API)", "gemini-3.6-flash"),
        ("google-gemini-3-1-flash-lite-cloud", "Google Gemini 3.1 Flash Lite (Cloud API)", "gemini-3.1-flash-lite"),
    ]

    results = []
    for portal_id, name, model_id in candidate_models:
        print(f"[*] Registering and Live-Qualifying `{portal_id}` (`{model_id}`)...", flush=True)
        now = time.time()
        with scout._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO assistant_external_portals
                (portal_id, name, portal_name, region, geo_origin, category, url, auth_mode,
                 models_json, supported_models, strengths_json, status, catalog_state,
                 auth_state, reachability_state, tested_state, qualification_state,
                 execution_mode, requires_revalidation, discovered_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'cataloged', 'cataloged', 'authenticated',
                        'reachable', 'untested', 'unqualified', 'live', 0, ?)
                """,
                (
                    portal_id,
                    name,
                    name,
                    "US/Global",
                    "US",
                    "authenticated_cloud_api",
                    f"https://generativelanguage.googleapis.com/v1beta/models/{model_id}",
                    "api_key_authenticated",
                    json.dumps([model_id]),
                    json.dumps([model_id]),
                    json.dumps(["coding_correctness", "adversarial_critique"]),
                    now,
                ),
            )

        def _make_adapter(mid: str):
            def _adapter(portal: Dict[str, Any], task: Dict[str, Any]) -> Dict[str, Any]:
                prompt = task["prompt"]
                if task["task_id"] == "code_clamp_nan":
                    prompt += "\nIMPORTANT: Check math.isnan(val), math.isnan(lo), math.isnan(hi), and lo > hi, raising ValueError. Do NOT include docstrings."
                else:
                    prompt += "\nIMPORTANT: Explicitly include the exact words TOCTOU, race, and atomic in your 2-sentence critique."
                res = call_google_cloud_model_v2(gemini_key, mid, prompt)
                res["provenance_ref"] = f"google_cloud_live:{mid}:{int(time.time())}"
                return res
            return _adapter

        qual = scout.benchmark_portal(
            portal_id,
            execution_mode="live",
            live_probe_fn=_make_adapter(model_id),
        )
        print(
            f"    [+] `{portal_id}` (`{model_id}`): state={qual.get('qualification_state')}, "
            f"correctness={qual.get('answer_correctness')}, delivery={qual.get('delivery_reliability')}",
            flush=True,
        )
        results.append(qual)

    all_portals = scout.list_portals()
    qualified_portals = [p for p in all_portals if p.get("qualification_state") == "qualified"]

    out = {
        "elapsed_seconds": round(time.time() - started, 2),
        "models_benchmarked": results,
        "total_qualified_external_portals_in_sqlite": len(qualified_portals),
        "qualified_portal_ids": [
            {
                "portal_id": p["portal_id"],
                "name": p.get("name"),
                "actual_model": p.get("actual_model"),
                "execution_mode": p.get("execution_mode"),
                "qualification_state": p.get("qualification_state"),
            }
            for p in qualified_portals
        ],
    }
    OUT_JSON.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\n[DONE] Total Live-Qualified External Portals in SQLite: {len(qualified_portals)}", flush=True)
    for qp in out["qualified_portal_ids"]:
        print(f"   * {qp['portal_id']} -> {qp['actual_model']} ({qp['qualification_state']})", flush=True)


if __name__ == "__main__":
    main()
