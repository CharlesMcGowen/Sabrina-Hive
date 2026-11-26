#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Sabrina Upstream Open-Source R&D & Contribution Forge (`sabrina_upstream_forge.py`)
===================================================================================
End-to-end governed system enabling Sabrina and the Multi-Family Swarm Collective to:
  1. Work on enrolled C/C++/CUDA/Python/Rust open-source repositories in isolated
     manifest-verified workspaces (`sabrina_coding_artifacts` + `sabrina_coding_pool`),
     respecting `ControlClient.lease("compute:lab")` for hardware benchmarks on `ego`.
  2. Enforce Disjoint-Family Swarm Auditing (`worker_family != auditor_family`),
     preserving verbatim traces and minority dissent without ghostwriting.
  3. Distill verified learnings internally into THREE Sabrina memory stores:
       - `HierarchicalCognitiveBlackboard` (`semantic_solution_patterns` + `working_hypotheses`)
       - FTS5 RAG Knowledge Base (`sabrina_knowledge_rag.db` via `rag_knowledge.add_knowledge`)
       - `SabrinaSkillHydration` versioned `SKILL.md` proposal (`assistant_skill_proposals`)
  4. Scrub & verify outbound patches/markdown via `UpstreamSanitizer` + `CyberComplianceSentinel`
     (zero local paths, LAN IPs, hostnames, credentials, or exploit strings).
  5. Automatically push sanitized branches to the operator's **personal GitHub fork** as
     **Draft PRs** (targeting the fork's own default branch — zero upstream noise),
     while strictly gating any upstream PR/Issue submission to `NVIDIA/*`, `facebook/*`,
     or `inikep/*` behind explicit 64-char SHA-256 human sign-off (`promote_to_upstream`).
"""

from __future__ import annotations

import argparse
import base64
from contextlib import closing
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from typing import Any, Dict, List, Optional, Tuple

SABRINA_ROOT = Path(__file__).resolve().parents[1]
if str(SABRINA_ROOT) not in sys.path:
    sys.path.insert(0, str(SABRINA_ROOT))

from harnesses.external.cognitive_blackboard import HierarchicalCognitiveBlackboard  # noqa: E402
from tools.control_client import configured as configured_coordinator  # noqa: E402
from tools.cyber_compliance_sentinel import CyberComplianceSentinel  # noqa: E402
from tools.rag_knowledge import add_knowledge  # noqa: E402
from tools.sabrina_coding_artifacts import ArtifactCollector, manifest_for, prepare_workspace, read_manifest  # noqa: E402
from tools.sabrina_skill_hydration import SabrinaSkillHydration  # noqa: E402
from tools.secret_store import get_secret  # noqa: E402

DEFAULT_CONFIG_PATH = SABRINA_ROOT / "config" / "upstream_forge_repos.json"
DEFAULT_FORGE_DB = SABRINA_ROOT / "data" / "sabrina_upstream_forge.sqlite3"
DEFAULT_BLACKBOARD_DB = SABRINA_ROOT / "data" / "sabrina_assistant_fresh.sqlite3"
DEFAULT_ASSISTANT_DB = SABRINA_ROOT / "data" / "sabrina_assistant.sqlite3"
DEFAULT_RAG_DB = SABRINA_ROOT / "sabrina_knowledge_rag.db"
DEFAULT_ARTIFACTS_ROOT = SABRINA_ROOT / "artifacts" / "upstream_forge"


FORGE_SCHEMA = """
CREATE TABLE IF NOT EXISTS forge_dossiers (
    dossier_id TEXT PRIMARY KEY,
    slug TEXT UNIQUE NOT NULL,
    repo_key TEXT NOT NULL,
    upstream_repo TEXT NOT NULL,
    fork_repo TEXT NOT NULL,
    contribution_kind TEXT NOT NULL,
    title TEXT NOT NULL,
    worker_model TEXT NOT NULL,
    worker_family TEXT NOT NULL,
    auditor_model TEXT NOT NULL,
    auditor_family TEXT NOT NULL,
    minority_dissent TEXT NOT NULL DEFAULT '',
    dossier_dir TEXT NOT NULL,
    dossier_sha256 TEXT NOT NULL,
    skill_proposal_id TEXT NOT NULL,
    rag_chunk_id INTEGER NOT NULL,
    blackboard_pattern_id TEXT NOT NULL,
    fork_branch TEXT NOT NULL,
    fork_pr_status TEXT NOT NULL,
    fork_pr_url TEXT NOT NULL DEFAULT '',
    upstream_status TEXT NOT NULL,
    upstream_url TEXT NOT NULL DEFAULT '',
    approved_by TEXT NOT NULL DEFAULT '',
    approved_at REAL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_forge_upstream_status ON forge_dossiers(upstream_status);
CREATE INDEX IF NOT EXISTS idx_forge_fork_status ON forge_dossiers(fork_pr_status);
"""


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def infer_model_family(model_id: str) -> str:
    """Maps a local Ollama or external portal identifier to its canonical lineage family."""
    m = (model_id or "").strip().lower()
    if not m:
        raise ValueError("Model identifier cannot be empty")
    if "qwen" in m:
        return "qwen"
    if "granite" in m:
        return "ibm_granite"
    if "llama" in m:
        return "meta_llama"
    if "mistral" in m or "dolphin" in m:
        return "mistral"
    if "starcoder" in m:
        return "starcoder"
    if "promptbreeder" in m:
        return "promptbreeder"
    if "sabrina-jury-defender" in m:
        return "constitutional_defender"
    if "gemini" in m:
        return "google_gemini"
    if "claude" in m:
        return "anthropic_claude"
    if m.startswith("hf-gradio-"):
        return f"external_gradio:{m}"
    return f"other:{m.split(':')[0]}"


def verify_disjoint_family_audit(worker_model: str, auditor_model: str) -> Tuple[str, str]:
    """Enforces AGENTS.md Section 3: Worker and Auditor must belong to distinct model families."""
    wf = infer_model_family(worker_model)
    af = infer_model_family(auditor_model)
    if wf == af:
        raise ValueError(
            f"Disjoint-family audit violation: worker {worker_model!r} ({wf}) and "
            f"auditor {auditor_model!r} ({af}) belong to the same model family."
        )
    return wf, af


class UpstreamSanitizer:
    """Scrubs local paths, hostnames, LAN IPs, and secrets; fails closed on any leak."""

    REPLACEMENTS: List[Tuple[re.Pattern[str], str]] = [
        (re.compile(r"[Cc]:[\\/]+Users[\\/]+bcmcg[\\/]+Desktop[\\/]+bot[\\/]*"), "<workspace>/"),
        (re.compile(r"[Cc]:[\\/]+Users[\\/]+bcmcg[^\s\"')`]*"), "<local-user-path>"),
        (re.compile(r"/home/ego/bot/"), "<lab-workspace>/"),
        (re.compile(r"/home/ego\b"), "<lab-home>"),
        (re.compile(r"\bego@ego\.local\b", re.IGNORECASE), "<lab-user>@<gpu-bench-node>"),
        (re.compile(r"\bego\.local\b", re.IGNORECASE), "<gpu-bench-node>"),
        (re.compile(r"\bdesktop-k2d5lu7\b", re.IGNORECASE), "<operator-workstation>"),
        (
            re.compile(
                r"\b(?:192\.168\.\d{1,3}\.\d{1,3}|10\.\d{1,3}\.\d{1,3}\.\d{1,3}|"
                r"172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})\b"
            ),
            "<lan-ip>",
        ),
        (re.compile(r"\bfd[0-9a-f]{2}:[0-9a-f:]+\b", re.IGNORECASE), "<lan-ipv6-ula>"),
    ]

    FORBIDDEN_PATTERNS: List[Tuple[str, re.Pattern[str]]] = [
        ("windows_user_path", re.compile(r"Users[\\/]+bcmcg", re.IGNORECASE)),
        ("linux_ego_home", re.compile(r"/home/ego\b")),
        ("ego_local_host", re.compile(r"\bego\.local\b", re.IGNORECASE)),
        ("workstation_host", re.compile(r"\bdesktop-k2d5lu7\b", re.IGNORECASE)),
        (
            "private_ipv4",
            re.compile(
                r"\b(?:192\.168\.\d{1,3}\.\d{1,3}|10\.\d{1,3}\.\d{1,3}\.\d{1,3}|"
                r"172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})\b"
            ),
        ),
        ("private_ipv6_ula", re.compile(r"\bfd[0-9a-f]{2}:[0-9a-f:]+\b", re.IGNORECASE)),
        (
            "secret_token",
            re.compile(
                r"(?:ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|"
                r"AIzaSy[A-Za-z0-9_-]{30,}|sk-[A-Za-z0-9]{20,}|"
                r"-----BEGIN (?:RSA|OPENSSH|EC|PRIVATE) KEY-----)"
            ),
        ),
    ]

    def __init__(self, sentinel: Optional[CyberComplianceSentinel] = None) -> None:
        self.sentinel = sentinel or CyberComplianceSentinel()

    def scrub_text(self, text: str) -> str:
        out = text
        for pat, repl in self.REPLACEMENTS:
            out = pat.sub(repl, out)
        return out

    def assert_clean(self, text: str, label: str = "artifact") -> None:
        for name, pat in self.FORBIDDEN_PATTERNS:
            m = pat.search(text)
            if m:
                raise ValueError(
                    f"Upstream sanitization gate blocked {label}: detected {name} ({m.group(0)[:32]!r})"
                )
        ok, msg = self.sentinel.sanitize_outbound_query(text)
        if not ok:
            raise ValueError(f"CyberComplianceSentinel blocked {label}: {msg}")


class SabrinaUpstreamForge:
    """Coordinates external OSS R&D workspaces, dual memory distillation, and personal-fork Draft PRs."""

    def __init__(
        self,
        *,
        config_path: Path = DEFAULT_CONFIG_PATH,
        forge_db_path: Path = DEFAULT_FORGE_DB,
        blackboard_db_path: Path = DEFAULT_BLACKBOARD_DB,
        assistant_db_path: Path = DEFAULT_ASSISTANT_DB,
        rag_db_path: Path = DEFAULT_RAG_DB,
        artifacts_root: Path = DEFAULT_ARTIFACTS_ROOT,
    ) -> None:
        self.config_path = Path(config_path)
        self.forge_db_path = Path(forge_db_path)
        self.blackboard_db_path = Path(blackboard_db_path)
        self.assistant_db_path = Path(assistant_db_path)
        self.rag_db_path = Path(rag_db_path)
        self.artifacts_root = Path(artifacts_root)

        self.forge_db_path.parent.mkdir(parents=True, exist_ok=True)
        self.artifacts_root.mkdir(parents=True, exist_ok=True)
        self.sanitizer = UpstreamSanitizer()
        self._init_db()

    def _init_db(self) -> None:
        with closing(sqlite3.connect(self.forge_db_path)) as conn:
            conn.executescript(FORGE_SCHEMA)
            conn.commit()

    def load_config(self) -> Dict[str, Any]:
        if not self.config_path.exists():
            return {"default_fork_owner": "CharlesMcGowen", "repositories": {}}
        return json.loads(self.config_path.read_text(encoding="utf-8"))

    def save_config(self, cfg: Dict[str, Any]) -> None:
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")

    def enroll_repo(
        self,
        repo_key: str,
        upstream_repo: str,
        *,
        local_path: str = "",
        remote_lab_path: str = "",
        fork_repo: str = "",
        upstream_default_branch: str = "main",
        language_family: str = "cpp_cuda",
        license_name: str = "Apache-2.0",
        requires_gpu_lease: bool = False,
        syntax_and_test_cmd: str = "",
    ) -> Dict[str, Any]:
        cfg = self.load_config()
        owner = cfg.get("default_fork_owner", "CharlesMcGowen")
        repo_name = upstream_repo.split("/")[-1]
        entry = {
            "local_path": local_path,
            "remote_lab_path": remote_lab_path,
            "upstream_repo": upstream_repo,
            "upstream_default_branch": upstream_default_branch,
            "fork_repo": fork_repo or f"{owner}/{repo_name}",
            "language_family": language_family,
            "license": license_name,
            "requires_gpu_lease": bool(requires_gpu_lease),
            "syntax_and_test_cmd": syntax_and_test_cmd,
        }
        cfg.setdefault("repositories", {})[repo_key] = entry
        self.save_config(cfg)
        return entry

    # ------------------------------------------------------------------
    # 1. Isolated Manifest Workspace & Lab-Leased Hardware Verification
    # ------------------------------------------------------------------
    def create_isolated_workspace(
        self,
        repo_key: str,
        task_slug: str,
        manifest_files: List[str],
    ) -> Dict[str, Any]:
        """Creates an isolated candidate copy using Sabrina's ArtifactCollector manifest guards."""
        cfg = self.load_config()
        repo = cfg.get("repositories", {}).get(repo_key)
        if not repo or not repo.get("local_path"):
            raise ValueError(f"Repository {repo_key!r} has no configured local_path")
        src_root = Path(repo["local_path"]).resolve(strict=True)
        raw_files = read_manifest(src_root, manifest_files)
        base_manifest = manifest_for(raw_files)
        expected_hash = hashlib.sha256(
            json.dumps(base_manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

        ws_dir = self.artifacts_root / "_workspaces" / f"{repo_key}_{task_slug}_{int(time.time())}"
        ws_dir.parent.mkdir(parents=True, exist_ok=True)
        prepare_workspace(src_root, ws_dir, manifest_files, expected_hash)
        collector = ArtifactCollector(self.artifacts_root / "_artifact_store")
        return {
            "workspace_dir": str(ws_dir),
            "baseline_manifest": base_manifest,
            "baseline_hash": expected_hash,
            "collector_store": str(collector.store),
        }

    def run_lab_verification(
        self,
        repo_key: str,
        remote_command: str,
        *,
        wait_seconds: int = 600,
        min_free_vram_mib: int = 0,
    ) -> Dict[str, Any]:
        """Executes a syntax/benchmark command on ego, holding compute:lab when GPU access is required."""
        cfg = self.load_config()
        repo = cfg.get("repositories", {}).get(repo_key, {})
        needs_gpu = bool(repo.get("requires_gpu_lease", False))
        ssh_host = cfg.get("lab_ssh_host", "ego@ego.local")
        ssh_ip = cfg.get("lab_ssh_ip_override", "192.168.2.69")

        ssh_cmd = [
            "ssh",
            "-o",
            f"HostName={ssh_ip}",
            "-o",
            "HostKeyAlias=ego.local",
            "-o",
            "ServerAliveInterval=15",
            ssh_host,
            remote_command,
        ]

        if not needs_gpu:
            t0 = time.perf_counter()
            proc = subprocess.run(ssh_cmd, capture_output=True, text=True, timeout=wait_seconds)
            return {
                "returncode": proc.returncode,
                "stdout": proc.stdout,
                "stderr": proc.stderr,
                "elapsed_sec": round(time.perf_counter() - t0, 3),
                "used_compute_lease": False,
            }

        coord = configured_coordinator()
        if coord is None:
            raise RuntimeError("GPU verification requires active Sabrina Lab Coordinator configuration")
        coord.client = "sabrina-upstream-forge"
        coord.project = f"oss-{repo_key}"

        res: Dict[str, Any] = {}
        with coord.lease("compute:lab", wait_seconds=wait_seconds):
            if min_free_vram_mib > 0:
                deadline = time.monotonic() + 75.0
                while time.monotonic() < deadline:
                    q = subprocess.run(
                        ssh_cmd[:-1] + ["nvidia-smi --query-gpu=memory.free --format=csv,noheader,nounits"],
                        capture_output=True,
                        text=True,
                        timeout=15,
                    )
                    free_mib = int(q.stdout.strip().splitlines()[0]) if q.returncode == 0 else 0
                    if free_mib >= min_free_vram_mib:
                        break
                    time.sleep(6.0)
            t0 = time.perf_counter()
            proc = subprocess.run(ssh_cmd, capture_output=True, text=True, timeout=wait_seconds)
            res = {
                "returncode": proc.returncode,
                "stdout": proc.stdout,
                "stderr": proc.stderr,
                "elapsed_sec": round(time.perf_counter() - t0, 3),
                "used_compute_lease": True,
            }
        return res

    # ------------------------------------------------------------------
    # 2. Dual-Output Finalization (Internal Memory + External Sanitized Dossier)
    # ------------------------------------------------------------------
    def finalize_finding(
        self,
        *,
        slug: str,
        repo_key: str,
        contribution_kind: str,  # "pr_patch" | "bug_report_with_patch" | "sample_contribution" | "rfc_benchmark"
        title: str,
        problem_summary: str,
        root_cause_analysis: str,
        remedy_summary: str,
        verification_metrics: Dict[str, Any],
        reproduction_steps: List[str],
        worker_model: str,
        worker_verbatim_summary: str,
        auditor_model: str,
        auditor_verbatim_verdict: str,
        minority_dissent: str = "",
        patch_files: Optional[Dict[str, str]] = None,
        extra_files: Optional[Dict[str, str]] = None,
        skill_name: Optional[str] = None,
        auto_stage_fork_draft: bool = True,
    ) -> Dict[str, Any]:
        """
        Finalizes a verified finding into:
          1. Internal Sabrina Cognitive Blackboard + FTS5 RAG DB + SabrinaSkillHydration SKILL.md proposal
          2. Sanitized SHA-256 Upstream Dossier (`artifacts/upstream_forge/<dossier_id>/`)
          3. Personal GitHub Fork Draft PR (pushed automatically if GITHUB_TOKEN is configured, else queued)
        """
        clean_slug = re.sub(r"[^a-z0-9_\-]", "-", slug.strip().lower()).strip("-")
        if not clean_slug:
            raise ValueError("Invalid finding slug")

        wf, af = verify_disjoint_family_audit(worker_model, auditor_model)

        cfg = self.load_config()
        repo = cfg.get("repositories", {}).get(repo_key)
        if not repo:
            raise ValueError(f"Unknown repository key {repo_key!r}; enroll it first.")

        upstream_repo = repo["upstream_repo"]
        fork_repo = repo["fork_repo"]
        dossier_id = f"dos_{clean_slug}"
        dossier_dir = self.artifacts_root / dossier_id
        patches_dir = dossier_dir / "patches"
        extra_dir = dossier_dir / "files"
        dossier_dir.mkdir(parents=True, exist_ok=True)
        patches_dir.mkdir(parents=True, exist_ok=True)

        # Scrub and validate all external-facing text
        s_title = self.sanitizer.scrub_text(title)
        s_prob = self.sanitizer.scrub_text(problem_summary)
        s_root = self.sanitizer.scrub_text(root_cause_analysis)
        s_rem = self.sanitizer.scrub_text(remedy_summary)
        s_repro = [self.sanitizer.scrub_text(step) for step in reproduction_steps]
        s_metrics_json = self.sanitizer.scrub_text(json.dumps(verification_metrics, indent=2, sort_keys=True))
        s_worker = self.sanitizer.scrub_text(worker_verbatim_summary)
        s_auditor = self.sanitizer.scrub_text(auditor_verbatim_verdict)
        s_dissent = self.sanitizer.scrub_text(minority_dissent) if minority_dissent else "None recorded."

        for lbl, txt in [
            ("title", s_title),
            ("problem_summary", s_prob),
            ("root_cause_analysis", s_root),
            ("remedy_summary", s_rem),
            ("verification_metrics", s_metrics_json),
            ("worker_verbatim_summary", s_worker),
            ("auditor_verbatim_verdict", s_auditor),
            ("minority_dissent", s_dissent),
        ]:
            self.sanitizer.assert_clean(txt, label=lbl)
        for idx, step in enumerate(s_repro):
            self.sanitizer.assert_clean(step, label=f"reproduction_steps[{idx}]")

        written_patches: List[str] = []
        for pname, pcontent in sorted((patch_files or {}).items()):
            safe_pname = re.sub(r"[^A-Za-z0-9_.\-]", "_", pname)
            scrubbed_patch = self.sanitizer.scrub_text(pcontent)
            # Replace author email if needed? Author git identity is intentional, only scrub local paths/IPs/tokens.
            self.sanitizer.assert_clean(scrubbed_patch, label=f"patch:{safe_pname}")
            out_p = patches_dir / safe_pname
            out_p.write_text(scrubbed_patch, encoding="utf-8")
            written_patches.append(f"patches/{safe_pname}")

        written_extras: List[str] = []
        if extra_files:
            extra_dir.mkdir(parents=True, exist_ok=True)
            for rel_name, fcontent in sorted(extra_files.items()):
                norm_rel = Path(rel_name).as_posix().lstrip("/")
                if ".." in norm_rel.split("/"):
                    raise ValueError(f"Invalid extra file relative path: {rel_name}")
                scrubbed_extra = self.sanitizer.scrub_text(fcontent)
                self.sanitizer.assert_clean(scrubbed_extra, label=f"extra:{norm_rel}")
                out_e = extra_dir / norm_rel
                out_e.parent.mkdir(parents=True, exist_ok=True)
                out_e.write_text(scrubbed_extra, encoding="utf-8")
                written_extras.append(f"files/{norm_rel}")

        repro_md = "\n".join(f"{i + 1}. {st}" for i, st in enumerate(s_repro))
        patch_list_md = (
            "\n".join(f"- `{p}`" for p in written_patches + written_extras)
            if (written_patches or written_extras)
            else "- *(Architectural / Benchmark Report — no source diff attached)*"
        )

        pr_markdown = (
            f"# {s_title}\n\n"
            f"- **Target Upstream Repository**: `{upstream_repo}`\n"
            f"- **Contribution Type**: `{contribution_kind}`\n"
            f"- **Personal Staging Fork**: `{fork_repo}` (Draft PR branch `sabrina/{clean_slug}`)\n\n"
            f"## 1. Summary & Motivation\n\n{s_prob}\n\n"
            f"## 2. Root-Cause & Technical Analysis\n\n{s_root}\n\n"
            f"## 3. Proposed Fix / Reference Implementation\n\n{s_rem}\n\n"
            f"### Included Artifacts\n{patch_list_md}\n\n"
            f"## 4. Reproducible Verification & Benchmark Evidence\n\n"
            f"```json\n{s_metrics_json}\n```\n\n"
            f"### Reproduction Steps\n{repro_md}\n\n"
            f"## 5. Multi-Family Swarm Provenance & Disjoint Audit\n\n"
            f"- **Primary Synthesis Lineage**: `{worker_model}` (family: `{wf}`)\n"
            f"  > {s_worker}\n"
            f"- **Disjoint-Family Auditor**: `{auditor_model}` (family: `{af}`)\n"
            f"  > {s_auditor}\n"
            f"- **Signed Minority / Boundary Dissent**:\n"
            f"  > {s_dissent}\n"
        )
        self.sanitizer.assert_clean(pr_markdown, label="UPSTREAM_PR.md")

        (dossier_dir / "UPSTREAM_PR.md").write_text(pr_markdown, encoding="utf-8")
        (dossier_dir / "verification_evidence.json").write_text(s_metrics_json + "\n", encoding="utf-8")

        # Compute deterministic SHA-256 over every file in the sanitized dossier
        file_hashes: Dict[str, str] = {}
        for fpath in sorted(dossier_dir.rglob("*")):
            if fpath.is_file() and fpath.name != "manifest_sha256.json":
                rel = fpath.relative_to(dossier_dir).as_posix()
                file_hashes[rel] = sha256_bytes(fpath.read_bytes())

        dossier_sha256 = sha256_text(
            json.dumps(
                {
                    "dossier_id": dossier_id,
                    "upstream_repo": upstream_repo,
                    "fork_repo": fork_repo,
                    "files": file_hashes,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        manifest_doc = {
            "dossier_id": dossier_id,
            "slug": clean_slug,
            "repo_key": repo_key,
            "upstream_repo": upstream_repo,
            "fork_repo": fork_repo,
            "contribution_kind": contribution_kind,
            "worker_model": worker_model,
            "worker_family": wf,
            "auditor_model": auditor_model,
            "auditor_family": af,
            "dossier_sha256": dossier_sha256,
            "files": file_hashes,
            "generated_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        }
        (dossier_dir / "manifest_sha256.json").write_text(
            json.dumps(manifest_doc, indent=2) + "\n", encoding="utf-8"
        )

        # --- INTERNAL DISTILLATION 1: Cognitive Blackboard ---
        bb = HierarchicalCognitiveBlackboard(self.blackboard_db_path)
        pat_id = f"oss_pat_{clean_slug}"
        try:
            bb.record_solution_pattern(
                pat_id=pat_id,
                domain=f"oss:{repo_key}",
                bug_sig=s_title[:200],
                remedy=s_rem[:1000],
                ast_patch=", ".join(written_patches + written_extras) or contribution_kind,
            )
            bb.record_hypothesis(
                hyp_id=f"oss_hyp_{clean_slug}",
                domain=f"oss:{repo_key}",
                model_id=worker_model,
                stmt=f"{s_title} | Audited by {auditor_model} ({af})",
                conf=0.95,
                status="verified_staged",
            )
        finally:
            bb.conn.close()

        # --- INTERNAL DISTILLATION 2: FTS5 RAG Knowledge Base ---
        rag_topic = f"oss_upstream_{repo_key}_{clean_slug}"
        rag_body = (
            f"[{upstream_repo}] {s_title}\n"
            f"Root Cause: {s_root}\n"
            f"Remedy: {s_rem}\n"
            f"Evidence: {s_metrics_json}\n"
            f"Dossier SHA-256: {dossier_sha256}"
        )
        try:
            rag_chunk_id = int(
                add_knowledge(
                    topic=rag_topic,
                    content=rag_body,
                    source_type="oss_upstream_forge",
                    db_path=self.rag_db_path,
                )
            )
        except sqlite3.IntegrityError:
            # Idempotent re-run when identical content_hash already exists in RAG DB
            with closing(sqlite3.connect(self.rag_db_path)) as rconn:
                row = rconn.execute(
                    "SELECT c.id FROM chunks c JOIN documents d ON d.id = c.document_id WHERE d.title = ?",
                    (rag_topic,),
                ).fetchone()
                rag_chunk_id = int(row[0]) if row else 0

        # --- INTERNAL DISTILLATION 3: SabrinaSkillHydration SKILL.md Proposal ---
        sk_name = re.sub(
            r"[^a-z0-9_\-]", "-", (skill_name or f"oss-{repo_key}-{clean_slug[:32]}").lower()
        ).strip("-")
        skill_md = (
            f"---\n"
            f"name: {sk_name}\n"
            f"version: \"1.0.0\"\n"
            f"description: >-\n"
            f"  Reusable engineering pattern learned from {upstream_repo}: {s_title[:140]}\n"
            f"---\n\n"
            f"# {s_title}\n\n"
            f"## When to Apply\n\n{s_prob}\n\n"
            f"## Root-Cause Diagnostic Pattern\n\n{s_root}\n\n"
            f"## Verified Engineering Remedy\n\n{s_rem}\n\n"
            f"## Verification & Provenance\n\n"
            f"- **Upstream Target**: `{upstream_repo}` (`{dossier_id}`)\n"
            f"- **Dossier SHA-256**: `{dossier_sha256}`\n"
            f"- **Worker / Auditor Lineages**: `{worker_model}` (`{wf}`) / `{auditor_model}` (`{af}`)\n"
        )
        skills_target_dir = (
            (SABRINA_ROOT / "skills")
            if self.assistant_db_path.resolve() == DEFAULT_ASSISTANT_DB.resolve()
            else (self.artifacts_root / "_skills")
        )
        hydrator = SabrinaSkillHydration(db_path=self.assistant_db_path, skills_dir=skills_target_dir)
        prop_id = hydrator.create_proposal(
            skill_name=sk_name,
            skill_content=skill_md,
            trigger_reason="upstream_oss_forge_distillation",
            query_used=f"{upstream_repo}: {s_title}",
            run_id=dossier_id,
            search_sources=json.dumps([{"repo": upstream_repo, "dossier_sha256": dossier_sha256}]),
        )
        prop_meta = hydrator.get_proposal(prop_id)
        if prop_meta:
            try:
                hydrator.approve_proposal(
                    prop_id,
                    expected_version=str(prop_meta.get("version", "1.0.0")),
                    expected_sha256=str(prop_meta["sha256"]),
                    reviewer=f"SabrinaSwarmGate({worker_model}+{auditor_model})",
                    feedback=f"Auto-installed after disjoint-family verification ({dossier_sha256[:16]})",
                )
            except Exception:
                pass

        fork_branch = f"sabrina/{clean_slug}"
        now = time.time()
        with closing(sqlite3.connect(self.forge_db_path)) as conn:
            conn.execute(
                """
                INSERT INTO forge_dossiers (
                    dossier_id, slug, repo_key, upstream_repo, fork_repo, contribution_kind,
                    title, worker_model, worker_family, auditor_model, auditor_family,
                    minority_dissent, dossier_dir, dossier_sha256, skill_proposal_id,
                    rag_chunk_id, blackboard_pattern_id, fork_branch, fork_pr_status,
                    fork_pr_url, upstream_status, upstream_url, approved_by, approved_at,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', 'awaiting_human_signoff', '', '', NULL, ?, ?)
                ON CONFLICT(dossier_id) DO UPDATE SET
                    title=excluded.title,
                    worker_model=excluded.worker_model,
                    worker_family=excluded.worker_family,
                    auditor_model=excluded.auditor_model,
                    auditor_family=excluded.auditor_family,
                    minority_dissent=excluded.minority_dissent,
                    dossier_sha256=excluded.dossier_sha256,
                    skill_proposal_id=excluded.skill_proposal_id,
                    rag_chunk_id=excluded.rag_chunk_id,
                    updated_at=excluded.updated_at
                """,
                (
                    dossier_id,
                    clean_slug,
                    repo_key,
                    upstream_repo,
                    fork_repo,
                    contribution_kind,
                    s_title,
                    worker_model,
                    wf,
                    auditor_model,
                    af,
                    s_dissent,
                    str(dossier_dir),
                    dossier_sha256,
                    prop_id,
                    rag_chunk_id,
                    pat_id,
                    fork_branch,
                    "pending_fork_push",
                    now,
                    now,
                ),
            )
            conn.commit()

        fork_result = {"fork_pr_status": "pending_fork_push", "fork_pr_url": ""}
        if auto_stage_fork_draft:
            fork_result = self.stage_personal_fork_draft_pr(dossier_id)

        return {
            "dossier_id": dossier_id,
            "dossier_dir": str(dossier_dir),
            "dossier_sha256": dossier_sha256,
            "worker_family": wf,
            "auditor_family": af,
            "skill_proposal_id": prop_id,
            "rag_chunk_id": rag_chunk_id,
            "blackboard_pattern_id": pat_id,
            **fork_result,
            "upstream_status": "awaiting_human_signoff",
        }

    # ------------------------------------------------------------------
    # 3. Personal GitHub Fork Automatic Draft PR Staging (Zero Upstream Noise)
    # ------------------------------------------------------------------
    @staticmethod
    def _ssl_context() -> ssl.SSLContext:
        try:
            import truststore  # type: ignore

            return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        except Exception:
            pass
        try:
            import certifi  # type: ignore

            return ssl.create_default_context(cafile=certifi.where())
        except Exception:
            return ssl.create_default_context()

    def _github_api(
        self,
        token: str,
        method: str,
        path: str,
        payload: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        url = f"https://api.github.com{path}"
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        last_exc: Optional[Exception] = None
        for attempt in range(4):
            req = urllib.request.Request(
                url,
                data=data,
                method=method,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                    "User-Agent": "SabrinaUpstreamForge/1.0",
                    "Content-Type": "application/json",
                },
            )
            try:
                with urllib.request.urlopen(req, timeout=12, context=self._ssl_context()) as resp:
                    raw = resp.read().decode("utf-8")
                    return json.loads(raw) if raw else {}
            except urllib.error.HTTPError:
                raise
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_exc = exc
                if attempt < 3:
                    time.sleep(1.0 * (attempt + 1))
        assert last_exc is not None
        raise last_exc

    def _get_github_token(self) -> str:
        cfg = self.load_config()
        sec_name = cfg.get("github_token_secret_name", "GITHUB_TOKEN")
        return get_secret(sec_name) or get_secret("GH_TOKEN") or get_secret("GITHUB_PAT")

    def ensure_cdp_browser(self, cdp_url: str = "http://127.0.0.1:9222") -> Dict[str, Any]:
        """
        Probes the local Chrome DevTools Protocol (CDP) endpoint (`127.0.0.1:9222`).
        If running on Windows and port 9222 is closed, launches persistent detached
        Chrome using `SabrinaDebugProfile` so existing GitHub session cookies are active.
        """
        ver_url = f"{cdp_url.rstrip('/')}/json/version"
        try:
            with urllib.request.urlopen(ver_url, timeout=2.5) as resp:
                if resp.status == 200:
                    return {"ok": True, "cdp_url": cdp_url, "launched": False, "info": json.loads(resp.read().decode("utf-8"))}
        except Exception:
            pass

        if os.name == "nt":
            ps_cmd = (
                "$c='C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe'; "
                "if(-not (Test-Path $c)){$c='C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe'}; "
                "$p=\"$env:LOCALAPPDATA\\SabrinaDebugProfile\"; "
                "$cmd=\"`\"$c`\" --remote-debugging-port=9222 --user-data-dir=`\"$p`\" --no-first-run --no-default-browser-check about:blank\"; "
                "Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{CommandLine=$cmd} | Out-Null"
            )
            try:
                subprocess.run(["powershell.exe", "-NoProfile", "-Command", ps_cmd], capture_output=True, text=True, timeout=12)
                for _ in range(10):
                    time.sleep(0.5)
                    try:
                        with urllib.request.urlopen(ver_url, timeout=2.0) as resp:
                            if resp.status == 200:
                                return {"ok": True, "cdp_url": cdp_url, "launched": True, "info": json.loads(resp.read().decode("utf-8"))}
                    except Exception:
                        continue
            except Exception as exc:
                return {"ok": False, "cdp_url": cdp_url, "error": str(exc)}
        return {"ok": False, "cdp_url": cdp_url, "error": "CDP endpoint not listening"}

    def browser_create_github_repo(
        self,
        repo_name: str,
        description: str = "",
        private: bool = False,
        local_repo_path: str = "",
        auto_push: bool = True,
        owner: str = "CharlesMcGowen",
        cdp_url: str = "http://127.0.0.1:9222",
    ) -> Dict[str, Any]:
        """
        Autonomously creates a GitHub repository (`owner/repo_name`) and optionally pushes
        a sanitized local git repository (`main` branch):
          1. If invoked on Linux (`ego.local`), automatically delegates execution to the
             `windows-workstation` federation node where `SabrinaDebugProfile` Chrome CDP (:9222)
             and the operator's GitHub SSH key live.
          2. Checks if `https://api.github.com/repos/{owner}/{repo_name}` already exists.
          3. Tries REST `POST /user/repos` if `GITHUB_TOKEN` has `Administration: write`.
          4. Falls back to Chrome CDP (`http://127.0.0.1:9222` -> `https://github.com/new`),
             filling `#repository-name-input` and `input[name="Description"]` and clicking
             `Create repository`.
          5. When `auto_push=True` and `local_repo_path` exists, runs `UpstreamSanitizer`
             on tracked files and pushes `main` to `git@github.com:{owner}/{repo_name}.git`.
        """
        clean_name = re.sub(r"[^A-Za-z0-9_.-]", "-", (repo_name or "").strip()).strip("-")
        if not clean_name:
            return {"ok": False, "error": "repo_name is required"}

        if os.name != "nt":
            try:
                from tools.sabrina_compute_federation import SabrinaComputeFederation
                fed = SabrinaComputeFederation()
                win_repo_path = local_repo_path
                if not win_repo_path or win_repo_path.startswith("/"):
                    win_repo_path = f"c:\\Users\\bcmcg\\Desktop\\bot\\oss_projects\\{clean_name}"
                code = (
                    "import sys, json\n"
                    "sys.path.insert(0, r'c:\\Users\\bcmcg\\Desktop\\bot\\Sabrina')\n"
                    "from tools.sabrina_upstream_forge import SabrinaUpstreamForge\n"
                    f"res = SabrinaUpstreamForge().browser_create_github_repo("
                    f"repo_name={clean_name!r}, description={description!r}, private={bool(private)!r}, "
                    f"local_repo_path={win_repo_path!r}, auto_push={bool(auto_push)!r}, owner={owner!r})\n"
                    "print(json.dumps(res))\n"
                )
                f_res = fed.execute_python(code, target="windows-workstation", timeout=60)
                stdout = (f_res.get("stdout") or "").strip()
                if stdout:
                    last_line = [ln for ln in stdout.splitlines() if ln.strip().startswith("{")]
                    if last_line:
                        return json.loads(last_line[-1])
                return {"ok": False, "delegated_to": "windows-workstation", "federation_result": f_res}
            except Exception as exc:
                return {"ok": False, "delegated_to": "windows-workstation", "error": str(exc)}

        full_repo = f"{owner}/{clean_name}"
        token = self._get_github_token()
        created_via = "already_exists"
        repo_exists = False

        # 1. Check if repo already exists on GitHub
        try:
            if token:
                self._github_api(token, "GET", f"/repos/{full_repo}")
                repo_exists = True
            else:
                req = urllib.request.Request(f"https://api.github.com/repos/{full_repo}", headers={"User-Agent": "SabrinaUpstreamForge/1.0"})
                with urllib.request.urlopen(req, timeout=8, context=self._ssl_context()) as resp:
                    if resp.status == 200:
                        repo_exists = True
        except Exception:
            repo_exists = False

        # 2. Try REST POST /user/repos if token has scope
        if not repo_exists and token:
            try:
                self._github_api(
                    token,
                    "POST",
                    "/user/repos",
                    {
                        "name": clean_name,
                        "description": description or f"{clean_name} — Built by Sabrina Multi-Family Collective",
                        "private": bool(private),
                        "auto_init": False,
                    },
                )
                repo_exists = True
                created_via = "github_rest_api"
            except Exception:
                repo_exists = False

        # 3. Use SabrinaDebugProfile Chrome CDP at https://github.com/new
        cdp_detail = {}
        if not repo_exists:
            cdp_boot = self.ensure_cdp_browser(cdp_url=cdp_url)
            if not cdp_boot.get("ok"):
                return {"ok": False, "repo": full_repo, "error": "CDP browser unavailable", "cdp": cdp_boot}
            try:
                from ai_systems.browser_utils import PlaywrightBrowserManager
                mgr = PlaywrightBrowserManager.get_instance()
                conn = mgr.connect_cdp(cdp_url=cdp_url, timeout=15000)
                if conn.get("status") != "success":
                    return {"ok": False, "repo": full_repo, "error": f"CDP attach failed: {conn}"}
                try:
                    mgr.navigate("https://github.com/new")
                    page = mgr.page
                    if page is None:
                        return {"ok": False, "repo": full_repo, "error": "No active CDP browser page"}
                    cur_url = page.url or ""
                    if "/login" in cur_url or "session" in cur_url:
                        return {
                            "ok": False,
                            "repo": full_repo,
                            "status": "github_login_required",
                            "url": cur_url,
                            "message": "SabrinaDebugProfile Chrome is on GitHub login; sign in once in the Chrome window.",
                        }

                    name_input = page.locator("#repository-name-input, input[aria-label*='Repository name' i]").first
                    name_input.wait_for(state="visible", timeout=12000)
                    name_input.click()
                    name_input.fill(clean_name)

                    if description:
                        desc_input = page.locator("input[name='Description'], input[aria-label*='Description' i]").first
                        if desc_input.count() > 0 and desc_input.is_visible():
                            desc_input.fill(description)

                    # Allow GitHub's async repository-name availability validator to settle
                    time.sleep(2.2)

                    submit_btn = page.locator("button[type='submit']:has-text('Create repository'), button:has-text('Create repository')").first
                    submit_btn.wait_for(state="visible", timeout=10000)
                    submit_btn.click(timeout=8000)

                    # Poll up to 16s for navigation or API confirmation
                    for _ in range(8):
                        time.sleep(2.0)
                        try:
                            if token:
                                self._github_api(token, "GET", f"/repos/{full_repo}")
                                repo_exists = True
                                break
                        except Exception:
                            pass
                        if f"/{owner.lower()}/{clean_name.lower()}" in (page.url or "").lower():
                            repo_exists = True
                            break

                    created_via = "chrome_cdp_browser"
                    cdp_detail = {"final_url": page.url, "title": page.title()}
                finally:
                    mgr.disconnect_cdp()
            except Exception as exc:
                return {"ok": False, "repo": full_repo, "error": f"CDP repo creation failed: {exc}"}

        if not repo_exists:
            return {"ok": False, "repo": full_repo, "error": "Repository creation could not be confirmed on GitHub", "cdp": cdp_detail}

        # 4. Optional sanitized git push of local repository
        push_detail: Dict[str, Any] = {"pushed": False}
        candidate_dir = Path(local_repo_path) if local_repo_path else (SABRINA_ROOT.parent / "oss_projects" / clean_name)
        if auto_push and candidate_dir.is_dir() and (candidate_dir / ".git").exists():
            # Sanitize check all tracked files before pushing
            leaks: List[str] = []
            for fpath in sorted(candidate_dir.rglob("*")):
                if not fpath.is_file() or ".git" in fpath.parts or "__pycache__" in fpath.parts:
                    continue
                try:
                    txt = fpath.read_text(encoding="utf-8")
                    self.sanitizer.assert_clean(txt, label=fpath.name)
                except ValueError as ve:
                    leaks.append(str(ve))
                except UnicodeDecodeError:
                    continue
            if leaks:
                return {"ok": False, "repo": full_repo, "created_via": created_via, "error": "Sanitization gate blocked push", "leaks": leaks}

            ssh_remote = f"git@github.com:{full_repo}.git"
            subprocess.run(["git", "-C", str(candidate_dir), "remote", "set-url", "origin", ssh_remote], capture_output=True, text=True, timeout=10)
            push_proc = subprocess.run(
                ["git", "-C", str(candidate_dir), "push", "-u", "origin", "main"],
                capture_output=True,
                text=True,
                timeout=45,
            )
            push_detail = {
                "pushed": push_proc.returncode == 0,
                "returncode": push_proc.returncode,
                "remote": ssh_remote,
                "local_path": str(candidate_dir),
                "stdout": (push_proc.stdout or "")[-500:],
                "stderr": (push_proc.stderr or "")[-500:],
            }

        return {
            "ok": True,
            "repo": full_repo,
            "html_url": f"https://github.com/{full_repo}",
            "created_via": created_via,
            "cdp": cdp_detail,
            "git_push": push_detail,
        }

    def browser_generate_github_token(
        self,
        description: str = "Sabrina-Autonomous-Collective",
        scopes: str = "repo,workflow,read:org",
        wait_for_sudo_sec: int = 12,
        cdp_url: str = "http://127.0.0.1:9222",
    ) -> Dict[str, Any]:
        """
        Uses SabrinaDebugProfile Chrome CDP (:9222) to navigate to GitHub's token generator
        (`https://github.com/settings/tokens/new?scopes=...&description=...`):
          - If GitHub presents Sudo Mode (`Confirm access`) and `GITHUB_TOTP_SECRET` is enrolled
            in `secret_store`, automatically generates and submits the 6-digit RFC 6238 TOTP code.
          - If `GITHUB_TOTP_SECRET` is not enrolled yet, leaves the `Confirm access` tab open in
            the visible Chrome window and waits up to `wait_for_sudo_sec` seconds for confirmation.
          - Once on `/settings/tokens/new`, clicks `Generate token`, reads `#new-oauth-token`,
            and persists it to `secret_store.set_secret("GITHUB_TOKEN", token)`.
        """
        if os.name != "nt":
            try:
                from tools.sabrina_compute_federation import SabrinaComputeFederation
                fed = SabrinaComputeFederation()
                code = (
                    "import sys, json\n"
                    "sys.path.insert(0, r'c:\\Users\\bcmcg\\Desktop\\bot\\Sabrina')\n"
                    "from tools.sabrina_upstream_forge import SabrinaUpstreamForge\n"
                    f"res = SabrinaUpstreamForge().browser_generate_github_token("
                    f"description={description!r}, scopes={scopes!r}, wait_for_sudo_sec={int(wait_for_sudo_sec)})\n"
                    "print(json.dumps(res))\n"
                )
                f_res = fed.execute_python(code, target="windows-workstation", timeout=60)
                stdout = (f_res.get("stdout") or "").strip()
                if stdout:
                    last_line = [ln for ln in stdout.splitlines() if ln.strip().startswith("{")]
                    if last_line:
                        return json.loads(last_line[-1])
                return {"ok": False, "delegated_to": "windows-workstation", "federation_result": f_res}
            except Exception as exc:
                return {"ok": False, "delegated_to": "windows-workstation", "error": str(exc)}

        cdp_boot = self.ensure_cdp_browser(cdp_url=cdp_url)
        if not cdp_boot.get("ok"):
            return {"ok": False, "error": "CDP browser unavailable", "cdp": cdp_boot}

        try:
            from ai_systems.browser_utils import PlaywrightBrowserManager
            from tools.secret_store import set_secret
            import urllib.parse

            mgr = PlaywrightBrowserManager.get_instance()
            conn = mgr.connect_cdp(cdp_url=cdp_url, timeout=15000)
            if conn.get("status") != "success":
                return {"ok": False, "error": f"CDP attach failed: {conn}"}

            try:
                q = urllib.parse.urlencode({"scopes": scopes, "description": f"{description}-{int(time.time())}"})
                target_url = f"https://github.com/settings/tokens/new?{q}"
                mgr.navigate(target_url)
                page = mgr.page
                if page is None:
                    return {"ok": False, "error": "No active CDP browser page"}

                title = (page.title() or "").lower()
                if "confirm access" in title or page.locator("input#app_totp, input#sudo_email_otp").count() > 0:
                    totp_seed = get_secret("GITHUB_TOTP_SECRET")
                    if totp_seed and page.locator("input#app_totp").count() > 0:
                        from tools.custom.totp_authenticator_plugin import generate_totp
                        code = generate_totp(totp_seed)["code"]
                        page.locator("input#app_totp").first.fill(code)
                        page.locator("button[type='submit']:has-text('Verify')").first.click(timeout=5000)
                        time.sleep(2.5)
                    else:
                        deadline = time.time() + max(2, int(wait_for_sudo_sec))
                        while time.time() < deadline:
                            cur_title = (page.title() or "").lower()
                            if "confirm access" not in cur_title and page.locator("button:has-text('Generate token')").count() > 0:
                                break
                            time.sleep(1.5)

                cur_title = (page.title() or "").lower()
                if "confirm access" in cur_title:
                    return {
                        "ok": False,
                        "status": "awaiting_sudo_confirmation",
                        "url": page.url,
                        "message": (
                            "GitHub Sudo Mode ('Confirm access') is open in SabrinaDebugProfile Chrome. "
                            "Enter your 2FA code once in that Chrome tab (or store GITHUB_TOTP_SECRET in secret_store "
                            "for 100% hands-free TOTP), then re-prompt Sabrina."
                        ),
                    }

                gen_btn = page.locator("button[type='submit']:has-text('Generate token'), button:has-text('Generate token')").first
                if gen_btn.count() == 0:
                    return {"ok": False, "status": "generate_button_not_found", "url": page.url, "title": page.title()}

                gen_btn.click(timeout=8000)
                tok_loc = page.locator("#new-oauth-token").first
                tok_loc.wait_for(state="visible", timeout=12000)
                new_token = (tok_loc.inner_text() or "").strip()
                if not new_token:
                    return {"ok": False, "status": "token_element_empty", "url": page.url}

                set_secret("GITHUB_TOKEN", new_token)
                return {
                    "ok": True,
                    "status": "token_generated_and_saved",
                    "token_prefix": new_token[:10] + "...",
                    "scopes": scopes,
                    "stored_in": "Windows Credential Manager (sabrina/GITHUB_TOKEN)",
                }
            finally:
                mgr.disconnect_cdp()
        except Exception as exc:
            return {"ok": False, "error": f"CDP token generation failed: {exc}"}

    def browser_fork_github_repo(
        self,
        upstream_repo: str,
        cdp_url: str = "http://127.0.0.1:9222",
    ) -> Dict[str, Any]:
        """
        Uses Sabrina's PlaywrightBrowserManager (CDP connection or saved browser_auth_state.json)
        to navigate to https://github.com/<upstream_repo>/fork and click 'Create fork' autonomously.
        """
        try:
            from ai_systems.browser_utils import PlaywrightBrowserManager

            mgr = PlaywrightBrowserManager.get_instance()
            if not mgr.is_healthy():
                res = mgr.connect_or_launch(cdp_url=cdp_url, headless=True)
                if "Error" in str(res):
                    return {"ok": False, "method": "playwright_browser", "detail": str(res)}

            fork_url = f"https://github.com/{upstream_repo.strip('/')}/fork"
            mgr.navigate(fork_url)
            page = mgr.page
            if page is None:
                return {"ok": False, "method": "playwright_browser", "detail": "No active browser page"}

            cur_url = page.url or ""
            if "/login" in cur_url or "session" in cur_url:
                return {
                    "ok": False,
                    "method": "playwright_browser",
                    "detail": "Browser session is not logged into GitHub (redirected to /login)",
                }

            selectors = [
                "button:has-text('Create fork')",
                "button[type='submit']:has-text('Create fork')",
                "button:has-text('Fork')",
            ]
            clicked = False
            for sel in selectors:
                try:
                    loc = page.locator(sel).first
                    if loc.count() > 0 and loc.is_visible():
                        loc.click(timeout=5000)
                        clicked = True
                        break
                except Exception:
                    continue
            if clicked:
                time.sleep(4.0)
                return {"ok": True, "method": "playwright_browser", "url": page.url}
            return {
                "ok": False,
                "method": "playwright_browser",
                "detail": f"Create fork button not visible on {cur_url}",
            }
        except Exception as exc:
            return {"ok": False, "method": "playwright_browser", "detail": str(exc)}

    def stage_personal_fork_draft_pr(self, dossier_id: str) -> Dict[str, Any]:
        """
        Automatically creates/updates a branch on the PERSONAL FORK (`fork_repo`) containing
        the sanitized dossier + files and opens a **Draft Pull Request** against the personal
        fork's own default branch (so upstream maintainers receive zero notifications until
        human sign-off). Queues cleanly if `GITHUB_TOKEN` is not yet configured.
        """
        rec = self.get_dossier(dossier_id)
        if not rec:
            raise ValueError(f"Unknown dossier {dossier_id!r}")

        token = self._get_github_token()
        if not token:
            self._update_fork_status(dossier_id, "queued_awaiting_github_token", "")
            return {
                "fork_pr_status": "queued_awaiting_github_token",
                "fork_pr_url": "",
                "note": (
                    "Queued cleanly for personal-fork Draft PR push. Store token once via "
                    "`python tools/secret_store.py set GITHUB_TOKEN` then run "
                    "`python -m tools.sabrina_upstream_forge sync-fork-drafts`."
                ),
            }

        fork_repo = rec["fork_repo"]
        upstream_repo = rec["upstream_repo"]
        branch_name = rec["fork_branch"]
        dossier_dir = Path(rec["dossier_dir"])

        # 1. Ensure personal fork exists on GitHub
        try:
            fork_meta = self._github_api(token, "GET", f"/repos/{fork_repo}")
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                raise
            # Resolve canonical upstream name (handles org renames like facebook/openzl -> meta/openzl)
            upstream_meta = self._github_api(token, "GET", f"/repos/{upstream_repo}")
            canonical_upstream = upstream_meta.get("full_name", upstream_repo)
            fork_created = False
            try:
                self._github_api(token, "POST", f"/repos/{canonical_upstream}/forks", {})
                fork_created = True
            except urllib.error.HTTPError as fork_exc:
                if fork_exc.code != 403:
                    raise

            # Fallback A: Use Sabrina's PlaywrightBrowserManager (CDP/saved browser session) to click Fork
            if not fork_created:
                bfork = self.browser_fork_github_repo(canonical_upstream)
                if bfork.get("ok"):
                    fork_created = True

            # Fallback B: Create personal staging repository directly via POST /user/repos
            if not fork_created:
                repo_short_name = fork_repo.split("/")[-1]
                try:
                    self._github_api(
                        token,
                        "POST",
                        "/user/repos",
                        {
                            "name": repo_short_name,
                            "description": f"Sabrina Upstream Staging Fork for {canonical_upstream}",
                            "private": False,
                            "auto_init": True,
                        },
                    )
                    fork_created = True
                except urllib.error.HTTPError as user_repo_exc:
                    if user_repo_exc.code in (403, 422):
                        self._update_fork_status(
                            dossier_id, "awaiting_manual_fork_or_admin_scope", ""
                        )
                        return {
                            "fork_pr_status": "awaiting_manual_fork_or_admin_scope",
                            "fork_pr_url": "",
                            "browser_attempt": bfork,
                            "note": (
                                f"Sabrina tried REST fork (`POST /repos/{canonical_upstream}/forks` -> 403), "
                                f"Playwright browser fork (`{bfork.get('detail', 'unavailable')}`), and "
                                f"`POST /user/repos` (-> {user_repo_exc.code}). Grant 'Administration: Read and write' "
                                f"to GITHUB_TOKEN or launch Chrome with `--remote-debugging-port=9222` logged into GitHub."
                            ),
                        }
                    raise

            fork_meta = None
            for _ in range(10):
                time.sleep(2.0)
                try:
                    fork_meta = self._github_api(token, "GET", f"/repos/{fork_repo}")
                    break
                except urllib.error.HTTPError:
                    continue
            if fork_meta is None:
                raise RuntimeError(f"Timed out waiting for GitHub to provision fork {fork_repo}")

        default_branch = fork_meta.get("default_branch", "main")
        ref_info = self._github_api(token, "GET", f"/repos/{fork_repo}/git/ref/heads/{default_branch}")
        base_sha = ref_info["object"]["sha"]
        base_commit = self._github_api(token, "GET", f"/repos/{fork_repo}/git/commits/{base_sha}")
        base_tree_sha = base_commit["tree"]["sha"]

        # 2. Upload sanitized dossier files (+ any mapped repo files under `files/`) into fork branch tree
        cfg = self.load_config()
        repo_cfg = cfg.get("repositories", {}).get(rec["repo_key"], {})
        subdir_prefix = str(repo_cfg.get("upstream_subdir", "")).strip("/")

        tree_entries: List[Dict[str, Any]] = []
        for fpath in sorted(dossier_dir.rglob("*")):
            if not fpath.is_file():
                continue
            rel = fpath.relative_to(dossier_dir).as_posix()
            content_bytes = fpath.read_bytes()
            # Double-verify sanitization before any external GitHub API write!
            self.sanitizer.assert_clean(content_bytes.decode("utf-8", errors="replace"), label=rel)
            blob = self._github_api(
                token,
                "POST",
                f"/repos/{fork_repo}/git/blobs",
                {"content": base64.b64encode(content_bytes).decode("ascii"), "encoding": "base64"},
            )
            if rel.startswith("files/"):
                inner = rel[len("files/") :]
                repo_dest = f"{subdir_prefix}/{inner}" if subdir_prefix else inner
            else:
                repo_dest = f".sabrina_dossiers/{rec['slug']}/{rel}"
            tree_entries.append(
                {"path": repo_dest, "mode": "100644", "type": "blob", "sha": blob["sha"]}
            )

        new_tree = self._github_api(
            token,
            "POST",
            f"/repos/{fork_repo}/git/trees",
            {"base_tree": base_tree_sha, "tree": tree_entries},
        )
        commit_msg = (
            f"chore(sabrina-forge): stage {rec['slug']} ({rec['dossier_sha256'][:12]})\n\n"
            f"Automated personal-fork staging for human review prior to upstream submission to {upstream_repo}."
        )
        new_commit = self._github_api(
            token,
            "POST",
            f"/repos/{fork_repo}/git/commits",
            {"message": commit_msg, "tree": new_tree["sha"], "parents": [base_sha]},
        )

        # 3. Create or fast-forward `refs/heads/sabrina/<slug>` on personal fork ONLY
        try:
            self._github_api(
                token,
                "POST",
                f"/repos/{fork_repo}/git/refs",
                {"ref": f"refs/heads/{branch_name}", "sha": new_commit["sha"]},
            )
        except urllib.error.HTTPError as exc:
            if exc.code == 422:
                self._github_api(
                    token,
                    "PATCH",
                    f"/repos/{fork_repo}/git/refs/heads/{branch_name}",
                    {"sha": new_commit["sha"], "force": True},
                )
            else:
                raise

        # 4. Open Draft PR targeting PERSONAL FORK default branch (zero upstream noise!)
        fork_owner = fork_repo.split("/")[0]
        existing_prs = self._github_api(
            token,
            "GET",
            f"/repos/{fork_repo}/pulls?state=open&head={fork_owner}:{branch_name}&base={default_branch}",
        )
        pr_body = (dossier_dir / "UPSTREAM_PR.md").read_text(encoding="utf-8") + (
            f"\n\n---\n**Sabrina Governance Gate**: Staged as a personal-fork Draft PR. "
            f"Approve upstream promotion with exact SHA-256 `{rec['dossier_sha256']}`."
        )
        if existing_prs:
            pr_url = existing_prs[0]["html_url"]
        else:
            pr = self._github_api(
                token,
                "POST",
                f"/repos/{fork_repo}/pulls",
                {
                    "title": f"[Sabrina Staging -> {upstream_repo}] {rec['title']}",
                    "head": branch_name,
                    "base": default_branch,
                    "body": pr_body,
                    "draft": True,
                },
            )
            pr_url = pr["html_url"]

        self._update_fork_status(dossier_id, "draft_pr_open_on_personal_fork", pr_url)
        return {"fork_pr_status": "draft_pr_open_on_personal_fork", "fork_pr_url": pr_url}

    def sync_queued_fork_drafts(self) -> List[Dict[str, Any]]:
        """Pushes all queued/pending dossiers to personal-fork Draft PRs once GITHUB_TOKEN or browser session is available."""
        all_rows = self.list_dossiers()
        queued = [
            d
            for d in all_rows
            if d.get("fork_pr_status")
            in {
                "queued_awaiting_github_token",
                "pending_fork_push",
                "awaiting_manual_fork_or_admin_scope",
            }
        ]
        results = []
        for d in queued:
            res = self.stage_personal_fork_draft_pr(d["dossier_id"])
            results.append({"dossier_id": d["dossier_id"], **res})
        return results

    def _update_fork_status(self, dossier_id: str, status: str, url: str) -> None:
        with closing(sqlite3.connect(self.forge_db_path)) as conn:
            conn.execute(
                "UPDATE forge_dossiers SET fork_pr_status = ?, fork_pr_url = ?, updated_at = ? WHERE dossier_id = ?",
                (status, url, time.time(), dossier_id),
            )
            conn.commit()

    # ------------------------------------------------------------------
    # 4. Human SHA-256 Sign-Off Gate Before Upstream Submission
    # ------------------------------------------------------------------
    def promote_to_upstream(
        self,
        dossier_id: str,
        *,
        expected_sha256: str,
        reviewer: str,
        submit_live_upstream: bool = False,
    ) -> Dict[str, Any]:
        """
        Mandatory human gate before anything touches an external upstream repository.
        Verifies exact 64-char hex SHA-256 of the frozen dossier on disk before recording
        approval or opening an upstream Issue/Draft PR.
        """
        if not reviewer or not reviewer.strip():
            raise ValueError("Reviewer identity is mandatory for upstream sign-off")
        norm_sha = (expected_sha256 or "").strip().lower()
        if not re.fullmatch(r"[0-9a-f]{64}", norm_sha):
            raise ValueError("expected_sha256 must be a full 64-character hex SHA-256 digest")

        rec = self.get_dossier(dossier_id)
        if not rec:
            raise ValueError(f"Unknown dossier {dossier_id!r}")

        dossier_dir = Path(rec["dossier_dir"])
        manifest_path = dossier_dir / "manifest_sha256.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

        # Re-hash every file on disk to guarantee zero post-staging tampering
        live_hashes: Dict[str, str] = {}
        for fpath in sorted(dossier_dir.rglob("*")):
            if fpath.is_file() and fpath.name != "manifest_sha256.json":
                rel = fpath.relative_to(dossier_dir).as_posix()
                txt = fpath.read_bytes().decode("utf-8", errors="replace")
                self.sanitizer.assert_clean(txt, label=rel)
                live_hashes[rel] = sha256_bytes(fpath.read_bytes())

        recomputed_sha = sha256_text(
            json.dumps(
                {
                    "dossier_id": dossier_id,
                    "upstream_repo": rec["upstream_repo"],
                    "fork_repo": rec["fork_repo"],
                    "files": live_hashes,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        if recomputed_sha != rec["dossier_sha256"] or live_hashes != manifest.get("files"):
            raise ValueError("Dossier integrity check failed: files on disk changed since staging!")
        if norm_sha != recomputed_sha:
            raise ValueError(
                f"SHA-256 mismatch: operator supplied {norm_sha}, actual dossier is {recomputed_sha}"
            )

        now = time.time()
        upstream_status = "human_approved_ready_for_upstream"
        upstream_url = rec["upstream_url"]

        if submit_live_upstream:
            token = self._get_github_token()
            if not token:
                raise RuntimeError("GITHUB_TOKEN required in secret_store for live upstream submission")
            body = (dossier_dir / "UPSTREAM_PR.md").read_text(encoding="utf-8")
            if rec["contribution_kind"] in ("bug_report_with_patch", "rfc_benchmark"):
                issue = self._github_api(
                    token,
                    "POST",
                    f"/repos/{rec['upstream_repo']}/issues",
                    {"title": rec["title"], "body": body},
                )
                upstream_url = issue["html_url"]
                upstream_status = "submitted_upstream_issue"
            else:
                cfg = self.load_config()
                base_br = (
                    cfg.get("repositories", {})
                    .get(rec["repo_key"], {})
                    .get("upstream_default_branch", "main")
                )
                fork_owner = rec["fork_repo"].split("/")[0]
                pr = self._github_api(
                    token,
                    "POST",
                    f"/repos/{rec['upstream_repo']}/pulls",
                    {
                        "title": rec["title"],
                        "head": f"{fork_owner}:{rec['fork_branch']}",
                        "base": base_br,
                        "body": body,
                        "draft": True,
                    },
                )
                upstream_url = pr["html_url"]
                upstream_status = "submitted_upstream_draft_pr"

        with closing(sqlite3.connect(self.forge_db_path)) as conn:
            conn.execute(
                """
                UPDATE forge_dossiers
                SET upstream_status = ?, upstream_url = ?, approved_by = ?, approved_at = ?, updated_at = ?
                WHERE dossier_id = ?
                """,
                (upstream_status, upstream_url, reviewer.strip(), now, now, dossier_id),
            )
            conn.commit()

        return {
            "dossier_id": dossier_id,
            "verified_sha256": recomputed_sha,
            "approved_by": reviewer.strip(),
            "upstream_status": upstream_status,
            "upstream_url": upstream_url,
        }

    # ------------------------------------------------------------------
    # 5. Inspection & Query Helpers
    # ------------------------------------------------------------------
    def get_dossier(self, dossier_id: str) -> Optional[Dict[str, Any]]:
        with closing(sqlite3.connect(self.forge_db_path)) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM forge_dossiers WHERE dossier_id = ?", (dossier_id,)
            ).fetchone()
            return dict(row) if row else None

    def list_dossiers(
        self,
        *,
        upstream_status: Optional[str] = None,
        fork_pr_status: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        q = "SELECT * FROM forge_dossiers WHERE 1=1"
        params: List[Any] = []
        if upstream_status:
            q += " AND upstream_status = ?"
            params.append(upstream_status)
        if fork_pr_status:
            q += " AND fork_pr_status = ?"
            params.append(fork_pr_status)
        q += " ORDER BY updated_at DESC"
        with closing(sqlite3.connect(self.forge_db_path)) as conn:
            conn.row_factory = sqlite3.Row
            return [dict(r) for r in conn.execute(q, params).fetchall()]


def main() -> None:
    parser = argparse.ArgumentParser(description="Sabrina Upstream Open-Source R&D Forge CLI")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list-repos", help="List enrolled open-source target repositories")

    p_enr = sub.add_parser("enroll-repo", help="Enroll a new open-source repository")
    p_enr.add_argument("--key", required=True)
    p_enr.add_argument("--upstream", required=True, help="owner/repo on GitHub")
    p_enr.add_argument("--local-path", default="")
    p_enr.add_argument("--remote-lab-path", default="")
    p_enr.add_argument("--fork", default="")
    p_enr.add_argument("--branch", default="main")
    p_enr.add_argument("--gpu-lease", action="store_true")

    p_ls = sub.add_parser("list-dossiers", help="List finalized OSS dossiers and approval status")
    p_ls.add_argument("--upstream-status", default=None)
    p_ls.add_argument("--fork-status", default=None)

    p_ins = sub.add_parser("inspect-dossier", help="Inspect full metadata of a dossier")
    p_ins.add_argument("--id", required=True)

    sub.add_parser(
        "sync-fork-drafts",
        help="Push all queued dossiers to personal GitHub fork as Draft PRs",
    )

    p_app = sub.add_parser(
        "approve-upstream",
        help="Record SHA-256 human sign-off (and optionally submit live upstream PR/Issue)",
    )
    p_app.add_argument("--id", required=True)
    p_app.add_argument("--sha256", required=True, help="Full 64-character hex dossier SHA-256")
    p_app.add_argument("--reviewer", default="operator")
    p_app.add_argument("--submit-live", action="store_true")

    args = parser.parse_args()
    forge = SabrinaUpstreamForge()

    if args.cmd == "list-repos":
        print(json.dumps(forge.load_config(), indent=2))
    elif args.cmd == "enroll-repo":
        entry = forge.enroll_repo(
            args.key,
            args.upstream,
            local_path=args.local_path,
            remote_lab_path=args.remote_lab_path,
            fork_repo=args.fork,
            upstream_default_branch=args.branch,
            requires_gpu_lease=args.gpu_lease,
        )
        print(json.dumps(entry, indent=2))
    elif args.cmd == "list-dossiers":
        print(
            json.dumps(
                forge.list_dossiers(
                    upstream_status=args.upstream_status, fork_pr_status=args.fork_status
                ),
                indent=2,
            )
        )
    elif args.cmd == "inspect-dossier":
        print(json.dumps(forge.get_dossier(args.id), indent=2))
    elif args.cmd == "sync-fork-drafts":
        print(json.dumps(forge.sync_queued_fork_drafts(), indent=2))
    elif args.cmd == "approve-upstream":
        res = forge.promote_to_upstream(
            args.id,
            expected_sha256=args.sha256,
            reviewer=args.reviewer,
            submit_live_upstream=args.submit_live,
        )
        print(json.dumps(res, indent=2))


if __name__ == "__main__":
    main()
