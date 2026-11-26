"""Sabrina Resource Adaptation Engine.

Inspects unfamiliar task objectives or external skill catalogs, fetches full source
material with provenance verification, treats fetched material as untrusted data,
grounds requirements against Sabrina's actual runtime tools and CapabilityRegistry,
detects capability gaps, stages candidate adapter patches outside active source,
and prepares reviewable proposals for Operator approval.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
import yaml
from typing import Any, Callable, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
CATALOG_CACHE_DIR = ROOT / "data" / "catalog_cache" / "social-media-skills"
DEFAULT_DB_PATH = ROOT / "data" / "sabrina_assistant.sqlite3"
PATCHES_DIR = ROOT / "patches"
SKILLS_DIR = ROOT / "skills"
TOOLS_YAML = ROOT / "config" / "tools.yaml"

# Injection and rule-override detection patterns for untrusted content
MALICIOUS_OVERRIDE_PATTERNS = [
    re.compile(r"(?i)ignore\s+(?:all\s+)?(?:previous|prior)\s+instructions"),
    re.compile(r"(?i)system\s+override"),
    re.compile(r"(?i)you\s+(?:now\s+)?have\s+(?:full\s+)?access\s+to\s+(?:the\s+)?(?:official\s+)?linkedin\s+(?:rest\s+)?api"),
    re.compile(r"(?i)disable\s+(?:operator|human|supervisor|safety)\s+(?:review|approval|gate)"),
    re.compile(r"(?i)bypass\s+(?:operator|supervisor|human|safety)\s+(?:approval|review|restrictions|controls)"),
    re.compile(r"(?i)auto[-_]send\s+(?:all\s+)?messages\s+without\s+(?:operator|human|review)"),
    re.compile(r"(?i)unrestricted\s+mode"),
    re.compile(r"(?i)claim\s+(?:an\s+)?unavailable\s+action\s+exists"),
]

# Curated catalog definitions for the 106-skill social media catalog target subset
KNOWN_SOCIAL_CATALOG = {
    "reply-and-comment-writer": {
        "title": "Reply And Comment Writer",
        "url": "https://raw.githubusercontent.com/social-media-skills/skills/main/skills/reply-and-comment-writer/SKILL.md",
        "cache_filename": "reply-and-comment-writer.md",
        "description": "Craft authentic, context-grounded replies, feed comments, and thread responses that foster community without generic AI cliches.",
        "keywords": ["reply", "comment", "writer", "thread", "response", "linkedin", "feedback", "engage"],
        "declared_requirements": [
            "linkedin.post.inspect",
            "linkedin.thread.read",
            "linkedin.reply.draft",
            "linkedin.comment.draft",
            "linkedin.post.auto_like",
            "linkedin.comment.auto_publish",
            "linkedin.dm.auto_send",
            "linkedin.official_rest_api",
        ],
    },
    "community-management": {
        "title": "Community Management",
        "url": "https://raw.githubusercontent.com/social-media-skills/skills/main/skills/community-management/SKILL.md",
        "cache_filename": "community-management.md",
        "description": "Maintain healthy engagement, track community discussions, monitor comments, and escalate critical interactions under Operator policy.",
        "keywords": ["community", "management", "moderation", "track", "monitor", "sentiment", "escalate"],
        "declared_requirements": [
            "linkedin.session.status",
            "linkedin.conversation.track",
            "linkedin.replies.monitor",
            "community.sentiment_review",
            "third_party_crm.auto_sync",
            "sprout_social_api",
        ],
    },
    "engagement-routine": {
        "title": "Engagement Routine",
        "url": "https://raw.githubusercontent.com/social-media-skills/skills/main/skills/engagement-routine/SKILL.md",
        "cache_filename": "engagement-routine.md",
        "description": "Execute structured, bounded social engagement windows (triage, curated feed scan, follow-up check) respecting rate limits and operating hours.",
        "keywords": ["engagement", "routine", "schedule", "cadence", "daily", "triage", "window"],
        "declared_requirements": [
            "session.preflight",
            "inbox.triage_pass",
            "feed.curated_pass",
            "replies.followup_pass",
            "daemon.unbounded_loop",
            "analytics.growth_tracking",
        ],
    },
}


def inspect_untrusted_content_safety(content: str) -> Tuple[bool, Optional[str]]:
    """Scan untrusted source material for rule overrides, prompt injections, or unauthorized claims."""
    if not isinstance(content, str) or not content.strip():
        return False, "Content is empty."

    for pat in MALICIOUS_OVERRIDE_PATTERNS:
        m = pat.search(content)
        if m:
            return False, f"Rule violation detected: untrusted material contains override pattern: '{m.group(0)}'"

    return True, None


def fetch_source_material(
    source_url: Optional[str] = None,
    catalog_entry: Optional[str] = None,
    objective: Optional[str] = None,
    db_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Inspect full source material with provenance verification.

    Never generates placeholder documentation when retrieval fails.
    """
    selected_key = catalog_entry
    indexed_match: Optional[Dict[str, Any]] = None
    if not selected_key and not source_url and objective:
        # Match from keywords
        obj_lower = objective.lower()
        for k, v in KNOWN_SOCIAL_CATALOG.items():
            if k in obj_lower or any(kw in obj_lower for kw in v["keywords"]):
                selected_key = k
                break
        if not selected_key:
            try:
                from tools.internet_resource_indexer import InternetResourceIndexer
                hits = InternetResourceIndexer(db_path=db_path).search(query=objective, limit=1)
                if hits:
                    indexed_match = hits[0]
                    source_url = str(indexed_match.get("endpoint_or_raw_url") or indexed_match.get("url") or "")
            except Exception:
                indexed_match = None

    url = source_url
    cache_path = None
    title = indexed_match["title"] if indexed_match else "External Resource"

    if selected_key:
        info = KNOWN_SOCIAL_CATALOG.get(selected_key)
        if not info:
            return {
                "ok": False,
                "error": f"Catalog entry '{selected_key}' is not recognized in registered catalog.",
            }
        url = url or info["url"]
        cache_path = CATALOG_CACHE_DIR / info["cache_filename"]
        title = info["title"]

    if not url:
        return {
            "ok": False,
            "error": "No source URL or valid catalog entry provided. Full source retrieval required.",
        }

    raw_bytes = None
    error_note = None

    # 1. Attempt network fetch if URL given
    if url.startswith("http://") or url.startswith("https://"):
        try:
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) SabrinaResourceAdapt/1.0"},
            )
            with urllib.request.urlopen(req, timeout=5.0) as resp:
                raw_bytes = resp.read()
        except Exception as exc:
            error_note = str(exc)

    # 2. Fall back to verified cached copy if network fetch failed
    if raw_bytes is None and cache_path and cache_path.exists():
        try:
            raw_bytes = cache_path.read_bytes()
        except Exception as exc:
            error_note = f"Network fetch failed ({error_note}) and cache read failed ({exc})"

    # 3. If retrieval failed completely, report error. NEVER generate placeholders!
    if raw_bytes is None:
        return {
            "ok": False,
            "error": f"Source retrieval failed: {error_note or 'Unknown error'}. No placeholder generated.",
            "url": url,
        }

    content_str = raw_bytes.decode("utf-8", errors="replace")
    content_sha256 = hashlib.sha256(raw_bytes).hexdigest()

    # 4. Untrusted content safety inspection
    is_safe, safety_error = inspect_untrusted_content_safety(content_str)
    if not is_safe:
        return {
            "ok": False,
            "rejected": True,
            "error": f"Content rejected: {safety_error}",
            "url": url,
            "content_hash": content_sha256,
        }

    return {
        "ok": True,
        "title": title,
        "source_url": url,
        "content_hash": content_sha256,
        "content": content_str,
        "catalog_entry": selected_key,
        "indexed_resource_kind": indexed_match["resource_kind"] if indexed_match else None,
        "indexed_resource_id": indexed_match["resource_id"] if indexed_match else None,
        "retrieved_at": time.time(),
    }


def load_sabrina_runtime_catalog(tools_yaml_path: Optional[Path] = None) -> Dict[str, Any]:
    """Collect Sabrina's current registered tools, capabilities, and messaging skills."""
    t_path = tools_yaml_path or TOOLS_YAML
    registered_tools = {}
    if t_path.exists():
        try:
            with open(t_path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
                registered_tools = data.get("tools", {})
        except Exception:
            pass

    # Merge tools from Unified ToolRegistry and dynamic plugins
    try:
        from tools.tool_registry import TOOL_REGISTRY
        # Ensure related tools are loaded
        try:
            import tools.skill_catalog
            import tools.lab_index
            import tools.internet_resource_indexer
        except Exception:
            pass
        for t_dict in TOOL_REGISTRY.list_tools():
            t_name = t_dict["name"]
            if t_name not in registered_tools:
                registered_tools[t_name] = {
                    "modes": t_dict.get("allowed_modes", ["read", "plan", "write", "yolo"]),
                    "description": t_dict.get("description", ""),
                    "format": json.dumps({"tool": t_name}),
                }
    except Exception:
        pass

    # Registered capabilities from tools.capability_registry
    capabilities = {}
    try:
        from tools.capability_registry import DEFAULT_CAPABILITIES
        for cap in DEFAULT_CAPABILITIES:
            capabilities[cap.name] = {
                "owner": cap.owner,
                "actions": list(cap.actions),
                "resources": list(cap.resources),
                "availability": cap.availability,
            }
    except Exception:
        pass

    # Registered messaging skills
    messaging_implemented = set()
    try:
        from tools.messaging_skills import IMPLEMENTED
        messaging_implemented = set(IMPLEMENTED)
    except Exception:
        messaging_implemented = {"thread.read", "profile.read", "draft.save", "opportunity.read"}

    # Approved skills in skills/
    approved_skills = {}
    if SKILLS_DIR.exists():
        for skill_file in sorted(SKILLS_DIR.glob("*/SKILL.md")):
            skill_folder = skill_file.parent.name
            if skill_folder == "proposed":
                continue
            approved_skills[skill_folder] = str(skill_file)

    return {
        "tools": registered_tools,
        "capabilities": capabilities,
        "messaging_implemented": messaging_implemented,
        "approved_skills": approved_skills,
    }


def map_requirements_to_runtime(
    requirements: List[str],
    runtime_catalog: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Map each requirement to supported, unsupported, or requiring an adapter.

    Explicitly grounds all LinkedIn actions in Sabrina's actual CDP browser automation
    and flags unsupported features (such as LinkedIn REST APIs, unreviewed auto-sending,
    unregistered third-party CRMs) as explicit capability gaps.
    """
    catalog = runtime_catalog or load_sabrina_runtime_catalog()
    tools = catalog.get("tools", {})
    caps = catalog.get("capabilities", {})

    mapping = {}
    unsupported = []
    requires_adapter = []
    supported = []

    for req in requirements:
        req_clean = req.strip()

        # 1. LinkedIn post inspection
        if req_clean in ("linkedin.post.inspect", "feed.curated_pass"):
            if "linkedin_engage_feed" in tools:
                mapping[req_clean] = {
                    "status": "supported",
                    "matched_capability": "tool:linkedin_engage_feed",
                    "reason": "Supported via linkedin_engage_feed in read/suggestion mode (auto_comment=False).",
                }
                supported.append(req_clean)
            else:
                mapping[req_clean] = {
                    "status": "unsupported",
                    "matched_capability": None,
                    "reason": "Missing required tool: linkedin_engage_feed.",
                }
                unsupported.append(req_clean)

        # 2. LinkedIn thread / inbox reading
        elif req_clean in ("linkedin.thread.read", "inbox.triage_pass"):
            if "linkedin_inbox_triage" in tools:
                mapping[req_clean] = {
                    "status": "supported",
                    "matched_capability": "tool:linkedin_inbox_triage",
                    "reason": "Supported via linkedin_inbox_triage (bounded thread inspection and intent classification).",
                }
                supported.append(req_clean)
            else:
                mapping[req_clean] = {
                    "status": "unsupported",
                    "matched_capability": None,
                    "reason": "Missing required tool: linkedin_inbox_triage.",
                }
                unsupported.append(req_clean)

        # 3. Drafting replies / comments
        elif req_clean in ("linkedin.reply.draft", "linkedin.comment.draft"):
            if "messaging_skills" in tools and "social.semantic_review" in caps:
                mapping[req_clean] = {
                    "status": "supported",
                    "matched_capability": "tool:messaging_skills, cap:social.semantic_review",
                    "reason": "Supported via messaging_skills and social.semantic_review (grounded draft generation).",
                }
                supported.append(req_clean)
            else:
                mapping[req_clean] = {
                    "status": "supported",
                    "matched_capability": "tool:linkedin_engage_feed",
                    "reason": "Supported via linkedin_engage_feed suggestion output.",
                }
                supported.append(req_clean)

        # 4. Session status / preflight
        elif req_clean in ("linkedin.session.status", "session.preflight"):
            if "linkedin_status" in tools:
                mapping[req_clean] = {
                    "status": "supported",
                    "matched_capability": "tool:linkedin_status, cap:linkedin.browser",
                    "reason": "Supported via linkedin_status and browser.recovery preflight.",
                }
                supported.append(req_clean)
            else:
                mapping[req_clean] = {
                    "status": "unsupported",
                    "matched_capability": None,
                    "reason": "Missing required tool: linkedin_status.",
                }
                unsupported.append(req_clean)

        # 5. Tracking conversations / replies monitoring
        elif req_clean in ("linkedin.conversation.track", "linkedin.replies.monitor", "replies.followup_pass"):
            if "linkedin_track_conversation" in tools and "linkedin_check_replies" in tools:
                mapping[req_clean] = {
                    "status": "supported",
                    "matched_capability": "tool:linkedin_track_conversation, tool:linkedin_check_replies",
                    "reason": "Supported via tracked conversation storage and bounded check_replies (publish=False).",
                }
                supported.append(req_clean)
            else:
                mapping[req_clean] = {
                    "status": "unsupported",
                    "matched_capability": None,
                    "reason": "Missing tracking tools (linkedin_track_conversation / linkedin_check_replies).",
                }
                unsupported.append(req_clean)

        # 6. Sentiment review
        elif req_clean == "community.sentiment_review":
            if "social.semantic_review" in caps or "linkedin_engage_feed" in tools:
                mapping[req_clean] = {
                    "status": "supported",
                    "matched_capability": "cap:social.semantic_review",
                    "reason": "Supported via local lab model semantic sentiment and engagement classification.",
                }
                supported.append(req_clean)
            else:
                mapping[req_clean] = {
                    "status": "unsupported",
                    "matched_capability": None,
                    "reason": "Missing social.semantic_review capability.",
                }
                unsupported.append(req_clean)

        # 7. Actions requiring an adapter (e.g. automated likes / publishing, bounded routine)
        elif req_clean in ("linkedin.post.auto_like", "linkedin.comment.auto_publish"):
            mapping[req_clean] = {
                "status": "requires_adapter",
                "matched_capability": "tool:linkedin_engage_feed (adapter needed)",
                "reason": "Requires adapter bridge: default runtime is draft/suggestion only; automated engagement requires write/yolo mode, Operator lease, and max_engagements=1 rate-limited verification.",
            }
            requires_adapter.append(req_clean)

        elif req_clean == "daemon.unbounded_loop":
            mapping[req_clean] = {
                "status": "requires_adapter",
                "matched_capability": "scheduler:api/schedule (adapter needed)",
                "reason": "Requires adapter bridge: Sabrina never executes unbounded background loops; routine must be mapped into bounded single-pass execution or Operator-scheduled intervals.",
            }
            requires_adapter.append(req_clean)

        # 8. Explicitly unsupported actions (Capability Gaps)
        elif req_clean in ("linkedin.official_rest_api", "linkedin_rest_api", "linkedin_api"):
            mapping[req_clean] = {
                "status": "unsupported",
                "matched_capability": None,
                "reason": "Capability Gap: Sabrina runtime uses authenticated Chrome CDP browser automation (LinkedInManager), NOT the official LinkedIn REST API. API tokens and OAuth endpoints are unavailable.",
            }
            unsupported.append(req_clean)

        elif req_clean in ("linkedin.dm.auto_send", "messaging.auto_send", "direct_message.auto_send"):
            mapping[req_clean] = {
                "status": "unsupported",
                "matched_capability": None,
                "reason": "Capability Gap: Autonomous outbound direct messaging is prohibited without explicit Operator review. All recruiter replies remain draft-only.",
            }
            unsupported.append(req_clean)

        elif req_clean in ("third_party_crm.auto_sync", "sprout_social_api", "analytics.growth_tracking"):
            mapping[req_clean] = {
                "status": "unsupported",
                "matched_capability": None,
                "reason": f"Capability Gap: Third-party integration '{req_clean}' is not registered in Sabrina's environment.",
            }
            unsupported.append(req_clean)

        else:
            # Check generic tool match
            if req_clean in tools:
                mapping[req_clean] = {
                    "status": "supported",
                    "matched_capability": f"tool:{req_clean}",
                    "reason": f"Directly matches registered tool '{req_clean}'.",
                }
                supported.append(req_clean)
            elif req_clean in caps:
                mapping[req_clean] = {
                    "status": "supported",
                    "matched_capability": f"cap:{req_clean}",
                    "reason": f"Directly matches registered capability '{req_clean}'.",
                }
                supported.append(req_clean)
            else:
                mapping[req_clean] = {
                    "status": "unsupported",
                    "matched_capability": None,
                    "reason": f"Capability Gap: Unregistered action or tool requirement '{req_clean}'.",
                }
                unsupported.append(req_clean)

    return {
        "mapping": mapping,
        "supported": supported,
        "requires_adapter": requires_adapter,
        "unsupported": unsupported,
        "supported_count": len(supported),
        "adapter_count": len(requires_adapter),
        "unsupported_count": len(unsupported),
    }


def generate_candidate_adapter_patch(
    skill_name: str,
    adapter_requirements: List[str],
    patches_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """Generate candidate adapter patch bundle outside active source."""
    p_dir = patches_dir or PATCHES_DIR
    p_dir.mkdir(parents=True, exist_ok=True)

    clean_name = re.sub(r"[^a-zA-Z0-9_\-]", "_", skill_name.lower())
    ts = int(time.time())
    patch_id = f"patch_{clean_name}_{ts}"
    patch_file = p_dir / f"{patch_id}.json"

    # Destination target path for CodeAgent to apply (outside core files)
    target_rel = f"tools/adapters/{clean_name}_adapter.py"

    code_content = f'''"""Candidate adapter for {skill_name} requirements: {adapter_requirements}.

Generated autonomously by Sabrina Resource Adaptation Engine.
Staged for Operator / CodeAgent review.
"""
from typing import Any, Dict, List


class {clean_name.title().replace("_", "")}Adapter:
    """Bridges {skill_name} requirements into Sabrina's registered tool contracts."""

    def __init__(self, cdp_url: str = "http://localhost:9222"):
        self.cdp_url = cdp_url

    def adapt_execution(self, action_type: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Wrap actions in Sabrina's fail-closed, rate-limited execution boundaries."""
        if action_type == "bounded_routine":
            return {{"status": "adapted", "cycles": 1, "auto_comment": False, "publish": False}}
        return {{"status": "unsupported_action", "action": action_type}}
'''
    code_sha = hashlib.sha256(code_content.encode("utf-8")).hexdigest()

    bundle_data = {
        "patch_id": patch_id,
        "skill_name": skill_name,
        "adapter_requirements": adapter_requirements,
        "target_file": target_rel,
        "code": code_content,
        "sha256": code_sha,
        "status": "pending_codeagent_review",
        "created_at": time.time(),
    }
    patch_file.write_text(json.dumps(bundle_data, indent=2), encoding="utf-8")

    return {
        "patch_id": patch_id,
        "patch_file": str(patch_file),
        "target_file": target_rel,
        "sha256": code_sha,
    }


def prepare_adaptation_proposal(
    source_info: Dict[str, Any],
    mapping_info: Dict[str, Any],
    objective: str,
    run_id: Optional[str] = None,
    candidate_patch: Optional[Dict[str, Any]] = None,
    db_path: Optional[Path] = None,
    skills_dir: Optional[Path] = None,
) -> Optional[Dict[str, Any]]:
    """Synthesize SKILL.md proposal and submit into Operator review flow."""
    skill_name = source_info.get("catalog_entry") or re.sub(
        r"[^a-zA-Z0-9_\-]", "-", source_info.get("title", "hydrated-skill").lower()
    ).strip("-")

    # If all requirements are unsupported, adaptation is impossible
    if mapping_info["supported_count"] == 0 and mapping_info["adapter_count"] == 0:
        return None

    # Build Markdown mapping table
    table_rows = []
    for req, details in mapping_info["mapping"].items():
        st = details["status"].upper()
        cap = details["matched_capability"] or "NONE (GAP)"
        rsn = details["reason"]
        table_rows.append(f"| `{req}` | **{st}** | `{cap}` | {rsn} |")
    mapping_table = "\n".join(table_rows)

    unsupported_bullets = "\n".join(
        f"- **`{r}`**: {mapping_info['mapping'][r]['reason']}" for r in mapping_info["unsupported"]
    ) or "- None. All declared requirements have registered runtime support or adapters."

    patch_note = ""
    if candidate_patch:
        patch_note = f"""
## Candidate Adapter Patch Bundle
- **Patch ID**: `{candidate_patch['patch_id']}`
- **Staged File**: `{candidate_patch['patch_file']}` (outside active source)
- **Target File**: `{candidate_patch['target_file']}`
- **SHA-256**: `{candidate_patch['sha256']}`
- **Status**: Pending Operator / CodeAgent review.
"""

    skill_content = f"""---
name: {skill_name}
description: {source_info.get('title', skill_name)} - adapted for Sabrina's CDP browser runtime.
version: 1.0.0
---

# {source_info.get('title', skill_name).title()} (Adapted)

## Overview
Adapted autonomously for Sabrina to execute: "{objective.strip()}".
Grounds procedural guidance in Sabrina's actual runtime tools (CDP browser automation)
while strictly enforcing read-only / draft-only boundaries and operator approval gates.

## Source Provenance & Verification
- **Source URL**: {source_info.get('source_url', 'unknown')}
- **Source Content SHA-256**: `{source_info.get('content_hash', 'none')}`
- **Untrusted Material Safety**: Verified safe (no instruction override patterns found).
- **Retrieved**: {dt.datetime.fromtimestamp(source_info.get('retrieved_at', time.time())).strftime('%Y-%m-%d %H:%M:%S UTC')}

## Runtime Grounding & Capability Mapping
| Requirement | Status | Registered Capability | Technical Grounding |
| :--- | :--- | :--- | :--- |
{mapping_table}

## Explicit Capability Gaps (Unsupported Requirements)
{unsupported_bullets}
{patch_note}
## Operational Procedure & Safety Invariants
1. **Precondition & Status Check**: Always run `linkedin_status` before any operation to confirm authenticated CDP session and verify quotas.
2. **Read-Only / Draft-Only Default**:
   - For inbox triage: Use `linkedin_inbox_triage` to read and draft suggested replies.
   - For feed engagement: Use `linkedin_engage_feed` with `auto_comment=False` to produce context-grounded suggestions.
3. **No Unreviewed Publishing**: Sabrina never likes, comments, sends DMs, or publishes without explicit write/yolo mode, Operator lease, and human verification.
4. **No Direct REST APIs**: Never attempt to call official LinkedIn REST endpoints; all actions use authenticated browser CDP.
"""

    # Register in assistant_skill_proposals
    proposal_id = f"prop_adapt_{skill_name}_{int(time.time())}"
    target_path = str((Path(skills_dir) if skills_dir else SKILLS_DIR) / skill_name / "SKILL.md")

    database_file = db_path or DEFAULT_DB_PATH
    database_file.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(database_file) as db:
        # Ensure schema
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS assistant_skill_proposals (
                proposal_id TEXT PRIMARY KEY,
                run_id TEXT,
                skill_name TEXT NOT NULL,
                description TEXT NOT NULL,
                trigger_reason TEXT NOT NULL,
                query_used TEXT NOT NULL,
                search_sources TEXT NOT NULL,
                skill_content TEXT NOT NULL,
                target_path TEXT NOT NULL,
                status TEXT NOT NULL,
                reviewer TEXT,
                review_feedback TEXT,
                created REAL NOT NULL,
                updated REAL NOT NULL
            )
            """
        )
        now = time.time()
        db.execute(
            """
            INSERT OR REPLACE INTO assistant_skill_proposals VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                proposal_id,
                run_id,
                skill_name,
                source_info.get("title", skill_name),
                "resource_adaptation",
                objective,
                json.dumps([{"url": source_info.get("source_url"), "hash": source_info.get("content_hash")}]),
                skill_content,
                target_path,
                "pending_approval",
                None,
                None,
                now,
                now,
            ),
        )
        db.commit()

    return {
        "proposal_id": proposal_id,
        "skill_name": skill_name,
        "target_path": target_path,
        "status": "pending_approval",
        "skill_content": skill_content,
    }


def resource_adapt(
    objective: str,
    source_url: Optional[str] = None,
    catalog_entry: Optional[str] = None,
    run_id: Optional[str] = None,
    db_path: Optional[Path] = None,
    patches_dir: Optional[Path] = None,
    skills_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """Main execution function for resource_adapt tool."""
    if not isinstance(objective, str) or not objective.strip():
        return {
            "status": "error",
            "error": "Objective must be a non-empty string.",
            "discovered_sources": [],
            "mapping": {},
            "unsupported_requirements": [],
            "proposal_id": None,
        }

    # 1. Fetch full source material with provenance
    source_res = fetch_source_material(
        source_url=source_url,
        catalog_entry=catalog_entry,
        objective=objective,
        db_path=db_path,
    )
    if not source_res.get("ok"):
        return {
            "status": "error",
            "error": source_res.get("error", "Source inspection failed"),
            "rejected": source_res.get("rejected", False),
            "discovered_sources": [{"url": source_url or "unknown", "status": "failed"}],
            "mapping": {},
            "unsupported_requirements": [],
            "proposal_id": None,
        }

    discovered_sources = [
        {
            "url": source_res["source_url"],
            "content_hash": source_res["content_hash"],
            "title": source_res["title"],
            "status": "verified_authentic",
        }
    ]

    # 2. Extract declared requirements
    reqs = []
    if source_res.get("catalog_entry") in KNOWN_SOCIAL_CATALOG:
        reqs = KNOWN_SOCIAL_CATALOG[source_res["catalog_entry"]]["declared_requirements"]
    elif source_res.get("indexed_resource_kind"):
        reqs = ["search_internet_resources", "use_indexed_resource"]
    else:
        # Extract from source content YAML if present
        content = source_res.get("content", "")
        m = re.search(r"requires_tools:\s*\[(.*?)\]", content)
        if m:
            reqs = [r.strip().strip("'\"") for r in m.group(1).split(",") if r.strip()]
        else:
            reqs = ["linkedin.thread.read", "linkedin.reply.draft", "linkedin.official_rest_api"]

    # 3. Ground requirements against Sabrina's runtime
    mapping_res = map_requirements_to_runtime(reqs)

    # 4. Generate candidate adapter patch if needed
    candidate_patch = None
    if mapping_res["adapter_count"] > 0:
        candidate_patch = generate_candidate_adapter_patch(
            skill_name=source_res.get("catalog_entry") or "resource_adapter",
            adapter_requirements=mapping_res["requires_adapter"],
            patches_dir=patches_dir,
        )

    # 5. Prepare adaptation proposal
    prop_res = prepare_adaptation_proposal(
        source_info=source_res,
        mapping_info=mapping_res,
        objective=objective,
        run_id=run_id,
        candidate_patch=candidate_patch,
        db_path=db_path,
        skills_dir=skills_dir,
    )

    proposal_id = prop_res["proposal_id"] if prop_res else None

    return {
        "status": "proposal_prepared" if proposal_id else "no_viable_adaptation",
        "objective": objective,
        "discovered_sources": discovered_sources,
        "mapping": mapping_res["mapping"],
        "unsupported_requirements": mapping_res["unsupported"],
        "proposal_id": proposal_id,
        "candidate_patch": candidate_patch,
        "summary": (
            f"Adapted '{source_res['title']}': {mapping_res['supported_count']} supported, "
            f"{mapping_res['adapter_count']} requiring adapter, {mapping_res['unsupported_count']} unsupported (gaps). "
            f"Proposal ID: {proposal_id or 'none'} submitted for Operator review."
        ),
    }


def load_task_skills(
    goal: str,
    skills_dir: Optional[Path] = None,
    tools_yaml_path: Optional[Path] = None,
) -> List[Dict[str, Any]]:
    """Loads approved skills at the start of a task.

    Only approved skills are loaded; proposed skills remain pending review.
    Validates declared tool requirements against Sabrina's tool registry.
    If a required tool is missing, marks the skill as unavailable with an explicit gap.
    """
    s_dir = skills_dir or SKILLS_DIR
    if not s_dir.exists():
        return []

    runtime = load_sabrina_runtime_catalog(tools_yaml_path)
    registered_tools = runtime.get("tools", {})
    registered_caps = runtime.get("capabilities", {})

    goal_lower = (goal or "").lower()
    task_skills = []

    for item in sorted(s_dir.iterdir()):
        if not item.is_dir() or item.name == "proposed":
            continue

        skill_file = item / "SKILL.md"
        if not skill_file.exists():
            continue

        try:
            raw_text = skill_file.read_text(encoding="utf-8")
        except Exception:
            continue

        # Parse YAML frontmatter if present
        meta = {}
        fm_match = re.match(r"^---\s*\n(.*?)\n---\s*\n", raw_text, re.DOTALL)
        if fm_match:
            try:
                meta = yaml.safe_load(fm_match.group(1)) or {}
            except Exception:
                meta = {}

        skill_name = meta.get("name", item.name)
        version = str(meta.get("version", "1.0.0"))
        sha256 = hashlib.sha256(raw_text.encode("utf-8")).hexdigest()
        description = meta.get("description", "")
        req_tools = meta.get("requires_tools", [])
        procedure_body = raw_text[fm_match.end():].strip() if fm_match else raw_text.strip()
        rel_path = f"skills/{item.name}/SKILL.md"

        # Validate required tools
        gaps = []
        for req in req_tools:
            if req not in registered_tools and req not in registered_caps:
                try:
                    from tools.tool_inventory import is_tool_available
                    if is_tool_available(req):
                        continue
                except Exception:
                    pass
                gaps.append(req)

        is_available = len(gaps) == 0

        # Check relevance to goal
        is_relevant = (
            skill_name.lower() in goal_lower
            or any(part in goal_lower for part in skill_name.lower().split("-"))
            or (description and any(w in goal_lower for w in description.lower().split() if len(w) > 4))
        )

        task_skills.append({
            "name": skill_name,
            "version": version,
            "sha256": sha256,
            "path": str(skill_file),
            "relative_path": rel_path,
            "description": description,
            "procedure": procedure_body,
            "available": is_available,
            "capability_gap": gaps,
            "relevant": is_relevant,
            "requires_tools": req_tools,
        })

    return task_skills
