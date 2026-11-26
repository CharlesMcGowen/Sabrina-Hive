"""Deterministic swarm payload & 2.1.0-collective schema audit with .MD/.JSON support and optional Flash diagnosis."""

from __future__ import annotations

import json
import math
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable


@dataclass
class SchemaAuditResult:
    valid: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    counts: dict[str, Any] = field(default_factory=dict)
    invariants: list[dict[str, Any]] = field(default_factory=list)
    flash_diagnosis: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def infer_provider_from_model(model_str: str, explicit_provider: str | None = None) -> str:
    """Infer provider identifier from model string, matching UI and role-binding conventions."""
    if explicit_provider and str(explicit_provider).strip():
        return str(explicit_provider).strip().lower()
    raw = str(model_str or "").strip().lower()
    if not raw:
        return "gemini"
    for prefix, prov in (
        ("openrouter/", "openrouter"),
        ("ollama/", "ollama"),
        ("local:", "ollama"),
        ("openai/", "openai"),
        ("anthropic/", "anthropic"),
        ("xai/", "xai"),
        ("deepseek/", "deepseek"),
        ("groq/", "groq"),
        ("mistral/", "mistral"),
        ("together/", "together"),
    ):
        if raw.startswith(prefix):
            return prov
    if raw.startswith(("gpt-", "o1", "o3", "o4")):
        return "openai"
    if raw.startswith("claude"):
        return "anthropic"
    if raw.startswith("grok"):
        return "xai"
    if raw.startswith("deepseek"):
        return "deepseek"
    if ":" in raw and "/" not in raw:
        return "ollama"
    return "gemini"


def is_provider_ready(provider: str, unlocked_providers: Iterable[str] | None = None) -> bool:
    """Return True if a provider is locally runnable or unlocked in the API key vault."""
    p = str(provider or "").strip().lower()
    if not p or p in ("ollama", "local"):
        return True
    unlocked = {str(x or "").strip().lower() for x in (unlocked_providers or ("ollama",))}
    if p in unlocked:
        return True
    if p.startswith("openrouter:"):
        sub = p.split(":", 1)[1]
        return "openrouter" in unlocked or sub in unlocked
    if p in ("google", "gemma"):
        return "gemini" in unlocked or "ollama" in unlocked
    return False


def _parse_scalar(val: str) -> Any:
    raw = val.strip()
    if not raw:
        return ""
    if (raw.startswith('"') and raw.endswith('"')) or (raw.startswith("'") and raw.endswith("'")):
        inner = raw[1:-1]
        return inner.replace('\\"', '"').replace("\\'", "'")
    low = raw.lower()
    if low == "true":
        return True
    if low == "false":
        return False
    if low in ("null", "none", "~"):
        return None
    if re.fullmatch(r"-?\d+", raw):
        try:
            return int(raw)
        except ValueError:
            pass
    if re.fullmatch(r"-?\d+\.\d+", raw):
        try:
            return float(raw)
        except ValueError:
            pass
    return raw


def _split_md_table_row(line: str) -> list[str]:
    """Split a Markdown pipe-table row into unescaped cell strings."""
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|") and not s.endswith(r"\|"):
        s = s[:-1]
    cells: list[str] = []
    buf: list[str] = []
    escaped = False
    for ch in s:
        if escaped:
            buf.append(ch)
            escaped = False
        elif ch == "\\":
            escaped = True
        elif ch == "|":
            cells.append("".join(buf).strip())
            buf = []
        else:
            buf.append(ch)
    if escaped:
        buf.append("\\")
    cells.append("".join(buf).strip())
    return cells


def _is_md_separator_row(cells: list[str]) -> bool:
    return bool(cells) and all(re.fullmatch(r":?-{2,}:?", c.replace(" ", "")) is not None for c in cells)


def _parse_md_table(section_lines: list[str]) -> list[dict[str, str]]:
    table_lines = [ln.strip() for ln in section_lines if "|" in ln and ln.strip().startswith("|")]
    if len(table_lines) < 2:
        # Also allow tables without leading pipe if at least 2 pipe lines exist
        table_lines = [ln.strip() for ln in section_lines if ln.count("|") >= 2]
    if len(table_lines) < 2:
        return []
    headers = [re.sub(r"[\s\-]+", "_", h.strip().lower()) for h in _split_md_table_row(table_lines[0])]
    rows: list[dict[str, str]] = []
    for raw_row in table_lines[1:]:
        cells = _split_md_table_row(raw_row)
        if _is_md_separator_row(cells):
            continue
        if len(cells) < len(headers):
            cells.extend([""] * (len(headers) - len(cells)))
        row = {headers[i]: cells[i] for i in range(len(headers)) if headers[i]}
        if any(v for v in row.values()):
            rows.append(row)
    return rows


def _parse_md_rule_bullets(section_lines: list[str], default_gate: float) -> list[dict[str, Any]]:
    rules: list[dict[str, Any]] = []
    idx = 0
    for raw_line in section_lines:
        ln = raw_line.strip()
        if not ln or not (ln.startswith("- ") or ln.startswith("* ")):
            continue
        body = ln[2:].strip()
        arrow_match = re.search(
            r"(?:(?:\*\*|`)?([A-Za-z0-9_.-]+)(?:\*\*|`)?\s*:\s*)?"
            r"`?([A-Za-z0-9_.-]+)`?\s*(?:->|➔|→)\s*`?([A-Za-z0-9_.-]+)`?",
            body,
        )
        if not arrow_match:
            continue
        idx += 1
        rule_id = (arrow_match.group(1) or f"R{idx}").strip()
        parent_role = arrow_match.group(2).strip()
        child_role = arrow_match.group(3).strip()
        lp_match = re.search(r"(?:min_logprob|logprob|lp)\s*(?:>=|≥|:|=)\s*(-?\d+(?:\.\d+)?)", body, re.I)
        min_lp = float(lp_match.group(1)) if lp_match else default_gate
        trig_match = re.search(r"(?:trigger|when)\s*[:=]\s*([^|)\n]+)", body, re.I)
        trans_match = re.search(r"(?:transition)\s*[:=]\s*([^|)\n]+)", body, re.I)
        rules.append(
            {
                "rule_id": rule_id,
                "parent_role": parent_role,
                "child_role": child_role,
                "min_logprob": min_lp,
                "trigger": trig_match.group(1).strip() if trig_match else "signal_ready",
                "transition": trans_match.group(1).strip() if trans_match else "SPAWNING -> ACTIVE -> VALIDATING",
            }
        )
    return rules


def parse_swarm_markdown(text: str) -> dict[str, Any]:
    """Parse a Markdown (.md) swarm definition (YAML frontmatter + ## Agents + ## Spawning Rules + optional ## Prompts)."""
    normalized = str(text or "").lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    frontmatter: dict[str, Any] = {}
    body = normalized

    fm_match = re.match(r"^---\s*\n(.*?)\n---\s*(?:\n|$)", normalized, re.S)
    if fm_match:
        fm_raw = fm_match.group(1)
        body = normalized[fm_match.end() :]
        current_nested: str | None = None
        for line in fm_raw.split("\n"):
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            indent = len(line) - len(line.lstrip(" "))
            if ":" not in line:
                continue
            k_raw, v_raw = line.strip().split(":", 1)
            key = k_raw.strip()
            val_str = v_raw.strip()
            if indent >= 2 and current_nested:
                nested = frontmatter.setdefault(current_nested, {})
                if isinstance(nested, dict):
                    nested[key] = _parse_scalar(val_str)
            else:
                if val_str == "":
                    current_nested = key
                    frontmatter.setdefault(key, {})
                else:
                    current_nested = None
                    frontmatter[key] = _parse_scalar(val_str)

    # Extract optional top-level H1 title and level-2 sections
    h1_title = ""
    sections: dict[str, list[str]] = {}
    current_sec: str | None = None
    for line in body.split("\n"):
        h2 = re.match(r"^##\s+(.+?)\s*$", line)
        if h2:
            current_sec = h2.group(1).strip().lower()
            sections[current_sec] = []
            continue
        if current_sec is None:
            h1 = re.match(r"^#\s+(.+?)\s*$", line)
            if h1 and not h1_title:
                h1_title = h1.group(1).strip()
        else:
            sections[current_sec].append(line)

    topo_raw = frontmatter.get("topology") if isinstance(frontmatter.get("topology"), dict) else {}
    topology: dict[str, Any] = dict(topo_raw)
    for tkey in (
        "spawn_mode",
        "topology_mode",
        "coordinator_count",
        "workers_per_coordinator",
        "quorum_split_threshold",
        "enable_work_stealing",
        "task_complexity",
        "max_spawn_depth",
        "max_concurrency",
        "max_total_agents",
        "confidence_gate_threshold",
        "confidence_gate_probability",
        "blackboard_bus",
    ):
        if tkey in frontmatter and tkey not in topology:
            topology[tkey] = frontmatter[tkey]
    if "max_total_agents" not in topology and "max_agents" in frontmatter:
        topology["max_total_agents"] = frontmatter["max_agents"]

    for opt_int_key in ("max_spawn_depth", "max_concurrency", "max_total_agents"):
        if opt_int_key in topology and _is_unbounded_token(topology[opt_int_key]):
            topology[opt_int_key] = None
    topology.setdefault("spawn_mode", "algorithmic")
    topology.setdefault("topology_mode", "elastic_stigmergy")
    for ratio_key, ratio_default in (("coordinator_count", 1), ("workers_per_coordinator", 4), ("quorum_split_threshold", 6)):
        try:
            topology[ratio_key] = max(1, int(topology.get(ratio_key, ratio_default)))
        except (TypeError, ValueError):
            topology[ratio_key] = ratio_default
    topology["enable_work_stealing"] = bool(topology.get("enable_work_stealing", True))
    if "task_complexity" in topology:
        try:
            topology["task_complexity"] = float(topology["task_complexity"])
        except (TypeError, ValueError):
            topology["task_complexity"] = 3.6
    topology.setdefault("max_spawn_depth", None)
    topology.setdefault("max_concurrency", None)
    topology.setdefault("max_total_agents", None)
    conf_gate = topology.get("confidence_gate_threshold", -0.35)
    try:
        conf_gate_f = float(conf_gate)
    except (TypeError, ValueError):
        conf_gate_f = -0.35
    if "confidence_gate_probability" not in topology:
        topology["confidence_gate_probability"] = round(max(0.0, min(1.0, math.exp(min(0.0, conf_gate_f)))), 3)
    topology.setdefault("confidence_gate_threshold", conf_gate)
    if "blackboard_bus" not in topology and "blackboard" in frontmatter:
        topology["blackboard_bus"] = frontmatter["blackboard"]
    topology.setdefault("blackboard_bus", "dnd_rsi_blackboard.sqlite3")

    # Parse ## Agents
    agents_lines: list[str] = []
    for k, lines in sections.items():
        if "agent" in k or "worker" in k or "node" in k:
            agents_lines = lines
            break
    agents: list[dict[str, Any]] = []
    for idx, row in enumerate(_parse_md_table(agents_lines)):
        aid = (row.get("id") or row.get("agent_id") or row.get("agent") or row.get("name") or f"agent-{idx + 1}").strip()
        role = (row.get("role") or row.get("type") or "execution_specialist").strip()
        model = (row.get("model") or row.get("primary_model") or row.get("model_id") or "gemini-4-argon").strip()
        provider = infer_provider_from_model(model, row.get("provider"))
        mc_raw = row.get("max_children") if "max_children" in row else row.get("max_ch")
        if _is_unbounded_token(mc_raw):
            max_children: int | None = None
        else:
            try:
                max_children = int(float(str(mc_raw)))
            except (TypeError, ValueError):
                max_children = None
        en_raw = (row.get("enabled") or row.get("on") or "true").strip().lower()
        enabled = en_raw not in ("false", "0", "no", "off")
        status = (row.get("status") or row.get("state") or "ACTIVE").strip().upper()
        task = (row.get("task") or row.get("description") or row.get("goal") or role).strip()
        agent_obj: dict[str, Any] = {
            "id": aid,
            "role": role,
            "model": model,
            "provider": provider,
            "status": status,
            "enabled": enabled,
            "max_children": max_children,
            "task": task,
        }
        parent_node = (row.get("parent_node") or row.get("parent") or "").strip()
        if parent_node:
            agent_obj["parent_node"] = parent_node
        agents.append(agent_obj)

    # Parse ## Spawning Rules
    rules_lines: list[str] = []
    for k, lines in sections.items():
        if "rule" in k or "spawn" in k:
            rules_lines = lines
            break
    rules: list[dict[str, Any]] = []
    rule_rows = _parse_md_table(rules_lines)
    if rule_rows:
        for idx, row in enumerate(rule_rows):
            rule_id = (row.get("rule_id") or row.get("id") or f"R{idx + 1}").strip()
            parent_role = (row.get("parent_role") or row.get("parent") or row.get("from") or "").strip()
            child_role = (row.get("child_role") or row.get("child") or row.get("to") or "").strip()
            lp_raw = row.get("min_logprob") or row.get("logprob") or row.get("gate") or str(conf_gate_f)
            lp_clean = re.sub(r"^[≥>= ]+", "", str(lp_raw).strip())
            try:
                min_lp: Any = float(lp_clean)
            except (TypeError, ValueError):
                min_lp = lp_raw
            trigger = (row.get("trigger") or row.get("when") or row.get("condition") or "signal_ready").strip()
            transition = (row.get("transition") or row.get("lifecycle") or "SPAWNING -> ACTIVE -> VALIDATING").strip()
            if parent_role or child_role:
                rules.append(
                    {
                        "rule_id": rule_id,
                        "parent_role": parent_role,
                        "child_role": child_role,
                        "trigger": trigger,
                        "transition": transition,
                        "min_logprob": min_lp,
                    }
                )
    else:
        rules = _parse_md_rule_bullets(rules_lines, conf_gate_f)

    # Parse optional ## Prompts
    prompts_lines: list[str] = []
    for k, lines in sections.items():
        if "prompt" in k:
            prompts_lines = lines
            break
    extra_prompts: dict[str, str] = {}
    if prompts_lines:
        cur_key: str | None = None
        buf: list[str] = []

        def _flush_prompt() -> None:
            if not cur_key:
                return
            text_val = "\n".join(buf).strip()
            if not text_val:
                return
            matched = [a for a in agents if a["id"] == cur_key]
            if matched:
                matched[0]["prompt"] = text_val
            else:
                extra_prompts[cur_key] = text_val

        for ln in prompts_lines:
            h3 = re.match(r"^###\s+(.+?)\s*$", ln)
            if h3:
                _flush_prompt()
                cur_key = h3.group(1).strip()
                buf = []
            elif cur_key is not None:
                buf.append(ln)
        _flush_prompt()

    schema: dict[str, Any] = {
        "schema_version": str(frontmatter.get("schema_version") or "2.1.0-collective"),
        "swarm_name": str(frontmatter.get("swarm_name") or frontmatter.get("title") or h1_title or "Imported Markdown Collective"),
        "execution_authorized": bool(frontmatter.get("execution_authorized", False)),
        "topology": topology,
        "deterministic_spawning_rules": rules,
        "agents": agents,
    }
    if extra_prompts:
        schema["prompts"] = extra_prompts
    if "objectives" in sections:
        obj_rows = _parse_md_table(sections["objectives"])
        if obj_rows:
            schema["objectives"] = [
                {
                    "id": r.get("id") or f"obj-{i + 1}",
                    "state": (r.get("state") or r.get("status") or "ACTIVE").upper(),
                    "goal": r.get("goal") or r.get("description") or "",
                }
                for i, r in enumerate(obj_rows)
            ]
    return schema


def _is_unbounded_token(val: Any) -> bool:
    if val is None:
        return True
    if isinstance(val, str):
        return val.strip().lower() in ("", "none", "null", "auto", "unlimited", "unbounded", "inf", "infinity", "∞")
    return False


def _parse_optional_positive_int(val: Any) -> tuple[int | None, bool]:
    """Return (parsed_int_or_None_for_unlimited, is_valid)."""
    if _is_unbounded_token(val):
        return None, True
    if isinstance(val, bool):
        return None, False
    try:
        if isinstance(val, float) and not val.is_integer():
            return None, False
        num = int(val)
        return (num, num >= 1)
    except (TypeError, ValueError):
        return None, False


def _escape_md_cell(val: Any) -> str:
    return str(val if val is not None else "").replace("\n", " ").replace("|", r"\|").strip()


def serialize_swarm_markdown(schema: dict[str, Any]) -> str:
    """Serialize a 2.1.0-collective schema dict into Markdown (YAML frontmatter + ## Agents + ## Spawning Rules + optional ## Prompts)."""
    topo = schema.get("topology") if isinstance(schema.get("topology"), dict) else {}
    schema_version = str(schema.get("schema_version") or "2.1.0-collective")
    swarm_name = str(schema.get("swarm_name") or schema.get("title") or "Sabrina Collective")
    spawn_mode = str(topo.get("spawn_mode") or schema.get("spawn_mode") or "algorithmic")
    task_complexity = topo.get("task_complexity", schema.get("task_complexity"))
    raw_depth = topo.get("max_spawn_depth", schema.get("max_spawn_depth"))
    raw_conc = topo.get("max_concurrency", schema.get("max_concurrency"))
    raw_max_agents = topo.get("max_total_agents", schema.get("max_total_agents"))
    conf_gate = topo.get("confidence_gate_threshold", schema.get("confidence_gate_threshold", -0.35))
    try:
        conf_prob = topo.get("confidence_gate_probability", round(math.exp(min(0.0, float(conf_gate))), 3))
    except (TypeError, ValueError):
        conf_prob = 0.705
    blackboard_bus = str(topo.get("blackboard_bus") or "dnd_rsi_blackboard.sqlite3")

    raw_agents = schema.get("agents") or schema.get("workers") or schema.get("nodes") or []
    agents_list = (
        raw_agents
        if isinstance(raw_agents, list)
        else [{"id": k, **(v if isinstance(v, dict) else {"role": str(v)})} for k, v in (raw_agents.items() if isinstance(raw_agents, dict) else [])]
    )

    lines = [
        "---",
        f'schema_version: "{schema_version}"',
        f'swarm_name: "{swarm_name.replace(chr(34), chr(92) + chr(34))}"',
        "execution_authorized: false",
        f'spawn_mode: "{spawn_mode}"',
    ]
    if task_complexity is not None:
        lines.append(f"task_complexity: {task_complexity}")
    lines.extend(
        [
            f'topology_mode: "{str(topo.get("topology_mode") or "elastic_stigmergy")}"',
            f"coordinator_count: {int(topo.get('coordinator_count') or 1)}",
            f"workers_per_coordinator: {int(topo.get('workers_per_coordinator') or 4)}",
            f"quorum_split_threshold: {int(topo.get('quorum_split_threshold') or 6)}",
            f"enable_work_stealing: {'true' if topo.get('enable_work_stealing', True) else 'false'}",
            f"max_spawn_depth: {raw_depth if not _is_unbounded_token(raw_depth) else 'unlimited'}",
            f"max_concurrency: {raw_conc if not _is_unbounded_token(raw_conc) else 'unlimited'}",
            f"max_total_agents: {raw_max_agents if not _is_unbounded_token(raw_max_agents) else 'unlimited'}",
            f"confidence_gate_threshold: {conf_gate}",
            f"confidence_gate_probability: {conf_prob}",
            f'blackboard_bus: "{blackboard_bus}"',
            "---",
            "",
            f"# {swarm_name}",
            "",
            "## Agents",
            "",
            "| id | role | model | provider | max_children | enabled | status | task |",
            "| --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
    )
    prompt_entries: list[tuple[str, str]] = []
    seen_prompt_keys: set[str] = set()

    for idx, a in enumerate(agents_list):
        if not isinstance(a, dict):
            continue
        aid = str(a.get("id") or a.get("name") or f"agent-{idx + 1}")
        role = str(a.get("role") or "execution_specialist")
        model = str(a.get("model") or a.get("primary_model") or "gemini-4-argon")
        provider = infer_provider_from_model(model, a.get("provider"))
        raw_ch = a.get("max_children")
        max_ch = "unlimited" if _is_unbounded_token(raw_ch) else raw_ch
        enabled = "false" if a.get("enabled") is False else "true"
        status = str(a.get("status") or a.get("state") or "ACTIVE").upper()
        task = str(a.get("task") or a.get("detail") or a.get("description") or role)
        lines.append(
            f"| {_escape_md_cell(aid)} | {_escape_md_cell(role)} | {_escape_md_cell(model)} | "
            f"{_escape_md_cell(provider)} | {_escape_md_cell(max_ch)} | {enabled} | "
            f"{_escape_md_cell(status)} | {_escape_md_cell(task)} |"
        )
        prompt_text = str(a.get("prompt") or "").strip()
        if prompt_text:
            prompt_entries.append((aid, prompt_text))
            seen_prompt_keys.add(aid)

    lines.extend(
        [
            "",
            "## Spawning Rules",
            "",
            "| rule_id | parent_role | child_role | min_logprob | trigger | transition |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
    )
    rules = (
        schema.get("deterministic_spawning_rules")
        if isinstance(schema.get("deterministic_spawning_rules"), list)
        else (
            schema.get("spawning_rules")
            if isinstance(schema.get("spawning_rules"), list)
            else (schema.get("rules") if isinstance(schema.get("rules"), list) else [])
        )
    )
    for idx, r in enumerate(rules):
        if not isinstance(r, dict):
            continue
        rid = str(r.get("rule_id") or r.get("id") or f"R{idx + 1}")
        p_role = str(r.get("parent_role") or "")
        c_role = str(r.get("child_role") or "")
        min_lp = r.get("min_logprob", conf_gate)
        trigger = str(r.get("trigger") or "signal_ready")
        transition = str(r.get("transition") or "SPAWNING -> ACTIVE -> VALIDATING")
        lines.append(
            f"| {_escape_md_cell(rid)} | {_escape_md_cell(p_role)} | {_escape_md_cell(c_role)} | "
            f"{_escape_md_cell(min_lp)} | {_escape_md_cell(trigger)} | {_escape_md_cell(transition)} |"
        )

    extra_prompts = schema.get("prompts")
    if isinstance(extra_prompts, dict):
        for k, v in extra_prompts.items():
            key_s = str(k).strip()
            val_s = str(v or "").strip()
            if key_s and val_s and key_s not in seen_prompt_keys:
                prompt_entries.append((key_s, val_s))
                seen_prompt_keys.add(key_s)

    if prompt_entries:
        lines.extend(["", "## Prompts", ""])
        for key_s, val_s in prompt_entries:
            lines.append(f"### {key_s}")
            lines.append(val_s)
            lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def parse_swarm_input(raw_text: str, filename: str = "") -> tuple[dict[str, Any] | None, str, str | None]:
    """Auto-detect and parse either JSON or Markdown swarm schema text."""
    text = str(raw_text or "").lstrip("\ufeff").strip()
    if not text:
        return None, "empty", "Empty schema input"

    fname_low = str(filename or "").strip().lower()
    looks_md = (
        fname_low.endswith((".md", ".markdown"))
        or text.startswith("---")
        or bool(re.search(r"^##\s+(?:Agents|Spawning Rules|Prompts)\b", text, re.I | re.M))
    )

    if not looks_md:
        try:
            parsed = json.loads(text)
            if not isinstance(parsed, dict):
                return None, "json", f"Top-level JSON must be an object, got {type(parsed).__name__}"
            return parsed, "json", None
        except Exception as json_exc:
            # Fallback to Markdown if frontmatter/tables are present
            if "|" in text and ("#" in text or "---" in text):
                return parse_swarm_markdown(text), "markdown", None
            return None, "json", f"JSON load error: {json_exc}"

    try:
        parsed_md = parse_swarm_markdown(text)
        return parsed_md, "markdown", None
    except Exception as md_exc:
        return None, "markdown", f"Markdown parse error: {md_exc}"


def _is_collective_schema(data: dict[str, Any]) -> bool:
    return any(
        k in data
        for k in ("schema_version", "deterministic_spawning_rules", "spawning_rules", "rules", "topology")
    )


def _normalize_agents_for_invariants(data: dict[str, Any]) -> list[dict[str, Any]]:
    raw_agents = data.get("agents") or data.get("workers") or data.get("nodes") or []
    raw_list = (
        raw_agents
        if isinstance(raw_agents, list)
        else [
            {"id": k, **(v if isinstance(v, dict) else {"role": str(v)})}
            for k, v in (raw_agents.items() if isinstance(raw_agents, dict) else [])
        ]
    )
    out: list[dict[str, Any]] = []
    for idx, item in enumerate(raw_list):
        if not isinstance(item, dict):
            continue
        aid = str(item.get("id") or item.get("name") or f"agent-{idx + 1}").strip()
        role = str(item.get("role") or item.get("type") or "execution_specialist").strip()
        model = str(item.get("model") or item.get("primary_model") or "gemini-4-argon").strip()
        provider = infer_provider_from_model(model, item.get("provider"))
        raw_mc = item.get("max_children")
        if _is_unbounded_token(raw_mc):
            max_ch: int | None = None
        else:
            try:
                max_ch = int(raw_mc)
            except (TypeError, ValueError):
                max_ch = None
        out.append(
            {
                "id": aid,
                "role": role,
                "model": model,
                "provider": provider,
                "enabled": item.get("enabled", True) is not False,
                "max_children": max_ch,
            }
        )
    return out


def _detect_role_cycles(adj: dict[str, list[str]]) -> list[list[str]]:
    color: dict[str, int] = {}  # 0=unvisited, 1=visiting, 2=done
    stack: list[str] = []
    cycles: list[list[str]] = []
    seen_sigs: set[str] = set()

    def dfs(node: str) -> None:
        color[node] = 1
        stack.append(node)
        for nxt in adj.get(node, []):
            st = color.get(nxt, 0)
            if st == 1:
                idx = stack.index(nxt)
                cyc = stack[idx:] + [nxt]
                sig = "->".join(cyc)
                if sig not in seen_sigs:
                    seen_sigs.add(sig)
                    cycles.append(cyc)
            elif st == 0:
                dfs(nxt)
        stack.pop()
        color[node] = 2

    for n in sorted(adj.keys()):
        if color.get(n, 0) == 0:
            dfs(n)
    return cycles


def evaluate_collective_invariants(
    data: dict[str, Any],
    *,
    unlocked_providers: Iterable[str] | None = None,
) -> tuple[list[dict[str, Any]], list[str], list[str], dict[str, Any]]:
    """Run deterministic INV-01..INV-05 stigmergic & structural invariant checks on a collective schema."""
    inv_errors: list[str] = []
    inv_warnings: list[str] = []
    invariants: list[dict[str, Any]] = []

    topo = data.get("topology") if isinstance(data.get("topology"), dict) else {}
    agents = _normalize_agents_for_invariants(data)
    enabled_agents = [a for a in agents if a["enabled"]]
    disabled_roles = {a["role"] for a in agents if not a["enabled"]}
    enabled_roles = {a["role"] for a in enabled_agents}
    enabled_ids = {a["id"] for a in enabled_agents}
    all_role_or_ids = enabled_roles | enabled_ids

    raw_rules = (
        data.get("deterministic_spawning_rules")
        if isinstance(data.get("deterministic_spawning_rules"), list)
        else (
            data.get("spawning_rules")
            if isinstance(data.get("spawning_rules"), list)
            else (data.get("rules") if isinstance(data.get("rules"), list) else [])
        )
    )
    rules = [r for r in raw_rules if isinstance(r, dict)]

    # --- INV-01: Root Reachability ---
    roots = [a for a in enabled_agents if a["role"] == "coordinator" or a["id"] == "sabrina"]
    inv1_details: list[str] = []
    inv1_status = "PASS"
    orphan_rules: list[str] = []
    disabled_target_rules: list[str] = []

    role_adj: dict[str, list[str]] = {}
    for idx, r in enumerate(rules):
        rid = str(r.get("rule_id") or r.get("id") or f"R{idx + 1}").strip()
        p_role = str(r.get("parent_role") or "").strip()
        c_role = str(r.get("child_role") or "").strip()
        missing_refs: list[str] = []
        disabled_refs: list[str] = []
        for ref in (p_role, c_role):
            if not ref:
                missing_refs.append("<empty>")
            elif ref not in all_role_or_ids:
                if ref in disabled_roles:
                    disabled_refs.append(ref)
                else:
                    missing_refs.append(ref)
        if missing_refs:
            orphan_rules.append(f"{rid} ({', '.join(missing_refs)})")
        elif disabled_refs:
            disabled_target_rules.append(f"{rid} ({', '.join(disabled_refs)} disabled)")
        if p_role and c_role:
            role_adj.setdefault(p_role, [])
            if c_role not in role_adj[p_role]:
                role_adj[p_role].append(c_role)
            role_adj.setdefault(c_role, [])

    if not roots:
        inv1_status = "FAIL"
        msg = "INV-01: Missing root coordinator ('coordinator' role or 'sabrina' id)"
        inv_errors.append(msg)
        inv1_details.append(msg)

    if orphan_rules:
        inv1_status = "FAIL"
        msg = f"INV-01: Orphan rule(s) reference undefined role(s): {', '.join(orphan_rules)}"
        inv_errors.append(msg)
        inv1_details.append(msg)

    # BFS reachability from root roles/ids
    reachable: set[str] = set()
    queue: list[str] = []
    for rt in roots:
        for token in (rt["role"], rt["id"]):
            if token and token not in reachable:
                reachable.add(token)
                queue.append(token)
    while queue:
        cur = queue.pop(0)
        for nxt in role_adj.get(cur, []):
            if nxt not in reachable:
                reachable.add(nxt)
                queue.append(nxt)

    unreachable_agents = [
        f"{a['id']} ({a['role']})"
        for a in enabled_agents
        if a["role"] not in reachable and a["id"] not in reachable
    ]
    if unreachable_agents:
        if inv1_status == "PASS":
            inv1_status = "WARN"
        msg = f"INV-01: Unreachable agent role(s) from coordinator: {', '.join(unreachable_agents)}"
        inv_warnings.append(msg)
        inv1_details.append(msg)

    if disabled_target_rules:
        if inv1_status == "PASS":
            inv1_status = "WARN"
        msg = f"INV-01: Rule(s) target disabled agent role: {', '.join(disabled_target_rules)}"
        inv_warnings.append(msg)
        inv1_details.append(msg)

    inv1_summary = (
        f"root={roots[0]['id']} · {len(enabled_agents) - len(unreachable_agents)}/{len(enabled_agents)} reachable · {len(orphan_rules)} orphan"
        if roots
        else f"NO ROOT · {len(orphan_rules)} orphan rules"
    )
    invariants.append(
        {
            "id": "INV-01",
            "name": "Root Reachability",
            "status": inv1_status,
            "summary": inv1_summary,
            "details": inv1_details,
        }
    )

    # --- INV-02: Bounded Termination ---
    inv2_details: list[str] = []
    inv2_status = "PASS"
    explicit_spawn_mode = str(topo.get("spawn_mode") or data.get("spawn_mode") or "").strip().lower()
    raw_depth = topo.get("max_spawn_depth", data.get("max_spawn_depth"))
    max_depth, depth_valid = _parse_optional_positive_int(raw_depth)

    if not depth_valid:
        inv2_status = "FAIL"
        msg = f"INV-02: Invalid max_spawn_depth ({raw_depth!r}); must be integer >= 1 or null/unlimited"
        inv_errors.append(msg)
        inv2_details.append(msg)

    cycles = _detect_role_cycles(role_adj)
    if cycles:
        inv2_status = "FAIL"
        for cyc in cycles:
            msg = f"INV-02: Role cycle detected ({' -> '.join(cyc)})"
            inv_errors.append(msg)
            inv2_details.append(msg)

    # Longest acyclic path depth from root roles
    longest_depth: dict[str, int] = {}
    for rt in roots:
        longest_depth[rt["role"]] = 0
        longest_depth[rt["id"]] = 0
    if not cycles and rules:
        for _ in range(len(role_adj) + 2):
            changed = False
            for p_role, children in role_adj.items():
                if p_role not in longest_depth:
                    continue
                cand = longest_depth[p_role] + 1
                for c_role in children:
                    if cand > longest_depth.get(c_role, -1) and cand <= len(role_adj) + 2:
                        longest_depth[c_role] = cand
                        changed = True
            if not changed:
                break
    observed_max_depth = max(longest_depth.values()) if longest_depth else 0
    if depth_valid and max_depth is not None and observed_max_depth > max_depth:
        if inv2_status == "PASS":
            inv2_status = "WARN"
        msg = f"INV-02: DAG depth ({observed_max_depth}) exceeds max_spawn_depth ({max_depth}); deeper roles will be depth-capped"
        inv_warnings.append(msg)
        inv2_details.append(msg)

    has_spindown = any("SPINDOWN" in str(r.get("transition") or "").upper() for r in rules)
    algorithmic_auto_spindown = (explicit_spawn_mode == "algorithmic") or (not rules and max_depth is None)
    if not has_spindown and not algorithmic_auto_spindown:
        if inv2_status == "PASS":
            inv2_status = "WARN"
        msg = "INV-02: No '-> SPINDOWN' termination transition present in spawning rules"
        inv_warnings.append(msg)
        inv2_details.append(msg)

    depth_cap_lbl = str(max_depth) if max_depth is not None else "∞ (entropy-convergent)"
    spindown_lbl = "SPINDOWN bound" if has_spindown else ("SPINDOWN auto" if algorithmic_auto_spindown else "no SPINDOWN")
    inv2_summary = (
        f"acyclic · depth {observed_max_depth}/{depth_cap_lbl} · {spindown_lbl}"
        if not cycles
        else f"{len(cycles)} CYCLE(S) · depth cap {depth_cap_lbl}"
    )
    invariants.append(
        {
            "id": "INV-02",
            "name": "Bounded Termination",
            "status": inv2_status,
            "summary": inv2_summary,
            "details": inv2_details,
            "cycles": [" -> ".join(c) for c in cycles],
            "has_spindown": has_spindown or algorithmic_auto_spindown,
            "observed_depth": observed_max_depth,
        }
    )

    # --- INV-03: Concurrency, Agent Limits & Fan-Out ---
    inv3_details: list[str] = []
    inv3_status = "PASS"
    raw_conc = topo.get("max_concurrency", data.get("max_concurrency"))
    max_conc, conc_valid = _parse_optional_positive_int(raw_conc)

    if not conc_valid:
        inv3_status = "FAIL"
        msg = f"INV-03: Invalid max_concurrency ({raw_conc!r}); must be integer >= 1 or null/unlimited"
        inv_errors.append(msg)
        inv3_details.append(msg)

    raw_max_agents = topo.get("max_total_agents", data.get("max_total_agents", topo.get("max_agents", data.get("max_agents"))))
    max_total_agents, agents_cap_valid = _parse_optional_positive_int(raw_max_agents)

    if not agents_cap_valid:
        inv3_status = "FAIL"
        msg = f"INV-03: Invalid max_total_agents ({raw_max_agents!r}); must be integer >= 1 or null/unlimited"
        inv_errors.append(msg)
        inv3_details.append(msg)

    aggregate_max_children = sum(max(0, a["max_children"]) for a in enabled_agents if a["max_children"] is not None)
    unbounded_child_agents = sum(1 for a in enabled_agents if a["max_children"] is None)
    # Check per-agent child fan-out truncation (only for agents with an explicit max_children cap)
    truncated_parents: list[str] = []
    for a in enabled_agents:
        if a["max_children"] is None or a["max_children"] < 0:
            continue
        child_targets: set[str] = set()
        for r in rules:
            p_role = str(r.get("parent_role") or "").strip()
            c_role = str(r.get("child_role") or "").strip()
            if p_role in (a["role"], a["id"]):
                for cand in enabled_agents:
                    if cand["id"] != a["id"] and c_role in (cand["role"], cand["id"]):
                        child_targets.add(cand["id"])
        if len(child_targets) > a["max_children"]:
            truncated_parents.append(f"{a['id']} ({len(child_targets)}>{a['max_children']})")

    if agents_cap_valid and max_total_agents is not None and len(enabled_agents) > max_total_agents:
        if inv3_status == "PASS":
            inv3_status = "WARN"
        msg = f"INV-03: Enabled agents ({len(enabled_agents)}) exceed max_total_agents ({max_total_agents}); excess agents capped"
        inv_warnings.append(msg)
        inv3_details.append(msg)
    if conc_valid and max_conc is not None and aggregate_max_children > max_conc:
        if inv3_status == "PASS":
            inv3_status = "WARN"
        msg = f"INV-03: Aggregate max_children fan-out ({aggregate_max_children}) exceeds max_concurrency ({max_conc}); wave throttling active"
        inv_warnings.append(msg)
        inv3_details.append(msg)
    if conc_valid and max_conc is not None and len(enabled_agents) > max_conc:
        if inv3_status == "PASS":
            inv3_status = "WARN"
        msg = f"INV-03: Enabled agents ({len(enabled_agents)}) exceed max_concurrency ({max_conc}); concurrent waves will queue"
        inv_warnings.append(msg)
        inv3_details.append(msg)
    if truncated_parents:
        if inv3_status == "PASS":
            inv3_status = "WARN"
        msg = f"INV-03: Parent max_children cap truncates fan-out on: {', '.join(truncated_parents)}"
        inv_warnings.append(msg)
        inv3_details.append(msg)

    raw_coord_cnt = topo.get("coordinator_count", data.get("coordinator_count", 1))
    raw_wpc = topo.get("workers_per_coordinator", data.get("workers_per_coordinator", 4))
    coord_cnt, coord_valid = _parse_optional_positive_int(raw_coord_cnt)
    wpc_cnt, wpc_valid = _parse_optional_positive_int(raw_wpc)
    if not coord_valid or coord_cnt is None:
        inv3_status = "FAIL"
        msg = f"INV-03: Invalid coordinator_count ({raw_coord_cnt!r}); must be integer >= 1"
        inv_errors.append(msg)
        inv3_details.append(msg)
        coord_cnt = 1
    if not wpc_valid or wpc_cnt is None:
        inv3_status = "FAIL"
        msg = f"INV-03: Invalid workers_per_coordinator ({raw_wpc!r}); must be integer >= 1"
        inv_errors.append(msg)
        inv3_details.append(msg)
        wpc_cnt = 4

    target_ratio_pool = coord_cnt * wpc_cnt
    if conc_valid and max_conc is not None and target_ratio_pool > max_conc:
        if inv3_status == "PASS":
            inv3_status = "WARN"
        msg = f"INV-03: Ratio pool {coord_cnt}:{wpc_cnt} ({target_ratio_pool} workers) exceeds max_concurrency ({max_conc}); wave throttling active"
        inv_warnings.append(msg)
        inv3_details.append(msg)
    if agents_cap_valid and max_total_agents is not None and target_ratio_pool > max_total_agents:
        if inv3_status == "PASS":
            inv3_status = "WARN"
        msg = f"INV-03: Ratio pool {coord_cnt}:{wpc_cnt} ({target_ratio_pool} workers) exceeds max_total_agents ({max_total_agents}); budget capped"
        inv_warnings.append(msg)
        inv3_details.append(msg)
    elif (
        agents_cap_valid
        and max_total_agents is not None
        and target_ratio_pool == max_total_agents
        and str(topo.get("topology_mode") or "elastic_stigmergy") == "elastic_stigmergy"
    ):
        if inv3_status == "PASS":
            inv3_status = "WARN"
        msg = f"INV-03: Ratio pool {coord_cnt}:{wpc_cnt} leaves 0 headroom under max_total_agents ({max_total_agents}); ELASTIC_BUD capped"
        inv_warnings.append(msg)
        inv3_details.append(msg)

    agents_lbl = f"agents≤{max_total_agents}" if max_total_agents is not None else "agents=∞"
    depth_lbl = f"depth≤{max_depth}" if max_depth is not None else "depth=∞"
    conc_lbl = f"conc={max_conc}" if max_conc is not None else "conc=∞"
    ratio_lbl = f"ratio={coord_cnt}:{wpc_cnt}"
    fan_lbl = f"Σmax_ch={aggregate_max_children}" + (f"+{unbounded_child_agents}∞" if unbounded_child_agents else "")
    inv3_summary = f"{ratio_lbl} · {agents_lbl} · {depth_lbl} · {conc_lbl} · {fan_lbl}"
    invariants.append(
        {
            "id": "INV-03",
            "name": "Concurrency vs. Fan-Out",
            "status": inv3_status,
            "summary": inv3_summary,
            "details": inv3_details,
            "coordinator_count": coord_cnt,
            "workers_per_coordinator": wpc_cnt,
            "max_spawn_depth": max_depth,
            "max_concurrency": max_conc,
            "max_total_agents": max_total_agents,
            "aggregate_max_children": aggregate_max_children,
            "unbounded_child_agents": unbounded_child_agents,
            "truncated_parents": truncated_parents,
        }
    )

    # --- INV-04: Confidence Thresholds ---
    inv4_details: list[str] = []
    inv4_status = "PASS"
    raw_gate = topo.get("confidence_gate_threshold", data.get("confidence_gate_threshold", -0.35))
    gate_valid = True
    try:
        global_gate = float(raw_gate)
        if not math.isfinite(global_gate) or global_gate > 0.0:
            gate_valid = False
    except (TypeError, ValueError):
        global_gate = -0.35
        gate_valid = False

    if not gate_valid:
        inv4_status = "FAIL"
        msg = f"INV-04: Invalid topology.confidence_gate_threshold ({raw_gate!r}); must be finite logprob <= 0"
        inv_errors.append(msg)
        inv4_details.append(msg)

    stricter_rules: list[dict[str, Any]] = []
    invalid_rule_gates: list[str] = []
    for idx, r in enumerate(rules):
        rid = str(r.get("rule_id") or r.get("id") or f"R{idx + 1}").strip()
        raw_rlp = r.get("min_logprob", global_gate)
        try:
            rlp = float(raw_rlp)
            if not math.isfinite(rlp) or rlp > 0.0:
                invalid_rule_gates.append(f"{rid} ({raw_rlp!r})")
                continue
        except (TypeError, ValueError):
            invalid_rule_gates.append(f"{rid} ({raw_rlp!r})")
            continue
        if gate_valid and rlp > global_gate + 1e-9:
            stricter_rules.append(
                {
                    "rule_id": rid,
                    "min_logprob": rlp,
                    "probability_pct": round(math.exp(rlp) * 100.0, 1),
                }
            )

    if invalid_rule_gates:
        inv4_status = "FAIL"
        msg = f"INV-04: Invalid rule min_logprob (> 0 or non-numeric): {', '.join(invalid_rule_gates)}"
        inv_errors.append(msg)
        inv4_details.append(msg)

    if stricter_rules:
        desc = ", ".join(f"{s['rule_id']} (≥{s['min_logprob']:.2f})" for s in stricter_rules)
        inv4_details.append(f"Stricter than global gate ({global_gate:.2f}): {desc}")

    gate_pct = round(math.exp(min(0.0, global_gate)) * 100.0, 1) if gate_valid else 0.0
    inv4_summary = (
        f"gate ≥ {global_gate:.2f} ({gate_pct}%) · {len(stricter_rules)} stricter rule(s)"
        if gate_valid
        else "INVALID LOGPROB GATE"
    )
    invariants.append(
        {
            "id": "INV-04",
            "name": "Confidence Thresholds",
            "status": inv4_status,
            "summary": inv4_summary,
            "details": inv4_details,
            "global_gate": global_gate,
            "stricter_rules": stricter_rules,
        }
    )

    # --- INV-05: Blackboard & Vault Readiness ---
    inv5_details: list[str] = []
    inv5_status = "PASS"
    blackboard_bus = str(topo.get("blackboard_bus") or data.get("blackboard_bus") or "").strip()
    if not blackboard_bus:
        inv5_status = "WARN"
        msg = "INV-05: Missing topology.blackboard_bus configuration"
        inv_warnings.append(msg)
        inv5_details.append(msg)

    ready_agents: list[str] = []
    missing_key_agents: list[str] = []
    missing_providers: set[str] = set()
    for a in enabled_agents:
        if is_provider_ready(a["provider"], unlocked_providers):
            ready_agents.append(a["id"])
        else:
            missing_key_agents.append(f"{a['id']} ({a['provider']})")
            missing_providers.add(a["provider"])

    if missing_key_agents:
        if inv5_status == "PASS":
            inv5_status = "WARN"
        msg = f"INV-05: Vault KEY_MISSING for {len(missing_key_agents)}/{len(enabled_agents)} agent(s): {', '.join(missing_key_agents)}"
        inv_warnings.append(msg)
        inv5_details.append(msg)

    inv5_summary = (
        f"bus={blackboard_bus or 'UNSET'} · vault {len(ready_agents)}/{len(enabled_agents)} READY"
        + (f" ({len(missing_key_agents)} KEY_MISSING)" if missing_key_agents else "")
    )
    invariants.append(
        {
            "id": "INV-05",
            "name": "Blackboard & Vault Readiness",
            "status": inv5_status,
            "summary": inv5_summary,
            "details": inv5_details,
            "blackboard_bus": blackboard_bus,
            "ready_count": len(ready_agents),
            "missing_key_count": len(missing_key_agents),
            "missing_providers": sorted(missing_providers),
        }
    )

    counts = {
        "agents": len(agents),
        "enabled_agents": len(enabled_agents),
        "rules": len(rules),
        "max_spawn_depth": max_depth,
        "max_concurrency": max_conc,
        "max_total_agents": max_total_agents,
        "aggregate_max_children": aggregate_max_children,
        "stricter_rules": len(stricter_rules),
        "ready_agents": len(ready_agents),
        "missing_key_agents": len(missing_key_agents),
    }
    return invariants, inv_errors, inv_warnings, counts


def validate_swarm_payload(
    data: Any,
    *,
    unlocked_providers: Iterable[str] | None = None,
) -> tuple[list[str], list[str], dict[str, Any]]:
    """Run microsecond deterministic schema checks on a swarm payload with zero LLM calls."""
    errors: list[str] = []
    warnings: list[str] = []
    counts: dict[str, Any] = {"agents": 0, "tasks": 0, "messages": 0}

    if not isinstance(data, dict):
        return [f"Top-level payload must be dict, got {type(data).__name__}"], warnings, counts

    agents_key = next((k for k in ("agents", "workers", "nodes") if k in data), None)
    tasks_key = next((k for k in ("tasks", "active_tasks", "jobs", "objectives") if k in data), None)
    msgs_key = next((k for k in ("messages", "board", "events", "activity_feed") if k in data), None)

    if not agents_key:
        errors.append("Missing agents/workers section")
    else:
        agents = data[agents_key]
        if isinstance(agents, list):
            counts["agents"] = len(agents)
            for idx, item in enumerate(agents[:20]):
                if not isinstance(item, dict) or not item.get("id"):
                    errors.append(f"{agents_key}[{idx}] missing string 'id'")
                elif "status" not in item and "state" not in item:
                    warnings.append(f"{agents_key}[{idx}] ({item.get('id')}) missing status/state")
        elif isinstance(agents, dict):
            counts["agents"] = len(agents)
        else:
            errors.append(f"{agents_key} must be list or dict, got {type(agents).__name__}")

    if not tasks_key:
        if not _is_collective_schema(data):
            warnings.append("No tasks/jobs section found")
    else:
        tasks = data[tasks_key]
        if isinstance(tasks, list):
            counts["tasks"] = len(tasks)
        elif isinstance(tasks, dict):
            counts["tasks"] = len(tasks)
        else:
            errors.append(f"{tasks_key} must be list or dict, got {type(tasks).__name__}")

    if msgs_key and isinstance(data[msgs_key], list):
        counts["messages"] = len(data[msgs_key])

    if _is_collective_schema(data):
        _, inv_errors, inv_warnings, inv_counts = evaluate_collective_invariants(
            data,
            unlocked_providers=unlocked_providers,
        )
        errors.extend(inv_errors)
        warnings.extend(inv_warnings)
        counts.update(inv_counts)

    return errors, warnings, counts


def build_compact_sample(data: dict[str, Any], max_items: int = 2) -> dict[str, Any]:
    """Return a compact structural sample of a swarm payload suitable for gated Flash audits."""
    sample: dict[str, Any] = {}
    for key, val in data.items():
        if isinstance(val, list):
            sample[key] = val[:max_items]
        elif isinstance(val, dict):
            keys = list(val.keys())[:max_items]
            sample[key] = {k: val[k] for k in keys}
        else:
            sample[key] = val
    return sample


def audit_swarm_payload(
    data: Any,
    flash_auditor: Callable[[str], str] | None = None,
    *,
    force_flash: bool = False,
    unlocked_providers: Iterable[str] | None = None,
) -> SchemaAuditResult:
    """Validate an in-memory swarm payload and optionally invoke Flash on drift or manual audit."""
    errors, warnings, counts = validate_swarm_payload(data, unlocked_providers=unlocked_providers)
    invariants: list[dict[str, Any]] = []
    if isinstance(data, dict) and _is_collective_schema(data):
        invariants, _, _, _ = evaluate_collective_invariants(data, unlocked_providers=unlocked_providers)

    result = SchemaAuditResult(
        valid=len(errors) == 0,
        errors=errors,
        warnings=warnings,
        counts=counts,
        invariants=invariants,
    )

    if (force_flash or errors or warnings) and flash_auditor is not None and isinstance(data, dict):
        sample_json = json.dumps(build_compact_sample(data), indent=2, default=str)[:2500]
        prompt = (
            "Check this swarm JSON sample against the reported issues.\n"
            f"Errors: {errors}\n"
            f"Warnings: {warnings}\n"
            f"Sample:\n```json\n{sample_json}\n```\n"
            "Return only concrete field mismatches and target normalized key names."
        )
        result.flash_diagnosis = flash_auditor(prompt)

    return result


def audit_swarm_file(
    path: Path,
    flash_auditor: Callable[[str], str] | None = None,
    *,
    force_flash: bool = False,
    unlocked_providers: Iterable[str] | None = None,
) -> SchemaAuditResult:
    """Load and audit a swarm .json or .md file on disk using deterministic checks + optional Flash."""
    try:
        file_path = Path(path)
        raw = file_path.read_text(encoding="utf-8")
        data, _, err = parse_swarm_input(raw, filename=file_path.name)
        if err or data is None:
            return SchemaAuditResult(valid=False, errors=[err or "Schema parse error"])
    except Exception as exc:
        return SchemaAuditResult(valid=False, errors=[f"JSON load error: {exc}"])
    return audit_swarm_payload(
        data,
        flash_auditor=flash_auditor,
        force_flash=force_flash,
        unlocked_providers=unlocked_providers,
    )


def make_gemini_flash_auditor(
    *,
    api_key: str | None = None,
    model: str = "gemini-3.5-flash-lite",
) -> Callable[[str], str]:
    """Build a decoupled Gemini Flash auditor callable using the Interactions API."""

    def _audit(prompt: str) -> str:
        resolved_key = (
            (api_key or "").strip()
            or os.environ.get("GEMINI_API_KEY", "").strip()
            or os.environ.get("GOOGLE_API_KEY", "").strip()
        )
        if not resolved_key:
            return "Flash audit skipped: no Gemini API key configured in vault or environment."
        try:
            from google import genai  # type: ignore[import-not-found]
        except ImportError:
            return "Flash audit skipped: install `google-genai` to enable optional Flash schema diagnosis."
        try:
            http_opts: Any = None
            try:
                from google.genai import types as genai_types  # type: ignore[import-not-found]

                retry_cls = getattr(genai_types, "HttpRetryOptions", None)
                opts_cls = getattr(genai_types, "HttpOptions", None)
                if opts_cls is not None:
                    if retry_cls is not None:
                        http_opts = opts_cls(timeout=10_000, retry_options=retry_cls(attempts=1))
                    else:
                        http_opts = opts_cls(timeout=10_000)
            except Exception:
                pass

            client = (
                genai.Client(api_key=resolved_key, http_options=http_opts)
                if http_opts
                else genai.Client(api_key=resolved_key)
            )
            interaction = client.interactions.create(
                model=model,
                input=prompt,
            )
            text = getattr(interaction, "output_text", None)
            if text:
                return str(text).strip()
            outputs = getattr(interaction, "outputs", None) or []
            for item in reversed(outputs):
                item_text = getattr(item, "text", None)
                if item_text:
                    return str(item_text).strip()
            return "Flash returned an empty diagnosis."
        except Exception as exc:
            err_str = str(exc)
            if "429" in err_str or "RESOURCE_EXHAUSTED" in err_str:
                return "Flash audit skipped: Gemini API rate limit or quota exceeded (429 Too Many Requests). Deterministic schema audit complete."
            return f"Flash audit error: {exc}"

    return _audit
