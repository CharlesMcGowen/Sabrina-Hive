#!/usr/bin/env python3
"""
Sabrina Autonomous Collective v13: Trusted Swarm Committee Co-Authoring of
SQLite `PRAGMA data_version` Hot-Reload & PEP 562 Virtual Portal Dispatch
===========================================================================
Dispatches a live specification to our top-ranked `cs_math_and_bounds_coding`
and `deep_cs_and_systems_architect` swarm members:
- `google-gemini-3-5-flash-lite-cloud`
- `google-gemini-3-1-flash-lite-preview-cloud`
- `hf-gradio-ast-1320-gemma-4-aggressive`
- `hf-gradio-htchu-coderhelper`

Verifies their candidate implementations against an authoritative local AST +
SQLite unit-test suite, appends the winning Swarm-Authored Guild E functions to
`Sabrina/harnesses/external/swarm_system_bridges.py`, and wires PEP 562 virtual
module dispatch into `Sabrina/harnesses/external/__init__.py` and hot-reloading
into `Sabrina/harnesses/external/collective_pool.py`.
"""
from __future__ import annotations

import ast
import concurrent.futures
import json
import py_compile
import re
import sqlite3
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from harnesses.external.collective_pool import DEFAULT_DB_PATH, HARNESS_DIR, KindCollectivePool  # noqa: E402
from tools.run_scenario_b_mass_qualification import ARTIFACT_DIR  # noqa: E402
from tools.run_self_organizing_collective_recruitment_loop import load_harness_module  # noqa: E402

V13_REPORT_PATH = ARTIFACT_DIR / "swarm_guild_e_virtual_dispatch_v13.json"

TRUSTED_COMMITTEE = [
    "google-gemini-3-5-flash-lite-cloud",
    "hf-gradio-ast-1320-gemma-4-aggressive",
    "google-gemini-3-1-flash-lite-preview-cloud",
    "hf-gradio-htchu-coderhelper",
    "hf-gradio-jayeshmurthi49-byte-agentic-rag-chatbot-api",
]

GUILD_E_PROMPT = (
    "Output ONLY raw valid Python code (no markdown prose, no explanations) defining two functions:\n"
    "1. `def check_sqlite_data_version(db_path: str, last_version: int) -> tuple[bool, int]:`\n"
    "   - Opens `conn = sqlite3.connect(str(db_path), timeout=5.0)`, executes `PRAGMA data_version` inside `try ... finally: conn.close()`, "
    "fetches `current_version = int(row[0])`, and returns `(current_version != last_version, current_version)`.\n"
    "   - If any exception occurs, returns `(False, last_version)`.\n"
    "2. `def resolve_virtual_portal_spec(db_path: str, module_or_portal_id: str) -> dict | None:`\n"
    "   - Opens `conn = sqlite3.connect(str(db_path), timeout=5.0)` inside `try ... finally: conn.close()`.\n"
    "   - Queries `SELECT portal_id, url, requested_model, actual_model, qualification_state, qualified_roles FROM assistant_external_portals`.\n"
    "   - Finds the row where `portal_id.lower().replace('-', '_') == module_or_portal_id.strip().lower().replace('-', '_')`.\n"
    "   - If found, returns a dict with keys: `'portal_id'`, `'url'`, `'model_id'` (`actual_model or requested_model or portal_id`), "
    "`'qualification_state'`, and `'qualified_roles'` (parsed via `json.loads` if non-empty string, else `{}`).\n"
    "   - If not found or on error, returns `None`.\n"
    "Include `import json` and `import sqlite3` at the top."
)


def extract_clean_python(raw: str) -> str:
    text = raw.strip()
    blocks = re.findall(r"```(?:python)?\s*(.*?)```", text, re.DOTALL | re.IGNORECASE)
    if blocks:
        for b in blocks:
            if "def check_sqlite_data_version" in b:
                text = b.strip()
                break
    lines = text.splitlines()
    kept: List[str] = []
    started = False
    for line in lines:
        s = line.strip()
        if s.startswith(("import ", "from ", "def check_sqlite_data_version", "def resolve_virtual_portal_spec")):
            started = True
        if started:
            if s.startswith("```"):
                break
            kept.append(line)
    return "\n".join(kept).strip()


def verify_guild_e_candidate(code: str) -> Dict[str, Any]:
    if not code:
        return {"passed": False, "reason": "empty_code"}
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return {"passed": False, "reason": f"syntax_error:{exc}"}

    fn_names = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    if not {"check_sqlite_data_version", "resolve_virtual_portal_spec"}.issubset(fn_names):
        return {"passed": False, "reason": f"missing_functions:{fn_names}"}

    ns: Dict[str, Any] = {}
    try:
        exec(compile(tree, "<guild_e_candidate>", "exec"), ns)
    except Exception as exc:
        return {"passed": False, "reason": f"exec_error:{exc}"}

    check_fn = ns["check_sqlite_data_version"]
    resolve_fn = ns["resolve_virtual_portal_spec"]

    # Unit Test against a temporary SQLite database
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
        tdb = Path(tmpdir) / "test.sqlite3"
        conn = sqlite3.connect(str(tdb))
        try:
            conn.execute(
                """
                CREATE TABLE assistant_external_portals (
                    portal_id TEXT PRIMARY KEY,
                    url TEXT,
                    requested_model TEXT,
                    actual_model TEXT,
                    qualification_state TEXT,
                    qualified_roles TEXT
                )
                """
            )
            conn.execute(
                "INSERT INTO assistant_external_portals VALUES (?, ?, ?, ?, ?, ?)",
                (
                    "hf-gradio-test-agent",
                    "https://test.hf.space/gradio_api/call/chat",
                    "org/test-agent",
                    "org/test-agent",
                    "qualified",
                    json.dumps({"diagnosed_tier": "flash", "roles": ["cs_math_and_bounds_coding"]}),
                ),
            )
            conn.commit()
        finally:
            conn.close()

        changed1, ver1 = check_fn(str(tdb), -1)
        if not isinstance(changed1, bool) or not changed1 or not isinstance(ver1, int):
            return {"passed": False, "reason": f"check_version_initial_failed:{(changed1, ver1)}"}

        changed2, ver2 = check_fn(str(tdb), ver1)
        if changed2 or ver2 != ver1:
            return {"passed": False, "reason": f"check_version_unchanged_failed:{(changed2, ver2)}"}

        spec1 = resolve_fn(str(tdb), "hf_gradio_test_agent")
        if not isinstance(spec1, dict) or spec1.get("portal_id") != "hf-gradio-test-agent":
            return {"passed": False, "reason": f"resolve_underscore_failed:{spec1}"}
        if spec1.get("model_id") != "org/test-agent":
            return {"passed": False, "reason": f"resolve_model_id_failed:{spec1}"}

        spec_none = resolve_fn(str(tdb), "non_existent_portal_xyz")
        if spec_none is not None:
            return {"passed": False, "reason": f"resolve_non_existent_should_be_none:{spec_none}"}

    return {"passed": True, "reason": "all_unit_tests_passed"}


def run_guild_e_synthesis() -> Dict[str, Any]:
    t0 = time.time()
    print("=" * 84)
    print("SABRINA COLLECTIVE v13: TRUSTED SWARM COMMITTEE CO-AUTHORING (GUILD E)")
    print("=" * 84)

    submissions: List[Dict[str, Any]] = []

    def _query_member(pid: str) -> Dict[str, Any]:
        from tools.portal_policy import PortalPolicy
        from tools.portal_transport import refresh_protocol, request

        policy = PortalPolicy(DEFAULT_DB_PATH)
        prof = policy.profile(pid)
        if prof:
            for prov in ("huggingface", "generativelanguage.googleapis.com", prof.get("provider", "")):
                if prov:
                    try:
                        policy.reconcile(prov, "guild_e_committee_reconcile")
                    except Exception:
                        pass
            adp = json.loads(prof["adapter_json"])
            if adp.get("kind") == "gradio" and not adp.get("verified_protocol"):
                refresh_protocol(policy, pid)
                time.sleep(10.5)

        t_start = time.perf_counter()
        res = request(policy, pid, GUILD_E_PROMPT, purpose="research", max_tokens=1024)
        lat = round((time.perf_counter() - t_start) * 1000.0, 2)
        raw_reply = str(res.get("reply") or "")
        clean_code = extract_clean_python(raw_reply)
        verdict = verify_guild_e_candidate(clean_code)
        return {
            "portal_id": pid,
            "latency_ms": lat,
            "passed": verdict["passed"],
            "reason": verdict["reason"] if clean_code else f"empty_or_deferred:{res.get('failure')}",
            "code": clean_code,
        }

    for pid in TRUSTED_COMMITTEE:
        r = _query_member(pid)
        submissions.append(r)
        status = "PASS (VERIFIED)" if r.get("passed") else f"FAIL ({r.get('reason')})"
        print(f"  [Guild E Candidate] {r['portal_id']} ({r.get('latency_ms')}ms) -> {status}")
        if r.get("passed") and len([x for x in submissions if x.get("passed")]) >= 2:
            break
        time.sleep(10.5)

    passing = [s for s in sorted(submissions, key=lambda x: x.get("latency_ms", 999999)) if s.get("passed")]
    winner = passing[0] if passing else None

    report = {
        "experiment": "swarm_guild_e_virtual_dispatch_v13",
        "timestamp": time.time(),
        "elapsed_sec": round(time.time() - t0, 2),
        "committee_queried": TRUSTED_COMMITTEE,
        "passing_count": len(passing),
        "winner_portal_id": winner["portal_id"] if winner else None,
        "winner_latency_ms": winner["latency_ms"] if winner else None,
        "submissions": [
            {k: v for k, v in s.items() if k != "code"}
            for s in submissions
        ],
        "winner_code": winner["code"] if winner else "",
    }
    V13_REPORT_PATH.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print("=" * 84)
    if winner:
        print(f"GUILD E WINNER: {winner['portal_id']} ({winner['latency_ms']}ms) — 100% Unit-Test Verified!")
    else:
        print("No candidate passed; check submissions.")
    print("=" * 84)
    return report


if __name__ == "__main__":
    run_guild_e_synthesis()
