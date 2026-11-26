#!/usr/bin/env python3
"""
Swarm Autonomous Artificial Immune System (AIS) Code Authorship
Dispatches 4 immunological guild tasks across 16 live external portals on ego:
1. Guild 1 (Macrophage / Innate Immunity): Antigen & Epitope Extraction
2. Guild 2 (Cytotoxic T-Cell / Viral Mutators): Adversarial Mutation & Fuzzing
3. Guild 3 (B-Cell / Somatic Hypermutation): Affinity Maturation & Neutralization Arena
4. Guild 4 (Memory B-Cell / Clonal Expansion): Durable Vaccination & Competence Selection
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

OUTPUT_DIR = REPO_ROOT / "artifacts" / "model_authored_code" / "immune_system"
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


IMMUNE_GUILD_TASKS: Dict[str, Dict[str, Any]] = {
    "guild_1_macrophage_antigen": {
        "biological_role": "Innate Immunity (Macrophage / Dendritic Cell)",
        "portals": [
            "hf-gradio-jayeshmurthi49-byte-agentic-rag-chatbot-api",
            "hf-gradio-himakesh-parth-ai-tutor",
            "hf-gradio-ksufian-ai-car-assistant",
            "hf-gradio-pelinbalci-pelin-notes-chat",
        ],
        "prompt": (
            "TASK: Write a standalone Python module implementing the Macrophage/Innate Immunity antigen extractor.\n\n"
            "Write TWO functions:\n"
            "1. `extract_antigen_epitope(code: str, exc_type: str, exc_msg: str, failing_input: str) -> dict`:\n"
            "   - Computes a deterministic `epitope_id` using hashlib.sha256 on f'{exc_type}::{exc_msg[:60]}::{failing_input}'.\n"
            "   - Returns a dict: {\n"
            "       'epitope_id': epitope_id[:16],\n"
            "       'bug_signature': f'{exc_type}:{exc_msg[:60]}',\n"
            "       'failing_input': str(failing_input),\n"
            "       'code_hash': hashlib.sha256(code.encode('utf-8')).hexdigest()[:16]\n"
            "     }.\n"
            "2. `check_memory_b_cell_immunity(blackboard, domain: str, epitope_bug_sig: str) -> dict | None`:\n"
            "   - Calls `patterns = blackboard.query_patterns(domain, epitope_bug_sig)`.\n"
            "   - If patterns is non-empty, returns the pattern with the highest `success_count`.\n"
            "   - Otherwise returns None.\n\n"
            "Return ONLY valid executable Python code with no markdown preamble."
        ),
        "required_symbols": ["extract_antigen_epitope", "check_memory_b_cell_immunity"],
    },
    "guild_2_tcell_mutator": {
        "biological_role": "Cytotoxic T-Cell (Viral Mutator & Adversarial Stressor)",
        "portals": [
            "hf-gradio-htchu-coderhelper",
            "hf-gradio-affanaiengr-rag",
            "hf-gradio-free234-snap-rag-assistant",
            "hf-gradio-lkeira108-resume-rag-chatbot",
        ],
        "prompt": (
            "TASK: Write a standalone Python module implementing viral mutation generation for adversarial stress-testing.\n\n"
            "Write ONE function:\n"
            "1. `synthesize_viral_mutations(base_input: any, domain: str) -> list`:\n"
            "   - Generates at least 6 deterministic adversarial mutations of `base_input` to test antibody generalization.\n"
            "   - If `base_input` is numeric (int/float): includes `base_input`, `0`, `-1`, `base_input * 2`, `base_input + 1000`, and `float('nan')`.\n"
            "   - If `base_input` is str: includes `base_input`, `''`, `' '`, `base_input * 5`, `base_input + '\\x00\\n'`, and `'<script>alert(1)</script>'`.\n"
            "   - If `base_input` is dict/list: includes `base_input`, empty collection, collection with None, and nested variant.\n"
            "   - Otherwise returns `[base_input, None, str(base_input), 0, '', False]`.\n"
            "   - Returns the list of mutated viral variants.\n\n"
            "Return ONLY valid executable Python code with no markdown preamble."
        ),
        "required_symbols": ["synthesize_viral_mutations"],
    },
    "guild_3_bcell_affinity_arena": {
        "biological_role": "B-Cell Affinity Maturation (Neutralization Arena)",
        "portals": [
            "hf-gradio-dkt3-digital-twin",
            "hf-gradio-rstanley2026-rodney-digital-twin",
            "hf-gradio-parmeshlata-portfolio-chatbot",
            "hf-gradio-netrisyl-lobels-stores-assistant",
        ],
        "prompt": (
            "TASK: Write a standalone Python module implementing antibody affinity evaluation.\n\n"
            "Write ONE function:\n"
            "1. `evaluate_antibody_affinity(candidate_fn: callable, viral_mutations: list, validator_fn: callable) -> dict`:\n"
            "   - Iterates through `viral_mutations`.\n"
            "   - For each `virus_inp` in `viral_mutations`, invokes `candidate_fn(virus_inp)` inside a try/except.\n"
            "   - If invocation succeeds and `validator_fn(virus_inp, output)` is True, counts as neutralized.\n"
            "   - Otherwise, records `virus_inp` into a `surviving_viruses` list.\n"
            "   - Returns a dict: {\n"
            "       'affinity_score': round(neutralized / max(1, len(viral_mutations)), 4),\n"
            "       'passed': neutralized,\n"
            "       'total': len(viral_mutations),\n"
            "       'surviving_viruses': surviving_viruses\n"
            "     }.\n\n"
            "Return ONLY valid executable Python code with no markdown preamble."
        ),
        "required_symbols": ["evaluate_antibody_affinity"],
    },
    "guild_4_memory_bcell_expansion": {
        "biological_role": "Memory B-Cell & Clonal Expansion (Vaccination Engine)",
        "portals": [
            "hf-gradio-hamza46758-my-gaia-agent",
            "hf-gradio-mahmudnadim479-krishiguard",
            "hf-gradio-kshitijthakkar-shlokartha-playground",
            "hf-gradio-nakaassoumatine-einstein-chatbot",
        ],
        "prompt": (
            "TASK: Write a standalone Python module implementing Memory B-Cell persistence and clonal expansion.\n\n"
            "Write ONE function:\n"
            "1. `commit_immunological_memory(blackboard, domain: str, epitope: dict, winning_portal: str, antibody_remedy: str, antibody_code: str, affinity_score: float, latency_ms: float) -> str`:\n"
            "   - Calls `blackboard.record_hypothesis(epitope['epitope_id'], domain, winning_portal, antibody_remedy, float(affinity_score), 'immunized')`.\n"
            "   - If `affinity_score >= 1.0`:\n"
            "     calls `blackboard.record_solution_pattern(f\"ab_{epitope['epitope_id']}\", domain, epitope['bug_signature'], antibody_remedy, antibody_code)`.\n"
            "   - Calls `blackboard.update_model_competence(winning_portal, domain, True, affinity_score >= 1.0, float(latency_ms))`.\n"
            "   - Returns `f\"ab_{epitope['epitope_id']}\"`.\n\n"
            "Return ONLY valid executable Python code with no markdown preamble."
        ),
        "required_symbols": ["commit_immunological_memory"],
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
    print("  SWARM ARTIFICIAL IMMUNE SYSTEM (AIS) 16-PORTAL CODE AUTHORSHIP")
    print("=" * 78)

    pool = KindCollectivePool(min_cooldown_sec=0.5, db_path=DEFAULT_DB_PATH, enable_semantic_cache=False)
    print(f"[*] Pool Portals Online: {len(pool._harnesses)}")

    summary_report: Dict[str, Any] = {
        "total_portals_in_pool": len(pool._harnesses),
        "guilds": {},
    }

    for guild_id, cfg in IMMUNE_GUILD_TASKS.items():
        print(f"\n--- [{guild_id}] Role: {cfg['biological_role']} ---", flush=True)
        portals = [p for p in cfg["portals"] if p in pool._harnesses]
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

        valid_cands = [c for c in guild_candidates if c.get("compiled") and c.get("symbols_ok")]
        valid_cands.sort(key=lambda x: float(x.get("latency_ms") or 999999.0))
        winner = valid_cands[0] if valid_cands else None
        if winner:
            win_src = REPO_ROOT / winner["file"]
            canonical_dest = OUTPUT_DIR / f"{guild_id}_winner.py"
            canonical_dest.write_text(win_src.read_text(encoding="utf-8"), encoding="utf-8")
            print(f"  [WINNER] {winner['portal_id']} -> {canonical_dest.relative_to(REPO_ROOT)}")

        summary_report["guilds"][guild_id] = {
            "biological_role": cfg["biological_role"],
            "winner": winner,
            "candidates": guild_candidates,
        }

    report_path = OUTPUT_DIR / "swarm_immune_system_report.json"
    report_path.write_text(json.dumps(summary_report, indent=2), encoding="utf-8")
    print(f"\n[+] Saved AIS report to {report_path.relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    main()
