"""Deterministic mission assembly from an objective and registered capabilities."""
from __future__ import annotations

from tools.capability_registry import CapabilityRegistry


def assemble_mission(objective: str, registry: CapabilityRegistry, scheduler) -> dict:
    text = (objective or "").lower()
    requested: list[str] = []
    if any(k in text for k in ("linkedin", "social", "engage", "post", "reply")):
        requested += ["social.semantic_review", "linkedin.browser", "linkedin.replies"]
    if any(k in text for k in ("job", "career", "application", "recruiter", "interview")):
        requested += ["jobs.research", "jobs.draft_application"]
    if any(k in text for k in ("browser", "recover", "chrome", "cdp")):
        requested.append("browser.recovery")
    if any(k in text for k in ("exploitgym", "cybergym", "arvo", "pwn", "exploit", "ctf", "challenge")):
        requested.append("exploitgym.rsi_cycle")
    if any(k in text for k in ("compress", "enwik8", "enwik9", "bpb", "arithmetic coder")):
        requested.append("compression.autonomous_experiment")
    if any(k in text for k in ("internet resource", "resource index", "public api", "sweep", "sandbox")):
        requested.append("resources.internet_index_sweep")
    if any(k in text for k in ("self-repair", "self repair", "fix bug", "fix the bug", "repair bug", "patch bug", "scan log", "stale model", "silent fallback")):
        requested.append("system.self_repair_workshop")
    if any(k in text for k in ("federat", "spread", "cluster", "across computer", "web server", "compute mesh", "multi-host", "windows workstation", "nanoquant", "littlebit", "bitnet", "recommend", "preference", "rpc-spec", "resolve-gguf", "quants")):
        requested.append("system.compute_federation")
    if any(k in text for k in ("github", "create repo", "publish repo", "push repo", "github token", "generate token", "personal access token", "upstream forge")) or ("sabrina-mesh" in text and any(k in text for k in ("github", "publish", "push", "commit", "repo"))):
        requested.append("github.autonomous_repo_forge")
    elif "sabrina-mesh" in text:
        requested.append("system.compute_federation")
    if any(k in text for k in ("daily paper", "paper forge", "paper-to-code", "paper to code", "arxiv tool", "paper_26", "daily_papers")):
        requested.append("forge.daily_paper_to_code")
    if any(k in text for k in ("cve", "ghsa", "variant", "patch parity", "bounty", "bugcrowd", "hackerone")):
        requested.append("bounty.cve_variant_hunter")
    requested = list(dict.fromkeys(requested))
    registered = {row["name"]: row for row in registry.discover()}
    available = [registered[name] for name in requested if name in registered]
    missing = [name for name in requested if name not in registered]
    running = scheduler.store.list(status="running")
    leased = {resource for job in running for resource in job.get("resources", [])}
    conflicts = [
        {"capability": row["name"], "resources": [r for r in row.get("resources", []) if r in leased],
         "reason": "resource leased by another running job"}
        for row in available if any(r in leased for r in row.get("resources", []))
    ]
    steps = []
    if missing:
        steps.append({"action": "request_capability_registration", "capabilities": missing})
    if conflicts:
        steps.append({"action": "wait_or_reschedule", "resources": sorted(leased)})
    if available and not conflicts:
        steps.append({"action": "dispatch_registered_capabilities", "capabilities": [x["name"] for x in available]})
    return {"objective": objective, "status": "needs_capability" if missing else ("blocked" if conflicts else "ready"),
            "registered": available, "missing": missing, "resource_conflicts": conflicts, "next_steps": steps}
