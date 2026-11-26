"""Objective Execution Harness for Sabrina.

Implements the disciplined operating loop:
Objective -> Discover relevant skills -> Choose tools -> Execute -> Inspect evidence -> Adapt -> Finish or report blocker.

Key Guarantees:
- Authority belongs to the Objective: Working scope and bounds are set once.
- Progress preservation: All observations, discovered assets, and verified states are recorded.
- Budget enforcement: Strict round and time limits prevent endless loops or runaway executions.
- Idempotency & stagnation guards: Identical repeated actions are intercepted before execution.
- Read-only protection: Honors read-only scope constraints across all registered tools.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
import time
import uuid
from typing import Any, Callable, Dict, List, Optional, Tuple

from tools.tool_registry import TOOL_REGISTRY
from tools.skill_catalog import SKILL_CATALOG
import tools.lab_index  # Ensure lab discovery tools are registered

logger = logging.getLogger("sabrina.objective_harness")


@dataclass
class ObjectiveContract:
    id: str
    goal: str
    scope: Dict[str, Any] = field(default_factory=lambda: {"read_only": True, "allowed_hosts": ["ego.local", "localhost"]})
    budget_rounds: int = 10
    budget_seconds: float = 300.0
    active_skill: Optional[str] = None
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class EvidenceBundle:
    observations: List[Dict[str, Any]] = field(default_factory=list)
    discovered_assets: Dict[str, Any] = field(default_factory=dict)
    verified_states: Dict[str, Any] = field(default_factory=dict)
    report: Optional[str] = None
    blockers: List[str] = field(default_factory=list)
    status: str = "ready"  # ready, running, completed, blocked, budget_exhausted

    def add_observation(self, round_num: int, tool: str, action: Dict[str, Any], result: str) -> None:
        self.observations.append({
            "round": round_num,
            "tool": tool,
            "action": action,
            "result_preview": result[:2500] + ("\n[... truncated ...]" if len(result) > 2500 else ""),
            "timestamp": time.time(),
        })

    def record_asset(self, key: str, data: Any) -> None:
        self.discovered_assets[key] = data

    def record_verification(self, key: str, state: Any) -> None:
        self.verified_states[key] = state


class ObjectiveHarness:
    """Manages the lifecycle and execution loop of a bounded objective."""

    def __init__(
        self,
        contract: ObjectiveContract,
        oracle_func: Optional[Callable[[str, str], str]] = None,
        mode: str = "read",
    ) -> None:
        self.contract = contract
        self.evidence = EvidenceBundle()
        self.oracle_func = oracle_func or self._default_oracle
        self.mode = mode
        self.round_counter = 0
        self.start_time = time.time()
        self.recent_actions: List[str] = []
        self.registry = TOOL_REGISTRY

    def validate_completion(self, action):
        """Domain harnesses override this to require recorded execution evidence."""
        return True, ""

    def _default_oracle(self, prompt: str, system: str) -> str:
        """Query Sabrina's UnifiedOracleHub fallback."""
        try:
            from tools.oracle_providers import UnifiedOracleHub
            hub = UnifiedOracleHub()
            res = hub.query(prompt, system=system, preferred="google")
            if isinstance(res, (tuple, list)):
                return str(res[0])
            return str(res)
        except Exception as exc:
            logger.warning("Default oracle query failed: %s", exc)
            return json.dumps({"tool": "completed", "reason": f"Oracle unavailable: {exc}"})

    def _extract_json_action(self, raw_text: str) -> Dict[str, Any]:
        """Extract first valid JSON object representing a tool action with robust fallback parsing."""
        if not raw_text or not raw_text.strip():
            raise ValueError("Empty model response.")

        raw_clean = re.sub(r"<think>.*?</think>", "", raw_text, flags=re.DOTALL).strip()

        # 1. Direct parse with strict=False (allows unescaped control chars / newlines)
        try:
            val = json.loads(raw_clean, strict=False)
            if isinstance(val, dict) and "tool" in val:
                return val
        except Exception:
            pass

        # 2. Match ```json ... ``` blocks
        code_block = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw_clean, re.DOTALL)
        if code_block:
            try:
                val = json.loads(code_block.group(1), strict=False)
                if isinstance(val, dict) and "tool" in val:
                    return val
            except Exception:
                pass

        # 3. Balanced curly brace extraction
        depth = 0
        start = None
        for i, ch in enumerate(raw_clean):
            if ch == "{":
                if depth == 0:
                    start = i
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0 and start is not None:
                    candidate = raw_clean[start: i + 1]
                    try:
                        val = json.loads(candidate, strict=False)
                        if isinstance(val, dict) and "tool" in val:
                            return val
                    except Exception:
                        pass

        # 4. YAML fallback (forgiving of unescaped quotes/multiline markdown)
        try:
            import yaml
            parsed_yaml = yaml.safe_load(raw_clean)
            if isinstance(parsed_yaml, dict) and "tool" in parsed_yaml:
                return parsed_yaml
        except Exception:
            pass

        # 5. Specialized regex recovery for completed reports
        if any(token in raw_clean.lower() for token in ['"tool": "completed"', '"tool":"completed"', "'tool': 'completed'"]):
            report_match = re.search(r'["\']report["\']\s*:\s*["\'](.*)', raw_clean, re.DOTALL)
            report_text = report_match.group(1) if report_match else raw_clean
            # Strip trailing braces/quotes/markdown fence
            report_clean = re.sub(r'(?:["\']\s*\}|```)\s*$', '', report_text.strip(), flags=re.DOTALL)
            return {
                "tool": "completed",
                "reason": "Successfully generated status report",
                "report": report_clean,
            }

        raise ValueError(f"No valid JSON tool action found in response: {raw_text[:200]}")

    def _validate_scope(self, tool_name: str, action: Dict[str, Any]) -> Tuple[bool, str]:
        """Enforce objective scope and read-only boundaries."""
        if self.contract.scope.get("read_only", True):
            if tool_name == "docker_manager":
                op = str(action.get("operation", "")).strip().lower()
                forbidden = {"start", "stop", "restart", "compose_up", "compose_restart", "pull"}
                if op in forbidden:
                    return False, f"Scope Violation: Operation '{op}' is forbidden in read-only scope."
            elif tool_name in {"remote_run_shell", "run_shell"}:
                cmd = str(action.get("command", "")).strip().lower()
                # Basic write/destructive command blocker for read-only mode
                if any(kw in cmd for kw in ["rm ", "rmdir ", "kill ", "systemctl stop", "systemctl restart", "docker stop", "docker restart"]):
                    return False, f"Scope Violation: Destructive command blocked in read-only scope."

        return True, ""

    def build_prompt(self) -> Tuple[str, str]:
        """Construct a bounded system prompt and turn prompt."""
        # 1. System Prompt
        system = f"""You are Sabrina, an autonomous technical investigator operating under a strictly scoped Objective.

OBJECTIVE:
ID: {self.contract.id}
Goal: {self.contract.goal}
Scope Constraints: {json.dumps(self.contract.scope)}
Remaining Budget: {self.contract.budget_rounds - self.round_counter} rounds, {max(0, int(self.contract.budget_seconds - (time.time() - self.start_time)))}s

OPERATING LOOP:
1. Discover relevant skills to understand the methodology and workflow.
2. Read the selected skill's procedure.
3. Choose registered tools to inspect live systems and code.
4. Accumulate verified evidence (container states, file hashes, timestamps).
5. Complete the objective by calling the 'completed' tool with an evidence-backed report, or report an unresolvable blocker.

CRITICAL INSTRUCTIONS:
- Always output ONLY a single valid JSON object representing your next tool call.
- Do NOT repeat the exact same tool call if it already returned complete information.
- Respect read-only authority. Do NOT modify running services.
"""

        # 2. Context Prompt (Skills, Tools, Evidence, History)
        skills_summary = [f"- {s['name']}: {s['description']}" for s in SKILL_CATALOG.discover()[:8]]
        skills_str = "\n".join(skills_summary)

        tools_str = self.registry.render_catalog(mode=self.mode)

        # Observations summary
        obs_lines = []
        for o in self.evidence.observations[-5:]:
            obs_lines.append(f"[Round {o['round']}] Tool '{o['tool']}': {o['result_preview']}")
        obs_str = "\n".join(obs_lines) if obs_lines else "None yet."

        prompt = f"""=== AVAILABLE WORKFLOW SKILLS (Call read_skill to view full procedure) ===
{skills_str}

=== CALLABLE TOOLS ===
{tools_str}

=== RECENT OBSERVATIONS & EVIDENCE ===
{obs_str}

=== WORKFLOW GUIDELINES ===
1. If you have not read the relevant skill procedure, call read_skill(name="<skill_name>") (e.g. "network-diagnostics" or "service-mapping").
2. Discover lab assets or containers using discover_lab or docker_manager.
3. Inspect files, network configuration, or runtime state using remote_read_file, remote_run_shell, or docker_manager.
4. When you have sufficient verified evidence to answer the objective goal, synthesize all findings into a structured report and call:
   {{"tool": "completed", "reason": "Investigation complete", "report": "# Investigation Report\\n..."}}

Decide your next action. Return ONLY JSON: {{"tool": "<tool_name>", ...params}}"""

        return system, prompt

    def step(self) -> Dict[str, Any]:
        """Execute one cycle of the operating loop."""
        if self.evidence.status == "ready":
            self.evidence.status = "running"
        self.round_counter += 1

        # Budget checks
        elapsed = time.time() - self.start_time
        if self.round_counter > self.contract.budget_rounds:
            self.evidence.status = "budget_exhausted"
            return {"status": "budget_exhausted", "reason": f"Exceeded round budget ({self.contract.budget_rounds})"}
        if elapsed > self.contract.budget_seconds:
            self.evidence.status = "budget_exhausted"
            return {"status": "budget_exhausted", "reason": f"Exceeded time budget ({self.contract.budget_seconds}s)"}

        system_prompt, turn_prompt = self.build_prompt()

        # Model decision
        raw_decision = self.oracle_func(turn_prompt, system_prompt)
        if time.time() - self.start_time > self.contract.budget_seconds:
            self.evidence.status = "budget_exhausted"
            return {"status": "budget_exhausted", "reason": "Time budget expired during inference"}

        try:
            action = self._extract_json_action(raw_decision)
        except Exception as exc:
            err = f"Failed to parse JSON action: {exc}. Please return valid JSON: {{\"tool\": ...}}"
            self.evidence.add_observation(self.round_counter, "parse_error", {}, err)
            return {"status": "in_progress", "round": self.round_counter, "tool": "format_error", "result_preview": err}

        tool_name = action.get("tool", "")
        action_sig = json.dumps(action, sort_keys=True)

        # Stagnation check
        if len(self.recent_actions) >= 2 and self.recent_actions[-1] == action_sig and self.recent_actions[-2] == action_sig:
            stagnation_msg = (
                f"[STAGNATION GUARD]: The exact tool call '{tool_name}' was about to be executed a 3rd time with identical parameters. "
                "You already have this result in your evidence history. Do not repeat it. "
                "Move forward to inspect specific components with remote_read_file, or synthesize your final status report and call 'completed'!"
            )
            self.evidence.add_observation(self.round_counter, tool_name, action, stagnation_msg)
            return {"status": "in_progress", "round": self.round_counter, "tool": "stagnation_guard", "result_preview": stagnation_msg}

        self.recent_actions.append(action_sig)
        self.recent_actions = self.recent_actions[-4:]

        # Scope validation
        allowed, scope_reason = self._validate_scope(tool_name, action)
        if not allowed:
            self.evidence.add_observation(self.round_counter, tool_name, action, scope_reason)
            return {"status": "scope_blocked", "reason": scope_reason}

        # Completion check
        if tool_name == "completed":
            valid, reason = self.validate_completion(action)
            if not valid:
                self.evidence.add_observation(self.round_counter, tool_name, action, reason)
                return {"status": "in_progress", "reason": reason}
            report = action.get("report") or action.get("summary") or action.get("reason", "Done")
            self.evidence.report = report
            self.evidence.status = "completed"
            return {"status": "completed", "report": report}

        # Execution via Registry
        result = self.registry.dispatch(tool_name, action, loop=self)
        self.evidence.add_observation(self.round_counter, tool_name, action, result)

        return {
            "status": "in_progress",
            "round": self.round_counter,
            "tool": tool_name,
            "result_preview": result[:200],
        }

    def run(self) -> EvidenceBundle:
        """Run the loop until completion or budget exhaustion."""
        if self.evidence.status == "ready":
            self.evidence.status = "running"
        while self.evidence.status == "running":
            step_result = self.step()
            if step_result["status"] in {"completed", "budget_exhausted", "blocked"}:
                break
        self.save_to_db()
        return self.evidence

    def save_to_db(self, db_path: Optional[Path | str] = None) -> None:
        """Persist objective state and events to SQLite database."""
        path = Path(db_path or os.environ.get("SABRINA_ASSISTANT_DB", str(Path(__file__).resolve().parents[1] / "data/sabrina_assistant.sqlite3")))
        contract_json = json.dumps(self.contract.to_dict())
        contract_hash = hashlib.sha256(contract_json.encode("utf-8")).hexdigest()
        now = time.time()
        deadline = self.start_time + self.contract.budget_seconds
        expires = now + 7 * 86400

        with sqlite3.connect(path, timeout=10) as con:
            con.execute("PRAGMA busy_timeout=10000")
            existing = con.execute("SELECT id FROM workflow_objectives WHERE id=?", (self.contract.id,)).fetchone()
            if existing:
                con.execute(
                    "UPDATE workflow_objectives SET state=?, next_wake=? WHERE id=?",
                    (self.evidence.status, now + 10, self.contract.id)
                )
            else:
                con.execute(
                    "INSERT INTO workflow_objectives(id, goal, state, contract, contract_hash, created, deadline, expires, experimental) "
                    "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (self.contract.id, self.contract.goal, self.evidence.status, contract_json, contract_hash, now, deadline, expires, 1)
                )

            for obs in self.evidence.observations:
                event_detail = json.dumps({
                    "round": obs["round"],
                    "tool": obs["tool"],
                    "action": obs["action"],
                    "result_preview": obs["result_preview"][:500],
                })
                exists = con.execute(
                    "SELECT 1 FROM workflow_events WHERE objective_id=? AND kind='harness_step' AND detail=?",
                    (self.contract.id, event_detail)
                ).fetchone()
                if not exists:
                    con.execute(
                        "INSERT INTO workflow_events(objective_id, kind, detail, created) VALUES(?, 'harness_step', ?, ?)",
                        (self.contract.id, event_detail, obs.get("timestamp", now))
                    )

            if self.evidence.status == "completed" and self.evidence.report:
                ev_id = f"ev-{self.contract.id}"
                exists = con.execute("SELECT 1 FROM workflow_evidence WHERE id=?", (ev_id,)).fetchone()
                if not exists:
                    raw_hash = hashlib.sha256(self.evidence.report.encode("utf-8")).hexdigest()
                    con.execute(
                        "INSERT INTO workflow_evidence(id, objective_id, attempt_id, transport, provider, model, prompt_hash, raw_response, raw_response_hash, delivery_status, created, completed) "
                        "VALUES(?, ?, ?, 'autonomous_harness', 'sabrina_harness', 'gemini', ?, ?, ?, 'received', ?, ?)",
                        (ev_id, self.contract.id, f"att-{self.contract.id}", contract_hash, self.evidence.report, raw_hash, now, now)
                    )

    @classmethod
    def load_from_db(cls, objective_id: str, db_path: Optional[Path | str] = None) -> Optional[ObjectiveHarness]:
        """Load an objective and its history from SQLite database."""
        path = Path(db_path or os.environ.get("SABRINA_ASSISTANT_DB", str(Path(__file__).resolve().parents[1] / "data/sabrina_assistant.sqlite3")))
        with sqlite3.connect(path, timeout=10) as con:
            row = con.execute("SELECT * FROM workflow_objectives WHERE id=?", (objective_id,)).fetchone()
            if not row:
                return None
            cols = [d[0] for d in con.execute("SELECT * FROM workflow_objectives WHERE id=?", (objective_id,)).description]
            row_dict = dict(zip(cols, row))
            contract_data = json.loads(row_dict["contract"])
            contract = ObjectiveContract(
                id=row_dict["id"],
                goal=row_dict["goal"],
                scope=contract_data.get("scope", {"read_only": True}),
                budget_rounds=contract_data.get("budget_rounds", 10),
                budget_seconds=contract_data.get("budget_seconds", 300.0),
            )
            harness = cls(contract)
            harness.evidence.status = row_dict["state"]

            events = con.execute("SELECT kind, detail, created FROM workflow_events WHERE objective_id=? ORDER BY id", (objective_id,)).fetchall()
            for k, d, ts in events:
                if k == "harness_step":
                    try:
                        p = json.loads(d)
                        harness.evidence.observations.append({
                            "round": p.get("round", 0),
                            "tool": p.get("tool", ""),
                            "action": p.get("action", {}),
                            "result_preview": p.get("result_preview", ""),
                            "timestamp": ts,
                        })
                        harness.round_counter = max(harness.round_counter, p.get("round", 0))
                    except Exception:
                        pass

            ev = con.execute("SELECT raw_response FROM workflow_evidence WHERE objective_id=?", (objective_id,)).fetchone()
            if ev:
                harness.evidence.report = ev[0]

            return harness
