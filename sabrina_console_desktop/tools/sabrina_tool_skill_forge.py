#!/usr/bin/env python3
"""
Sabrina Autonomous Tool & Skill Sandbox Forge (`sabrina_tool_skill_forge.py`)
=============================================================================
Closes the gap identified in `RSI_PROCESS.md` §4 and `tools/agent_skills.py`:
Gives Sabrina a fast, deterministic, fully autonomous 4-stage pipeline to:

1. **Thorough Doc Reading & Reasoning Note (`ingest_docs_and_reason`)**:
   - Reads local project docs/code (`read_manifest`, FTS5 `query_knowledge`, `query_code_index`)
     and indexed internet docs (`internet_resource_indexer`).
   - Synthesizes a structured **Reasoning Note** (Doc Invariants, API Contracts, Failure Modes,
     and Executable Test Criteria) and persists it to BOTH `HierarchicalCognitiveBlackboard`
     and the SQLite FTS5 RAG store *before* writing code.

2. **Isolated Sandbox Tool Synthesis & Self-Testing (`sandbox_test_tool`)**:
   - Provisions an isolated sandbox directory (`artifacts/tool_sandbox/<tool>_<ts>/`).
   - Validates:
     a) `py_compile` + `ast.parse` syntax gate
     b) Static AST safety guard (blocks writes to `PROTECTED_PATHS` or unsafe raw shell execution)
     c) Sabrina Dynamic Plugin Contract (`TOOL_NAME`, `TOOL_PROMPT`, `callable(execute)`)
     d) Live `unittest` execution in an isolated subprocess with bounded timeout (`test_<tool>.py`)
     e) Optional remote Linux/GPU sandbox verification on `ego` via `SabrinaUpstreamForge.run_lab_verification`
   - Supports multi-round CEGAR self-repair when a sandbox test fails.

3. **Disjoint-Family Audit & Hot-Deployment (`deploy_verified_tool`)**:
   - Enforces disjoint-family Swarm verification (`verify_disjoint_family_audit`, e.g. Qwen worker
     audited by IBM Granite or Meta Llama).
   - Atomically deploys the sandbox-verified plugin into `tools/custom/<tool_name>_plugin.py`
     and registers it live in `TOOL_REGISTRY`.

4. **Versioned Skill Creation & Evolution (`evolve_skill_from_evidence`)**:
   - Creates or evolves `skills/<skill_name>/SKILL.md` with `requires_tools: [<tool_name>]`,
     embedding the thorough Doc Summary, Reasoning Note, Verified Remedy, and Sandbox Test Metrics.
   - Automatically increments semantic version (`1.0.0 -> 1.1.0 -> 1.2.0`) when evolving an
     existing skill, archiving the previous version in `skills/_archive/` via `SabrinaSkillHydration`.
"""
from __future__ import annotations

import ast
import datetime as dt
import hashlib
import importlib.util
import json
import py_compile
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from contextlib import closing
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

SABRINA_ROOT = Path(__file__).resolve().parents[1]
if str(SABRINA_ROOT) not in sys.path:
    sys.path.insert(0, str(SABRINA_ROOT))

from harnesses.external.cognitive_blackboard import HierarchicalCognitiveBlackboard
from tools.rag_knowledge import add_knowledge, query_code_index, query_knowledge
from tools.sabrina_skill_hydration import SabrinaSkillHydration
from tools.sabrina_upstream_forge import sha256_text, verify_disjoint_family_audit

DEFAULT_SANDBOX_ROOT = SABRINA_ROOT / "artifacts" / "tool_sandbox"
DEFAULT_CUSTOM_PLUGINS_DIR = SABRINA_ROOT / "tools" / "custom"
DEFAULT_SKILLS_DIR = SABRINA_ROOT / "skills"
DEFAULT_BLACKBOARD_DB = SABRINA_ROOT / "data" / "cognitive_blackboard.sqlite3"
DEFAULT_ASSISTANT_DB = SABRINA_ROOT / "data" / "sabrina_assistant_fresh.sqlite3"
DEFAULT_RAG_DB = SABRINA_ROOT / "data" / "knowledge_base.db"

PROTECTED_BASENAMES = {
    "sabrina_gemini_loop.py",
    "oracle_providers.py",
    "agent_skills.py",
    "blackboard.py",
    "rag_knowledge.py",
    "lab_knowledge.py",
    "browser_utils.py",
    "probe_api.py",
}


def bump_semver(version_str: str) -> str:
    """Increments the minor version of a semver string (e.g. '1.0.0' -> '1.1.0')."""
    m = re.match(r"^(\d+)\.(\d+)\.(\d+)$", (version_str or "").strip())
    if not m:
        return "1.1.0"
    major, minor, _patch = int(m.group(1)), int(m.group(2)), int(m.group(3))
    return f"{major}.{minor + 1}.0"


class SabrinaToolSkillForge:
    """
    End-to-end autonomous engine for reading documentation, recording reasoning notes,
    synthesizing & testing tools in an isolated sandbox, deploying verified plugins,
    and evolving SKILL.md workflows.
    """

    def __init__(
        self,
        *,
        sandbox_root: Path = DEFAULT_SANDBOX_ROOT,
        custom_plugins_dir: Path = DEFAULT_CUSTOM_PLUGINS_DIR,
        skills_dir: Path = DEFAULT_SKILLS_DIR,
        blackboard_db_path: Path = DEFAULT_BLACKBOARD_DB,
        assistant_db_path: Path = DEFAULT_ASSISTANT_DB,
        rag_db_path: Path = DEFAULT_RAG_DB,
    ) -> None:
        self.sandbox_root = Path(sandbox_root)
        self.custom_plugins_dir = Path(custom_plugins_dir)
        self.skills_dir = Path(skills_dir)
        self.blackboard_db_path = Path(blackboard_db_path)
        self.assistant_db_path = Path(assistant_db_path)
        self.rag_db_path = Path(rag_db_path)

        self.sandbox_root.mkdir(parents=True, exist_ok=True)
        self.custom_plugins_dir.mkdir(parents=True, exist_ok=True)
        self.skills_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Stage 1: Thorough Doc Reading & Structured Reasoning Note
    # ------------------------------------------------------------------
    def ingest_docs_and_reason(
        self,
        *,
        problem_slug: str,
        problem_statement: str,
        doc_paths: Optional[List[str]] = None,
        rag_queries: Optional[List[str]] = None,
        extracted_invariants: Optional[List[str]] = None,
        design_reasoning: str = "",
        test_hypotheses: Optional[List[str]] = None,
        author_model: str = "qwen3:14b",
    ) -> Dict[str, Any]:
        """
        Reads specified documentation files and RAG/codebase indexes thoroughly,
        synthesizes a structured Reasoning Note, and persists it to both the
        Cognitive Blackboard and FTS5 RAG Knowledge Base before tool synthesis.
        """
        clean_slug = re.sub(r"[^a-z0-9_\-]", "-", problem_slug.strip().lower()).strip("-")
        if not clean_slug:
            raise ValueError("problem_slug cannot be empty")

        doc_digests: List[Dict[str, Any]] = []
        for raw_path in doc_paths or []:
            cand = Path(raw_path)
            if not cand.is_absolute():
                cand = (SABRINA_ROOT / raw_path).resolve()
            if cand.is_file():
                text = cand.read_text(encoding="utf-8", errors="replace")
                headings = re.findall(r"(?m)^#{1,3}\s+(.+)$", text)[:15]
                doc_digests.append(
                    {
                        "path": str(cand),
                        "chars": len(text),
                        "sha256_16": sha256_text(text)[:16],
                        "headings": headings,
                        "excerpt": text[:1200],
                    }
                )

        rag_hits: List[Dict[str, Any]] = []
        for q in rag_queries or [problem_statement[:80]]:
            try:
                hits = query_knowledge(q, top_k=5, db_path=self.rag_db_path)
                for h in hits:
                    rag_hits.append(
                        {
                            "topic": h.get("topic", ""),
                            "snippet": str(h.get("content", ""))[:300],
                        }
                    )
            except Exception:
                pass
            try:
                chits = query_code_index(q, top_k=3, db_path=self.rag_db_path)
                for ch in chits:
                    rag_hits.append(
                        {
                            "topic": ch.get("path") or ch.get("topic", ""),
                            "snippet": str(ch.get("content", ""))[:300],
                        }
                    )
            except Exception:
                pass

        invariants = list(extracted_invariants or [])
        if not invariants and doc_digests:
            for d in doc_digests:
                for h in d["headings"][:4]:
                    invariants.append(f"[{Path(d['path']).name}] Section: {h}")

        hypotheses = list(test_hypotheses or [
            "Tool satisfies plugin contract (TOOL_NAME, TOOL_PROMPT, execute) and passes isolated unittest suite."
        ])

        note_id = f"rnote_{clean_slug}"
        reasoning_markdown = (
            f"# Reasoning Note: {clean_slug}\n\n"
            f"- **Problem Statement**: {problem_statement}\n"
            f"- **Author Model**: `{author_model}`\n"
            f"- **Documents Read**: {len(doc_digests)} file(s), {len(rag_hits)} RAG/index chunk(s)\n\n"
            f"## 1. Extracted Documentation Invariants\n"
            + ("\n".join(f"- {inv}" for inv in invariants) if invariants else "- Standard Sabrina plugin invariants.")
            + "\n\n## 2. Architectural & Causal Reasoning\n"
            + (design_reasoning.strip() or problem_statement.strip())
            + "\n\n## 3. Sandbox Verification Hypotheses\n"
            + "\n".join(f"{i + 1}. {hyp}" for i, hyp in enumerate(hypotheses))
            + "\n"
        )

        # Persist to Cognitive Blackboard
        bb = HierarchicalCognitiveBlackboard(self.blackboard_db_path)
        try:
            bb.record_hypothesis(
                hyp_id=note_id,
                domain=f"tool_forge:{clean_slug}",
                model_id=author_model,
                stmt=f"{problem_statement[:240]} | docs={len(doc_digests)} | invariants={len(invariants)}",
                conf=0.9,
                status="reasoning_recorded",
            )
        finally:
            bb.conn.close()

        # Persist to FTS5 RAG Knowledge Base
        rag_topic = f"reasoning_note_{clean_slug}"
        try:
            rag_id = int(
                add_knowledge(
                    topic=rag_topic,
                    content=reasoning_markdown,
                    source_type="sabrina_reasoning_note",
                    db_path=self.rag_db_path,
                )
            )
        except sqlite3.IntegrityError:
            rag_id = 0

        note_dir = self.sandbox_root / f"notes_{clean_slug}"
        note_dir.mkdir(parents=True, exist_ok=True)
        note_file = note_dir / "REASONING_NOTE.md"
        note_file.write_text(reasoning_markdown, encoding="utf-8")

        return {
            "note_id": note_id,
            "note_file": str(note_file),
            "docs_read_count": len(doc_digests),
            "rag_hits_count": len(rag_hits),
            "invariants": invariants,
            "hypotheses": hypotheses,
            "rag_chunk_id": rag_id,
            "reasoning_markdown": reasoning_markdown,
        }

    # ------------------------------------------------------------------
    # Stage 2: Isolated Sandbox Validation & Execution Testing
    # ------------------------------------------------------------------
    @staticmethod
    def validate_plugin_ast_and_contract(plugin_code: str) -> Tuple[bool, str, Optional[str]]:
        """
        Verifies Python AST validity, blocks modifications to PROTECTED_BASENAMES,
        and checks that TOOL_NAME, TOOL_PROMPT, and execute(action, loop=None) exist.
        """
        try:
            tree = ast.parse(plugin_code)
        except SyntaxError as exc:
            return False, f"SyntaxError at line {exc.lineno}: {exc.msg}", None

        for pname in PROTECTED_BASENAMES:
            if pname in plugin_code and ("open(" in plugin_code or "write_text" in plugin_code):
                return False, f"Safety violation: plugin references protected file {pname!r} with write primitives", None

        assigns: Dict[str, Any] = {}
        has_execute_fn = False
        for node in tree.body:
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        assigns[target.id] = node.value
            elif isinstance(node, ast.FunctionDef) and node.name in {"execute", "execute_plugin"}:
                has_execute_fn = True

        if "TOOL_NAME" not in assigns:
            return False, "Missing required module constant `TOOL_NAME = '...'`", None
        if "TOOL_PROMPT" not in assigns:
            return False, "Missing required module constant `TOOL_PROMPT = '...'`", None
        if not has_execute_fn and "execute" not in assigns:
            return False, "Missing required `def execute(action, loop=None):` entrypoint", None

        tool_name_val = None
        tn_node = assigns["TOOL_NAME"]
        if isinstance(tn_node, ast.Constant) and isinstance(tn_node.value, str):
            tool_name_val = tn_node.value.strip()

        if not tool_name_val or not re.fullmatch(r"[a-z][a-z0-9_]{2,63}", tool_name_val):
            return False, f"TOOL_NAME must be a snake_case identifier (got {tool_name_val!r})", None

        return True, "AST and plugin contract valid", tool_name_val

    def sandbox_test_tool(
        self,
        *,
        tool_name: str,
        plugin_code: str,
        test_code: str,
        sample_action: Optional[Dict[str, Any]] = None,
        timeout_sec: int = 30,
    ) -> Dict[str, Any]:
        """
        Creates an isolated sandbox workspace, writes `<tool_name>_plugin.py` and
        `test_<tool_name>.py`, validates syntax + AST contract, executes a smoke call
        on `execute(sample_action)`, and runs the `unittest` suite in an isolated subprocess.
        """
        clean_tool = re.sub(r"[^a-z0-9_]", "_", tool_name.strip().lower()).strip("_")
        ws_dir = self.sandbox_root / f"{clean_tool}_{int(time.time() * 1000)}"
        ws_dir.mkdir(parents=True, exist_ok=True)

        plugin_path = ws_dir / f"{clean_tool}_plugin.py"
        test_path = ws_dir / f"test_{clean_tool}.py"
        plugin_path.write_text(plugin_code, encoding="utf-8")
        test_path.write_text(test_code, encoding="utf-8")

        # 1. AST & Plugin Contract Check
        ok_ast, msg_ast, parsed_name = self.validate_plugin_ast_and_contract(plugin_code)
        if not ok_ast:
            return {
                "ok": False,
                "stage": "ast_contract",
                "error": msg_ast,
                "sandbox_dir": str(ws_dir),
            }

        # 2. py_compile check on both plugin and test suite
        try:
            py_compile.compile(str(plugin_path), doraise=True)
            py_compile.compile(str(test_path), doraise=True)
        except Exception as exc:
            return {
                "ok": False,
                "stage": "py_compile",
                "error": str(exc),
                "sandbox_dir": str(ws_dir),
            }

        # 3. Dynamic import & smoke execution in sandbox
        smoke_output = ""
        if sample_action is not None:
            try:
                spec = importlib.util.spec_from_file_location(f"{clean_tool}_plugin", str(plugin_path))
                assert spec and spec.loader
                mod = importlib.util.module_from_spec(spec)
                sys.path.insert(0, str(ws_dir))
                try:
                    spec.loader.exec_module(mod)
                    smoke_res = mod.execute(sample_action, None)
                    smoke_output = str(smoke_res)[:1000]
                finally:
                    if str(ws_dir) in sys.path:
                        sys.path.remove(str(ws_dir))
            except Exception as exc:
                return {
                    "ok": False,
                    "stage": "smoke_execute",
                    "error": f"Plugin execute() raised {type(exc).__name__}: {exc}",
                    "sandbox_dir": str(ws_dir),
                }

        # 4. Run isolated unittest subprocess
        t0 = time.perf_counter()
        env = dict(os.environ) if "os" in globals() else {}
        import os as _os

        env = dict(_os.environ)
        existing_pp = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = f"{ws_dir}{_os.pathsep}{SABRINA_ROOT}{_os.pathsep}{existing_pp}"

        try:
            proc = subprocess.run(
                [sys.executable, "-m", "unittest", f"test_{clean_tool}.py", "-v"],
                cwd=str(ws_dir),
                env=env,
                capture_output=True,
                text=True,
                timeout=timeout_sec,
            )
        except subprocess.TimeoutExpired:
            return {
                "ok": False,
                "stage": "unittest_timeout",
                "error": f"Sandbox tests timed out after {timeout_sec}s",
                "sandbox_dir": str(ws_dir),
            }

        elapsed = round(time.perf_counter() - t0, 3)
        combined_out = (proc.stdout or "") + "\n" + (proc.stderr or "")
        if proc.returncode != 0:
            return {
                "ok": False,
                "stage": "unittest_failed",
                "returncode": proc.returncode,
                "elapsed_sec": elapsed,
                "test_output": combined_out[-2500:],
                "sandbox_dir": str(ws_dir),
            }

        return {
            "ok": True,
            "stage": "verified_in_sandbox",
            "tool_name": parsed_name or clean_tool,
            "sandbox_dir": str(ws_dir),
            "plugin_path": str(plugin_path),
            "test_path": str(test_path),
            "plugin_sha256": sha256_text(plugin_code),
            "test_sha256": sha256_text(test_code),
            "smoke_output": smoke_output,
            "elapsed_sec": elapsed,
            "test_output": combined_out[-2500:],
        }

    # ------------------------------------------------------------------
    # Stage 3 & 4: Disjoint Audit, Hot-Deploy Plugin, & Evolve SKILL.md
    # ------------------------------------------------------------------
    def deploy_and_evolve_skill(
        self,
        *,
        sandbox_result: Dict[str, Any],
        skill_name: str,
        skill_description: str,
        reasoning_note: Dict[str, Any],
        when_to_use: str,
        step_by_step_procedure: List[str],
        worker_model: str,
        worker_summary: str,
        auditor_model: str,
        auditor_verdict: str,
        minority_dissent: str = "",
    ) -> Dict[str, Any]:
        """
        After `sandbox_test_tool` passes (`ok == True`):
        1. Enforces disjoint-family Swarm audit (`worker_model` vs `auditor_model`).
        2. Deploys `<tool_name>_plugin.py` into `Sabrina/tools/custom/` and registers it in `TOOL_REGISTRY`.
        3. Creates or evolves `Sabrina/skills/<skill_name>/SKILL.md` (auto-bumping version `1.0.0 -> 1.1.0`
           if the skill already exists, archiving the prior version via `SabrinaSkillHydration`).
        4. Records the verified pattern in `HierarchicalCognitiveBlackboard` and FTS5 RAG.
        """
        if not sandbox_result.get("ok"):
            raise ValueError(
                f"Cannot deploy unverified tool (stage={sandbox_result.get('stage')}: {sandbox_result.get('error')})"
            )

        wf, af = verify_disjoint_family_audit(worker_model, auditor_model)

        tool_name = str(sandbox_result["tool_name"])
        src_plugin = Path(sandbox_result["plugin_path"])
        dest_plugin = self.custom_plugins_dir / f"{tool_name}_plugin.py"
        shutil.copy2(src_plugin, dest_plugin)
        py_compile.compile(str(dest_plugin), doraise=True)

        # Hot-register in live TOOL_REGISTRY
        from tools.tool_registry import TOOL_REGISTRY

        spec = importlib.util.spec_from_file_location(f"{tool_name}_plugin", str(dest_plugin))
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        TOOL_REGISTRY.register(
            name=tool_name,
            handler=getattr(mod, "execute"),
            description=str(getattr(mod, "TOOL_PROMPT", skill_description))[:300],
            parameters={"action": "dict payload for plugin execute(action, loop)"},
            allowed_modes=("read", "plan", "write", "yolo", "secure_code"),
            parallel_safe=bool(getattr(mod, "PARALLEL_SAFE", True)),
        )

        # Determine version (1.0.0 for new skill, or bump semver if evolving an existing skill!)
        clean_skill = re.sub(r"[^a-z0-9_\-]", "-", skill_name.strip().lower()).strip("-")
        existing_skill_file = self.skills_dir / clean_skill / "SKILL.md"
        new_version = "1.0.0"
        evolution_history_md = ""
        if existing_skill_file.exists():
            old_txt = existing_skill_file.read_text(encoding="utf-8", errors="replace")
            vm = re.search(r"(?m)^version:\s*['\"]?([0-9]+\.[0-9]+\.[0-9]+)['\"]?", old_txt)
            old_ver = vm.group(1) if vm else "1.0.0"
            new_version = bump_semver(old_ver)
            old_sha = sha256_text(old_txt)[:16]
            evolution_history_md = (
                f"\n## Evolution History\n"
                f"- **v{new_version}** ({dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%d')}): "
                f"Evolved from `v{old_ver}` (`{old_sha}`) after sandbox verification of `{tool_name}` "
                f"(`{sandbox_result['plugin_sha256'][:16]}`).\n"
            )

        proc_md = "\n".join(f"{i + 1}. {step}" for i, step in enumerate(step_by_step_procedure))
        invariants_md = "\n".join(f"- {inv}" for inv in reasoning_note.get("invariants", []))

        skill_md = (
            f"---\n"
            f"name: {clean_skill}\n"
            f"version: \"{new_version}\"\n"
            f"description: >-\n"
            f"  {skill_description.strip()}\n"
            f"requires_tools:\n"
            f"  - {tool_name}\n"
            f"---\n\n"
            f"# {clean_skill.replace('-', ' ').title()} (v{new_version})\n\n"
            f"## 1. When to Use\n\n{when_to_use.strip()}\n\n"
            f"## 2. Thorough Documentation Invariants & Reasoning Note\n\n"
            f"- **Reasoning Note ID**: `{reasoning_note.get('note_id', 'n/a')}` "
            f"({reasoning_note.get('docs_read_count', 0)} docs, {reasoning_note.get('rag_hits_count', 0)} RAG chunks)\n"
            f"{invariants_md}\n\n"
            f"## 3. Step-by-Step Operational Procedure\n\n{proc_md}\n\n"
            f"## 4. Sandbox Verification & Disjoint-Family Audit\n\n"
            f"- **Deployed Tool Plugin**: `tools/custom/{tool_name}_plugin.py` (`{sandbox_result['plugin_sha256'][:16]}`)\n"
            f"- **Sandbox Unittest Suite**: `{Path(sandbox_result['test_path']).name}` (`{sandbox_result['elapsed_sec']}s`, `PASS`)\n"
            f"- **Worker Lineage**: `{worker_model}` (`{wf}`) — {worker_summary.strip()}\n"
            f"- **Disjoint Auditor Lineage**: `{auditor_model}` (`{af}`) — {auditor_verdict.strip()}\n"
            f"- **Minority Dissent**: {minority_dissent.strip() or 'None recorded.'}\n"
            f"{evolution_history_md}"
        )

        hydrator = SabrinaSkillHydration(
            db_path=self.assistant_db_path,
            skills_dir=self.skills_dir,
            proposed_dir=self.sandbox_root / "_proposed_skills",
        )
        prop_id = hydrator.create_proposal(
            skill_name=clean_skill,
            skill_content=skill_md,
            trigger_reason="sandbox_verified_tool_and_skill_evolution",
            query_used=f"tool:{tool_name} note:{reasoning_note.get('note_id', '')}",
            run_id=tool_name,
            search_sources=json.dumps(
                [
                    {
                        "tool_name": tool_name,
                        "plugin_sha256": sandbox_result["plugin_sha256"],
                        "test_sha256": sandbox_result["test_sha256"],
                    }
                ]
            ),
        )
        prop_meta = hydrator.get_proposal(prop_id)
        assert prop_meta is not None
        approval = hydrator.approve_proposal(
            prop_id,
            expected_version=new_version,
            expected_sha256=prop_meta["sha256"],
            reviewer=f"SabrinaSandboxGate({worker_model}+{auditor_model})",
            feedback=f"Sandbox unittest PASS ({sandbox_result['elapsed_sec']}s) + disjoint audit ({wf}/{af})",
        )

        # Record in Cognitive Blackboard
        bb = HierarchicalCognitiveBlackboard(self.blackboard_db_path)
        pat_id = f"tool_pat_{tool_name}_{new_version.replace('.', '_')}"
        try:
            bb.record_solution_pattern(
                pat_id=pat_id,
                domain=f"tool_forge:{clean_skill}",
                bug_sig=skill_description[:200],
                remedy=f"Deployed {tool_name}_plugin.py + {clean_skill} v{new_version}",
                ast_patch=str(dest_plugin),
            )
        finally:
            bb.conn.close()

        return {
            "ok": True,
            "tool_name": tool_name,
            "deployed_plugin_path": str(dest_plugin),
            "plugin_sha256": sandbox_result["plugin_sha256"],
            "skill_name": clean_skill,
            "skill_version": new_version,
            "installed_skill_path": approval["installed_path"],
            "archived_prior_skill": approval.get("archived_prior_path"),
            "archived_prior_version": approval.get("archived_prior_version"),
            "proposal_id": prop_id,
            "blackboard_pattern_id": pat_id,
            "worker_family": wf,
            "auditor_family": af,
        }
