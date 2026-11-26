"""Sabrina dynamic tool plugin backed by the Upstream Open-Source Contributor skill.

Gives Sabrina direct, autonomous execution of:
  - Repository enrollment & isolated manifest workspace creation (`create_workspace`)
  - Lab-leased hardware verification on `ego` (`run_lab_verification`)
  - Dual-output finding finalization: Blackboard + FTS5 RAG + SKILL.md + SHA-256 dossier (`finalize_finding`)
  - Autonomous GitHub fork creation (REST API + CDP/Playwright browser automation + staging mirror fallback) & Draft PR staging (`sync_fork_drafts`, `stage_draft_pr`, `browser_fork_repo`)
  - Dossier inspection & SHA-256 human sign-off (`list_dossiers`, `inspect_dossier`, `approve_upstream`)
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

from tools.sabrina_upstream_forge import SabrinaUpstreamForge

TOOL_NAME = "sabrina_upstream_forge"
PARALLEL_SAFE = True
_SKILL = Path(__file__).resolve().parents[2] / "skills" / "upstream-oss-contributor" / "SKILL.md"
TOOL_PROMPT = (
    _SKILL.read_text(encoding="utf-8", errors="replace")
    if _SKILL.exists()
    else "Sabrina Upstream Open-Source Contributor & Autonomous GitHub Fork/Draft-PR Forge."
)


def execute_plugin(action: Dict[str, Any], loop: Any = None) -> str:
    forge = SabrinaUpstreamForge()
    op = str(
        action.get("operation")
        or action.get("action")
        or action.get("op")
        or action.get("command")
        or "list_dossiers"
    ).strip().lower()

    read_only_ops = {"list_repos", "list_dossiers", "inspect_dossier", "get_dossier"}
    if loop is not None and getattr(loop, "mode", "yolo") in {"plan", "read"} and op not in read_only_ops:
        return (
            f"sabrina_upstream_forge stopped: mode={getattr(loop, 'mode', 'read')!r} "
            f"allows only {sorted(read_only_ops)}."
        )

    try:
        if op == "list_repos":
            return json.dumps(forge.load_config(), indent=2)

        if op == "enroll_repo":
            res = forge.enroll_repo(
                repo_key=str(action["repo_key"]),
                upstream_repo=str(action["upstream_repo"]),
                local_path=str(action.get("local_path", "")),
                remote_lab_path=str(action.get("remote_lab_path", "")),
                fork_repo=str(action.get("fork_repo", "")),
                upstream_default_branch=str(action.get("upstream_default_branch", "main")),
                language_family=str(action.get("language_family", "cpp_cuda")),
                license_name=str(action.get("license_name", "Apache-2.0")),
                requires_gpu_lease=bool(action.get("requires_gpu_lease", False)),
                syntax_and_test_cmd=str(action.get("syntax_and_test_cmd", "")),
            )
            return json.dumps({"ok": True, "enrolled": res}, indent=2)

        if op in {"create_workspace", "create_isolated_workspace"}:
            res = forge.create_isolated_workspace(
                repo_key=str(action["repo_key"]),
                task_slug=str(action["task_slug"]),
                manifest_files=list(action.get("manifest_files") or []),
            )
            return json.dumps({"ok": True, **res}, indent=2)

        if op == "run_lab_verification":
            res = forge.run_lab_verification(
                repo_key=str(action["repo_key"]),
                remote_command=str(action["remote_command"]),
                wait_seconds=int(action.get("wait_seconds", 600) or 600),
                min_free_vram_mib=int(action.get("min_free_vram_mib", 0) or 0),
            )
            return json.dumps({"ok": res.get("returncode") == 0, **res}, indent=2)

        if op == "finalize_finding":
            res = forge.finalize_finding(
                slug=str(action["slug"]),
                repo_key=str(action["repo_key"]),
                contribution_kind=str(action.get("contribution_kind", "pr_patch")),
                title=str(action["title"]),
                problem_summary=str(action["problem_summary"]),
                root_cause_analysis=str(action["root_cause_analysis"]),
                remedy_summary=str(action["remedy_summary"]),
                verification_metrics=dict(action.get("verification_metrics") or {}),
                reproduction_steps=list(action.get("reproduction_steps") or []),
                worker_model=str(action["worker_model"]),
                worker_verbatim_summary=str(action["worker_verbatim_summary"]),
                auditor_model=str(action["auditor_model"]),
                auditor_verbatim_verdict=str(action["auditor_verbatim_verdict"]),
                minority_dissent=str(action.get("minority_dissent", "")),
                patch_files=dict(action.get("patch_files") or {}),
                extra_files=dict(action.get("extra_files") or {}),
                skill_name=action.get("skill_name"),
                auto_stage_fork_draft=bool(action.get("auto_stage_fork_draft", True)),
            )
            return json.dumps({"ok": True, **res}, indent=2)

        if op == "list_dossiers":
            status_filter = action.get("upstream_status")
            fork_filter = action.get("fork_pr_status")
            rows = forge.list_dossiers(upstream_status=status_filter, fork_pr_status=fork_filter)
            return json.dumps({"ok": True, "count": len(rows), "dossiers": rows}, indent=2)

        if op in {"inspect_dossier", "get_dossier"}:
            did = str(action.get("dossier_id") or action.get("id") or "")
            rec = forge.get_dossier(did)
            if not rec:
                return json.dumps({"ok": False, "error": f"Unknown dossier {did!r}"})
            return json.dumps({"ok": True, "dossier": rec}, indent=2)

        if op == "stage_draft_pr":
            did = str(action.get("dossier_id") or action.get("id") or "")
            res = forge.stage_personal_fork_draft_pr(did)
            return json.dumps({"ok": True, "dossier_id": did, **res}, indent=2)

        if op in {"sync_fork_drafts", "sync_queued_fork_drafts"}:
            res = forge.sync_queued_fork_drafts()
            return json.dumps({"ok": True, "synced": res}, indent=2)

        if op == "browser_fork_repo":
            upstream_repo = str(action.get("upstream_repo") or "")
            res = forge.browser_fork_github_repo(upstream_repo)
            return json.dumps(res, indent=2)

        if op in {"browser_create_repo", "create_github_repo", "publish_repo"}:
            repo_name = str(action.get("repo_name") or action.get("name") or "sabrina-mesh")
            description = str(action.get("description") or "")
            private = bool(action.get("private", False))
            local_repo_path = str(action.get("local_repo_path") or "")
            auto_push = bool(action.get("auto_push", True))
            res = forge.browser_create_github_repo(
                repo_name=repo_name,
                description=description,
                private=private,
                local_repo_path=local_repo_path,
                auto_push=auto_push,
            )
            return json.dumps(res, indent=2)

        if op in {"browser_generate_token", "generate_github_token"}:
            description = str(action.get("description") or "Sabrina-Autonomous-Collective")
            scopes = str(action.get("scopes") or "repo,workflow,read:org")
            wait_sec = int(action.get("wait_for_sudo_sec", 12) or 12)
            res = forge.browser_generate_github_token(
                description=description,
                scopes=scopes,
                wait_for_sudo_sec=wait_sec,
            )
            return json.dumps(res, indent=2)

        if op in {"approve_upstream", "promote_to_upstream"}:
            did = str(action.get("dossier_id") or action.get("id") or "")
            res = forge.promote_to_upstream(
                did,
                expected_sha256=str(action["expected_sha256"]),
                reviewer=str(action["reviewer"]),
                submit_live_upstream=bool(action.get("submit_live_upstream", False)),
            )
            return json.dumps({"ok": True, **res}, indent=2)

        return json.dumps({"ok": False, "error": f"Unsupported sabrina_upstream_forge operation: {op!r}"})
    except Exception as exc:
        return json.dumps({"ok": False, "operation": op, "error": str(exc)}, indent=2)


# Sabrina's dynamic-plugin contract expects this exact function name.
execute = execute_plugin
