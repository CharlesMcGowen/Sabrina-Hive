"""Performance view component for Sabrina Operator Console.

Displays coverage, model/configuration comparisons, task-level failures,
tool-path reliability, and links to supporting events.
Distinguishes LIVE EXECUTION, REPLAY, and SIMULATION modes.
"""
from __future__ import annotations

import datetime as dt
import json
import tkinter as tk
from tkinter import ttk
from typing import Any, Callable, Dict, List, Optional


class PerformanceView:
    """Tkinter frame and controller for the Operator Console Performance tab."""

    def __init__(
        self,
        console: Any,
        parent: ttk.Frame,
        on_select_run: Optional[Callable[[str], None]] = None,
    ):
        self.console = console
        self.parent = parent
        self.on_select_run = on_select_run
        self._snapshot_data: Dict[str, Any] = {}
        self._build_ui()

    def _build_ui(self) -> None:
        top_bar = ttk.Frame(self.parent)
        top_bar.pack(fill="x", padx=8, pady=(8, 4))

        self.cards = {}
        for name in ("Total Trials", "Evaluated Configs", "Qualified Categories", "Tool Reliability"):
            card = ttk.Frame(top_bar, style="Card.TFrame", padding=8)
            card.pack(side="left", fill="both", expand=True, padx=4)
            ttk.Label(card, text=name, style="Card.TLabel", font=("Segoe UI", 9)).pack(anchor="w")
            lbl = ttk.Label(card, text="—", style="Card.TLabel", font=("Segoe UI", 12, "bold"))
            lbl.pack(anchor="w", pady=(2, 0))
            self.cards[name] = lbl

        # Legend indicator
        legend_frame = ttk.Frame(self.parent)
        legend_frame.pack(fill="x", padx=12, pady=(2, 6))
        ttk.Label(
            legend_frame,
            text="Evidence Modes:  [LIVE EXECUTION]  ·  [REPLAY]  ·  [SIMULATION]  |  Independent grading; think=true/false tracked separately",
            font=("Segoe UI", 8),
            foreground="#9ca3af",
        ).pack(side="left")

        # Sub-notebook for Performance views
        sub_book = ttk.Notebook(self.parent)
        sub_book.pack(fill="both", expand=True, padx=8, pady=(0, 8))

        tab_comparisons = ttk.Frame(sub_book)
        tab_failures = ttk.Frame(sub_book)
        tab_inventory = ttk.Frame(sub_book)

        sub_book.add(tab_comparisons, text="Model Comparisons & Qualifications")
        sub_book.add(tab_failures, text="Task Failures & Tool Reliability")
        sub_book.add(tab_inventory, text="Model Inventory & Supporting Events")

        self._build_comparisons_tab(tab_comparisons)
        self._build_failures_tab(tab_failures)
        self._build_inventory_tab(tab_inventory)

    def _build_comparisons_tab(self, parent: ttk.Frame) -> None:
        panes = ttk.PanedWindow(parent, orient="vertical")
        panes.pack(fill="both", expand=True, padx=4, pady=4)

        upper = ttk.Frame(panes)
        panes.add(upper, weight=3)
        ttk.Label(
            upper,
            text="Model / Configuration Comparisons (Separated by result category, think setting, and mode)",
            font=("Segoe UI", 9, "bold"),
        ).pack(anchor="w", pady=(2, 4))

        self.tree_comparisons = ttk.Treeview(
            upper,
            columns=(
                "mode",
                "result_cat",
                "model",
                "think",
                "trials",
                "acc",
                "ci95",
                "op_fail",
                "latency",
                "tool_val",
                "recovery",
            ),
            show="headings",
        )
        col_defs = (
            ("mode", "Mode", 95),
            ("result_cat", "Category", 140),
            ("model", "Model / App", 180),
            ("think", "Think", 65),
            ("trials", "Trials (G/T)", 85),
            ("acc", "Verified Acc", 90),
            ("ci95", "95% CI", 100),
            ("op_fail", "Op Block %", 85),
            ("latency", "Latency (s)", 80),
            ("tool_val", "Tool Valid", 80),
            ("recovery", "Recovery", 80),
        )
        for col, label, width in col_defs:
            self.tree_comparisons.heading(col, text=label)
            self.tree_comparisons.column(col, width=width)
        self.tree_comparisons.pack(fill="both", expand=True)

        self.tree_comparisons.tag_configure("live", foreground="#86efac")
        self.tree_comparisons.tag_configure("replay", foreground="#c084fc")
        self.tree_comparisons.tag_configure("sim", foreground="#93c5fd")

        lower = ttk.Frame(panes)
        panes.add(lower, weight=2)
        ttk.Label(
            lower,
            text="Task Category Qualifications (Gated: >=80% on 6 held-out tasks x3 + 0 scope violations + 3 live tasks)",
            font=("Segoe UI", 9, "bold"),
        ).pack(anchor="w", pady=(4, 4))

        self.tree_qualifications = ttk.Treeview(
            lower,
            columns=(
                "category",
                "role",
                "model",
                "think",
                "status",
                "pass_rate",
                "ci95",
                "critical",
                "live_verified",
                "latency",
            ),
            show="headings",
        )
        qual_defs = (
            ("category", "Task Category", 170),
            ("role", "Role", 90),
            ("model", "Model Config", 170),
            ("think", "Think", 65),
            ("status", "Qualified?", 110),
            ("pass_rate", "Pass Rate", 85),
            ("ci95", "95% CI", 95),
            ("critical", "Critical Scope", 95),
            ("live_verified", "Live Verified", 85),
            ("latency", "Mean Latency", 90),
        )
        for col, label, width in qual_defs:
            self.tree_qualifications.heading(col, text=label)
            self.tree_qualifications.column(col, width=width)
        self.tree_qualifications.pack(fill="both", expand=True)
        self.tree_qualifications.tag_configure("qualified", foreground="#86efac")
        self.tree_qualifications.tag_configure("pending", foreground="#fcd34d")

    def _build_failures_tab(self, parent: ttk.Frame) -> None:
        panes = ttk.PanedWindow(parent, orient="horizontal")
        panes.pack(fill="both", expand=True, padx=4, pady=4)

        left = ttk.Frame(panes)
        panes.add(left, weight=1)
        ttk.Label(
            left,
            text="Task-Level Failures (Separating operational blocks from logic errors)",
            font=("Segoe UI", 9, "bold"),
        ).pack(anchor="w", pady=(2, 4))

        self.tree_failures = ttk.Treeview(
            left,
            columns=("task_id", "category", "total", "op_blocked", "logic_fail", "failure_rate"),
            show="headings",
        )
        fail_defs = (
            ("task_id", "Task ID", 150),
            ("category", "Category", 140),
            ("total", "Trials", 60),
            ("op_blocked", "Op Blocked", 80),
            ("logic_fail", "Logic Fails", 80),
            ("failure_rate", "Failure Rate", 85),
        )
        for col, label, width in fail_defs:
            self.tree_failures.heading(col, text=label)
            self.tree_failures.column(col, width=width)
        self.tree_failures.pack(fill="both", expand=True)

        right = ttk.Frame(panes)
        panes.add(right, weight=1)
        ttk.Label(
            right,
            text="Tool-Path Reliability (Validity, Recovery, & Execution Timing)",
            font=("Segoe UI", 9, "bold"),
        ).pack(anchor="w", pady=(2, 4))

        self.tree_tools = ttk.Treeview(
            right,
            columns=("tool_name", "total_calls", "valid_rate", "recovery_rate", "avg_duration"),
            show="headings",
        )
        tool_defs = (
            ("tool_name", "Tool Path", 150),
            ("total_calls", "Invocations", 85),
            ("valid_rate", "Validity Rate", 90),
            ("recovery_rate", "Recovery Rate", 95),
            ("avg_duration", "Avg Duration", 85),
        )
        for col, label, width in tool_defs:
            self.tree_tools.heading(col, text=label)
            self.tree_tools.column(col, width=width)
        self.tree_tools.pack(fill="both", expand=True)

    def _build_inventory_tab(self, parent: ttk.Frame) -> None:
        panes = ttk.PanedWindow(parent, orient="vertical")
        panes.pack(fill="both", expand=True, padx=4, pady=4)

        upper = ttk.Frame(panes)
        panes.add(upper, weight=2)
        ttk.Label(
            upper,
            text="Model Inventory & Access Status (Lab Ollama & Subscription-Backed Apps)",
            font=("Segoe UI", 9, "bold"),
        ).pack(anchor="w", pady=(2, 4))

        self.tree_inventory = ttk.Treeview(
            upper,
            columns=("model_id", "provider", "access_status", "capabilities", "measured_threads", "last_verified"),
            show="headings",
        )
        inv_defs = (
            ("model_id", "Model / Application ID", 220),
            ("provider", "Provider / Channel", 140),
            ("access_status", "Access Status", 110),
            ("capabilities", "Capabilities", 170),
            ("measured_threads", "CPU Preset", 90),
            ("last_verified", "Last Verified", 140),
        )
        for col, label, width in inv_defs:
            self.tree_inventory.heading(col, text=label)
            self.tree_inventory.column(col, width=width)
        self.tree_inventory.pack(fill="both", expand=True)
        self.tree_inventory.tag_configure("active", foreground="#86efac")
        self.tree_inventory.tag_configure("pending", foreground="#fcd34d")
        self.tree_inventory.tag_configure("unavailable", foreground="#fca5a5")

        lower = ttk.Frame(panes)
        panes.add(lower, weight=3)
        ttk.Label(
            lower,
            text="Supporting Events & Recent Trials (Double-click or press Enter to inspect in Decision Trace)",
            font=("Segoe UI", 9, "bold"),
        ).pack(anchor="w", pady=(4, 4))

        self.tree_trials = ttk.Treeview(
            lower,
            columns=("trial_id", "category", "model", "mode", "outcome", "latency", "run_id", "time"),
            show="headings",
        )
        trial_defs = (
            ("trial_id", "Trial ID", 140),
            ("category", "Task Category", 140),
            ("model", "Model Config", 160),
            ("mode", "Mode", 80),
            ("outcome", "Outcome Status", 110),
            ("latency", "Total Time (s)", 90),
            ("run_id", "Linked Run ID", 140),
            ("time", "Timestamp", 140),
        )
        for col, label, width in trial_defs:
            self.tree_trials.heading(col, text=label)
            self.tree_trials.column(col, width=width)
        self.tree_trials.pack(fill="both", expand=True)
        self.tree_trials.bind("<Double-1>", self._on_trial_double_click)
        self.tree_trials.bind("<Return>", self._on_trial_double_click)

        self.tree_trials.tag_configure("passed", foreground="#86efac")
        self.tree_trials.tag_configure("failed", foreground="#fca5a5")
        self.tree_trials.tag_configure("blocked", foreground="#fcd34d")

    def _on_trial_double_click(self, event=None) -> None:
        sel = self.tree_trials.selection()
        if not sel:
            return
        item = self.tree_trials.item(sel[0])
        values = item.get("values", [])
        if len(values) >= 7:
            run_id = str(values[6])
            if run_id and run_id != "—" and self.on_select_run:
                self.on_select_run(run_id)

    def update(self, snapshot: Dict[str, Any]) -> None:
        """Update all widgets from the performance snapshot."""
        if not snapshot:
            return
        self._snapshot_data = snapshot

        # Update cards
        comps = snapshot.get("config_comparisons", [])
        quals = snapshot.get("qualifications", [])
        trials = snapshot.get("recent_trials", [])
        tools = snapshot.get("tool_reliability", [])

        total_trials_count = len(trials)
        self.cards["Total Trials"].configure(text=str(total_trials_count))
        self.cards["Evaluated Configs"].configure(text=str(len(comps)))

        qualified_count = sum(1 for q in quals if q.get("qualified"))
        self.cards["Qualified Categories"].configure(
            text=f"{qualified_count} / {len(quals)}" if quals else "0"
        )

        if tools:
            avg_tool_val = sum(float(t.get("valid_rate", 0)) for t in tools) / len(tools)
            self.cards["Tool Reliability"].configure(text=f"{avg_tool_val * 100:.1f}%")
        else:
            self.cards["Tool Reliability"].configure(text="—")

        # Update Model Comparisons Tree
        for item in self.tree_comparisons.get_children():
            self.tree_comparisons.delete(item)
        for c in comps:
            mode = c.get("execution_mode", "LIVE").upper()
            tag = "live" if "LIVE" in mode else ("replay" if "REPLAY" in mode else "sim")
            acc_str = f"{c.get('verified_accuracy', 0.0) * 100:.1f}%" if c.get('verified_accuracy') is not None else "—"
            ci = c.get("ci95", [0.0, 1.0])
            ci_str = f"[{ci[0]*100:.0f}%, {ci[1]*100:.0f}%]"
            op_fail_str = f"{c.get('operational_failure_rate', 0.0) * 100:.1f}%"
            tool_val = f"{c.get('avg_tool_validity', 0.0) * 100:.0f}%" if c.get('avg_tool_validity') is not None else "—"
            rec_val = f"{c.get('avg_recovery_success', 0.0) * 100:.0f}%" if c.get('avg_recovery_success') is not None else "—"
            trials_str = f"{c.get('graded_trials', 0)} / {c.get('total_trials', 0)}"

            self.tree_comparisons.insert(
                "",
                "end",
                values=(
                    f"[{mode}]",
                    c.get("result_category", "unknown"),
                    f"{c.get('model_id', 'unknown')} ({c.get('app_name', 'lab')})",
                    c.get("think_setting", "—"),
                    trials_str,
                    acc_str,
                    ci_str,
                    op_fail_str,
                    f"{c.get('avg_total_seconds', 0.0):.2f}",
                    tool_val,
                    rec_val,
                ),
                tags=(tag,),
            )

        # Update Qualifications Tree
        for item in self.tree_qualifications.get_children():
            self.tree_qualifications.delete(item)
        for q in quals:
            is_qual = bool(q.get("qualified"))
            status_str = "QUALIFIED (>=80%)" if is_qual else "PENDING"
            tag = "qualified" if is_qual else "pending"
            pass_rate = f"{float(q.get('benchmark_pass_rate', 0.0)) * 100:.1f}%"
            ci_str = f"[{float(q.get('ci_lower', 0.0))*100:.0f}%, {float(q.get('ci_upper', 1.0))*100:.0f}%]"
            crit_str = "PASS" if q.get("critical_scope_passed") else "FAIL"
            live_str = f"{q.get('verified_live_tasks', 0)} / 3"
            lat_str = f"{float(q.get('mean_total_seconds', 0.0)):.2f}s"

            self.tree_qualifications.insert(
                "",
                "end",
                values=(
                    q.get("category", "unknown"),
                    q.get("role", "execution"),
                    f"{q.get('model_id', 'unknown')} [{q.get('config_key', '')[:8]}]",
                    q.get("think_setting", "—"),
                    status_str,
                    pass_rate,
                    ci_str,
                    crit_str,
                    live_str,
                    lat_str,
                ),
                tags=(tag,),
            )

        # Update Task Failures Tree
        for item in self.tree_failures.get_children():
            self.tree_failures.delete(item)
        for f in snapshot.get("task_failures", []):
            total = int(f.get("total_trials", 0))
            op_b = int(f.get("operational_blocked", 0))
            log_f = int(f.get("logic_failed", 0))
            rate = f"{float(f.get('failure_rate', 0.0)) * 100:.1f}%"
            self.tree_failures.insert(
                "",
                "end",
                values=(f.get("task_id", "unknown"), f.get("category", "—"), total, op_b, log_f, rate),
            )

        # Update Tool Reliability Tree
        for item in self.tree_tools.get_children():
            self.tree_tools.delete(item)
        for t in tools:
            v_rate = f"{float(t.get('valid_rate', 0.0)) * 100:.1f}%"
            r_rate = f"{float(t.get('recovery_rate', 0.0)) * 100:.1f}%"
            dur = f"{float(t.get('avg_duration_seconds', 0.0)):.2f}s"
            self.tree_tools.insert(
                "",
                "end",
                values=(t.get("tool_name", "unknown"), t.get("total_calls", 0), v_rate, r_rate, dur),
            )

        # Update Inventory Tree
        for item in self.tree_inventory.get_children():
            self.tree_inventory.delete(item)
        for inv in snapshot.get("inventory", []):
            status = str(inv.get("access_status", "unknown")).lower()
            tag = "active" if "verified" in status or "active" in status else ("pending" if "pending" in status else "unavailable")
            caps = inv.get("capabilities", "text")
            stamp = dt.datetime.fromtimestamp(inv.get("last_verified", 0)).strftime("%Y-%m-%d %H:%M") if inv.get("last_verified") else "—"
            self.tree_inventory.insert(
                "",
                "end",
                values=(
                    inv.get("model_id", "unknown"),
                    inv.get("provider", "lab"),
                    inv.get("access_status", "unknown").upper(),
                    caps,
                    inv.get("cpu_preset_threads") or "GPU / cloud",
                    stamp,
                ),
                tags=(tag,),
            )

        # Update Trials Tree
        for item in self.tree_trials.get_children():
            self.tree_trials.delete(item)
        for tr in trials:
            outcome = tr.get("outcome_status", "unknown")
            tag = "passed" if outcome == "passed" else ("blocked" if outcome not in ("passed", "failed") else "failed")
            lat = f"{float(tr.get('total_seconds', 0.0)):.2f}"
            stamp = dt.datetime.fromtimestamp(tr.get("created_at", 0)).strftime("%m-%d %H:%M:%S") if tr.get("created_at") else "—"
            self.tree_trials.insert(
                "",
                "end",
                values=(
                    tr.get("trial_id", "—"),
                    tr.get("task_category", "—"),
                    tr.get("model_id", "—"),
                    tr.get("execution_mode", "LIVE").upper(),
                    outcome.upper(),
                    lat,
                    tr.get("run_id") or "—",
                    stamp,
                ),
                tags=(tag,),
            )
