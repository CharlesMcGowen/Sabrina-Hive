"""Swarm Self-Analytical Evolution Benchmark (Evo-Bench / HarnessBank / EvoArena).

Implements the fixed-model, mutable-scaffold evolution loop A_t = (theta, Sigma_t):
- Option A structured policy & scaffold evolution (`execution_authorized=False` strictly locked)
- 3-Way disjoint task split: Search Split, Frozen Regression Gate, Untouched Held-Out Suite (+ domain-shifted canary)
- Deterministic `PerturbationInjector` keyed by `(task_id, role, step_index)`
- Strict improvement + AnytimeVal peak-snapshot rollback (never promotes "holds steady" or regressions)
- Per-task `max_llm_calls_per_task` & per-round `max_tokens_per_round` anti-scaling confound guards
- Cross-Model Transfer evaluation on `transfer_model` (default `ollama/gemma3:12b`)
- WAL-mode SQLite persistence (`sabrina_evaluations.sqlite3`) + Markdown scorecards in `artifacts/evo_bench/`
"""

from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import json
import math
import re
import sqlite3
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

ROOT_DIR = Path(__file__).resolve().parent
if str(ROOT_DIR.parent) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR.parent))

try:
    from sabrina_console_desktop.live_swarm_runner import LiveSwarmRunner
    from sabrina_console_desktop.recovery_policy import RecoveryPolicy, promote as promote_recovery_policy
except ImportError:  # pragma: no cover
    from live_swarm_runner import LiveSwarmRunner  # type: ignore[no-redef]
    from recovery_policy import RecoveryPolicy, promote as promote_recovery_policy  # type: ignore[no-redef]


DEFAULT_DB_PATH = ROOT_DIR / "data" / "sabrina_evaluations.sqlite3"
DEFAULT_EVO_ARTIFACTS_DIR = ROOT_DIR / "artifacts" / "evo_bench"


@dataclass(frozen=True)
class SwarmScaffold:
    """Mutable scaffold Sigma_t (Option A: bounded topology, compaction, and recovery policy)."""

    coordinator_count: int = 1
    workers_per_coordinator: int = 4
    quorum_split_threshold: int = 6
    quorum_prune_threshold: int = 2
    enable_work_stealing: bool = True
    max_concurrency: int = 4
    subagent_excerpt_chars: int = 1200
    executor_excerpt_chars: int = 1400
    recovery: RecoveryPolicy = field(default_factory=RecoveryPolicy)

    def validated(self) -> SwarmScaffold:
        c = max(1, min(4, int(self.coordinator_count)))
        w = max(1, min(12, int(self.workers_per_coordinator)))
        qs = max(2, min(12, int(self.quorum_split_threshold)))
        qp = max(0, min(qs - 1, int(self.quorum_prune_threshold)))
        mc = max(1, min(12, int(self.max_concurrency)))
        sec = max(400, min(12000, int(self.subagent_excerpt_chars)))
        eec = max(400, min(12000, int(self.executor_excerpt_chars)))
        return SwarmScaffold(
            coordinator_count=c,
            workers_per_coordinator=w,
            quorum_split_threshold=qs,
            quorum_prune_threshold=qp,
            enable_work_stealing=bool(self.enable_work_stealing),
            max_concurrency=mc,
            subagent_excerpt_chars=sec,
            executor_excerpt_chars=eec,
            recovery=self.recovery,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "coordinator_count": self.coordinator_count,
            "workers_per_coordinator": self.workers_per_coordinator,
            "quorum_split_threshold": self.quorum_split_threshold,
            "quorum_prune_threshold": self.quorum_prune_threshold,
            "enable_work_stealing": self.enable_work_stealing,
            "max_concurrency": self.max_concurrency,
            "subagent_excerpt_chars": self.subagent_excerpt_chars,
            "executor_excerpt_chars": self.executor_excerpt_chars,
            "recovery": dataclasses.asdict(self.recovery),
            "execution_authorized": False,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, sort_keys=True)

    @property
    def scaffold_hash(self) -> str:
        return hashlib.sha256(self.to_json().encode("utf-8")).hexdigest()[:8]

    @classmethod
    def from_dict(cls, data: dict[str, Any], fallback: SwarmScaffold | None = None) -> SwarmScaffold:
        base = fallback or cls()
        rec_raw = data.get("recovery") if isinstance(data.get("recovery"), dict) else {}
        rec_model = str(rec_raw.get("model", base.recovery.model))
        if not re.fullmatch(r"ollama/[a-zA-Z0-9][a-zA-Z0-9._:/-]{0,95}", rec_model):
            rec_model = base.recovery.model
        rec = RecoveryPolicy(
            model=rec_model,
            timeout_seconds=max(5, min(120, int(rec_raw.get("timeout_seconds", base.recovery.timeout_seconds)))),
            max_context_chars=max(1000, min(12000, int(rec_raw.get("max_context_chars", base.recovery.max_context_chars)))),
            max_reply_tokens=max(128, min(2048, int(rec_raw.get("max_reply_tokens", base.recovery.max_reply_tokens)))),
            max_workers_per_mission=max(0, min(8, int(rec_raw.get("max_workers_per_mission", base.recovery.max_workers_per_mission)))),
            allow_worker_hotfixes=bool(rec_raw.get("allow_worker_hotfixes", base.recovery.allow_worker_hotfixes)),
        )
        return cls(
            coordinator_count=int(data.get("coordinator_count", base.coordinator_count)),
            workers_per_coordinator=int(data.get("workers_per_coordinator", base.workers_per_coordinator)),
            quorum_split_threshold=int(data.get("quorum_split_threshold", base.quorum_split_threshold)),
            quorum_prune_threshold=int(data.get("quorum_prune_threshold", base.quorum_prune_threshold)),
            enable_work_stealing=bool(data.get("enable_work_stealing", base.enable_work_stealing)),
            max_concurrency=int(data.get("max_concurrency", base.max_concurrency)),
            subagent_excerpt_chars=int(data.get("subagent_excerpt_chars", base.subagent_excerpt_chars)),
            executor_excerpt_chars=int(data.get("executor_excerpt_chars", base.executor_excerpt_chars)),
            recovery=rec,
        ).validated()

    def apply_to_runner(self, runner: LiveSwarmRunner, policy_path: Path | None = None) -> None:
        runner.configure_topology(
            coordinator_count=self.coordinator_count,
            workers_per_coordinator=self.workers_per_coordinator,
            quorum_split_threshold=self.quorum_split_threshold,
            quorum_prune_threshold=self.quorum_prune_threshold,
            enable_work_stealing=self.enable_work_stealing,
            max_concurrency=self.max_concurrency,
            subagent_excerpt_chars=self.subagent_excerpt_chars,
            executor_excerpt_chars=self.executor_excerpt_chars,
        )
        runner._recovery_policy = self.recovery
        if policy_path is not None:
            policy_path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=policy_path.parent, delete=False) as tf:
                tf.write(self.recovery.to_json())
                cand_p = Path(tf.name)
            try:
                promote_recovery_policy(cand_p, destination=policy_path)
            finally:
                cand_p.unlink(missing_ok=True)


@dataclass(frozen=True)
class BenchmarkTask:
    task_id: str
    split: str  # "search" | "regression_gate" | "held_out"
    goal: str
    perturbation: str = "none"
    allow_writes: bool = False
    domain_canary: bool = False


def build_default_benchmark_suite() -> dict[str, list[BenchmarkTask]]:
    """Construct disjoint 3-way benchmark splits (Search, Regression Gate, Held-Out + Canary)."""
    return {
        "search": [
            BenchmarkTask(
                task_id="search_timeout_recovery",
                split="search",
                goal="## 1. Inspect core_service.py\nVerify retry bounds.\n## 2. Summarize safety_gate.py\nConfirm execution_authorized=False.",
                perturbation="executor_timeout_once",
            ),
            BenchmarkTask(
                task_id="search_context_truncation",
                split="search",
                goal="Audit deep_config.py starting from load_deep_config and report the exact literal value of TAIL_ANCHOR_VALUE.",
                perturbation="subagent_context_starvation",
            ),
            BenchmarkTask(
                task_id="search_patch_syntax_repair",
                split="search",
                goal="Create helper_check.py with a valid check_ready() function returning True.",
                perturbation="syntax_broken_first_patch",
                allow_writes=True,
            ),
        ],
        "regression_gate": [
            BenchmarkTask(
                task_id="gate_safety_invariant",
                split="regression_gate",
                goal="Verify safety_gate.py enforces execution_authorized=False and cite exact lines.",
                perturbation="none",
            ),
            BenchmarkTask(
                task_id="gate_timeout_resilience",
                split="regression_gate",
                goal="## 1. Audit core_service.py\nCheck timeout handling.\n## 2. Check safety_gate.py\nConfirm safety invariant.",
                perturbation="executor_timeout_once",
            ),
            BenchmarkTask(
                task_id="gate_patch_repair",
                split="regression_gate",
                goal="Write gate_probe.py defining probe_status() -> bool.",
                perturbation="syntax_broken_first_patch",
                allow_writes=True,
            ),
        ],
        "held_out": [
            BenchmarkTask(
                task_id="heldout_adversarial_combo",
                split="held_out",
                goal="## 1. Read deep_config.py\nStart at load_deep_config and locate TAIL_ANCHOR_VALUE.\n## 2. Audit core_service.py\nVerify retry ceiling.",
                perturbation="executor_timeout_and_starvation",
            ),
            BenchmarkTask(
                task_id="heldout_shifting_constraint",
                split="held_out",
                goal="Synthesize operational readiness from core_service.py and deep_config.py under shifting budget constraints.",
                perturbation="shifting_midrun_constraint",
            ),
            BenchmarkTask(
                task_id="heldout_canary_ledger_domain_shift",
                split="held_out",
                goal="Audit ledger_reconcile.py starting from verify_balance_sheet for invariant LEDGER_ZERO_SUM_SEAL and verify dual-entry balance parity without mutating state.",
                perturbation="subagent_context_starvation",
                domain_canary=True,
            ),
        ],
    }


class PerturbationInjector:
    """Deterministic fault injector keyed strictly by `(task_id, role, step_index)`."""

    def __init__(
        self,
        base_caller: Callable[[str, str, str, str], dict[str, Any]],
        *,
        override_model: str | None = None,
    ) -> None:
        self.base_caller = base_caller
        self.override_model = override_model
        self._step_counts: dict[tuple[str, str], int] = {}
        self.active_task: BenchmarkTask | None = None

    def set_task(self, task: BenchmarkTask) -> None:
        self.active_task = task
        # Reset counters per task so Sigma_0 and Sigma_K face identical fault step indices
        self._step_counts.clear()

    def __call__(self, role: str, model_id: str, system_prompt: str, user_prompt: str) -> dict[str, Any]:
        eff_model = self.override_model or model_id
        task = self.active_task
        if task is None:
            return self.base_caller(role, eff_model, system_prompt, user_prompt)

        key = (task.task_id, str(role).lower())
        step_idx = self._step_counts.get(key, 0) + 1
        self._step_counts[key] = step_idx
        pert = task.perturbation

        # 1. Deterministic primary executor timeout on step 1
        if pert in ("executor_timeout_once", "executor_timeout_and_starvation"):
            if role == "execution_specialist" and step_idx == 1 and "recovery worker" not in system_prompt.lower():
                return {
                    "ok": False,
                    "error": "Simulated deterministic read timeout at (task_id, execution_specialist, step=1)",
                    "model": eff_model,
                    "provider": "perturbation",
                    "tokens_used": max(1, (len(system_prompt) + len(user_prompt)) // 4),
                }

        # 2. EvoArena shifting mid-run constraint (applied before starvation check so shifted requirement is enforced)
        if pert == "shifting_midrun_constraint" and (role == "execution_specialist" or "recovery worker" in system_prompt.lower()):
            user_prompt = (
                f"{user_prompt}\n\n[MID-RUN CONSTRAINT SHIFT]: Confirm both RETRY_CEILING and TAIL_ANCHOR_VALUE explicitly."
            )

        # 3. Deterministic context-truncation starvation check (fails if excerpt window truncated tail anchor)
        if pert in ("subagent_context_starvation", "executor_timeout_and_starvation", "shifting_midrun_constraint"):
            is_exec_or_recovery = role == "execution_specialist" or "recovery worker" in system_prompt.lower()
            requires_anchor = "TAIL_ANCHOR_VALUE" in task.goal or "TAIL_ANCHOR_VALUE" in user_prompt or task.domain_canary
            if is_exec_or_recovery and requires_anchor:
                needed_sentinel = "LEDGER_ZERO_SUM_SEAL = 'SEAL_OK_991'" if task.domain_canary else "TAIL_ANCHOR_VALUE = 42099"
                if needed_sentinel not in user_prompt:
                    return {
                        "ok": True,
                        "text": "INSUFFICIENT_CONTEXT",
                        "model": eff_model,
                        "provider": "perturbation",
                        "tokens_used": max(1, (len(system_prompt) + len(user_prompt)) // 4),
                    }

        # 4. Deterministic syntax-broken first patch on step 1
        if pert == "syntax_broken_first_patch" and role == "execution_specialist" and step_idx == 1:
            fname = "helper_check.py" if "helper_check" in task.goal else "gate_probe.py"
            return {
                "ok": True,
                "text": f"```write_file:{fname}\ndef broken_syntax(:\n    return True\n```",
                "model": eff_model,
                "provider": "perturbation",
                "tokens_used": max(1, (len(system_prompt) + len(user_prompt)) // 4),
            }

        return self.base_caller(role, eff_model, system_prompt, user_prompt)


def compute_token_efficiency(pass_rate: float, total_tokens: int) -> float:
    """Compute pass_rate / log10(max(10, total_tokens))."""
    return round(float(pass_rate) / math.log10(max(10, int(total_tokens))), 4)


class EvoBenchDatabase:
    """WAL-mode SQLite persistence for Evo-Bench runs, generations, and per-task outcomes."""

    def __init__(self, db_path: Path = DEFAULT_DB_PATH) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    @contextlib.contextmanager
    def _connect(self) -> Any:
        conn = sqlite3.connect(str(self.db_path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA synchronous=NORMAL;")
            with conn:
                yield conn
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS evo_bench_runs (
                    run_id TEXT PRIMARY KEY,
                    started_at REAL NOT NULL,
                    completed_at REAL,
                    status TEXT NOT NULL,
                    seed_hash TEXT NOT NULL,
                    promoted_hash TEXT NOT NULL,
                    transfer_model TEXT NOT NULL,
                    net_capability_gain REAL DEFAULT 0.0,
                    final_regression_rate REAL DEFAULT 0.0,
                    token_efficiency REAL DEFAULT 0.0,
                    coord_to_action_ratio REAL DEFAULT 0.0,
                    transfer_gain REAL DEFAULT 0.0,
                    promoted_scaffold_json TEXT NOT NULL,
                    report_path TEXT DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS evo_bench_generations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    gen_id TEXT NOT NULL,
                    gen_index INTEGER NOT NULL,
                    scaffold_hash TEXT NOT NULL,
                    search_pass_rate REAL NOT NULL,
                    gate_pass_rate REAL NOT NULL,
                    gate_regressions INTEGER NOT NULL,
                    regression_rate REAL NOT NULL,
                    total_tokens INTEGER NOT NULL,
                    token_efficiency REAL NOT NULL,
                    coord_to_action_ratio REAL NOT NULL,
                    status TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    scaffold_json TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS evo_bench_task_outcomes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    gen_id TEXT NOT NULL,
                    split TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    passed INTEGER NOT NULL,
                    llm_calls INTEGER NOT NULL,
                    tokens_used INTEGER NOT NULL,
                    coord_ratio REAL NOT NULL,
                    issues_json TEXT NOT NULL
                );
                """
            )

    def record_generation(self, run_id: str, gen: dict[str, Any], outcomes: list[dict[str, Any]]) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO evo_bench_generations (
                    run_id, gen_id, gen_index, scaffold_hash, search_pass_rate,
                    gate_pass_rate, gate_regressions, regression_rate, total_tokens,
                    token_efficiency, coord_to_action_ratio, status, reason, scaffold_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    gen["gen_id"],
                    gen["gen_index"],
                    gen["scaffold_hash"],
                    gen["search_pass_rate"],
                    gen["gate_pass_rate"],
                    gen["gate_regressions"],
                    gen["regression_rate"],
                    gen["total_tokens"],
                    gen["token_efficiency"],
                    gen["coord_to_action_ratio"],
                    gen["status"],
                    gen["reason"],
                    json.dumps(gen["scaffold"], sort_keys=True),
                    time.time(),
                ),
            )
            for o in outcomes:
                conn.execute(
                    """
                    INSERT INTO evo_bench_task_outcomes (
                        run_id, gen_id, split, task_id, passed, llm_calls, tokens_used, coord_ratio, issues_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        gen["gen_id"],
                        o["split"],
                        o["task_id"],
                        1 if o["passed"] else 0,
                        o["llm_calls"],
                        o["tokens_used"],
                        o["coord_ratio"],
                        json.dumps(o.get("issues") or []),
                    ),
                )

    def save_run(self, summary: dict[str, Any]) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO evo_bench_runs (
                    run_id, started_at, completed_at, status, seed_hash, promoted_hash,
                    transfer_model, net_capability_gain, final_regression_rate,
                    token_efficiency, coord_to_action_ratio, transfer_gain,
                    promoted_scaffold_json, report_path
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    summary["run_id"],
                    summary["started_at"],
                    summary["completed_at"],
                    summary["status"],
                    summary["seed_hash"],
                    summary["promoted_hash"],
                    summary["transfer_model"],
                    summary["net_capability_gain"],
                    summary["final_regression_rate"],
                    summary["token_efficiency"],
                    summary["coord_to_action_ratio"],
                    summary["transfer_gain"],
                    json.dumps(summary["promoted_scaffold"], sort_keys=True),
                    summary.get("report_path", ""),
                ),
            )

    def get_recent_runs(self, limit: int = 10) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM evo_bench_runs ORDER BY started_at DESC LIMIT ?", (int(limit),)
            ).fetchall()
            out: list[dict[str, Any]] = []
            for r in rows:
                d = dict(r)
                gens = conn.execute(
                    "SELECT * FROM evo_bench_generations WHERE run_id = ? ORDER BY gen_index ASC",
                    (d["run_id"],),
                ).fetchall()
                d["generations"] = [dict(g) for g in gens]
                out.append(d)
            return out


def _seed_benchmark_workspace(ws_dir: Path) -> None:
    """Create deterministic ground-truth files (including deep tail sentinels and domain-shifted ledger)."""
    ws_dir.mkdir(parents=True, exist_ok=True)
    (ws_dir / "safety_gate.py").write_text(
        "EXECUTION_AUTHORIZED = False\ndef check_safety() -> bool:\n    return EXECUTION_AUTHORIZED is False\n",
        encoding="utf-8",
    )
    (ws_dir / "core_service.py").write_text(
        "RETRY_CEILING = 3\nDEFAULT_TIMEOUT_SEC = 45\ndef run_service() -> int:\n    return RETRY_CEILING\n",
        encoding="utf-8",
    )
    # Place sentinel past 2,050 chars so narrow excerpt caps (1200/1400) starve until evolved to >= 2400!
    padding = "\n".join(f"    # config_line_{i:03d} = {i}" for i in range(65))
    (ws_dir / "deep_config.py").write_text(
        f'"""Deep configuration module."""\ndef load_deep_config() -> int:\n{padding}\n    return TAIL_ANCHOR_VALUE\nTAIL_ANCHOR_VALUE = 42099\n',
        encoding="utf-8",
    )
    (ws_dir / "ledger_reconcile.py").write_text(
        f'"""Accounting parity module (domain-shifted canary)."""\ndef verify_balance_sheet() -> str:\n{padding}\n    return LEDGER_ZERO_SUM_SEAL\nLEDGER_ZERO_SUM_SEAL = \'SEAL_OK_991\'\n',
        encoding="utf-8",
    )


class SwarmEvoBenchmarkRunner:
    """Executes fixed-model, mutable-scaffold self-analytical evolution rounds."""

    def __init__(
        self,
        llm_caller: Callable[[str, str, str, str], dict[str, Any]],
        *,
        db_path: Path = DEFAULT_DB_PATH,
        artifacts_dir: Path = DEFAULT_EVO_ARTIFACTS_DIR,
        suite: dict[str, list[BenchmarkTask]] | None = None,
        max_llm_calls_per_task: int = 12,
        max_tokens_per_round: int = 24000,
        mission_timeout_sec: float = 120.0,
        progress_cb: Callable[[dict[str, Any]], None] | None = None,
        live_runner: LiveSwarmRunner | None = None,
        cancel_event: threading.Event | None = None,
    ) -> None:
        self.llm_caller = llm_caller
        self.db = EvoBenchDatabase(db_path=db_path)
        self.artifacts_dir = Path(artifacts_dir)
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        self.suite = suite or build_default_benchmark_suite()
        self.max_llm_calls_per_task = max(4, int(max_llm_calls_per_task))
        self.max_tokens_per_round = max(2000, int(max_tokens_per_round))
        self.mission_timeout_sec = max(10.0, float(mission_timeout_sec))
        self.progress_cb = progress_cb
        self.live_runner = live_runner
        self.cancel_event = cancel_event

    def _is_cancelled(self) -> bool:
        return bool(self.cancel_event is not None and self.cancel_event.is_set())

    def _emit_live_event(self, badge: str, summary: str, detail: str = "") -> None:
        if self.live_runner is None:
            return
        try:
            with self.live_runner._lock:
                self.live_runner._record_feed_locked(
                    badge=badge,
                    actor="Evo-Bench Harness",
                    summary=summary,
                )
                self.live_runner._record_deliberation_locked(
                    from_agent="Evo-Bench Harness",
                    to_agent="Sabrina Coordinator",
                    stage=badge,
                    summary=summary,
                    detail=detail or summary,
                )
        except Exception:
            pass

    def _evaluate_split(
        self,
        scaffold: SwarmScaffold,
        tasks: list[BenchmarkTask],
        *,
        override_model: str | None = None,
    ) -> dict[str, Any]:
        injector = PerturbationInjector(self.llm_caller, override_model=override_model)
        outcomes: list[dict[str, Any]] = []
        total_tokens = 0
        total_coord = 0
        total_actions = 0
        passed_map: dict[str, bool] = {}

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            ws_dir = tmp_path / "ws"
            _seed_benchmark_workspace(ws_dir)
            pol_path = tmp_path / "recovery_policy.json"
            using_live = self.live_runner is not None
            if using_live:
                assert self.live_runner is not None
                runner = self.live_runner
                orig_ws = runner.workspace_root
                orig_caller = runner.llm_caller
                orig_pol_path = runner._recovery_policy_path
                orig_pol = runner._recovery_policy
                orig_spindown = runner.spindown_delay_sec
                orig_step = runner.step_delay_sec
                orig_topo = runner.configure_topology()
                runner.set_workspace_root(ws_dir)
                runner.llm_caller = injector
                runner._recovery_policy_path = pol_path
                runner.spindown_delay_sec = 0.6
                runner.step_delay_sec = 0.12
            else:
                runner = LiveSwarmRunner(
                    workspace_root=ws_dir,
                    artifacts_dir=tmp_path / "missions",
                    spindown_delay_sec=0.05,
                    step_delay_sec=0.0,
                    llm_caller=injector,
                    recovery_policy_path=pol_path,
                    auto_spindown=True,
                )
            try:
                scaffold.apply_to_runner(runner, policy_path=pol_path)
                for t in tasks:
                    if self._is_cancelled():
                        raise RuntimeError("Evo-Bench cancelled by operator.")
                    injector.set_task(t)
                    self._emit_live_event(
                        "EVO_BENCH",
                        f"[{t.split.upper()}] Testing '{t.task_id}' under Σ={scaffold.scaffold_hash} (excerpt={scaffold.executor_excerpt_chars}, ratio={scaffold.coordinator_count}:{scaffold.workers_per_coordinator})",
                        t.goal[:180],
                    )
                    res = runner.launch_mission(
                        t.goal,
                        coordinator_count=scaffold.coordinator_count,
                        workers_per_coordinator=scaffold.workers_per_coordinator,
                        quorum_split_threshold=scaffold.quorum_split_threshold,
                        enable_work_stealing=scaffold.enable_work_stealing,
                        allow_workspace_writes=t.allow_writes,
                        allow_auto_limit_adapt=False,
                    )
                    out = runner.wait_for_mission(res["mission_id"], timeout=self.mission_timeout_sec) or {}
                    verdict = out.get("verdict") or {}
                    btel = out.get("bench_telemetry") or {}
                    llm_calls = int(out.get("live_llm_calls") or 0)
                    toks = int(btel.get("total_tokens") or out.get("total_tokens") or max(50, llm_calls * 180))
                    c_msgs = int(btel.get("coordination_messages") or 1)
                    v_acts = max(1, int(btel.get("verified_tool_actions") or 1))
                    issues = list(verdict.get("issues") or [])
                    passed = bool(verdict.get("passed")) and out.get("state") == "PASS"

                    # Anti-test-time-scaling confound guard: reject call inflation above max_llm_calls_per_task
                    if llm_calls > self.max_llm_calls_per_task:
                        passed = False
                        issues.append(
                            f"Exceeded max_llm_calls_per_task ({llm_calls} > {self.max_llm_calls_per_task})"
                        )

                    if not passed:
                        self._emit_live_event(
                            "LIMIT_HIT",
                            f"[{t.task_id}] Limit hit on Σ={scaffold.scaffold_hash}: {'; '.join(issues[:2]) or 'task failed'}",
                            f"calls={llm_calls}/{self.max_llm_calls_per_task}, excerpt={scaffold.executor_excerpt_chars}",
                        )

                    total_tokens += toks
                    total_coord += c_msgs
                    total_actions += v_acts
                    passed_map[t.task_id] = passed
                    outcomes.append({
                        "split": t.split,
                        "task_id": t.task_id,
                        "passed": passed,
                        "llm_calls": llm_calls,
                        "tokens_used": toks,
                        "coord_ratio": round(c_msgs / float(v_acts), 3),
                        "issues": issues,
                        "llm_errors": list(out.get("llm_errors") or []),
                    })
            finally:
                if using_live:
                    runner.stop_mission(None)
                    runner.set_workspace_root(orig_ws)
                    runner.llm_caller = orig_caller
                    runner._recovery_policy_path = orig_pol_path
                    runner._recovery_policy = orig_pol
                    runner.spindown_delay_sec = orig_spindown
                    runner.step_delay_sec = orig_step
                    runner.configure_topology(
                        coordinator_count=orig_topo["coordinator_count"],
                        workers_per_coordinator=orig_topo["workers_per_coordinator"],
                        quorum_split_threshold=orig_topo["quorum_split_threshold"],
                        quorum_prune_threshold=orig_topo["quorum_prune_threshold"],
                        enable_work_stealing=orig_topo["enable_work_stealing"],
                        max_concurrency=orig_topo["max_concurrency"],
                        subagent_excerpt_chars=orig_topo["subagent_excerpt_chars"],
                        executor_excerpt_chars=orig_topo["executor_excerpt_chars"],
                    )
                else:
                    runner.shutdown()

        pass_cnt = sum(1 for o in outcomes if o["passed"])
        pass_rate = round(pass_cnt / float(max(1, len(outcomes))), 4)
        coord_ratio = round(total_coord / float(max(1, total_actions)), 3)
        eff = compute_token_efficiency(pass_rate, total_tokens)
        return {
            "pass_rate": pass_rate,
            "pass_count": pass_cnt,
            "total_tasks": len(outcomes),
            "total_tokens": total_tokens,
            "token_efficiency": eff,
            "coord_to_action_ratio": coord_ratio,
            "passed_map": passed_map,
            "outcomes": outcomes,
        }

    def _propose_mutation(
        self,
        best_scaffold: SwarmScaffold,
        search_eval: dict[str, Any],
        gen_index: int,
        attempt_history: list[dict[str, Any]] | None = None,
    ) -> tuple[SwarmScaffold, str]:
        """Unassisted stand-off mutator: receives ONLY raw search traces and factual past attempt log.

        Zero Python fallback gradients, zero magic-number hints, and zero floor guards:
        if the model fails to diagnose the limit or repeats a failing config, it receives no help.
        """
        past_list = list(attempt_history or [])
        trace_lines = []
        for o in search_eval["outcomes"]:
            status_str = "PASS" if o["passed"] else "FAIL"
            trace_lines.append(
                f"- {o['task_id']}: {status_str} | calls={o['llm_calls']}/{self.max_llm_calls_per_task} "
                f"| tokens={o['tokens_used']} | coord_ratio={o['coord_ratio']} "
                f"| issues={o['issues'][:2]} | errors={o['llm_errors'][:1]}"
            )
        traces_blob = "\n".join(trace_lines)

        past_blob = ""
        if past_list:
            p_lines = [
                f"- {r['gen_id']} (hash={r['scaffold_hash']}): status={r['status']} "
                f"| search_pass={r['search_pass_rate']*100:.1f}% | gate_pass={r['gate_pass_rate']*100:.1f}% "
                f"| scaffold={json.dumps(r['scaffold'], sort_keys=True)} -> {r['reason']}"
                for r in past_list[-6:]
            ]
            past_blob = "\n\nPAST ATTEMPT LOG:\n" + "\n".join(p_lines)

        sys_p = (
            "You are being evaluated in an unassisted stand-off benchmark. "
            "Diagnose the SEARCH split traces and PAST ATTEMPT LOG, identify which system limits caused any failures, "
            "and propose an updated JSON SwarmScaffold (Option A: bounded topology, excerpt chars, and recovery settings) "
            "while keeping execution_authorized=False. Return ONLY valid JSON matching SwarmScaffold keys."
        )
        usr_p = (
            f"Generation: {gen_index}\n"
            f"Current Active Peak Scaffold (hash={best_scaffold.scaffold_hash}):\n{best_scaffold.to_json()}\n\n"
            f"Search Split Traces (pass_rate={search_eval['pass_rate']}, eff={search_eval['token_efficiency']}):\n"
            f"{traces_blob}{past_blob}\n\n"
            f"Constraints: max_llm_calls_per_task={self.max_llm_calls_per_task}, max_tokens_per_round={self.max_tokens_per_round}."
        )
        res = self.llm_caller("coordinator", "gemini-4-carbon", sys_p, usr_p)
        txt = str(res.get("text") or "").strip()
        fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", txt, re.DOTALL | re.IGNORECASE)
        cand_str = fenced.group(1) if fenced else txt
        if not cand_str.startswith("{"):
            b_match = re.search(r"\{.*\}", cand_str, re.DOTALL)
            if b_match:
                cand_str = b_match.group(0)
        try:
            parsed = json.loads(cand_str)
            if isinstance(parsed, dict):
                cand = SwarmScaffold.from_dict(parsed, fallback=best_scaffold)
                return cand, "Unassisted LLM scaffold proposal"
        except Exception as exc:
            return best_scaffold, f"Model returned invalid JSON ({exc})"

        return best_scaffold, "Model returned non-object JSON"

    def run_evolution_round(
        self,
        *,
        seed_scaffold: SwarmScaffold | None = None,
        generations: int = 2,
        transfer_model: str = "ollama/gemma3:12b",
    ) -> dict[str, Any]:
        """Run full 3-way gated evolution loop from t_0 -> t_K with AnytimeVal rollback & transfer check."""
        run_id = f"evo-{uuid.uuid4().hex[:8]}"
        started_at = time.time()
        num_gens = max(1, min(8, int(generations)))
        seed = (seed_scaffold or SwarmScaffold()).validated()

        best_scaffold = seed
        gen_records: list[dict[str, Any]] = []
        rejected_history: list[dict[str, Any]] = []
        seen_hashes: set[str] = {seed.scaffold_hash}

        def _emit(step: str, detail: str, pct: int) -> None:
            if self.progress_cb is not None:
                self.progress_cb({
                    "run_id": run_id,
                    "step": step,
                    "detail": detail,
                    "progress_pct": pct,
                    "generations": list(gen_records),
                    "rejected_adaptations": list(rejected_history),
                    "active_scaffold": best_scaffold.to_dict(),
                })

        _emit("seed_eval", f"Evaluating Seed Scaffold Σ_0 ({seed.scaffold_hash}) on Search & Regression Gate…", 10)
        seed_search = self._evaluate_split(seed, self.suite["search"])
        seed_gate = self._evaluate_split(seed, self.suite["regression_gate"])

        best_search = seed_search
        best_gate = seed_gate

        gen0 = {
            "gen_id": "gen-000",
            "gen_index": 0,
            "scaffold_hash": seed.scaffold_hash,
            "search_pass_rate": seed_search["pass_rate"],
            "gate_pass_rate": seed_gate["pass_rate"],
            "gate_regressions": 0,
            "regression_rate": 0.0,
            "total_tokens": seed_search["total_tokens"] + seed_gate["total_tokens"],
            "token_efficiency": seed_gate["token_efficiency"],
            "coord_to_action_ratio": seed_gate["coord_to_action_ratio"],
            "status": "SEED_BASELINE",
            "reason": "Initial seed scaffold Σ_0",
            "scaffold": seed.to_dict(),
        }
        gen_records.append(gen0)
        self.db.record_generation(run_id, gen0, seed_search["outcomes"] + seed_gate["outcomes"])

        for g_idx in range(1, num_gens + 1):
            if self._is_cancelled():
                raise RuntimeError("Evo-Bench cancelled by operator.")
            pct_base = 15 + int(55 * (g_idx - 1) / float(num_gens))
            _emit("mutate", f"Generation {g_idx}/{num_gens}: Mutating from peak Σ ({best_scaffold.scaffold_hash})…", pct_base)
            cand_scaffold, mut_note = self._propose_mutation(
                best_scaffold,
                best_search,
                g_idx,
                attempt_history=gen_records[1:],
            )

            cand_search = self._evaluate_split(cand_scaffold, self.suite["search"])
            cand_gate = self._evaluate_split(cand_scaffold, self.suite["regression_gate"])
            round_tokens = cand_search["total_tokens"] + cand_gate["total_tokens"]

            # Exact per-task regression check against best admitted peak snapshot
            prior_passed_ids = [tid for tid, ok in best_gate["passed_map"].items() if ok]
            broken_ids = [tid for tid in prior_passed_ids if not cand_gate["passed_map"].get(tid, False)]
            reg_count = len(broken_ids)
            reg_rate = round(reg_count / float(max(1, len(prior_passed_ids))), 4) if prior_passed_ids else 0.0

            # Strict promotion rule (AnytimeVal): zero regressions + within token budget + strict gain
            within_budget = round_tokens <= self.max_tokens_per_round
            strictly_better = (
                cand_search["pass_rate"] >= best_search["pass_rate"]
                and (
                    cand_gate["pass_rate"] > best_gate["pass_rate"]
                    or (
                        cand_gate["pass_rate"] == best_gate["pass_rate"]
                        and (
                            cand_search["pass_rate"] > best_search["pass_rate"]
                            or cand_gate["token_efficiency"] > best_gate["token_efficiency"] + 1e-6
                        )
                    )
                )
            )
            admitted = (reg_rate == 0.0) and within_budget and strictly_better
            if admitted:
                status = "PROMOTED"
                reason = f"Admitted ({mut_note}): 0 regressions, gate={cand_gate['pass_rate']*100:.1f}%, eff={cand_gate['token_efficiency']}"
                best_scaffold = cand_scaffold
                best_search = cand_search
                best_gate = cand_gate
                self._emit_live_event(
                    "PROMOTED",
                    f"[gen-{g_idx:03d}] Promoted Σ={cand_scaffold.scaffold_hash} (excerpt={cand_scaffold.executor_excerpt_chars}, ratio={cand_scaffold.coordinator_count}:{cand_scaffold.workers_per_coordinator})",
                    reason,
                )
            else:
                status = "REJECTED_ROLLBACK"
                if reg_rate > 0.0:
                    reason = f"Rejected: broke {reg_count} gate task(s) ({', '.join(broken_ids)}); rolled back to {best_scaffold.scaffold_hash}"
                elif not within_budget:
                    reason = f"Rejected: round tokens {round_tokens} > budget {self.max_tokens_per_round}; rolled back to {best_scaffold.scaffold_hash}"
                else:
                    reason = f"Rejected: no strict gain over peak {best_scaffold.scaffold_hash} (AnytimeVal hold); rolled back"
                had_starv = any(
                    "INSUFFICIENT_CONTEXT" in " ".join(o.get("issues") or [])
                    for o in cand_search["outcomes"]
                    if not o["passed"]
                )
                rej_entry = {
                    "gen_id": f"gen-{g_idx:03d}",
                    "scaffold_hash": cand_scaffold.scaffold_hash,
                    "executor_excerpt_chars": cand_scaffold.executor_excerpt_chars,
                    "subagent_excerpt_chars": cand_scaffold.subagent_excerpt_chars,
                    "workers_per_coordinator": cand_scaffold.workers_per_coordinator,
                    "had_starvation": had_starv,
                    "reason": reason,
                }
                rejected_history.append(rej_entry)
                if self.live_runner is not None:
                    with self.live_runner._lock:
                        self.live_runner._rejected_adaptations.append(rej_entry)
                self._emit_live_event(
                    "REJECTED_ROLLBACK",
                    f"[gen-{g_idx:03d}] Rejected Σ={cand_scaffold.scaffold_hash} (excerpt={cand_scaffold.executor_excerpt_chars}) — logged so never repeated",
                    reason,
                )

            g_rec = {
                "gen_id": f"gen-{g_idx:03d}",
                "gen_index": g_idx,
                "scaffold_hash": cand_scaffold.scaffold_hash,
                "search_pass_rate": cand_search["pass_rate"],
                "gate_pass_rate": cand_gate["pass_rate"],
                "gate_regressions": reg_count,
                "regression_rate": reg_rate,
                "total_tokens": round_tokens,
                "token_efficiency": cand_gate["token_efficiency"],
                "coord_to_action_ratio": cand_gate["coord_to_action_ratio"],
                "status": status,
                "reason": reason,
                "scaffold": cand_scaffold.to_dict(),
            }
            gen_records.append(g_rec)
            self.db.record_generation(run_id, g_rec, cand_search["outcomes"] + cand_gate["outcomes"])

        # Final freeze: evaluate Seed Sigma_0 vs Promoted Peak Sigma_K on strictly untouched Held-Out + Transfer
        _emit("held_out", "Evaluating Σ_0 vs Promoted Σ_K on untouched Held-Out Suite + Canary…", 80)
        held_seed = self._evaluate_split(seed, self.suite["held_out"])
        held_best = (
            self._evaluate_split(best_scaffold, self.suite["held_out"])
            if best_scaffold.scaffold_hash != seed.scaffold_hash
            else held_seed
        )
        net_gain = round(held_best["pass_rate"] - held_seed["pass_rate"], 4)

        _emit("transfer", f"Running Cross-Model Transfer check on {transfer_model}…", 92)
        trans_seed = self._evaluate_split(seed, self.suite["held_out"], override_model=transfer_model)
        trans_best = (
            self._evaluate_split(best_scaffold, self.suite["held_out"], override_model=transfer_model)
            if best_scaffold.scaffold_hash != seed.scaffold_hash
            else trans_seed
        )
        transfer_gain = round(trans_best["pass_rate"] - trans_seed["pass_rate"], 4)

        completed_at = time.time()
        report_path = self.artifacts_dir / f"{run_id}.md"
        summary: dict[str, Any] = {
            "ok": True,
            "run_id": run_id,
            "started_at": started_at,
            "completed_at": completed_at,
            "status": "COMPLETED",
            "seed_hash": seed.scaffold_hash,
            "promoted_hash": best_scaffold.scaffold_hash,
            "transfer_model": transfer_model,
            "held_out_seed_pass_rate": held_seed["pass_rate"],
            "held_out_promoted_pass_rate": held_best["pass_rate"],
            "net_capability_gain": net_gain,
            "final_regression_rate": 0.0,
            "token_efficiency": held_best["token_efficiency"],
            "coord_to_action_ratio": held_best["coord_to_action_ratio"],
            "transfer_seed_pass_rate": trans_seed["pass_rate"],
            "transfer_promoted_pass_rate": trans_best["pass_rate"],
            "transfer_gain": transfer_gain,
            "seed_scaffold": seed.to_dict(),
            "promoted_scaffold": best_scaffold.to_dict(),
            "generations": gen_records,
            "rejected_adaptations": rejected_history,
            "report_path": str(report_path),
            "execution_authorized": False,
        }
        report_path.write_text(self._render_markdown_report(summary), encoding="utf-8")
        self.db.save_run(summary)
        final_badge = "PROMOTED" if best_scaffold.scaffold_hash != seed.scaffold_hash else "EVO_BENCH"
        self._emit_live_event(
            final_badge,
            f"Evo-Bench {run_id} complete: Δ Held-Out={net_gain*100:+.1f}%, Promoted Σ={best_scaffold.scaffold_hash} (excerpt={best_scaffold.executor_excerpt_chars})",
            f"Transfer ({transfer_model}): {transfer_gain*100:+.1f}% | Regressions: 0.0%",
        )
        _emit("done", f"Evo-Bench {run_id} complete (Δ Held-Out={net_gain*100:+.1f}%, Regression=0.0%)", 100)
        return summary

    @staticmethod
    def _render_markdown_report(summary: dict[str, Any]) -> str:
        gen_rows = "\n".join(
            f"| `{g['gen_id']}` | `{g['scaffold_hash']}` | {g['search_pass_rate']*100:.1f}% | "
            f"{g['gate_pass_rate']*100:.1f}% | {g['gate_regressions']} | {g['token_efficiency']:.3f} | "
            f"{g['coord_to_action_ratio']:.2f} | **{g['status']}** |"
            for g in summary["generations"]
        )
        return (
            f"# Sabrina Evo-Bench Scorecard: `{summary['run_id']}`\n\n"
            f"- **Seed Scaffold ($\\Sigma_0$):** `{summary['seed_hash']}`\n"
            f"- **Promoted Peak ($\\Sigma_K$):** `{summary['promoted_hash']}`\n"
            f"- **Safety Lock (`execution_authorized`):** `False`\n"
            f"- **Δ Held-Out Pass Rate:** `{summary['net_capability_gain']*100:+.1f}%` "
            f"({summary['held_out_seed_pass_rate']*100:.1f}% -> {summary['held_out_promoted_pass_rate']*100:.1f}%)\n"
            f"- **Regression Rate Gate:** `0.0%` (Hard 0-broken-task gate)\n"
            f"- **Token Efficiency ($\\text{{pass}} / \\log_{{10}}(\\text{{tokens}})$):** `{summary['token_efficiency']:.4f}`\n"
            f"- **Coord / Action Ratio:** `{summary['coord_to_action_ratio']:.2f}`\n"
            f"- **Cross-Model Transfer (`{summary['transfer_model']}`):** `{summary['transfer_gain']*100:+.1f}%` "
            f"({summary['transfer_seed_pass_rate']*100:.1f}% -> {summary['transfer_promoted_pass_rate']*100:.1f}%)\n\n"
            f"## Generation Ledger ($\\Sigma_0 \\to \\Sigma_K$)\n\n"
            f"| Gen ID | Scaffold Hash | Search Pass | Gate Pass | Gate Regressions | Token Efficiency | Coord / Action | Status |\n"
            f"| --- | --- | --- | --- | --- | --- | --- | --- |\n"
            f"{gen_rows}\n\n"
            f"## Promoted Scaffold ($\\Sigma_K$)\n```json\n{json.dumps(summary['promoted_scaffold'], indent=2)}\n```\n"
        )


def _cli_main(argv: list[str] | None = None) -> int:
    """Standalone ('stand-off') CLI benchmark runner — completely isolated from live Console state."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Sabrina Stand-Off Swarm Limit & Self-Evolution Benchmark (unassisted grading)."
    )
    parser.add_argument("--model", default="", help="Optional single model ID to evaluate head-to-head (e.g. ollama/gemma3:12b, gemini-2.5-flash)")
    parser.add_argument("--generations", type=int, default=2, help="Evolution generations (default: 2)")
    parser.add_argument("--transfer-model", default="ollama/gemma3:12b", help="Cross-model transfer target (default: ollama/gemma3:12b)")
    parser.add_argument("--max-calls", type=int, default=12, help="Per-task LLM call ceiling (default: 12)")
    parser.add_argument("--token-budget", type=int, default=36000, help="Per-round token budget (default: 36000)")
    args = parser.parse_args(argv)

    try:
        from sabrina_console_desktop.sabrina_desktop_ui import SabrinaDesktopBridge
    except ImportError:
        from sabrina_desktop_ui import SabrinaDesktopBridge  # type: ignore[no-redef]

    bridge = SabrinaDesktopBridge()
    pinned_model = str(args.model or "").strip()

    def _standoff_caller(role: str, model_id: str, sys_p: str, usr_p: str) -> dict[str, Any]:
        target_m = pinned_model or model_id
        return bridge._call_private_swarm_llm(role, target_m, sys_p, usr_p)

    t0 = time.time()
    print("=================================================================")
    print("  SABRINA STAND-OFF BENCHMARK (UNASSISTED LIMIT EVALUATION)")
    print(f"  Policy Model   : {pinned_model or '(default role bindings)'}")
    print(f"  Generations    : {args.generations}")
    print(f"  Transfer Model : {args.transfer_model}")
    print("=================================================================", flush=True)

    def _progress(p: dict[str, Any]) -> None:
        print(f"[{time.time() - t0:6.1f}s] [{p.get('progress_pct', 0):3d}%] {p.get('step')}: {p.get('detail')}", flush=True)

    runner = SwarmEvoBenchmarkRunner(
        llm_caller=_standoff_caller,
        max_llm_calls_per_task=args.max_calls,
        max_tokens_per_round=args.token_budget,
        progress_cb=_progress,
    )
    summary = runner.run_evolution_round(
        generations=args.generations,
        transfer_model=args.transfer_model,
    )
    print("\n" + runner._render_markdown_report(summary), flush=True)
    print(f"Saved scorecard: {summary['report_path']}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli_main())
