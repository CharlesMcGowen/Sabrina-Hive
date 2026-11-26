"""Background-loaded workflow view; app claims never display as verified work."""
from __future__ import annotations

from tools.sabrina_lab_assistant_mcp import create_objective_and_start
import os
from pathlib import Path
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import ttk

from tools.sabrina_workflow import DEFAULT_DB, ROOT, WorkflowStore


class WorkflowView:
    def __init__(self, console, parent, on_select_run=None):
        self.console, self.on_select_run = console, on_select_run
        self.path = Path(os.environ.get('SABRINA_ASSISTANT_DB', str(DEFAULT_DB)))
        self.busy, self.offset, self.after_event, self.detail = False, 0, 0, None
        bar = ttk.Frame(parent)
        bar.pack(fill='x')
        for label, command in [('Start duration fixture', self.start), ('Pause', lambda: self.control('pause')),
                               ('Resume', lambda: self.control('resume')), ('Cancel', lambda: self.control('cancel')),
                               ('Decision Trace', self.trace), ('Previous runs', lambda: self.page(-50)),
                               ('Next runs', lambda: self.page(50)), ('Next events', self.next_events), ('Step', self.step_selected), ('New Objective', self._new_objective_dialog)]:
            ttk.Button(bar, text=label, command=command).pack(side='left')
        self.status = tk.StringVar(value='App completion, output collection, and independent verification are separate states.')
        ttk.Label(parent, textvariable=self.status, wraplength=1000).pack(fill='x')
        self.rows = ttk.Treeview(parent, columns=('goal','state','external'), show='headings', height=6)
        for key, label in [('goal','Objective'),('state','Workflow status'),('external','External stop status')]:
            self.rows.heading(key,text=label)
        self.rows.pack(fill='x')
        self.rows.bind('<<TreeviewSelect>>', self.selected)
        self.text = tk.Text(parent, wrap='word', state='disabled')
        self.text.pack(fill='both',expand=True)
        self.refresh()

    def _new_objective_dialog(self):
        """Open a dialog to collect parameters for an arbitrary objective and start it."""
        dlg = tk.Toplevel(self.console)
        dlg.title('Create New Objective')
        dlg.transient(self.console)
        # Task description
        ttk.Label(dlg, text='Task description:').grid(row=0, column=0, sticky='w', padx=5, pady=5)
        task_entry = ttk.Entry(dlg, width=80)
        task_entry.grid(row=0, column=1, padx=5, pady=5)
        # Allowed apps (comma-separated)
        ttk.Label(dlg, text='Allowed apps (comma-separated, optional):').grid(row=1, column=0, sticky='w', padx=5, pady=5)
        apps_entry = ttk.Entry(dlg, width=80)
        apps_entry.grid(row=1, column=1, padx=5, pady=5)
        # Ops checkboxes
        ttk.Label(dlg, text='Permitted operations:').grid(row=2, column=0, sticky='w', padx=5, pady=5)
        ops_vars = {
            'desktop': tk.BooleanVar(value=True),
            'host': tk.BooleanVar(value=False),
            'file': tk.BooleanVar(value=False),
        }
        ops_frame = ttk.Frame(dlg)
        ops_frame.grid(row=2, column=1, sticky='w', padx=5, pady=5)
        ttk.Checkbutton(ops_frame, text='Desktop', variable=ops_vars['desktop']).pack(side='left')
        ttk.Checkbutton(ops_frame, text='Host', variable=ops_vars['host']).pack(side='left')
        ttk.Checkbutton(ops_frame, text='File', variable=ops_vars['file']).pack(side='left')
        # Retry and timeout
        ttk.Label(dlg, text='Retry limit:').grid(row=3, column=0, sticky='w', padx=5, pady=5)
        retry_spin = ttk.Spinbox(dlg, from_=1, to=10, width=5)
        retry_spin.set('3')
        retry_spin.grid(row=3, column=1, sticky='w', padx=5, pady=5)
        ttk.Label(dlg, text='Timeout (seconds):').grid(row=4, column=0, sticky='w', padx=5, pady=5)
        timeout_spin = ttk.Spinbox(dlg, from_=30, to=1800, increment=30, width=7)
        timeout_spin.set('300')
        timeout_spin.grid(row=4, column=1, sticky='w', padx=5, pady=5)
        # Mode selector
        ttk.Label(dlg, text='Operating mode:').grid(row=5, column=0, sticky='w', padx=5, pady=5)
        mode_var = tk.StringVar(value='autonomous')
        mode_frame = ttk.Frame(dlg)
        mode_frame.grid(row=5, column=1, sticky='w', padx=5, pady=5)
        ttk.Radiobutton(mode_frame, text='Autonomous Harness (Skills + Tools)', variable=mode_var, value='autonomous').pack(side='left', padx=5)
        ttk.Radiobutton(mode_frame, text='Duration Desktop Fixture', variable=mode_var, value='fixture').pack(side='left', padx=5)
        # Start button
        def start_objective():
            task = task_entry.get().strip()
            apps_raw = apps_entry.get().strip()
            apps = [a.strip() for a in apps_raw.split(',') if a.strip()] if apps_raw else None
            ops = {k: v.get() for k, v in ops_vars.items()}
            retry = int(retry_spin.get())
            timeout = int(timeout_spin.get())
            is_autonomous = (mode_var.get() == 'autonomous')
            try:
                result = create_objective_and_start(task, apps=apps, ops=ops, retry=retry, timeout=timeout, autonomous=is_autonomous)
                self.console.after(0, lambda: self.status.set(f"Started objective {result['objective_id']} ({result.get('harness', 'standard')})"))
                dlg.destroy()
                self.refresh(schedule=False)
            except Exception as e:
                self.console.after(0, lambda: self.status.set(f"Error: {e}"))
        ttk.Button(dlg, text='Start', command=start_objective).grid(row=6, column=0, columnspan=2, pady=10)
        dlg.grab_set()

    def selected(self, _event=None):
        self.after_event = 0
        self.refresh(schedule=False)

    def page(self, delta):
        self.offset = max(0,self.offset+delta)
        self.rows.selection_remove(*self.rows.selection())
        self.after_event = 0
        self.refresh(schedule=False)

    def next_events(self):
        if self.detail:
            self.after_event = self.detail['next_event_id']
            self.refresh(schedule=False)

    def refresh(self, schedule=True):
        if schedule:
            self.console.after(3000, self.refresh)
        if self.busy:
            return
        self.busy = True
        selected = self.rows.selection()
        oid = selected[0] if selected else None
        offset, cursor = self.offset, self.after_event
        def read():
            try:
                store = WorkflowStore(self.path)
                rows = store.list_objectives(offset=offset)
                detail = store.status(oid, after_id=cursor) if oid else None
                self.console.after(0, lambda: self.render(rows,detail))
            except Exception as exc:
                self.console.after(0, lambda msg=str(exc): self.status.set('Workflow unavailable: '+msg))
            finally:
                self.console.after(0, lambda: setattr(self,'busy',False))
        threading.Thread(target=read,daemon=True).start()

    def render(self, rows, detail):
        ids = {r['id'] for r in rows}
        for iid in self.rows.get_children():
            if iid not in ids:
                self.rows.delete(iid)
        for row in rows:
            values = (row['goal'], row['state'],row['external_stop'])
            if self.rows.exists(row['id']):
                self.rows.item(row['id'],values=values)
            else:
                self.rows.insert('', 'end',iid=row['id'],values=values)
        # Only apply the result for the currently selected objective. Expired
        # records are re-read/redacted every refresh, clearing displayed content.
        selected = self.rows.selection()
        if detail and selected and detail['objective']['id'] == selected[0]:
            self.detail = detail
            self.text.configure(state='normal')
            self.text.delete('1.0','end')
            self.text.insert('end',json.dumps(detail,indent=2))
            self.text.configure(state='disabled')
        elif not selected:
            self.detail = None
            self.text.configure(state='normal'); self.text.delete('1.0','end'); self.text.configure(state='disabled')

    def _launch(self, oid):
        directory = ROOT / 'artifacts/workflows' / oid
        directory.mkdir(parents=True, exist_ok=True)
        is_autonomous = False
        try:
            from tools.objective_harness import ObjectiveHarness
            harness = ObjectiveHarness.load_from_db(oid, self.path)
            if harness and getattr(harness.contract, "scope", None):
                is_autonomous = True
        except Exception:
            pass

        cmd = [sys.executable, '-m', 'tools.objective_cli', 'run', oid, '--db', str(self.path)] if is_autonomous else [sys.executable, '-m', 'tools.sabrina_workflow', 'work', '--id', oid, '--db', str(self.path)]
        with (directory / 'worker.log').open('a', encoding='utf-8') as output:
            subprocess.Popen(cmd,
                             cwd=ROOT, stdout=output, stderr=output,
                             creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))

    def step_selected(self):
        """Execute a single step for the selected objective."""
        selected = self.rows.selection()
        if not selected:
            self.status.set("No objective selected to step.")
            return
        oid = selected[0]
        def work():
            try:
                from tools.sabrina_lab_assistant_mcp import sabrina_objective_step
                res = sabrina_objective_step(oid)
                status_val = res.get('status') or res.get('step', {}).get('status', 'ok')
                self.console.after(0, lambda: self.status.set(f"Stepped {oid}: {status_val}"))
                self.console.after(0, lambda: self.refresh(schedule=False))
            except Exception as exc:
                self.console.after(0, lambda msg=str(exc): self.status.set('Step failed: ' + msg))
        threading.Thread(target=work, daemon=True).start()

    def start(self):
        self._action(lambda store: self._launch(store.create('Implement and independently verify the frozen duration parser fixture.')['objective']['id']))

    def control(self, action):
        selected = self.rows.selection()
        if selected:
            oid = selected[0]
            def operate(store):
                store.control(oid,action)
                if action == 'resume':
                    self._launch(oid)
            self._action(operate)

    def _action(self, action):
        def work():
            try:
                action(WorkflowStore(self.path))
                self.console.after(0,lambda: self.refresh(schedule=False))
            except Exception as exc:
                self.console.after(0,lambda msg=str(exc): self.status.set(msg))
        threading.Thread(target=work,daemon=True).start()

    def trace(self):
        if self.detail and self.on_select_run:
            runs = [a['run_id'] for a in self.detail['attempts'] if a.get('run_id')]
            if runs:
                self.on_select_run(runs[-1])
