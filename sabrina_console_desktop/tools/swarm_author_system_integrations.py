#!/usr/bin/env python3
"""
Swarm Autonomous Multi-System Integration Authorship
Dispatches integration tasks across 12+ live external portals in parallel guilds
so the swarm models themselves write the integration code connecting
`HierarchicalCognitiveBlackboard` v2 and the 200+ portal pool into:
1. Guild A: `CollectiveRouter` (`tools/collective_router_and_shadow_daemon.py`)
2. Guild B: `UnifiedOracleHub` (`tools/oracle_providers.py`)
3. Guild C: `CollectiveProblemSolvingEngine` (`tools/collective_problem_solving_engine.py`)
4. Guild D: `SharedBlackboard` (`ai_systems/framework/blackboard.py`)
"""
from __future__ import annotations

import ast
import concurrent.futures
import hashlib
import json
import py_compile
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from harnesses.external.collective_pool import DEFAULT_DB_PATH, KindCollectivePool

OUTPUT_DIR = REPO_ROOT / "artifacts" / "model_authored_code" / "system_integrations"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def extract_python_code(raw_text: str) -> str:
    cleaned = (raw_text or "").strip()
    if cleaned.startswith("FULL REPLY:"):
        cleaned = cleaned[len("FULL REPLY:"):].strip()
    blocks = re.findall(r"```(?:python)?\s*(.*?)\s*```", cleaned, re.DOTALL)
    if blocks:
        return max(blocks, key=len).strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        cleaned = "\n".join(lines).strip()
    return cleaned


def compute_ast_fp(code: str) -> str:
    try:
        tree = ast.parse(code)
        dumped = ast.dump(tree, include_attributes=False)
        return hashlib.sha256(dumped.encode("utf-8")).hexdigest()[:16]
    except Exception as e:
        return f"invalid_syntax:{type(e).__name__}"


GUILD_TASKS: Dict[str, Dict[str, Any]] = {
    "guild_a_router_bridge": {
        "target_system": "CollectiveRouter & KindCollectivePool",
        "portals": [
            "hf-gradio-jayeshmurthi49-byte-agentic-rag-chatbot-api",
            "hf-gradio-himakesh-parth-ai-tutor",
            "hf-gradio-ksufian-ai-car-assistant",
            "hf-gradio-pelinbalci-pelin-notes-chat",
        ],
        "prompt": (
            "TASK: Write a standalone Python module containing helper functions to integrate "
            "`HierarchicalCognitiveBlackboard` with `CollectiveRouter`.\n\n"
            "Write TWO functions:\n"
            "1. `enrich_prompt_with_blackboard_remedies(blackboard, prompt: str, domain: str, bug_sig: str = None) -> str`:\n"
            "   - Calls `patterns = blackboard.query_patterns(domain, bug_sig)`.\n"
            "   - If patterns is non-empty, appends a `# KNOWN DOMAIN REMEDIES:` section listing up to 3 `bug_signature` -> `remedy` pairs to `prompt` and returns it; otherwise returns `prompt` unchanged.\n"
            "2. `prioritize_committee_by_competence(blackboard, domain: str, default_portals: list[str], committee_size: int = 5) -> list[str]`:\n"
            "   - Calls `ranking = blackboard.get_competence_ranking(domain)`.\n"
            "   - Extracts `model_id` for entries with `reliability > 0`, places them first, then appends any portals from `default_portals` not already in the list, returning up to `committee_size` items.\n\n"
            "Return ONLY valid executable Python code with no markdown explanation."
        ),
        "required_symbols": ["enrich_prompt_with_blackboard_remedies", "prioritize_committee_by_competence"],
    },
    "guild_b_oracle_hub_bridge": {
        "target_system": "UnifiedOracleHub (tools/oracle_providers.py)",
        "portals": [
            "hf-gradio-htchu-coderhelper",
            "hf-gradio-affanaiengr-rag",
            "hf-gradio-free234-snap-rag-assistant",
            "hf-gradio-lkeira108-resume-rag-chatbot",
        ],
        "prompt": (
            "TASK: Write a standalone Python module providing the `UnifiedOracleHub` collective fallback and blackboard bridge.\n\n"
            "Write TWO functions:\n"
            "1. `oracle_collective_fallback(collective_router, Blackboard, system: str, prompt: str, tier: str = 'auto', domain: str = 'oracle_general') -> tuple[str | None, str]`:\n"
            "   - Combines `full_prompt = f'{system}\\n\\n{prompt}'.strip() if system else prompt`.\n"
            "   - Calls `res = collective_router.route(full_prompt, tier=tier)`.\n"
            "   - If `res.get('ok')` and `res.get('reply')`, calls `Blackboard.update_model_competence(res.get('portal_id', 'unknown'), domain, True, True, float(res.get('latency_ms', 0.0)))` and returns `(res['reply'], f\"collective:{res.get('portal_id')}:{res.get('actual_model', 'unknown')}\")`.\n"
            "   - Otherwise returns `(None, 'fallback_none')`.\n"
            "2. `record_oracle_attempt_on_blackboard(blackboard, provider_label: str, domain: str, ok: bool, latency_ms: float) -> None`:\n"
            "   - Calls `blackboard.update_model_competence(provider_label, domain, ok, ok, float(latency_ms))`.\n\n"
            "Return ONLY valid executable Python code with no markdown explanation."
        ),
        "required_symbols": ["oracle_collective_fallback", "record_oracle_attempt_on_blackboard"],
    },
    "guild_c_problem_solving_bridge": {
        "target_system": "CollectiveProblemSolvingEngine (tools/collective_problem_solving_engine.py)",
        "portals": [
            "hf-gradio-dkt3-digital-twin",
            "hf-gradio-rstanley2026-rodney-digital-twin",
            "hf-gradio-parmeshlata-portfolio-chatbot",
            "hf-gradio-netrisyl-lobels-stores-assistant",
        ],
        "prompt": (
            "TASK: Write a standalone Python module connecting `CollectiveProblemSolvingEngine` to `HierarchicalCognitiveBlackboard`.\n\n"
            "Write TWO functions:\n"
            "1. `select_competent_model_for_capability(blackboard, capability: str, fallback_candidates: list[str]) -> str`:\n"
            "   - Calls `ranked = blackboard.get_competence_ranking(capability.lower())`.\n"
            "   - If `ranked` is non-empty and `ranked[0].get('reliability', 0) > 0`, returns `ranked[0]['model_id']`.\n"
            "   - Otherwise returns `fallback_candidates[0]` if `fallback_candidates` is non-empty else `'qwen2.5-coder:14b'`.\n"
            "2. `sync_peer_resolution_to_blackboard(blackboard, request_id: str, capability: str, helper_model: str, diagnosis: str, proposed_solution: str, confidence: float) -> None`:\n"
            "   - Calls `blackboard.record_hypothesis(request_id, capability.lower(), helper_model, diagnosis, float(confidence), 'verified')`.\n"
            "   - Calls `blackboard.record_solution_pattern(f'pat_{request_id}', capability.lower(), diagnosis[:80], proposed_solution, '')`.\n\n"
            "Return ONLY valid executable Python code with no markdown explanation."
        ),
        "required_symbols": ["select_competent_model_for_capability", "sync_peer_resolution_to_blackboard"],
    },
    "guild_d_shared_blackboard_bridge": {
        "target_system": "SharedBlackboard (ai_systems/framework/blackboard.py)",
        "portals": [
            "hf-gradio-hamza46758-my-gaia-agent",
            "hf-gradio-mahmudnadim479-krishiguard",
            "hf-gradio-kshitijthakkar-shlokartha-playground",
            "hf-gradio-nakaassoumatine-einstein-chatbot",
        ],
        "prompt": (
            "TASK: Write a standalone Python module bridging `SharedBlackboard` security findings to `HierarchicalCognitiveBlackboard`.\n\n"
            "Write TWO functions:\n"
            "1. `persist_finding_to_cognitive_blackboard(blackboard, finding: dict) -> str`:\n"
            "   - Extracts `vuln_type = str(finding.get('vuln_type', 'general')).lower()`, `url = str(finding.get('url', 'unknown'))`, `sev = str(finding.get('severity', 'info')).lower()`.\n"
            "   - Computes `hyp_id = f'finding_{vuln_type}_{abs(hash(url)) % 100000}'`.\n"
            "   - Maps confidence: `0.95` if `sev in ('critical', 'high')` else `0.75`.\n"
            "   - Calls `blackboard.record_hypothesis(hyp_id, vuln_type, str(finding.get('agent', 'swarm')), f'{vuln_type} on {url}', conf, 'verified')`.\n"
            "   - Calls `blackboard.record_solution_pattern(f'pat_{hyp_id}', vuln_type, url, str(finding.get('evidence', finding.get('description', 'verified finding'))), '')`.\n"
            "   - Returns `hyp_id`.\n"
            "2. `get_ prior_vuln_patterns(blackboard, vuln_type: str) -> list[dict]` (named `get_prior_vuln_patterns`):\n"
            "   - Returns `blackboard.query_patterns(str(vuln_type).lower())`.\n\n"
            "Return ONLY valid executable Python code with no markdown explanation."
        ),
        "required_symbols": ["persist_finding_to_cognitive_blackboard", "get_prior_vuln_patterns"],
    },
}


def verify_candidate_module(file_path: Path, required_symbols: List[str]) -> Dict[str, Any]:
    try:
        py_compile.compile(str(file_path), doraise=True)
    except Exception as e:
        return {"compiled": False, "symbols_ok": False, "error": str(e)}

    code = file_path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(code)
    except Exception as e:
        return {"compiled": False, "symbols_ok": False, "error": str(e)}

    defined_funcs = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    missing = [s for s in required_symbols if s not in defined_funcs]
    return {
        "compiled": True,
        "symbols_ok": len(missing) == 0,
        "defined_functions": sorted(defined_funcs),
        "missing_symbols": missing,
        "ast_fp": compute_ast_fp(code),
    }


def main() -> None:
    print("=" * 78)
    print("  SWARM 16-PORTAL MULTI-GUILD SYSTEM INTEGRATION CODE AUTHORSHIP")
    print("=" * 78)

    pool = KindCollectivePool(min_cooldown_sec=0.5, db_path=DEFAULT_DB_PATH, enable_semantic_cache=False)
    print(f"[*] Total Discovered External Portal Harnesses in Pool: {len(pool._harnesses)}")

    summary_report: Dict[str, Any] = {
        "total_portals_in_pool": len(pool._harnesses),
        "guilds": {},
    }

    for guild_id, cfg in GUILD_TASKS.items():
        print(f"\n--- [{guild_id}] Target: {cfg['target_system']} ---", flush=True)
        portals = [p for p in cfg["portals"] if p in pool._harnesses]
        # Add backup portals from pool if any requested portal isn't in _harnesses
        if len(portals) < 4:
            for cand in pool.get_tier_candidates("auto"):
                if cand not in portals:
                    portals.append(cand)
                if len(portals) >= 4:
                    break

        guild_candidates: List[Dict[str, Any]] = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
            fut_map = {
                ex.submit(pool.invoke_member, pid, cfg["prompt"], guild_id, False): pid
                for pid in portals
            }
            for fut in concurrent.futures.as_completed(fut_map):
                pid = fut_map[fut]
                res = fut.result()
                raw_reply = res.get("reply", "")
                code = extract_python_code(raw_reply)
                slug = re.sub(r"[^a-zA-Z0-9_]", "_", pid).lower()
                cand_file = OUTPUT_DIR / f"{guild_id}__{slug}.py"
                cand_file.write_text(code, encoding="utf-8")

                ver = verify_candidate_module(cand_file, cfg["required_symbols"]) if code else {
                    "compiled": False,
                    "symbols_ok": False,
                    "error": "empty_reply",
                }
                entry = {
                    "portal_id": pid,
                    "actual_model": res.get("actual_model", pid),
                    "latency_ms": res.get("latency_ms", 0.0),
                    "bytes": len(code),
                    "file": str(cand_file.relative_to(REPO_ROOT)),
                    **ver,
                }
                guild_candidates.append(entry)
                print(
                    f"  -> {pid}: compiled={ver.get('compiled')} | symbols_ok={ver.get('symbols_ok')} "
                    f"| bytes={len(code)} | latency={res.get('latency_ms')}ms",
                    flush=True,
                )

        # Select winning swarm-authored module for this guild
        valid_cands = [c for c in guild_candidates if c.get("compiled") and c.get("symbols_ok")]
        valid_cands.sort(key=lambda x: float(x.get("latency_ms") or 999999.0))
        winner = valid_cands[0] if valid_cands else None
        if winner:
            win_src = REPO_ROOT / winner["file"]
            canonical_dest = OUTPUT_DIR / f"{guild_id}_winner.py"
            canonical_dest.write_text(win_src.read_text(encoding="utf-8"), encoding="utf-8")
            print(f"  [WINNER] {winner['portal_id']} -> {canonical_dest.relative_to(REPO_ROOT)}")

        summary_report["guilds"][guild_id] = {
            "target_system": cfg["target_system"],
            "winner": winner,
            "candidates": guild_candidates,
        }

    report_path = OUTPUT_DIR / "swarm_system_integrations_report.json"
    report_path.write_text(json.dumps(summary_report, indent=2), encoding="utf-8")
    print(f"\n[+] Saved report to {report_path.relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    main()
