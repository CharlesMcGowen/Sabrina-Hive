"""Native research program status and owned controls for the Operator Console."""
import threading
try:
    import tkinter as tk
    from tkinter import ttk
except ModuleNotFoundError:
    tk = ttk = None  # Status formatting also runs on the headless lab host.
from datetime import datetime
from zoneinfo import ZoneInfo


def describe(program):
    s, b = program['state'], program['budget']
    running = [j for j in program['jobs'] if j['status'] == 'running']
    activity = running[0]['kind'] if running else 'No experiment executing'
    wake = s.get('next_wake')
    return '\n'.join([
        f"{program['id']} | {program['status']} | {activity}",
        'Hypothesis: '+s.get('hypothesis', 'Initial historical-data qualification'),
        'Reason: '+s.get('selection_reason', 'Awaiting director selection'),
        f"Today: {b['spent']:.0f}s used, {b['reserved']:.0f}s reserved; {b['remaining_seconds']:.0f}s available / {b.get('daily_seconds',7200)}s",
        *([f"Automatic planning retries after a failure per Chicago day: {b['planning_retries']}"] if b.get('planning_retries') is not None else []),
        f"Campaigns started: {b['campaigns']} (no count cap; one active at a time). Next job: {s.get('next_job', 'None')}",
        'Next scheduled check: '+(datetime.fromtimestamp(wake, ZoneInfo('America/Chicago')).strftime('%Y-%m-%d %H:%M %Z') if wake else 'None'),
        'Blocker: '+str(s.get('blocker') or 'None'),
        'Last result: '+str(s.get('last_job') or 'None'),
        'Paper research only. A running service is not evidence of an executing experiment.'
    ])


class ResearchView(ttk.LabelFrame if ttk else object):
    def __init__(self, parent, request):
        if ttk is None: raise RuntimeError('ResearchView requires the desktop Tk runtime')
        super().__init__(parent, text='Autonomous Kraken research')
        self.request, self.busy, self.program, self.timer = request, False, None, None
        self.text = tk.StringVar(value='Loading research program...')
        ttk.Label(self, textvariable=self.text, wraplength=950, justify='left').pack(anchor='w', padx=8, pady=6)
        buttons = ttk.Frame(self); buttons.pack(anchor='w', padx=8, pady=4)
        for label, action in (('Refresh', None), ('Pause new work', 'pause'), ('Resume', 'resume'), ('Cancel program', 'cancel')):
            ttk.Button(buttons, text=label, command=lambda a=action: self.refresh(a)).pack(side='left', padx=(0, 5))
        self.refresh()

    def refresh(self, action=None):
        if self.timer is not None:
            self.after_cancel(self.timer); self.timer = None
        if self.busy: return
        self.busy = True
        def run():
            try:
                if action and self.program:
                    self.request('/api/kraken/research/'+self.program+'/'+action, 'POST')
                rows = self.request('/api/kraken/research')['programs']
                self.after(0, lambda: self.show(rows))
            except Exception as exc:
                self.after(0, lambda message=str(exc): self.failed(message))
        threading.Thread(target=run, daemon=True).start()

    def show(self, rows):
        self.busy = False
        p = next((x for x in reversed(rows) if x['status'] != 'cancelled'), rows[-1] if rows else None)
        self.program = p['id'] if p else None
        self.text.set(describe(p) if p else 'No autonomous research program configured.')
        self.timer = self.after(15000, self.refresh)

    def failed(self, message):
        self.busy = False; self.text.set('Research status unavailable: '+message)
        self.timer = self.after(30000, self.refresh)
