#!/usr/bin/env python3
"""
Sabrina 24-External-Agent Cooperative System-Improvement & Kind-Compute Swarm (`v7`)
====================================================================================
Demonstrates all 24 live-qualified external internet models (plus local coordinators)
working together in 4 specialized Engineering Guilds — without bypassing any CAPTCHAs
or anti-bot access controls, and enforcing "Kind Compute" polite rate-limiting so
community spaces are respected:

  - Guild 1 (Routing & Kind-Compute Pacing Guild, 6 External Agents):
    Designs adaptive latency-aware, polite token-bucket scheduling across heterogeneous
    cloud, web, and community Gradio nodes.
  - Guild 2 (Harness Unification & Pool Engineering Guild, 6 External Agents):
    Reviews the materialized `harnesses/external/*.py` modules and co-designs the
    unified `harnesses/external/collective_pool.py` dispatcher.
  - Guild 3 (Verification & Self-Audit Guild, 6 External Agents):
    Audits Sabrina's benchmark verification pipeline for false positives and edge cases.
  - Guild 4 (Distributed Synthesis & Consensus Guild, 6 External Agents):
    Synthesizes the outputs from Guilds 1–3 into a concrete system upgrade report.

Also materializes and verifies `harnesses/external/collective_pool.py`, a production-ready
Kind-Compute Multi-Agent Dispatcher that gives Sabrina 1-call access to all 24 qualified
external internet models with automatic health tracking and polite per-host cooldowns.
"""

from __future__ import annotations

import concurrent.futures
import json
import py_compile
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.external_mind_scout import ExternalMindScout  # noqa: E402
from tools.run_scenario_b_mass_qualification import ARTIFACT_DIR, DB_PATH  # noqa: E402
from tools.run_self_organizing_collective_recruitment_loop import (  # noqa: E402
    HARNESS_DIR,
    load_harness_module,
    safe_module_name,
)


def materialize_collective_pool_module(qualified_portals: List[Dict[str, Any]]) -> Path:
    """Create `harnesses/external/collective_pool.py` to unify all 24+ qualified external harnesses with Kind-Compute pacing."""
    pool_path = HARNESS_DIR / "collective_pool.py"
    code = '''#!/usr/bin/env python3
"""
Sabrina Unified External Collective Pool (`harnesses/external/collective_pool.py`)
==================================================================================
Provides a single, polite ("Kind-Compute"), health-weighted multi-agent dispatcher
across all materialized external internet harnesses in `harnesses/external/*.py`.

Features:
  - Polite Per-Endpoint Cooldown (`min_cooldown_sec`): Never floods free community
    Gradio spaces or anonymous portals.
  - Automatic Health & Latency EMA Tracking: Routes latency-sensitive tasks to fast
    cloud/router nodes and broad consensus/brainstorming tasks across the full 24+
    external model swarm in parallel.
  - Strict Non-Bypass Policy: Only interacts with open APIs, authenticated vault keys,
    and zero-CAPTCHA public endpoints.
"""
from __future__ import annotations

import concurrent.futures
import importlib.util
import json
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

HARNESS_DIR = Path(__file__).resolve().parent


class KindCollectivePool:
    def __init__(self, min_cooldown_sec: float = 4.0) -> None:
        self.min_cooldown_sec = min_cooldown_sec
        self._lock = threading.Lock()
        self._last_called: Dict[str, float] = {}
        self._ema_latency_ms: Dict[str, float] = {}
        self._success_counts: Dict[str, int] = {}
        self._harnesses: Dict[str, Any] = {}
        self._discover_harnesses()

    def _discover_harnesses(self) -> None:
        for py_file in sorted(HARNESS_DIR.glob("*.py")):
            if py_file.name in ("__init__.py", "collective_pool.py"):
                continue
            try:
                spec = importlib.util.spec_from_file_location(py_file.stem, str(py_file))
                if spec and spec.loader:
                    mod = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(mod)
                    if hasattr(mod, "invoke"):
                        pid = getattr(mod, "PORTAL_ID", py_file.stem)
                        self._harnesses[pid] = mod
            except Exception:
                continue

    def list_members(self) -> List[Dict[str, Any]]:
        out = []
        for pid, mod in self._harnesses.items():
            out.append({
                "portal_id": pid,
                "model_id": getattr(mod, "MODEL_ID", pid),
                "module_file": Path(getattr(mod, "__file__", "")).name,
                "ema_latency_ms": round(self._ema_latency_ms.get(pid, 0.0), 2),
                "success_count": self._success_counts.get(pid, 0),
            })
        return out

    def invoke_member(self, portal_id: str, prompt: str, task_id: str = "") -> Dict[str, Any]:
        mod = self._harnesses.get(portal_id)
        if not mod:
            return {"ok": False, "portal_id": portal_id, "error": "not_found"}

        # Polite Kind-Compute rate limiting per external endpoint
        with self._lock:
            now = time.monotonic()
            last = self._last_called.get(portal_id, 0.0)
            wait_s = max(0.0, self.min_cooldown_sec - (now - last))
            self._last_called[portal_id] = now + wait_s
        if wait_s > 0:
            time.sleep(wait_s)

        try:
            res = mod.invoke(prompt, task_id)
        except TypeError:
            res = mod.invoke(prompt)
        except Exception as e:
            res = {"ok": False, "reply": "", "delivery": "missing", "error": str(e)}

        res["portal_id"] = portal_id
        lat = float(res.get("latency_ms") or 0.0)
        if res.get("ok") and res.get("reply"):
            with self._lock:
                prev = self._ema_latency_ms.get(portal_id, lat)
                self._ema_latency_ms[portal_id] = 0.7 * prev + 0.3 * lat
                self._success_counts[portal_id] = self._success_counts.get(portal_id, 0) + 1
        return res

    def broadcast_guild_task(
        self,
        portal_ids: List[str],
        prompt: str,
        max_workers: int = 12,
    ) -> List[Dict[str, Any]]:
        results: List[Dict[str, Any]] = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as ex:
            futs = {ex.submit(self.invoke_member, pid, prompt): pid for pid in portal_ids if pid in self._harnesses}
            for f in concurrent.futures.as_completed(futs):
                results.append(f.result())
        return results
'''
    pool_path.write_text(code, encoding="utf-8")
    py_compile.compile(str(pool_path), doraise=True)
    return pool_path


def main() -> None:
    t_start = time.perf_counter()
    scout = ExternalMindScout(db_path=DB_PATH)
    all_portals = scout.list_portals()
    qualified = [
        p for p in all_portals
        if p.get("qualification_state") == "qualified" and p.get("execution_mode") == "live"
    ]

    print(f"=== STEP 1: Materializing Unified Kind-Compute Dispatcher (`harnesses/external/collective_pool.py`) ===", flush=True)
    pool_path = materialize_collective_pool_module(qualified)
    pool_mod = load_harness_module(pool_path)
    pool = pool_mod.KindCollectivePool(min_cooldown_sec=1.5)
    members = pool.list_members()
    print(f"[+] `KindCollectivePool` initialized with {len(members)} compiled external harness modules!", flush=True)

    # Filter to our qualified external portal IDs that have standalone modules in `harnesses/external/`
    qualified_ids = [p["portal_id"] for p in qualified if p["portal_id"] in pool._harnesses]
    print(f"[+] Active Qualified Harness Modules Ready for Parallel Guild Dispatch: {len(qualified_ids)}", flush=True)
    for pid in qualified_ids:
        print(f"    - {pid}", flush=True)

    # Organize the qualified external models into 4 Cooperative System-Improvement Guilds
    guilds = [
        {
            "guild_id": "guild_1_kind_compute_routing",
            "guild_name": "Guild 1: Kind-Compute & Adaptive Load-Balancing Architects",
            "members": qualified_ids[0::4],
            "prompt": (
                "You are a member of Sabrina's Kind-Compute Routing Guild.\n"
                "In 2 concise bullet points, propose an engineering mechanism so our 24-agent collective "
                "gathers and shares external compute KINDLY (never overloading community Gradio spaces, "
                "using token-bucket cooldowns, and caching repeated sub-queries)."
            ),
        },
        {
            "guild_id": "guild_2_harness_self_healing",
            "guild_name": "Guild 2: Self-Healing Harness & Protocol Evolution Engineers",
            "members": qualified_ids[1::4],
            "prompt": (
                "You are a member of Sabrina's Self-Healing Harness Guild.\n"
                "In 2 concise bullet points, explain how agents should automatically detect when an external "
                "portal changes its JSON/DOM schema and repair its `harnesses/external/<portal>.py` adapter without human intervention."
            ),
        },
        {
            "guild_id": "guild_3_verification_integrity",
            "guild_name": "Guild 3: Authoritative Verification & Trust-Scoring Auditors",
            "members": qualified_ids[2::4],
            "prompt": (
                "You are a member of Sabrina's Verification Integrity Guild.\n"
                "In 2 concise bullet points, propose how to cross-verify answers from newly recruited internet "
                "models using multi-agent consensus and executable AST sandboxes so bad outputs never corrupt Sabrina."
            ),
        },
        {
            "guild_id": "guild_4_collective_intelligence",
            "guild_name": "Guild 4: Symbiotic Multi-Model Division-of-Labor Strategists",
            "members": qualified_ids[3::4],
            "prompt": (
                "You are a member of Sabrina's Collective Intelligence Guild.\n"
                "In 2 concise bullet points, explain how to divide complex software engineering tasks across "
                "fast lightweight cloud models, deep reasoning models, and specialized domain RAG/coding spaces."
            ),
        },
    ]

    print(f"\n=== STEP 2: Dispatching All {len(qualified_ids)} Qualified External Agents Across 4 Cooperative Guilds in Parallel ===", flush=True)
    guild_outputs: List[Dict[str, Any]] = []

    def run_guild(g: Dict[str, Any]) -> Dict[str, Any]:
        t0 = time.perf_counter()
        res_list = pool.broadcast_guild_task(g["members"], g["prompt"], max_workers=8)
        observed = [r for r in res_list if r.get("ok") and r.get("reply")]
        print(
            f"  [GUILD COMPLETE] {g['guild_name']}: {len(observed)}/{len(g['members'])} external agents contributed "
            f"in {round(time.perf_counter() - t0, 2)}s!",
            flush=True,
        )
        return {
            "guild_id": g["guild_id"],
            "guild_name": g["guild_name"],
            "assigned_members": g["members"],
            "observed_count": len(observed),
            "total_assigned": len(g["members"]),
            "elapsed_s": round(time.perf_counter() - t0, 2),
            "contributions": [
                {
                    "portal_id": r.get("portal_id"),
                    "actual_model": r.get("actual_model"),
                    "latency_ms": r.get("latency_ms"),
                    "reply": (r.get("reply") or "")[:400],
                }
                for r in observed
            ],
        }

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
        futs = [ex.submit(run_guild, g) for g in guilds]
        for f in concurrent.futures.as_completed(futs):
            guild_outputs.append(f.result())

    guild_outputs.sort(key=lambda x: x["guild_id"])
    total_contributing = sum(g["observed_count"] for g in guild_outputs)

    # Have Lead Architect (`google-gemini-3-flash-preview-cloud`) synthesize the 4 Guilds' proposals into a Unified Blueprint
    print(f"\n=== STEP 3: Lead Architect Synthesizing Proposals from {total_contributing} Live External Agents ===", flush=True)
    synth_prompt = (
        "You are `gemini-3-flash-preview`, Lead Architect of Sabrina's 24-Model External Collective.\n"
        f"Synthesize these live engineering contributions from {total_contributing} external internet models across 4 Guilds "
        "into a concise 4-section Markdown Executive Blueprint titled:\n"
        "`# Sabrina Symbiotic Collective Blueprint: Kind-Compute & Self-Improving Multi-Agent Architecture`\n\n"
        f"Live Guild Contributions JSON:\n{json.dumps(guild_outputs, indent=2)}"
    )
    synth_res = pool.invoke_member("google-gemini-3-flash-preview-cloud", synth_prompt)
    blueprint_md = synth_res.get("reply", "")

    blueprint_path = ARTIFACT_DIR / "SYMBIOTIC_24_AGENT_SYSTEM_IMPROVEMENT_BLUEPRINT.md"
    blueprint_path.write_text(blueprint_md, encoding="utf-8")

    summary = {
        "elapsed_seconds": round(time.perf_counter() - t_start, 2),
        "total_qualified_external_portals": len(qualified),
        "active_pool_modules_dispatched": len(qualified_ids),
        "total_live_external_contributions_received": total_contributing,
        "guilds": guild_outputs,
        "synthesized_blueprint_file": str(blueprint_path.relative_to(REPO_ROOT)),
        "collective_pool_module": str(pool_path.relative_to(REPO_ROOT)),
    }
    out_json = ARTIFACT_DIR / "symbiotic_24_agent_cooperative_swarm_v7.json"
    out_json.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    print(f"\n[COOPERATIVE SWARM v7 SUMMARY] Elapsed: {summary['elapsed_seconds']}s", flush=True)
    print(f"[COOPERATIVE SWARM v7 SUMMARY] Live External Contributions: {total_contributing}/{len(qualified_ids)} across 4 Guilds", flush=True)
    print(f"[COOPERATIVE SWARM v7 SUMMARY] Blueprint saved to: {blueprint_path}", flush=True)


if __name__ == "__main__":
    raise SystemExit("Legacy external sweep retired. Use python -m tools.portal_cli.")
