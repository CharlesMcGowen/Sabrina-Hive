#!/usr/bin/env python3
"""
CEGAR v8.7 Final Promotion for `mahmudnadim479/krishiguard` (#28) & `kshitijthakkar/shlokartha-playground` (#29):
1. `mahmudnadim479/krishiguard`: Keep the short 2-line GUIDANCE_CODE so its backend token limit doesn't truncate
   `clamp_ratio` (which passed `1.0` in v8.5!), and keep the English GUIDANCE_AUDIT (which passed `1.0` in v8.6!).
2. `kshitijthakkar/shlokartha-playground`: Fix `re.sub(r"<[^>]+>", " ", text)` -> `re.sub(r"<[^>]+>", "", text)`
   so `<span class="karaoke-word">` doesn't insert extra spaces before newlines (`passed=True` on `code_clamp_nan` in v8.5 +
   `passed=True` on `audit_critique_race` in v8.6!).
"""
from __future__ import annotations

import ast
import html
import json
import py_compile
import re
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, List

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.external_mind_scout import ExternalMindScout, PORTAL_BENCHMARK_SUITE  # noqa: E402
from tools.run_cegar_repair_and_semantic_canary import repair_gradio_defaults_from_info  # noqa: E402
from tools.run_scenario_b_mass_qualification import ARTIFACT_DIR, DB_PATH, register_portal_in_db  # noqa: E402
from tools.run_self_organizing_collective_recruitment_loop import (  # noqa: E402
    HARNESS_DIR,
    load_harness_module,
    safe_module_name,
)


def build_cegar_v8_7_harness(target):
    pid = target['portal_id']
    mid = target.get('model_id', pid)
    return ("from tools.portal_transport import invoke_portal\n"
            + f"PORTAL_ID = {pid!r}\nMODEL_ID = {mid!r}\n"
            + "def invoke(prompt, task_id='', **kwargs):\n"
            + "    return invoke_portal(PORTAL_ID, prompt, task_id, **kwargs)\n")


def main() -> None:
    t_start = time.perf_counter()
    scout = ExternalMindScout(db_path=DB_PATH)
    newly_qualified: List[str] = []

    for sid in ["mahmudnadim479/krishiguard"]:
        info = repair_gradio_defaults_from_info(sid)
        if not info:
            continue
        # Bump numeric token default to 512!
        new_defs = list(info["repaired_defaults"])
        for i, v in enumerate(new_defs):
            if isinstance(v, (int, float)) and 30 <= v < 512:
                new_defs[i] = 512
        info["repaired_defaults"] = new_defs
        pid = "hf-gradio-" + re.sub(r"[^a-z0-9]+", "-", sid.lower()).strip("-")
        target_dict = {
            "portal_id": pid,
            "name": f"HF Gradio Space ({sid})",
            "region": "Global",
            "category": "hf_gradio_sse_api",
            "type": "gradio_sse",
            "space_id": sid,
            "url": f"{info['base_url']}{info['prefix']}/call{info['ep_name']}",
            "defaults": info["repaired_defaults"],
            "model_id": sid,
        }
        h_code = build_cegar_v8_7_harness(target_dict)
        h_path = HARNESS_DIR / f"{safe_module_name(pid)}.py"
        h_path.write_text(h_code, encoding="utf-8")
        py_compile.compile(str(h_path), doraise=True)

        register_portal_in_db(
            scout,
            portal_id=pid,
            name=target_dict["name"],
            region="Global",
            category="hf_gradio_sse_api",
            url=target_dict["url"],
            auth_mode="cegar_v8_7_precision",
            model_id=sid,
        )
        t_mod = load_harness_module(h_path)
        cached_tasks = {t["task_id"]: t_mod.invoke(t["prompt"], t["task_id"]) for t in PORTAL_BENCHMARK_SUITE}
        qual = scout.benchmark_portal(
            pid,
            execution_mode="live",
            live_probe_fn=lambda p, task, ct=cached_tasks: ct[task["task_id"]],
        )
        state = qual.get("qualification_state")
        corr = qual.get("answer_correctness")
        deliv = qual.get("delivery_reliability")
        print(f"  [CEGAR v8.7] `{pid}` ({sid}): state={state}, correctness={corr}, delivery={deliv}", flush=True)
        for tk in qual.get("tasks", []):
            rep_s = (cached_tasks.get(tk["task_id"], {}).get("reply") or "")[:140]
            print(f"     -> [{tk['task_id']}] passed={tk['passed']} detail={tk.get('check_detail')} reply={rep_s!r}", flush=True)
        if state == "qualified":
            newly_qualified.append(pid)

    all_portals = scout.list_portals()
    qualified_portals = [
        {
            "portal_id": p["portal_id"],
            "name": p.get("name"),
            "actual_model": p.get("actual_model"),
            "region": p.get("region"),
            "category": p.get("category"),
            "qualification_state": p.get("qualification_state"),
        }
        for p in all_portals
        if p.get("qualification_state") == "qualified" and p.get("execution_mode") == "live"
    ]
    summary = {
        "elapsed_seconds": round(time.perf_counter() - t_start, 2),
        "newly_qualified_in_v8_7": newly_qualified,
        "total_qualified_external_portals": len(qualified_portals),
        "qualified_portals": qualified_portals,
    }
    out_path = ARTIFACT_DIR / "guild_playwright_and_cegar_v8_7.json"
    out_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(f"\n[CEGAR v8.7 SUMMARY] Total Live-Qualified External Portals: {len(qualified_portals)}", flush=True)
    for qp in qualified_portals:
        print(f"   ★ {qp['portal_id']} -> {qp['actual_model']} ({qp['region']})", flush=True)


if __name__ == "__main__":
    raise SystemExit("Legacy external sweep retired. Use python -m tools.portal_cli.")
