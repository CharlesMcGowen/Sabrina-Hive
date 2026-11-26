"""Unified Tool Registry for Sabrina.

Provides a pluggable, consistent tool registry where tools declare:
- name: Unique tool identifier
- handler: Callable(action_dict, loop=None) -> Any
- description: Human and model-readable description
- parameters: JSON schema / dictionary defining expected parameters
- allowed_modes: Tuple of execution modes where this tool is permitted
- parallel_safe: Whether background crew agents may invoke this concurrently

Ensures input normalization, error encapsulation, mode checking, and uniform dispatch.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import logging
import os
import sqlite3
import subprocess
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger("sabrina.tool_registry")


@dataclass
class ToolDefinition:
    name: str
    handler: Callable[[Dict[str, Any], Any], Any]
    description: str
    parameters: Dict[str, Any] = field(default_factory=dict)
    allowed_modes: Tuple[str, ...] = ("read", "plan", "write", "yolo", "secure_code")
    parallel_safe: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
            "allowed_modes": list(self.allowed_modes),
            "parallel_safe": self.parallel_safe,
        }


class ToolRegistry:
    """Thread-safe catalog and dispatcher for Sabrina executable tools."""

    def __init__(self, *, include_paper_tools: bool = False) -> None:
        self._tools: Dict[str, ToolDefinition] = {}
        self._include_paper_tools = include_paper_tools

    def _paper_tools(self):
        if not self._include_paper_tools:
            return {}
        from tools.sabrina_tool_forge import catalog, invoke
        try:
            records = catalog()['tools']
        except (sqlite3.Error, OSError, ValueError):
            logger.warning('Daily paper catalog temporarily unavailable', exc_info=True)
            return {}
        return {row['name']: ToolDefinition(
            name=row['name'], description=row['contract']['statistic'],
            parameters={'rows': row['contract']['definition'], 'width': row['contract']['width'],
                        'domain': row['contract']['domain']},
            handler=lambda action, loop=None, name=row['name']: invoke(name, action['rows']),
        ) for row in records}

    def register(
        self,
        name: str,
        handler: Callable[[Dict[str, Any], Any], Any],
        description: str,
        parameters: Optional[Dict[str, Any]] = None,
        allowed_modes: Optional[Sequence[str]] = None,
        parallel_safe: bool = True,
    ) -> ToolDefinition:
        """Register a new tool into the catalog."""
        name = name.strip()
        modes = tuple(allowed_modes) if allowed_modes else ("read", "plan", "write", "yolo", "secure_code")
        tool_def = ToolDefinition(
            name=name,
            handler=handler,
            description=description.strip(),
            parameters=parameters or {},
            allowed_modes=modes,
            parallel_safe=parallel_safe,
        )
        self._tools[name] = tool_def
        return tool_def

    def unregister(self, name: str) -> bool:
        return self._tools.pop(name, None) is not None

    def get(self, name: str) -> Optional[ToolDefinition]:
        tool = self._tools.get(name)
        if tool is None and name.startswith('paper_'):
            return self._paper_tools().get(name)
        return tool

    def has_tool(self, name: str) -> bool:
        return self.get(name) is not None

    def list_tools(self, mode: Optional[str] = None) -> List[Dict[str, Any]]:
        """Return metadata for all tools accessible under the given mode."""
        results = []
        for tool in {**self._paper_tools(), **self._tools}.values():
            if mode is None or mode in tool.allowed_modes or "yolo" in tool.allowed_modes:
                results.append(tool.to_dict())
        return results

    def dispatch(self, tool_name: str, action: Dict[str, Any], loop: Any = None) -> str:
        """Dispatch a tool action through its registered handler."""
        tool = self.get(tool_name)
        if tool is None:
            return f"Error: Tool '{tool_name}' is not registered in ToolRegistry."

        # Mode check if loop provides a mode
        current_mode = getattr(loop, "mode", None)
        if current_mode and current_mode not in tool.allowed_modes and current_mode != "yolo":
            return f"BLOCKED: Tool '{tool_name}' is unavailable in '{current_mode}' mode."

        try:
            raw_result = tool.handler(action, loop)
            if isinstance(raw_result, (dict, list)):
                return json.dumps(raw_result, indent=2, ensure_ascii=False)
            return str(raw_result)
        except Exception as exc:
            logger.exception("Error executing tool %s", tool_name)
            return f"Error executing '{tool_name}': {type(exc).__name__}: {exc}"

    def render_catalog(self, mode: Optional[str] = None) -> str:
        """Format available tools as concise prompt documentation."""
        lines = []
        for t in self.list_tools(mode):
            params_str = json.dumps(t["parameters"]) if t["parameters"] else "{}"
            lines.append(f"- **{t['name']}**: {t['description']}\n  Format: {{\"tool\": \"{t['name']}\", ...params: {params_str}}}")
        return "\n".join(lines)


# Global singleton
TOOL_REGISTRY = ToolRegistry(include_paper_tools=True)


# ==============================================================================
# Built-in Core Handlers
# ==============================================================================

def _handle_completed(action: Dict[str, Any], loop: Any = None) -> str:
    # If an objective contract is active, enforce completion evidence gate
    if loop is not None and getattr(loop, "objective", None) is not None:
        try:
            from tools.objective import CompletionValidator
            is_valid, verdict_msg, missing = CompletionValidator.validate(loop.objective, action)
            if not is_valid:
                return verdict_msg
        except Exception as exc:
            logger.warning("Completion validation error: %s", exc)

    reason = action.get("reason", "Goal marked as completed.")
    report = action.get("report") or action.get("summary") or ""
    if report:
        return f"COMPLETED: {reason}\n\nEvidence-Backed Status Report:\n{report}"
    return f"COMPLETED: {reason}"


def _handle_docker_manager(action: Dict[str, Any], loop: Any = None) -> str:
    try:
        from tools.docker_remote import execute as execute_docker
        clean_action = dict(action)
        op = str(clean_action.get("operation") or clean_action.get("action") or clean_action.get("op") or clean_action.get("command") or "").strip().lower()
        if op in {"ps", "list", "list_containers", "docker ps"}:
            op = "containers"
        elif op in {"status", "info", "docker info"}:
            op = "host_status"
        elif op in {"log", "read_logs", "docker logs"}:
            op = "logs"
        elif op in {"show", "detail", "details", "docker inspect"}:
            op = "inspect"
        clean_action["operation"] = op

        target = clean_action.get("target") or clean_action.get("container") or clean_action.get("name") or clean_action.get("id") or clean_action.get("image")
        if target:
            clean_action["target"] = target

        result = execute_docker(clean_action)
        if loop is not None and getattr(loop, "objective", None) is not None:
            try:
                loop.objective.evidence_store.record_service_observation(
                    service_name=target or f"docker:{op}",
                    state="inspected",
                    is_running=("up" in str(result).lower() or "running" in str(result).lower()),
                    details={"operation": op, "target": target, "preview": str(result)[:300]},
                )
            except Exception:
                pass
        return result
    except ImportError:
        return "Error: tools.docker_remote module not available."
    except Exception as exc:
        return f"docker_manager error: {exc}"


def _handle_read_file(action: Dict[str, Any], loop: Any = None) -> str:
    path = action.get("path", "")
    if not path:
        return "Error: read_file requires 'path'."
    offset = int(action.get("offset", 0) or 0)
    max_chars = int(action.get("max_chars", 0) or 0)
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        total_len = len(content)
        if not offset and not max_chars:
            return f"File contents of {path} ({total_len} chars):\n{content}"
        chunk = content[offset: offset + max_chars] if max_chars else content[offset:]
        end = offset + len(chunk)
        more = f"\n[... {total_len - end} more characters -- recall read_file with offset={end}]" if end < total_len else ""
        return f"File contents of {path} (chars {offset}-{end} of {total_len}):\n{chunk}{more}"
    except Exception as exc:
        return f"Error reading file {path}: {exc}"


def _handle_read_lines(action: Dict[str, Any], loop: Any = None) -> str:
    path = action.get("path", "")
    if not path or not os.path.isfile(path):
        return f"Error: read_lines needs an existing 'path' ({path!r} not found)."
    start = max(1, int(action.get("start", 1) or 1))
    max_lines = int(action.get("max_lines", 200) or 200)
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        total = len(lines)
        end = min(total, start + max_lines - 1)
        slice_lines = lines[start - 1: end]
        numbered = [f"{i:4d} | {l.rstrip()}" for i, l in enumerate(slice_lines, start=start)]
        return f"{path} lines {start}-{end} of {total}:\n" + "\n".join(numbered)
    except Exception as exc:
        return f"Error reading lines from {path}: {exc}"


def _handle_find_files(action: Dict[str, Any], loop: Any = None) -> str:
    import glob as _glob
    pattern = action.get("pattern") or action.get("glob") or ""
    if not pattern:
        return "Error: find_files requires 'pattern' (e.g. '**/*.py')."
    base = action.get("path") or "."
    max_results = int(action.get("max_results", 100) or 100)
    exclude = {".git", "__pycache__", ".venv", "venv", "node_modules", "data", "artifacts", "logs"}
    matched = []
    try:
        for match in _glob.glob(os.path.join(base, pattern), recursive=True):
            rel = os.path.relpath(match).replace("\\", "/")
            parts = rel.split("/")
            if any(p in exclude for p in parts):
                continue
            if os.path.isfile(match):
                matched.append(rel)
        matched = sorted(set(matched))[:max_results]
        return f"{len(matched)} file(s) matching '{pattern}' under {base}:\n" + "\n".join(matched)
    except Exception as exc:
        return f"find_files error: {exc}"


def _handle_search_code(action: Dict[str, Any], loop: Any = None) -> str:
    import re as _re, fnmatch as _fn
    pattern = action.get("pattern") or action.get("query") or ""
    if not pattern:
        return "Error: search_code requires 'pattern' (regex)."
    base = action.get("path") or "."
    glob_pat = action.get("glob") or "*.py"
    max_results = int(action.get("max_results", 50) or 50)
    try:
        rx = _re.compile(pattern)
    except _re.error as exc:
        return f"Bad regex {pattern!r}: {exc}"
    exclude = {".git", "__pycache__", ".venv", "venv", "node_modules", "data", "artifacts", "logs"}
    hits = []
    for dp, dns, fns in os.walk(base):
        dns[:] = [d for d in dns if d not in exclude]
        for fn in fns:
            if not _fn.fnmatch(fn, glob_pat):
                continue
            fp = os.path.join(dp, fn)
            try:
                with open(fp, "r", encoding="utf-8", errors="replace") as f:
                    for i, line in enumerate(f, 1):
                        if rx.search(line):
                            hits.append(f"{os.path.relpath(fp)}:{i}: {line.rstrip()[:200]}")
                            if len(hits) >= max_results:
                                return f"{len(hits)} match(es) for /{pattern}/ (glob {glob_pat}):\n" + "\n".join(hits)
            except Exception:
                pass
    return f"{len(hits)} match(es) for /{pattern}/ (glob {glob_pat}):\n" + "\n".join(hits) if hits else f"No matches for /{pattern}/ under {base}."


def _handle_remote_read_file(action: Dict[str, Any], loop: Any = None) -> str:
    """Safe, read-only remote file reader on the lab host (ego.local) via SSH."""
    path = action.get("path", "")
    if not path:
        return "Error: remote_read_file requires 'path'."
    if not path.startswith("/"):
        path = f"/home/ego/kraken-explorer/{path.lstrip('./')}"

    # Check objective contract root containment
    if loop is not None and getattr(loop, "objective", None) is not None:
        if not loop.objective.is_path_permitted(path):
            return (
                f"BLOCKED: remote path '{path}' is not within authorized candidate roots: "
                f"{', '.join(loop.objective.permitted_roots)}"
            )

    offset = int(action.get("offset", 0) or 0)
    max_chars = int(action.get("max_chars", 12000) or 12000)
    host = os.environ.get("SABRINA_DOCKER_HOST", "ego.local")
    user = os.environ.get("SABRINA_DOCKER_USER", "ego")

    # Bounded read using head/tail over SSH
    remote_cmd = f"test -f {path} && dd if={path} bs=1 skip={offset} count={max_chars} 2>/dev/null || echo '__NOT_FOUND__'"
    cmd = ["ssh", "-4", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", f"{user}@{host}", remote_cmd]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=20, errors="replace")
        output = proc.stdout
        if "__NOT_FOUND__" in output:
            if loop is not None and getattr(loop, "objective", None) is not None:
                loop.objective.evidence_store.record_path_verification(path, host=host, exists=False, details="File not found")
            return f"Error: Remote file '{path}' not found or not a regular file on {host}."

        if loop is not None and getattr(loop, "objective", None) is not None:
            loop.objective.evidence_store.record_path_verification(path, host=host, exists=True, details=output[:200])

        return f"Remote file {host}:{path} (chars {offset}-{offset + len(output)}):\n{output}"
    except subprocess.TimeoutExpired:
        return f"Error: Remote read timed out connecting to {host}."
    except Exception as exc:
        return f"Error reading remote file: {exc}"


def _handle_remote_run_shell(action: Dict[str, Any], loop: Any = None) -> str:
    """Run an authorized remote command on the lab host."""
    command = action.get("command", "")
    if not command:
        return "Error: remote_run_shell requires 'command'."
    host = os.environ.get("SABRINA_DOCKER_HOST", "ego.local")
    user = os.environ.get("SABRINA_DOCKER_USER", "ego")
    cmd = ["ssh", "-4", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", f"{user}@{host}", command]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60, errors="replace")
        return f"Stdout:\n{proc.stdout}\nStderr:\n{proc.stderr}\nExit Code: {proc.returncode}"
    except Exception as exc:
        return f"Error executing remote command: {exc}"


# Register standard built-in tools
TOOL_REGISTRY.register(
    name="completed",
    handler=_handle_completed,
    description="Mark the overarching goal or objective as completed with a verified report.",
    parameters={"reason": "string", "report": "optional string containing final evidence-backed status report"},
    allowed_modes=("read", "plan", "write", "yolo", "secure_code"),
    parallel_safe=False,
)

TOOL_REGISTRY.register(
    name="docker_manager",
    handler=_handle_docker_manager,
    description="Manage Docker containers, images, volumes, logs, and Compose projects on the lab host (ego.local).",
    parameters={
        "operation": "containers | inspect | logs | stats | images | compose_ps | compose_logs | host_status",
        "target": "container name or ID (for inspect/logs)",
        "tail": "number of log lines (optional)",
    },
    allowed_modes=("read", "plan", "write", "yolo"),
    parallel_safe=False,
)

TOOL_REGISTRY.register(
    name="read_file",
    handler=_handle_read_file,
    description="Read contents of a local file with optional pagination.",
    parameters={"path": "file path", "offset": "optional integer offset", "max_chars": "optional max chars"},
    allowed_modes=("read", "plan", "write", "yolo", "secure_code"),
    parallel_safe=True,
)

TOOL_REGISTRY.register(
    name="read_lines",
    handler=_handle_read_lines,
    description="Read specific line ranges of a file with line numbers.",
    parameters={"path": "file path", "start": "starting line number", "max_lines": "line count (default 200)"},
    allowed_modes=("read", "plan", "write", "yolo", "secure_code"),
    parallel_safe=True,
)

TOOL_REGISTRY.register(
    name="find_files",
    handler=_handle_find_files,
    description="Find files matching a glob pattern.",
    parameters={"pattern": "glob pattern (e.g. '**/*.py')", "path": "search root", "max_results": "max results"},
    allowed_modes=("read", "plan", "write", "yolo", "secure_code"),
    parallel_safe=True,
)

TOOL_REGISTRY.register(
    name="search_code",
    handler=_handle_search_code,
    description="Regex search across files in the codebase.",
    parameters={"pattern": "regex pattern", "path": "search root", "glob": "file pattern (e.g. '*.py')"},
    allowed_modes=("read", "plan", "write", "yolo", "secure_code"),
    parallel_safe=True,
)

TOOL_REGISTRY.register(
    name="remote_read_file",
    handler=_handle_remote_read_file,
    description="Read contents of a remote file on the lab host (ego.local) via SSH.",
    parameters={"path": "absolute Linux path (e.g. /home/ego/...)", "offset": "byte offset", "max_chars": "max chars"},
    allowed_modes=("read", "plan", "write", "yolo", "secure_code"),
    parallel_safe=True,
)

TOOL_REGISTRY.register(
    name="remote_run_shell",
    handler=_handle_remote_run_shell,
    description="Run a shell command on the lab host (ego.local) via SSH.",
    parameters={"command": "Linux shell command string"},
    allowed_modes=("read", "plan", "write", "yolo", "secure_code"),
    parallel_safe=False,
)


def _handle_search_code_index(action: Dict[str, Any], loop: Any = None) -> str:
    """Search locally indexed codebase files and chunks via SQLite FTS5."""
    query = action.get("query") or action.get("pattern") or action.get("search") or ""
    if not query:
        return "Error: search_code_index requires 'query'."
    max_results = int(action.get("max_results", 10) or 10)
    try:
        from tools.rag_knowledge import query_code_index
        results = query_code_index(query, top_k=max_results)
        if not results:
            return f"No code index matches for '{query}'. Run index_codebase to index local/remote paths."
        lines = []
        for r in results:
            path = r.get("path") or r.get("topic") or "unknown"
            snippet = str(r.get("content", ""))[:250].replace("\n", " ")
            lines.append(f"- **{path}** (score {r.get('score', 0):.2f}): {snippet}")
        return f"Found {len(results)} code index matches for '{query}':\n" + "\n".join(lines)
    except Exception as exc:
        return f"search_code_index error: {exc}"


TOOL_REGISTRY.register(
    name="search_code_index",
    handler=_handle_search_code_index,
    description="Search locally indexed source code and configs by keyword, symbol, or filename.",
    parameters={"query": "keyword, function, or symbol name", "max_results": "optional integer limit (default 10)"},
    allowed_modes=("read", "plan", "write", "yolo", "secure_code"),
    parallel_safe=True,
)


# Collective Self-Ordering Swarm Tools
def _handle_submit_dag_plan(action: Dict[str, Any], loop: Any = None) -> str:
    from tools.collective_ordering import submit_dag_plan_tool
    return submit_dag_plan_tool(action, loop=loop)


def _handle_get_unblocked_tasks(action: Dict[str, Any], loop: Any = None) -> str:
    from tools.collective_ordering import get_unblocked_tasks_tool
    return get_unblocked_tasks_tool(action, loop=loop)


def _handle_claim_dag_task(action: Dict[str, Any], loop: Any = None) -> str:
    from tools.collective_ordering import claim_dag_task_tool
    return claim_dag_task_tool(action, loop=loop)


def _handle_complete_dag_task(action: Dict[str, Any], loop: Any = None) -> str:
    from tools.collective_ordering import complete_dag_task_tool
    return complete_dag_task_tool(action, loop=loop)


def _handle_view_dag_wavefront(action: Dict[str, Any], loop: Any = None) -> str:
    from tools.collective_ordering import view_dag_wavefront_tool
    return view_dag_wavefront_tool(action, loop=loop)


TOOL_REGISTRY.register(
    name="submit_dag_plan",
    handler=_handle_submit_dag_plan,
    description="Submit a multi-task topological DAG graph with dependency and cycle validation.",
    parameters={"project": "project identifier", "tasks": "list of dicts with task_id, goal, depends_on, produces_artifact"},
    allowed_modes=("read", "plan", "write", "yolo", "secure_code"),
    parallel_safe=True,
)

TOOL_REGISTRY.register(
    name="get_unblocked_tasks",
    handler=_handle_get_unblocked_tasks,
    description="Stigmergic query: fetch tasks whose prerequisites are 100% completed and ready for execution.",
    parameters={"project": "project identifier"},
    allowed_modes=("read", "plan", "write", "yolo", "secure_code"),
    parallel_safe=True,
)

TOOL_REGISTRY.register(
    name="claim_dag_task",
    handler=_handle_claim_dag_task,
    description="Atomically lease an unblocked task with TTL lock, preventing concurrent multi-agent race conditions.",
    parameters={"task_id": "target task ID", "lease_seconds": "TTL lock in seconds (default 180)"},
    allowed_modes=("read", "plan", "write", "yolo", "secure_code"),
    parallel_safe=True,
)

TOOL_REGISTRY.register(
    name="complete_dag_task",
    handler=_handle_complete_dag_task,
    description="Mark a task completed with verified evidence, release lease, and compute newly unblocked downstream frontier.",
    parameters={"task_id": "target task ID", "evidence": "verification evidence / test output", "produces_artifact": "optional produced file path"},
    allowed_modes=("read", "plan", "write", "yolo", "secure_code"),
    parallel_safe=True,
)

TOOL_REGISTRY.register(
    name="view_dag_wavefront",
    handler=_handle_view_dag_wavefront,
    description="View live wavefront telemetry: completed, active, unblocked ready, and blocked downstream tasks.",
    parameters={"project": "project identifier"},
    allowed_modes=("read", "plan", "write", "yolo", "secure_code"),
    parallel_safe=True,
)


def _handle_inspect_network_flows(action: Dict[str, Any], loop: Any = None) -> str:
    from tools.network_flow_inspector import inspect_network_flows_tool
    return inspect_network_flows_tool(action, loop=loop)


def _handle_get_flow_telemetry(action: Dict[str, Any], loop: Any = None) -> str:
    from tools.network_flow_inspector import get_flow_telemetry_tool
    return get_flow_telemetry_tool(action, loop=loop)


def _handle_audit_worker_latency(action: Dict[str, Any], loop: Any = None) -> str:
    from tools.network_flow_inspector import audit_worker_latency_tool
    return audit_worker_latency_tool(action, loop=loop)


TOOL_REGISTRY.register(
    name="inspect_network_flows",
    handler=_handle_inspect_network_flows,
    description="Inspect captured network and API traffic flows with verified credential redaction. Read-only.",
    parameters={"worker_id": "optional filter by worker", "model": "optional filter by model", "target_host": "optional host filter", "limit": "max flows to return (default 25)"},
    allowed_modes=("read", "plan", "write", "yolo", "secure_code"),
    parallel_safe=True,
)

TOOL_REGISTRY.register(
    name="get_flow_telemetry",
    handler=_handle_get_flow_telemetry,
    description="Get detailed telemetry, timings (queue vs TTFT), parameters (think, num_ctx), and redacted headers for a flow.",
    parameters={"flow_id": "target flow ID"},
    allowed_modes=("read", "plan", "write", "yolo", "secure_code"),
    parallel_safe=True,
)

TOOL_REGISTRY.register(
    name="audit_worker_latency",
    handler=_handle_audit_worker_latency,
    description="Aggregate connection, queue wait, and generation latency statistics across endpoints and models.",
    parameters={"endpoint": "optional target host filter", "hours": "time window in hours (default 24)"},
    allowed_modes=("read", "plan", "write", "yolo", "secure_code"),
    parallel_safe=True,
)


def _handle_search_internet_resources(action: Dict[str, Any], loop: Any = None) -> Dict[str, Any]:
    from tools.internet_resource_indexer import search_internet_resources_tool
    return search_internet_resources_tool(action, loop=loop)


def _handle_use_indexed_resource(action: Dict[str, Any], loop: Any = None) -> Dict[str, Any]:
    from tools.internet_resource_indexer import use_indexed_resource_tool
    return use_indexed_resource_tool(action, loop=loop)


def _handle_run_internet_resource_sweep(action: Dict[str, Any], loop: Any = None) -> Dict[str, Any]:
    from tools.internet_resource_indexer import run_internet_resource_sweep_tool
    return run_internet_resource_sweep_tool(action, loop=loop)


TOOL_REGISTRY.register(
    name="search_internet_resources",
    handler=_handle_search_internet_resources,
    description="Search Sabrina's verified zero-auth internet index of Public APIs, Datasets, and Documentation/Skill repositories.",
    parameters={"query": "search keywords", "resource_kind": "all | public_api | dataset | doc_skill_repo", "limit": "max results (default 8)"},
    allowed_modes=("read", "plan", "write", "yolo", "secure_code"),
    parallel_safe=True,
)

TOOL_REGISTRY.register(
    name="use_indexed_resource",
    handler=_handle_use_indexed_resource,
    description="Fetch live zero-auth JSON/Markdown data from an indexed internet resource_id and update empirical usage quality.",
    parameters={"resource_id": "indexed resource_id from search_internet_resources", "max_chars": "max preview length (default 8000)"},
    allowed_modes=("read", "plan", "write", "yolo", "secure_code"),
    parallel_safe=True,
)

TOOL_REGISTRY.register(
    name="run_internet_resource_sweep",
    handler=_handle_run_internet_resource_sweep,
    description="Execute one bounded discovery, verification, indexing, and UCB bandit query-evolution sweep across public APIs, datasets, and doc repos.",
    parameters={"channels": "optional list/csv of public_api,dataset,doc_skill_repo", "arms_per_sweep": "bandit arms to pull (default 3)", "max_probes_per_arm": "max HTTP probes per arm (default 5)"},
    allowed_modes=("read", "plan", "write", "yolo", "secure_code"),
    parallel_safe=True,
)


def _handle_run_compression_experiment(action: Dict[str, Any], loop: Any = None) -> Dict[str, Any]:
    from tools.neural_compressor_lab import run_compression_experiment_tool
    return run_compression_experiment_tool(action, loop=loop)


def _handle_get_compression_leaderboard(action: Dict[str, Any], loop: Any = None) -> Dict[str, Any]:
    from tools.neural_compressor_lab import get_compression_leaderboard_tool
    return get_compression_leaderboard_tool(action, loop=loop)


TOOL_REGISTRY.register(
    name="run_compression_experiment",
    handler=_handle_run_compression_experiment,
    description="Train and benchmark Sabrina's lossless neural byte compressor (PyTorch + exact 32-bit arithmetic coder) against lzma/bz2/zlib BPB.",
    parameters={"corpus_name": "enwik8_100k | enwik8_1m", "allow_download": "bool (default True)"},
    allowed_modes=("read", "plan", "write", "yolo", "secure_code"),
    parallel_safe=False,
)

TOOL_REGISTRY.register(
    name="get_compression_leaderboard",
    handler=_handle_get_compression_leaderboard,
    description="Fetch Sabrina's verified lossless neural compression Bits-Per-Byte (BPB) leaderboard.",
    parameters={"limit": "max rows (default 15)"},
    allowed_modes=("read", "plan", "write", "yolo", "secure_code"),
    parallel_safe=True,
)


def _handle_run_exploitgym_rsi_cycle(action: Dict[str, Any], loop: Any = None) -> Dict[str, Any]:
    from tools.coder_agent.exploitgym_rsi_controller import run_exploitgym_rsi_cycle_tool
    return run_exploitgym_rsi_cycle_tool(action, loop=loop)


def _handle_get_exploitgym_rsi_status(action: Dict[str, Any], loop: Any = None) -> Dict[str, Any]:
    from tools.coder_agent.exploitgym_rsi_controller import get_exploitgym_rsi_status_tool
    return get_exploitgym_rsi_status_tool(action, loop=loop)


TOOL_REGISTRY.register(
    name="run_exploitgym_rsi_cycle",
    handler=_handle_run_exploitgym_rsi_cycle,
    description="Run one autonomous two-plane ExploitGym RSI cycle (constrained-JSON Docker eval -> bottleneck classification -> isolated research -> workshop -> replay -> promotion gate).",
    parameters={"model": "local model (default qwen2.5-coder:14b)", "max_turns": "turns per task (default 20)", "timeout_seconds": "seconds per task (default 240)", "run_replay": "bool (default True)"},
    allowed_modes=("read", "plan", "write", "yolo", "secure_code"),
    parallel_safe=False,
)

TOOL_REGISTRY.register(
    name="get_exploitgym_rsi_status",
    handler=_handle_get_exploitgym_rsi_status,
    description="Inspect Sabrina's ExploitGym 869-task research-assisted coverage, blind generalization score, active lessons, and RSI cycle history.",
    parameters={},
    allowed_modes=("read", "plan", "write", "yolo", "secure_code"),
    parallel_safe=True,
)


def _handle_recruit_swarm_members(action: Dict[str, Any], loop: Any = None) -> Dict[str, Any]:
    from tools.autonomous_swarm_recruiter import recruit_swarm_members_tool
    return recruit_swarm_members_tool(action, loop=loop)


def _handle_get_swarm_recruitment_status(action: Dict[str, Any], loop: Any = None) -> Dict[str, Any]:
    from tools.autonomous_swarm_recruiter import get_swarm_recruitment_status_tool
    return get_swarm_recruitment_status_tool(action, loop=loop)


TOOL_REGISTRY.register(
    name="recruit_swarm_members",
    handler=_handle_recruit_swarm_members,
    description="Run one concurrent zero-auth swarm discovery and live verification wave against Windows's authoritative portal DB.",
    parameters={"batch_size": "candidates to probe (default 60)", "workers": "concurrency (default 12)", "discover_fresh": "bool (default True)"},
    allowed_modes=("read", "plan", "write", "yolo", "secure_code"),
    parallel_safe=False,
)

TOOL_REGISTRY.register(
    name="get_swarm_recruitment_status",
    handler=_handle_get_swarm_recruitment_status,
    description="Inspect live external swarm portal counts, reachable members, and staged schema-verified backlog.",
    parameters={},
    allowed_modes=("read", "plan", "write", "yolo", "secure_code"),
    parallel_safe=True,
)


def _handle_harvest_sabrina_bugs(action: Dict[str, Any], loop: Any = None) -> Dict[str, Any]:
    from tools.sabrina_self_repair_workshop import harvest_sabrina_bugs_tool
    return harvest_sabrina_bugs_tool(action, loop=loop)


def _handle_run_self_repair_cycle(action: Dict[str, Any], loop: Any = None) -> Dict[str, Any]:
    from tools.sabrina_self_repair_workshop import run_self_repair_cycle_tool
    return run_self_repair_cycle_tool(action, loop=loop)


def _handle_approve_self_repair_patch(action: Dict[str, Any], loop: Any = None) -> Dict[str, Any]:
    from tools.sabrina_self_repair_workshop import approve_self_repair_patch_tool
    return approve_self_repair_patch_tool(action, loop=loop)


TOOL_REGISTRY.register(
    name="harvest_sabrina_bugs",
    handler=_handle_harvest_sabrina_bugs,
    description="Harvest live Sabrina runtime/collective/scheduler bugs and inspect staged self-repair patches.",
    parameters={"limit": "max rows (default 10)"},
    allowed_modes=("read", "plan", "write", "yolo", "secure_code"),
    parallel_safe=True,
)

TOOL_REGISTRY.register(
    name="run_self_repair_cycle",
    handler=_handle_run_self_repair_cycle,
    description="Run one isolated sandbox self-repair cycle (qwen2.5-coder:14b patch -> py_compile -> fail-before/pass-after pytest -> disjoint granite3.1-dense:8b audit).",
    parameters={"max_tickets": "tickets to attempt (default 1)", "auto_apply": "bool (default False; stage for approval)"},
    allowed_modes=("read", "plan", "write", "yolo", "secure_code"),
    parallel_safe=False,
)

TOOL_REGISTRY.register(
    name="approve_self_repair_patch",
    handler=_handle_approve_self_repair_patch,
    description="Atomically apply a verified staged self-repair patch with SHA-256 backup and smoke validation.",
    parameters={"patch_id": "staged patch_id", "approver": "operator identity"},
    allowed_modes=("write", "yolo"),
    parallel_safe=False,
)


def _handle_federation_status(action: Dict[str, Any], loop: Any = None) -> Dict[str, Any]:
    from tools.sabrina_compute_federation import federation_status_tool
    return federation_status_tool(action, loop=loop)


def _handle_federated_run_python(action: Dict[str, Any], loop: Any = None) -> Dict[str, Any]:
    from tools.sabrina_compute_federation import federated_run_python_tool
    return federated_run_python_tool(action, loop=loop)


def _handle_federated_llm_chat(action: Dict[str, Any], loop: Any = None) -> Dict[str, Any]:
    from tools.sabrina_compute_federation import federated_llm_chat_tool
    return federated_llm_chat_tool(action, loop=loop)


TOOL_REGISTRY.register(
    name="federation_status",
    handler=_handle_federation_status,
    description="Inspect and probe Sabrina's Distributed Compute Federation Mesh (Windows Workstation RX 5700 XT, ego.local Lab, and safe zero-auth Web Sandboxes).",
    parameters={"probe": "bool (default True)"},
    allowed_modes=("read", "plan", "write", "yolo", "secure_code"),
    parallel_safe=True,
)

TOOL_REGISTRY.register(
    name="federated_run_python",
    handler=_handle_federated_run_python,
    description="Execute or spread stateless/isolated Python compute across Windows Workstation, ego.local Lab, Wandbox, and Godbolt web sandboxes.",
    parameters={"code": "single Python snippet", "snippets": "optional list of Python snippets to spread in parallel", "target": "auto | windows-workstation | web-wandbox-py312 | web-godbolt-py312 | local"},
    allowed_modes=("read", "plan", "write", "yolo", "secure_code"),
    parallel_safe=True,
)

TOOL_REGISTRY.register(
    name="federated_llm_chat",
    handler=_handle_federated_llm_chat,
    description="Route LLM inference across Sabrina's multi-host GPU fleet (ego-lab RTX 5060 Ti, Windows RX 5700 XT) and external zero-auth web providers.",
    parameters={"prompt": "prompt string", "system_prompt": "optional system prompt", "max_tokens": "int (default 600)"},
    allowed_modes=("read", "plan", "write", "yolo", "secure_code"),
    parallel_safe=True,
)


def _handle_sabrina_mesh_cli(action: Dict[str, Any], loop: Any = None) -> str:
    from tools.sabrina_compute_federation import sabrina_mesh_cli_tool
    return json.dumps(sabrina_mesh_cli_tool(action, loop=loop), indent=2)


TOOL_REGISTRY.register(
    name="sabrina_mesh_cli",
    handler=_handle_sabrina_mesh_cli,
    description=(
        "Run sabrina-mesh v0.3.0 distributed sharding planner, multi-backend RPC spec generator, "
        "zero-copy Ollama GGUF blob resolver, and hardware-adaptive preference recommender "
        "(Samsung NanoQuant 0.61b, LittleBit 0.15b, Microsoft BitNet b1.58, GGUF Q4..Q8)."
    ),
    parameters={
        "operation": "recommend | quants | models | plan | rpc-spec | resolve-gguf | demo | probe",
        "preference": "balanced | max_quality | max_speed | ultra_low_bit_frontier | all_profiles",
        "profile": "live | 8gb | 16gb | 24gb-mesh | cpu",
        "args": "optional CLI args list or string (e.g. ['rpc-spec', '--model', 'qwen3-30b-a3b'])",
    },
    allowed_modes=("read", "plan", "write", "yolo", "secure_code"),
    parallel_safe=True,
)


def _handle_sabrina_upstream_forge(action: Dict[str, Any], loop: Any = None) -> str:
    from tools.custom.sabrina_upstream_forge_plugin import execute_plugin
    return execute_plugin(action, loop=loop)


TOOL_REGISTRY.register(
    name="sabrina_upstream_forge",
    handler=_handle_sabrina_upstream_forge,
    description=(
        "Autonomous Open-Source R&D, Lab Verification, Dual-Output Distillation (Blackboard + FTS5 RAG + SKILL.md), "
        "Sanitized SHA-256 Dossier Generation, and Personal GitHub Fork / Browser Fork / Draft PR Staging."
    ),
    parameters={
        "operation": (
            "list_repos | enroll_repo | create_workspace | run_lab_verification | finalize_finding | "
            "list_dossiers | inspect_dossier | stage_draft_pr | sync_fork_drafts | browser_fork_repo | approve_upstream"
        ),
        "dossier_id": "optional dossier ID (e.g. dos_openzl-fp16-signfrac-padding)",
        "repo_key": "optional enrolled repo key (e.g. openzl, lzbench, nvcomp)",
        "upstream_repo": "optional GitHub owner/repo (e.g. meta/openzl)",
    },
    allowed_modes=("read", "plan", "write", "yolo", "secure_code"),
    parallel_safe=True,
)


def _handle_sabrina_tool_forge(action: Dict[str, Any], loop: Any = None) -> str:
    operation = action.get("operation")
    if operation in ("daily_paper_catalog", "run_paper_tool", "daily_paper_cycle"):
        from tools import sabrina_tool_forge as papers
        if operation == "daily_paper_catalog":
            return json.dumps(papers.catalog())
        if operation == "run_paper_tool":
            return json.dumps(papers.invoke(action["tool_name"], action["rows"]))
        if getattr(loop, "mode", None) in ("read", "plan"):
            return "Daily Paper Forge publication requires a write-capable run."
        return json.dumps(papers.cycle())
    from tools.custom.sabrina_tool_forge_plugin import execute_plugin
    return execute_plugin(action, loop=loop)


TOOL_REGISTRY.register(
    name="sabrina_tool_forge",
    handler=_handle_sabrina_tool_forge,
    description=(
        "Autonomous Tool & Skill Sandbox Forge: Thoroughly read docs, persist Reasoning Notes to Blackboard/RAG, "
        "synthesize & self-test candidate plugins in an isolated sandbox, hot-deploy verified tools, and create/evolve SKILL.md."
    ),
    parameters={
        "operation": "daily_paper_catalog | run_paper_tool | daily_paper_cycle | ingest_docs_and_reason | sandbox_test_tool | deploy_and_evolve_skill | full_forge_pipeline",
        "rows": "numeric matrix for run_paper_tool; see the published input contract",
        "problem_slug": "identifier for the reasoning note",
        "tool_name": "snake_case tool name to test/deploy",
        "skill_name": "kebab-case SKILL.md identifier to create or evolve",
    },
    allowed_modes=("read", "plan", "write", "yolo", "secure_code"),
    parallel_safe=True,
)





