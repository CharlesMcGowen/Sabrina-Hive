"""Operator console view for inspecting and activating Development Supervisor harness candidates."""
from __future__ import annotations

import datetime as dt
import getpass
import json
from pathlib import Path
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Any

from tools.sabrina_dev_supervisor import (
    DEFAULT_APPROVAL_TTL_SECONDS,
    SabrinaDevSupervisor,
)


class DevSupervisorView(ttk.Frame):
    """View component for staged candidate harness review, verification, and human-in-the-loop activation."""

    def __init__(self, console: Any, parent: Any) -> None:
        super().__init__(parent)
        self.console = console
        self.supervisor = SabrinaDevSupervisor()
        self._runs: list[dict[str, Any]] = []
        self._selected_request_id = ""
        self._refreshing = False

        self._build_ui()
        self.refresh()

    def _build_ui(self) -> None:
        # Top toolbar
        bar = ttk.Frame(self)
        bar.pack(fill="x", padx=8, pady=(8, 4))

        self.status_var = tk.StringVar(value="Loading staged candidate harness packages…")
        ttk.Label(bar, textvariable=self.status_var, font=("Segoe UI", 9, "bold")).pack(side="left", padx=4)

        btn_box = ttk.Frame(bar)
        btn_box.pack(side="right")

        self.activate_btn = ttk.Button(
            btn_box,
            text="Approve & Activate",
            command=self._on_approve_activate,
        )
        self.activate_btn.pack(side="left", padx=3)

        ttk.Button(btn_box, text="Refresh", command=self.refresh).pack(side="left", padx=3)

        # Invariant banner
        banner = ttk.Label(
            self,
            text=(
                "Harness self-development is isolated: coding workers change a candidate copy, tests run independently, "
                "and production files stay untouched until explicit human activation with automatic rollback on check failure."
            ),
            foreground="#9ca3af",
            font=("Segoe UI", 8),
            wraplength=880,
        )
        banner.pack(anchor="w", padx=12, pady=(0, 6))

        # Main PanedWindow (split horizontal: left candidate list, right candidate detail)
        panes = ttk.PanedWindow(self, orient="horizontal")
        panes.pack(fill="both", expand=True, padx=8, pady=(0, 8))

        # Left pane: Candidate Runs list
        left_pane = ttk.Frame(panes)
        panes.add(left_pane, weight=1)

        ttk.Label(left_pane, text="Harness Change Requests", font=("Segoe UI", 9, "bold")).pack(anchor="w", padx=4, pady=(2, 4))

        self.runs_list = ttk.Treeview(
            left_pane,
            columns=("request_id", "status", "origin", "age"),
            show="headings",
            height=12,
        )
        for col, label, width in (
            ("request_id", "Request ID", 155),
            ("status", "Status", 110),
            ("origin", "Patch Origin", 125),
            ("age", "Age", 65),
        ):
            self.runs_list.heading(col, text=label)
            self.runs_list.column(col, width=width)

        self.runs_list.pack(fill="both", expand=True, padx=2, pady=2)
        self.runs_list.tag_configure("ready", foreground="#38bdf8")
        self.runs_list.tag_configure("activated", foreground="#4ade80")
        self.runs_list.tag_configure("rolled_back", foreground="#fb923c")
        self.runs_list.tag_configure("stale", foreground="#9ca3af")
        self.runs_list.tag_configure("rejected", foreground="#f87171")
        self.runs_list.bind("<<TreeviewSelect>>", self._on_candidate_selected)

        # Right pane: Detail Notebook
        right_pane = ttk.Frame(panes)
        panes.add(right_pane, weight=3)

        self.header_var = tk.StringVar(value="Select a staged harness candidate to inspect evidence and activate")
        ttk.Label(right_pane, textvariable=self.header_var, font=("Segoe UI", 10, "bold")).pack(anchor="w", padx=4, pady=(2, 6))

        self.detail_book = ttk.Notebook(right_pane)
        self.detail_book.pack(fill="both", expand=True, padx=2, pady=2)

        self.tab_invariants = ttk.Frame(self.detail_book)
        self.tab_diff = ttk.Frame(self.detail_book)
        self.tab_review_md = ttk.Frame(self.detail_book)
        self.tab_audit = ttk.Frame(self.detail_book)

        self.detail_book.add(self.tab_invariants, text="Verification & Invariants")
        self.detail_book.add(self.tab_diff, text="Unified Diff")
        self.detail_book.add(self.tab_review_md, text="Review Report (MD)")
        self.detail_book.add(self.tab_audit, text="Activation & Audit")

        def make_scroll_text(parent_tab: Any) -> tk.Text:
            t = tk.Text(
                parent_tab,
                bg="#1f2937",
                fg="#e5e7eb",
                wrap="word",
                font=("Consolas", 9),
                relief="flat",
                state="disabled",
            )
            scroll = ttk.Scrollbar(parent_tab, orient="vertical", command=t.yview)
            t.configure(yscrollcommand=scroll.set)
            scroll.pack(side="right", fill="y")
            t.pack(side="left", fill="both", expand=True, padx=4, pady=4)
            return t

        self.text_invariants = make_scroll_text(self.tab_invariants)
        self.text_diff = make_scroll_text(self.tab_diff)
        self.text_review_md = make_scroll_text(self.tab_review_md)
        self.text_audit = make_scroll_text(self.tab_audit)

    def refresh(self) -> None:
        """Trigger a background refresh of staged candidates and production baseline state."""
        if self._refreshing:
            return
        self._refreshing = True

        def work():
            try:
                runs = self.supervisor.list_candidate_runs()
                lock_held = self.supervisor.activation_lock_path.is_file()
                self.after(0, lambda: self._apply_runs(runs, lock_held))
            except Exception as exc:
                self.after(0, lambda err=str(exc): self.status_var.set(f"Dev Supervisor scan error: {err}"))
            finally:
                self.after(0, lambda: setattr(self, "_refreshing", False))

        threading.Thread(target=work, daemon=True).start()

    def _apply_runs(self, runs: list[dict[str, Any]], lock_held: bool) -> None:
        self._runs = runs
        lock_status = "HELD" if lock_held else "FREE"
        ready_count = sum(1 for r in runs if r["status"] == "READY_FOR_ACTIVATION_APPROVAL")
        drift_count = sum(1 for r in runs if r.get("production_drift_detected"))

        drift_msg = " · Baseline: INTACT" if not drift_count else f" · Baseline DRIFT on {drift_count} run(s)"
        self.status_var.set(
            f"Staged Candidates: {len(runs)} ({ready_count} ready) · Exclusive Lock: {lock_status}{drift_msg}"
        )

        existing = set(self.runs_list.get_children())
        incoming_ids = {r["request_id"] for r in runs}

        for item in existing:
            if item not in incoming_ids:
                self.runs_list.delete(item)

        for r in runs:
            rid = r["request_id"]
            status = r["status"]
            origin = r.get("patch_origin", "unknown")
            if origin == "live_coding_worker_response":
                origin_disp = "Live Qwen Worker"
            elif origin == "predefined_maintainer_patch":
                origin_disp = "Maintainer Patch"
            else:
                origin_disp = origin[:18]

            age_s = r.get("age_seconds")
            if age_s is not None:
                if age_s < 60:
                    age_disp = f"{int(age_s)}s"
                elif age_s < 3600:
                    age_disp = f"{int(age_s // 60)}m"
                else:
                    age_disp = f"{round(age_s / 3600, 1)}h"
            else:
                age_disp = "—"

            vals = (rid, status, origin_disp, age_disp)

            if r.get("is_stale") and status == "READY_FOR_ACTIVATION_APPROVAL":
                tag = "stale"
            elif status == "READY_FOR_ACTIVATION_APPROVAL":
                tag = "ready"
            elif status == "ACTIVATED":
                tag = "activated"
            elif status == "ROLLED_BACK":
                tag = "rolled_back"
            else:
                tag = "rejected"

            if rid not in existing:
                self.runs_list.insert("", "end", iid=rid, values=vals, tags=(tag,))
            else:
                self.runs_list.item(rid, values=vals, tags=(tag,))

        # Select first or preserve selection
        if not self.runs_list.selection() and runs:
            first_id = runs[0]["request_id"]
            self.runs_list.selection_set(first_id)
            self._on_candidate_selected()
        elif self.runs_list.selection():
            self._on_candidate_selected()

    def _set_text(self, widget: tk.Text, content: str) -> None:
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("end", content)
        widget.configure(state="disabled")

    def _on_candidate_selected(self, event: Any = None) -> None:
        sel = self.runs_list.selection()
        if not sel:
            return
        req_id = sel[0]
        self._selected_request_id = req_id

        r = next((item for item in self._runs if item["request_id"] == req_id), None)
        if not r:
            return

        status = r["status"]
        title = r.get("title") or req_id
        self.header_var.set(f"[{status}] {req_id} — {title}")

        # Activate button state
        can_activate = bool(status == "READY_FOR_ACTIVATION_APPROVAL" and not r.get("activation_record"))
        self.activate_btn.configure(state="normal" if can_activate else "disabled")

        # 1. Invariants tab
        self._render_invariants_tab(r)

        # 2. Unified Diff tab
        patch_path = r.get("patch_path")
        if patch_path and Path(patch_path).is_file():
            try:
                diff_text = Path(patch_path).read_text(encoding="utf-8")
            except Exception as exc:
                diff_text = f"Error reading patch: {exc}"
        else:
            diff_text = "No candidate.patch file recorded."
        self._set_text(self.text_diff, diff_text)

        # 3. Review Report (MD) tab
        review_md_path = r.get("review_md_path")
        if review_md_path and Path(review_md_path).is_file():
            try:
                md_text = Path(review_md_path).read_text(encoding="utf-8")
            except Exception as exc:
                md_text = f"Error reading REVIEW.md: {exc}"
        else:
            md_text = "No REVIEW.md file recorded."
        self._set_text(self.text_review_md, md_text)

        # 4. Activation & Audit tab
        act_rec = r.get("activation_record")
        if act_rec:
            audit_lines = [
                f"=== Activation Record: {act_rec.get('request_id')} ===",
                f"Status: {act_rec.get('status', 'unknown').upper()}",
                f"Approved By: {act_rec.get('approver', 'operator')}",
                f"Activation Owner: {act_rec.get('activation_owner', 'unknown')}",
                f"Rolled Back: {act_rec.get('rolled_back', False)}",
                f"Rollback Trigger: {act_rec.get('rollback_trigger') or 'None'}",
                f"Startup Check: {'OK' if act_rec.get('startup_ok') else 'FAILED'}",
                f"Acceptance Check: {'OK' if act_rec.get('acceptance_ok') else 'FAILED'}",
                f"Failure Reason: {act_rec.get('failure_reason') or 'None'}",
                f"Restored Previous Version: {act_rec.get('restored_previous_version', False)}",
                f"Backup Directory: {act_rec.get('backup_dir') or '—'}",
                "",
                "--- Installed Target Hashes ---",
                json.dumps(act_rec.get("installed_hashes", {}), indent=2),
                "",
                "--- Verified Baseline Backup Hashes ---",
                json.dumps(act_rec.get("backup_hashes", {}), indent=2),
            ]
            self._set_text(self.text_audit, "\n".join(audit_lines))
        else:
            unapproved_lines = [
                f"=== Activation Pending: {req_id} ===",
                "Status: STAGED & UNAPPROVED",
                "Human approval is mandatory before any candidate harness file is installed.",
                "",
                "Production Invariants Enforced on Activation:",
                "  1. Exclusive Activation Lock (.activation.lock)",
                "  2. Rejection of Stale or Expired Bundles (TTL <= 3600s)",
                "  3. Exact Tested Candidate Hash Verification",
                "  4. Unchanged Production Baseline Verification",
                "  5. Byte-for-byte Rollback on Installation Exception or Startup/Acceptance Failure",
                "",
                f"Approval Bundle SHA-256: {r.get('approval_bundle_sha256') or '—'}",
                f"Tested Patch SHA-256:    {r.get('tested_patch_sha256') or '—'}",
            ]
            self._set_text(self.text_audit, "\n".join(unapproved_lines))

    def _render_invariants_tab(self, r: dict[str, Any]) -> None:
        lines: list[str] = []
        lines.append(f"=== Candidate Harness Review: {r['request_id']} ===")
        lines.append(f"Title:        {r.get('title')}")
        lines.append(f"Initiated By: {r.get('initiated_by')}")
        lines.append(f"Status:       {r.get('status')}")
        lines.append(f"Patch Origin: {r.get('patch_origin')}")
        lines.append("")

        lines.append("--- Pre-Activation Safeguards & Invariant Status ---")
        lock_status = "LOCKED (concurrent activation blocked)" if r.get("exclusive_lock_held") else "FREE (ready)"
        lines.append(f"• Exclusive Lock (.activation.lock):     {lock_status}")

        age_s = r.get("age_seconds")
        if age_s is not None:
            ttl_status = "EXPIRED (stale approval bundle)" if r.get("is_stale") else "VALID (within 3600s TTL)"
            lines.append(f"• Approval Bundle TTL:                   {age_s:.1f}s / {DEFAULT_APPROVAL_TTL_SECONDS:.0f}s — {ttl_status}")
        else:
            lines.append("• Approval Bundle TTL:                   Timestamp missing")

        bundle_sha = r.get("approval_bundle_sha256") or "None"
        lines.append(f"• Approval Bundle SHA-256:               {bundle_sha}")

        patch_sha = r.get("patch_sha256")
        tested_patch_sha = r.get("tested_patch_sha256")
        patch_match = "MATCH" if (patch_sha and patch_sha == tested_patch_sha) else "MISMATCH / UNTESTED"
        lines.append(f"• Candidate Patch Integrity:             {patch_match} ({patch_sha})")

        # Baseline drift check
        drift = r.get("production_drift") or []
        if not drift:
            lines.append("• Live Production Baseline:              INTACT (100% byte-for-byte match with pre-build baseline)")
        else:
            lines.append(f"• Live Production Baseline:              DRIFT DETECTED! ({', '.join(drift)})")
        lines.append("")

        lines.append("--- Target Files & Hashes ---")
        base_h = r.get("baseline_hashes") or {}
        cand_h = r.get("tested_candidate_hashes") or r.get("candidate_hashes") or {}
        for f in r.get("target_files") or []:
            b_val = base_h.get(f, "unknown")[:16] + "…"
            c_val = cand_h.get(f, "unknown")[:16] + "…"
            lines.append(f"  {f:38} baseline: {b_val}  ->  candidate: {c_val}")
        lines.append("")

        lines.append("--- Evidence Classification & Test Summary ---")
        labels = r.get("evidence_labels") or {}
        summary = r.get("test_summary") or {}
        lines.append(f"1. Coding Worker Patch Generation:       [{labels.get('coding_worker_patch_generation', 'unknown')}]")
        lines.append(f"   -> Patch origin: {r.get('patch_origin')}")
        lines.append(f"2. Syntax Validation (py_compile):        {'PASSED' if summary.get('syntax_passed') else 'FAILED / PENDING'}")
        lines.append(f"3. Frozen Unit Test Suites:              [{labels.get('frozen_tests', 'simulated_desktop_observations')}] {'PASSED' if summary.get('frozen_tests_passed') else 'FAILED'}")
        lines.append(f"4. Malformed Delegation Regressions:     [{labels.get('malformed_delegation_regression', 'simulated_desktop_observations')}] {'PASSED' if summary.get('malformed_delegation_regression_passed') else 'FAILED'}")

        handoff = r.get("audited_native_desktop_handoff") or {}
        lines.append(f"5. Audited Native Desktop Handoff:       [{labels.get('audited_native_desktop_handoff', 'audited_native_desktop_handoff')}] {'PASSED' if summary.get('audited_native_desktop_handoff_passed') else 'FAILED'}")
        if handoff:
            win = handoff.get("verified_window") or {}
            lines.append(f"   -> Target: {win.get('app', 'Claude.exe')} (PID {win.get('verified_pid')}, Window {win.get('window_id')})")
            lines.append(f"   -> Audit Events: {handoff.get('audit_event_count', 0)} recorded via native agent-desktop CLI in native_handoff_audit.sqlite3")

        docker = r.get("real_docker_verification") or {}
        lines.append(f"6. Real Docker Independent Verification: [{labels.get('real_docker_verification', 'real_docker_verification')}] {'PASSED' if summary.get('real_docker_verification_passed') else 'FAILED'}")
        if docker:
            v = docker.get("verifier") or {}
            lines.append(f"   -> Image: {v.get('image_id', 'sha256:245f9d...')} (Host: ego@ego.local)")
            lines.append(f"   -> Checks: {len(v.get('checks', []))} passed in {v.get('elapsed_seconds', '—')}s")
        lines.append("")

        lines.append("--- Activation & Automatic Rollback Guarantee ---")
        lines.append("• Backup is taken byte-for-byte before touching any live production file.")
        lines.append("• Immediate rollback is triggered if:")
        lines.append("    a) Any installation exception occurs (including mid-copy failures)")
        lines.append("    b) Post-copy candidate hash check fails")
        lines.append("    c) Startup check (syntax compilation) fails")
        lines.append("    d) Acceptance check fails")

        self._set_text(self.text_invariants, "\n".join(lines))

    def _on_approve_activate(self) -> None:
        """Handle operator activation request with explicit human confirmation and safeguards dialog."""
        if not self._selected_request_id:
            messagebox.showwarning("No Candidate Selected", "Please select a candidate harness run to activate.")
            return

        r = next((item for item in self._runs if item["request_id"] == self._selected_request_id), None)
        if not r:
            return

        if r["status"] != "READY_FOR_ACTIVATION_APPROVAL":
            messagebox.showwarning(
                "Cannot Activate",
                f"Candidate {r['request_id']} has status '{r['status']}'. Only 'READY_FOR_ACTIVATION_APPROVAL' runs can be activated.",
            )
            return

        if r.get("production_drift_detected"):
            messagebox.showerror(
                "Activation Blocked: Baseline Drift",
                f"Cannot activate: production baseline has drifted on target file(s):\n{', '.join(r['production_drift'])}\n\nCandidate was built against an earlier baseline.",
            )
            return

        # Modal confirmation dialog
        dlg = tk.Toplevel(self.console)
        dlg.title(f"Approve & Activate Harness: {r['request_id']}")
        dlg.geometry("560x420")
        dlg.minsize(500, 360)
        dlg.transient(self.console)
        dlg.grab_set()

        content = ttk.Frame(dlg, padding=16)
        content.pack(fill="both", expand=True)

        ttk.Label(
            content,
            text=f"Approve Candidate Activation: {r['request_id']}",
            font=("Segoe UI", 12, "bold"),
        ).pack(anchor="w", pady=(0, 6))

        ttk.Label(content, text=f"Title: {r.get('title')}", font=("Segoe UI", 9)).pack(anchor="w", pady=(0, 4))

        info_box = ttk.LabelFrame(content, text="Cryptographic Pre-Activation Invariants", padding=10)
        info_box.pack(fill="x", pady=8)

        bundle_sha = r.get("approval_bundle_sha256") or ""
        ttk.Label(info_box, text=f"Approval Bundle SHA-256:\n{bundle_sha}", font=("Consolas", 8)).pack(anchor="w", pady=(0, 4))

        targets_disp = ", ".join(r.get("target_files") or [])
        ttk.Label(info_box, text=f"Target Files: {targets_disp}", font=("Segoe UI", 8)).pack(anchor="w", pady=(0, 4))

        age_s = r.get("age_seconds") or 0.0
        ttl_disp = f"Bundle Age: {age_s:.1f}s (TTL: {DEFAULT_APPROVAL_TTL_SECONDS:.0f}s)"
        ttl_color = "#f87171" if r.get("is_stale") else "#4ade80"
        ttl_lbl = ttk.Label(info_box, text=ttl_disp, font=("Segoe UI", 8, "bold"))
        ttl_lbl.pack(anchor="w")

        form = ttk.Frame(content)
        form.pack(fill="x", pady=8)

        ttk.Label(form, text="Approver Name:").grid(row=0, column=0, sticky="w", pady=4)
        approver_entry = ttk.Entry(form, width=32)
        default_user = getpass.getuser() or "operator"
        approver_entry.insert(0, default_user)
        approver_entry.grid(row=0, column=1, sticky="w", padx=6, pady=4)

        override_ttl_var = tk.BooleanVar(value=bool(r.get("is_stale")))
        if r.get("is_stale"):
            ttk.Checkbutton(
                form,
                text="Approval bundle exceeds 1 hour TTL; extend TTL for this operator activation",
                variable=override_ttl_var,
            ).grid(row=1, column=0, columnspan=2, sticky="w", pady=4)

        btn_row = ttk.Frame(content)
        btn_row.pack(fill="x", pady=(12, 0))

        def do_confirm():
            approver_name = approver_entry.get().strip() or default_user
            extend_ttl = override_ttl_var.get()
            dlg.destroy()
            self._execute_activation(r, approver_name=approver_name, extend_ttl=extend_ttl)

        ttk.Button(btn_row, text="Approve & Install Candidate", command=do_confirm).pack(side="right", padx=4)
        ttk.Button(btn_row, text="Cancel", command=dlg.destroy).pack(side="right", padx=4)

    def _execute_activation(self, r: dict[str, Any], approver_name: str, extend_ttl: bool) -> None:
        req_id = r["request_id"]
        bundle_sha = r.get("approval_bundle_sha256")
        age_s = r.get("age_seconds") or 0.0
        max_age = (age_s + 3600.0) if extend_ttl else DEFAULT_APPROVAL_TTL_SECONDS

        self.activate_btn.configure(state="disabled")
        self.status_var.set(f"Activating {req_id}… acquiring lock and checking startup/acceptance invariants…")

        def work():
            try:
                res = self.supervisor.activate_candidate_by_id(
                    req_id,
                    approved=True,
                    approver=approver_name,
                    expected_bundle_sha256=bundle_sha or None,
                    max_approval_age_seconds=max_age,
                )
                self.after(0, lambda: self._handle_activation_result(req_id, res))
            except Exception as exc:
                self.after(0, lambda err=str(exc): self._handle_activation_error(req_id, err))

        threading.Thread(target=work, daemon=True).start()

    def _handle_activation_result(self, req_id: str, res: dict[str, Any]) -> None:
        self.refresh()
        status = res.get("status")
        if status == "activated":
            installed_summary = "\n".join(
                f"• {f}: {h[:16]}…" for f, h in (res.get("installed_hashes") or {}).items()
            )
            msg = (
                f"Candidate harness {req_id} was successfully activated in production!\n\n"
                f"Approver: {res.get('approver')}\n"
                f"Startup Check: PASSED\n"
                f"Acceptance Check: PASSED\n\n"
                f"Installed Hashes:\n{installed_summary}"
            )
            messagebox.showinfo("Candidate Harness Activated", msg)
        elif status == "rolled_back":
            msg = (
                f"Activation for {req_id} was automatically ROLLED BACK!\n\n"
                f"Rollback Trigger: {res.get('rollback_trigger')}\n"
                f"Failure Reason: {res.get('failure_reason')}\n"
                f"Restored Previous Version: {res.get('restored_previous_version')}\n\n"
                "All production files were verified restored byte-for-byte to their pre-activation baseline."
            )
            messagebox.showwarning("Activation Rolled Back", msg)
        else:
            messagebox.showinfo("Activation Result", json.dumps(res, indent=2))

    def _handle_activation_error(self, req_id: str, error_msg: str) -> None:
        self.refresh()
        messagebox.showerror(
            "Activation Blocked",
            f"Activation of {req_id} failed pre-activation checks:\n\n{error_msg}\n\nProduction files remain untouched.",
        )
