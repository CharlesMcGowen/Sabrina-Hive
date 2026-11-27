"""Native Windows operator console for the Sabrina social runtime."""
from __future__ import annotations
import datetime as dt, hashlib, json, os, sqlite3, subprocess, sys, threading, time, tkinter as tk, urllib.request
from tools.sabrina_lab_assistant_mcp import create_objective_and_start
from pathlib import Path
from tkinter import messagebox, ttk
from tools.run_measurement import build_console_measurement_summary
from tools.desktop_trace_reader import DesktopTraceReader

# Sabrina Social API on ego, reached through the SSH tunnel in tools/tunnel_ego_social_api.ps1.
# Set SABRINA_OPERATOR_API to point elsewhere (for example http://127.0.0.1:8087 for this machine's own API).
API = os.environ.get("SABRINA_OPERATOR_API", "http://127.0.0.1:18087").rstrip("/")
def request(path, method="GET", payload=None):
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(API + path, data=data, method=method, headers={"Content-Type":"application/json"})
    with urllib.request.urlopen(req, timeout=5) as response: return json.loads(response.read().decode())

class Console(tk.Tk):
    def __init__(self):
        super().__init__(); self.title("Sabrina Operator Console"); self.geometry("900x650"); self.minsize(760,540)
        self.status=tk.StringVar(value="Connecting…"); self.cards={}; self._jobs=[]; self._caps=[]; self._measurement={}; self._trace_reader=DesktopTraceReader(); self._trace_runs=[]; self._selected_run_id=""; self._trace_events_page=0; self._trace_page_size=100; self._perf_view=None; self._main_book=None; self._trace_tab_frame=None; self._refreshing=False; self.after_idle(self._start_console)
    def _start_console(self):
        self._build()
        self._poll()
        print('Operator console ready', flush=True)
    def _build(self):
        style=ttk.Style(self); style.theme_use("clam"); self.configure(bg="#111827")
        for name in ("TFrame","TLabel","TNotebook","TNotebook.Tab"): style.configure(name,background="#111827",foreground="#e5e7eb")
        style.configure("Title.TLabel",font=("Segoe UI",18,"bold")); style.configure("Card.TFrame",background="#1f2937"); style.configure("Card.TLabel",background="#1f2937",foreground="#e5e7eb")
        ttk.Label(self,text="Sabrina Operator Console",style="Title.TLabel").pack(anchor="w",padx=18,pady=(16,4)); ttk.Label(self,textvariable=self.status).pack(anchor="w",padx=18,pady=(0,10))
        meters=ttk.Frame(self); meters.pack(fill="x",padx=14)
        for name in ("Supervisor","Scheduler","Bridge","Jobs"):
            card=ttk.Frame(meters,style="Card.TFrame",padding=12); card.pack(side="left",fill="both",expand=True,padx=4); ttk.Label(card,text=name,style="Card.TLabel").pack(anchor="w"); value=ttk.Label(card,text="—",style="Card.TLabel",font=("Segoe UI",15,"bold")); value.pack(anchor="w",pady=(5,0)); self.cards[name]=value
        self.activity=tk.StringVar(value='Checking actual model activity…')
        self.desktop_step=tk.StringVar(value='Checking desktop review progress…')
        self.social_activity=tk.StringVar(value='Checking social supervisor…')
        ttk.Label(self,textvariable=self.social_activity,wraplength=850).pack(anchor='w',padx=18,pady=(8,2))
        ttk.Label(self,textvariable=self.activity,wraplength=850).pack(anchor='w',padx=18,pady=(8,2))
        ttk.Label(self,textvariable=self.desktop_step,wraplength=850).pack(anchor='w',padx=18)
        controls=ttk.Frame(self); controls.pack(fill="x",padx=18,pady=12); ttk.Button(controls,text="Refresh",command=self.refresh).pack(side="left",padx=3); ttk.Button(controls,text="Open Web Dashboard",command=lambda:subprocess.Popen(["cmd","/c","start","",API])).pack(side="left",padx=3); ttk.Button(controls,text="Open Swarm Chat (:8095)",command=lambda:subprocess.Popen(["cmd","/c","start","","http://127.0.0.1:8095"])).pack(side="left",padx=3)
        book=ttk.Notebook(self); self._main_book=book; book.pack(fill="both",expand=True,padx=18,pady=(0,14)); operations,calendar,capabilities,lab,models,journal=ttk.Frame(book),ttk.Frame(book),ttk.Frame(book),ttk.Frame(book),ttk.Frame(book),ttk.Frame(book); book.add(operations,text="Scheduled jobs"); book.add(calendar,text="Calendar"); book.add(capabilities,text="Capabilities"); book.add(lab,text="Lab queue"); book.add(models,text="Models"); book.add(journal,text="LinkedIn action journal")
        ttk.Label(operations,text="Operations scheduler only. Lab inference is shown in Lab queue; desktop tasks are shown in Workflows.",wraplength=800).pack(anchor="w",padx=8,pady=4)
        self.workshop_status=tk.StringVar(value='Windows workshop schedules: checking status...')
        ttk.Label(operations,textvariable=self.workshop_status,wraplength=800).pack(anchor='w',padx=8,pady=4)
        self.jobs=tk.Listbox(operations,bg="#1f2937",fg="#e5e7eb",relief="flat",highlightthickness=0); self.jobs.pack(fill="both",expand=True,padx=8,pady=8); self.jobs.bind("<Double-1>",self.show_job_details)
        actions=ttk.Frame(operations); actions.pack(fill="x",padx=8,pady=(0,8))
        for label,action in (("Run now","run-now"),("Pause","pause"),("Resume","resume"),("Cancel","cancel")): ttk.Button(actions,text=label,command=lambda a=action:self.job_action(a)).pack(side="left",padx=3)
        from tools.sabrina_console_calendar import GranularCalendarView
        self._calendar_view=GranularCalendarView(calendar, console=self)
        self._calendar_view.pack(fill="both",expand=True)
        # Keep hidden legacy widgets so existing refresh() helpers stay compatible
        self.capability=ttk.Combobox(calendar,state="readonly",width=26)
        self.delay=ttk.Entry(calendar,width=9); self.delay.insert(0,"0")
        self.interval_min=ttk.Entry(calendar,width=8); self.interval_max=ttk.Entry(calendar,width=8)
        self.calendar=ttk.Treeview(calendar,columns=("when","operation","state","repeat"),show="headings")
        self.capabilities=ttk.Treeview(capabilities,columns=("name","owner","actions","availability","competence","blocks"),show="headings")
        for col,label,width in (("name","Capability",170),("owner","Owner",120),("actions","Actions",250),("availability","State",80),("competence","Verified competence",135),("blocks","Env/App blocks",110)): self.capabilities.heading(col,text=label); self.capabilities.column(col,width=width)
        self.capabilities.pack(fill="both",expand=True,padx=8,pady=8)
        self.lab_status=tk.StringVar(value="Loading coordinator status…")
        ttk.Label(lab,textvariable=self.lab_status,wraplength=780).pack(anchor="w",padx=8,pady=8)
        self.lab_queue=ttk.Treeview(lab,columns=("task","model","placement","state","error"),show="headings")
        for col,label,width in (("task","Task / client",240),("model","Model",220),("placement","Placement",100),("state","State",100),("error","Error",180)): self.lab_queue.heading(col,text=label); self.lab_queue.column(col,width=width)
        self.lab_queue.pack(fill="both",expand=True,padx=8,pady=8)
        ttk.Label(lab,text="Placement is the coordinator's recorded selection. Completed means inference returned, not that an app action succeeded.",wraplength=780).pack(anchor="w",padx=8,pady=(0,8))
        self.model_status=tk.StringVar(value="Loading model inventory…")
        ttk.Label(models,textvariable=self.model_status,wraplength=780).pack(anchor="w",padx=8,pady=8)
        self.model_list=ttk.Treeview(models,columns=("name","size","residency","vram"),show="headings")
        for col,label,width in (("name","Installed model",330),("size","Size",100),("residency","Residency",130),("vram","GPU memory",120)): self.model_list.heading(col,text=label); self.model_list.column(col,width=width)
        self.model_list.pack(fill="both",expand=True,padx=8,pady=8)
        ttk.Label(models,text="Inventory and loaded models only; assignment and model quality are not verified here.",wraplength=780).pack(anchor="w",padx=8,pady=(0,8))
        ttk.Label(journal,text="Completed scans are not proof of a LinkedIn action. An action is verified only when the record has platform evidence.",wraplength=850).pack(anchor="w",padx=8,pady=(8,2))
        self.linkedin_funnel_status=tk.StringVar(value="Loading Round-Robin, Quotas & Job Application Funnel...")
        ttk.Label(journal,textvariable=self.linkedin_funnel_status,wraplength=850,foreground="#6ee7b7").pack(anchor="w",padx=8,pady=(0,4))
        j_controls=ttk.Frame(journal); j_controls.pack(fill="x",padx=8,pady=(0,6))
        ttk.Button(j_controls,text="Discover Jobs Now",command=lambda:threading.Thread(target=lambda:(request("/api/jobs/discover","POST",{"max_jobs":6,"easy_apply_only":True}),self.refresh()),daemon=True).start()).pack(side="left",padx=3)
        ttk.Button(j_controls,text="Run RSI Loop (3 Gens)",command=lambda:threading.Thread(target=lambda:(request("/api/rsi/run","POST",{"rounds":3}),self.refresh()),daemon=True).start()).pack(side="left",padx=3)
        ttk.Button(j_controls,text="Self-Heal AI Services",command=lambda:threading.Thread(target=lambda:(request("/api/ai-services/heal","POST",{}),self.refresh()),daemon=True).start()).pack(side="left",padx=3)
        ttk.Button(j_controls,text="Toggle 24/7 Boost Mode",command=lambda:threading.Thread(target=lambda:(request("/api/boost-mode","POST",{"enabled":not getattr(self,"_boost_24_7",False)}),self.refresh()),daemon=True).start()).pack(side="left",padx=3)
        self.action_journal=ttk.Treeview(journal,columns=("time","kind","operation","action","evidence"),show="headings")
        for col,label,width in (("time","Time",155),("kind","Operation",150),("operation","Run status",105),("action","Action result",185),("evidence","LinkedIn evidence",300)): self.action_journal.heading(col,text=label); self.action_journal.column(col,width=width)
        self.action_journal.pack(fill="both",expand=True,padx=8,pady=(0,8))
        self.action_journal.tag_configure("verified",foreground="#86efac")
        self.action_journal.tag_configure("unverified",foreground="#fca5a5")
        self.action_journal.tag_configure("scan",foreground="#cbd5e1")
        reviews=ttk.Frame(book); book.add(reviews,text="Desktop reviews")
        self.reviews=tk.Text(reviews,bg="#1f2937",fg="#e5e7eb",wrap="word",state="disabled",font=("Segoe UI",10))
        self.reviews.pack(fill="both",expand=True,padx=8,pady=8)
        skills_frame=ttk.Frame(book); book.add(skills_frame,text="Skills")
        skills_panes=ttk.PanedWindow(skills_frame,orient="horizontal")
        skills_panes.pack(fill="both",expand=True,padx=8,pady=8)
        left_pane=ttk.Frame(skills_panes); skills_panes.add(left_pane,weight=1)
        right_pane=ttk.Frame(skills_panes); skills_panes.add(right_pane,weight=3)

        ttk.Label(skills_frame,text="Project entries are Sabrina skill files. Codex global/system entries are visible here for reference; they are not automatically loaded by Sabrina.",wraplength=850).pack(anchor="w",padx=8,pady=(8,0))
        self.skills_list=ttk.Treeview(left_pane,columns=("name","scope","competence"),show="headings")
        self.skills_list.heading("name",text="Skill"); self.skills_list.column("name",width=155)
        self.skills_list.heading("scope",text="Source"); self.skills_list.column("scope",width=100)
        self.skills_list.heading("competence",text="Competence"); self.skills_list.column("competence",width=105)
        self.skills_list.pack(fill="both",expand=True)
        self.skills_list.bind("<<TreeviewSelect>>",self._on_skill_selected)

        right_top=ttk.Frame(right_pane); right_top.pack(fill="x",pady=(0,6))
        self.skill_title=tk.StringVar(value="Select a skill to inspect")
        ttk.Label(right_top,textvariable=self.skill_title,font=("Segoe UI",11,"bold")).pack(side="left")
        self.skill_approve_btn=ttk.Button(right_top,text="Approve Proposal",command=self._approve_selected_proposal)
        self.skill_reject_btn=ttk.Button(right_top,text="Reject Proposal",command=self._reject_selected_proposal)
        self.skill_validate_btn=ttk.Button(right_top,text="Validate Skill",command=self._validate_selected_skill)
        self.skill_validate_btn.pack(side="right")

        self.skill_detail=tk.Text(right_pane,bg="#1f2937",fg="#e5e7eb",wrap="word",font=("Consolas",10),relief="flat",state="disabled")
        self.skill_detail.pack(fill="both",expand=True)
        self._skills_data={}
        trace_frame=ttk.Frame(book); book.add(trace_frame,text="Decision Trace"); self._build_decision_trace_tab(trace_frame)
        from tools.sabrina_console_desktop_catalog import DesktopCatalogView
        self.desktop_catalog_view=DesktopCatalogView(book); book.add(self.desktop_catalog_view,text="Desktop Tools")
        self._trace_tab_frame=trace_frame
        perf_frame=ttk.Frame(book); book.add(perf_frame,text="Performance")
        from tools.sabrina_console_performance import PerformanceView
        self._perf_view=PerformanceView(self, perf_frame, on_select_run=self._jump_to_trace_run)
        workflow_frame=ttk.Frame(book); book.add(workflow_frame,text="Workflows")
        from tools.sabrina_console_workflow import WorkflowView
        self._workflow_view=WorkflowView(self, workflow_frame, on_select_run=self._jump_to_trace_run)
        harness_frame=ttk.Frame(book); book.add(harness_frame,text="Harness Updates")
        from tools.sabrina_console_dev_supervisor import DevSupervisorView
        self._devsup_view=DevSupervisorView(self, harness_frame)
        from tools.sabrina_console_kraken import KrakenView
        self._kraken_view=KrakenView(book, request); book.add(self._kraken_view,text="Kraken")
        book.insert(2, self._kraken_view)
        from tools.sabrina_console_swarm import SwarmCommandView
        self._swarm_view=SwarmCommandView(book, console=self, on_select_run=self._jump_to_trace_run)
        book.add(self._swarm_view,text="Swarm Control")
        book.insert(0, self._swarm_view)
        from tools.swarm_chat_portal import SwarmChatTab
        self._swarm_chat_tab=SwarmChatTab(book, console=self)
        book.add(self._swarm_chat_tab,text="Swarm Chat")
        book.insert(1, self._swarm_chat_tab)
        book.select(self._swarm_view)
    def _poll(self):
        self.refresh()
        self._kraken_view.refresh()
        if getattr(self, "_swarm_view", None):
            self._swarm_view.refresh()
        if getattr(self, "_calendar_view", None):
            self._calendar_view.refresh()
        self.after(3000,self._poll)
    def refresh(self):
        if self._refreshing: return
        self._refreshing=True
        def work():
            try:
                from tools.sabrina_desktop_workshop_jobs import status_lines
                workshop=status_lines()
                self.after(0,lambda value=workshop:self.workshop_status.set(value))
            except Exception as exc:
                self.after(0,lambda error=str(exc):self.workshop_status.set('Workshop status unavailable: '+error))
            try:
                health=request("/health"); jobs=request("/api/schedule").get("jobs",[]); caps=request("/api/capabilities").get("capabilities",[]); self.after(0,lambda:self._show(health,jobs,caps))
                action_ops=request("/api/operations?limit=100").get("operations",[]); self.after(0,lambda:self._show_action_journal(action_ops))
                try:
                    vis=request("/api/reports/visibility")
                    self.after(0,lambda v=vis:self._show_visibility(v))
                except Exception:
                    pass
            except Exception as exc: self.after(0,lambda error=str(exc):self.status.set(f"Dashboard unavailable: {error}"))
            try:
                from tools.control_client import configured
                client=configured()
                if client is None: raise RuntimeError("Lab coordinator is not configured")
                lab=client.request('/v1/status')
                self.after(0,lambda:self._show_lab(lab))
            except Exception as exc:
                self.after(0,lambda error=str(exc):self._lab_unavailable(error))
            try:
                base=os.environ.get('SABRINA_OLLAMA_BASE_URL','http://ego.local:11434').rstrip('/')
                with urllib.request.urlopen(base+'/api/tags',timeout=5) as response: installed=json.load(response).get('models',[])
                with urllib.request.urlopen(base+'/api/ps',timeout=5) as response: loaded=json.load(response).get('models',[])
                self.after(0,lambda:self._show_models(installed,loaded))
            except Exception as exc:
                self.after(0,lambda error=str(exc):self.model_status.set(f"Model inventory unavailable: {error}"))
            try:
                path=Path(os.environ.get('SABRINA_ASSISTANT_DB',str(Path(__file__).parent/'data/sabrina_assistant.sqlite3')))
                with sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=2) as db:
                    rows=db.execute('SELECT id,task,state,progress,answer,updated,messages,model,activity,verified_result,verification_evidence FROM assistant_sessions ORDER BY updated DESC LIMIT 5').fetchall()
                    decisions={r[0]:db.execute('SELECT tool,state,result,decision_reason FROM assistant_desktop_proposals WHERE run_id=? ORDER BY updated DESC LIMIT 5',(r[0],)).fetchall() for r in rows}
                    measurement=build_console_measurement_summary(db,Path(__file__).parent)
                self.after(0,lambda m=measurement:self._show_reviews(rows,decisions,m))
            except Exception as exc:
                self.after(0,lambda error=str(exc):self._show_reviews_error(error))
            try:
                found_skills={}
                local_dir=Path(__file__).parent/"skills"
                global_dir=Path.home()/".codex/skills"
                for s_dir,scope in ((local_dir,"project"),(global_dir,"global")):
                    if s_dir.exists():
                        for md_file in sorted(s_dir.rglob("SKILL.md")):
                            relative=md_file.parent.relative_to(s_dir)
                            if any(part.startswith(".") and part!=".system" for part in relative.parts): continue
                            source_scope=("Sabrina project" if scope=="project" else
                                          "Codex system" if relative.parts and relative.parts[0]==".system" else
                                          "Codex global")
                            skill_id=scope+":"+relative.as_posix()
                            found_skills[skill_id]={"name":md_file.parent.name,"path":md_file,"scope":source_scope}
                try:
                    from tools.sabrina_skill_hydration import SabrinaSkillHydration
                    proposals=SabrinaSkillHydration().list_proposals(status="pending_approval")
                    for prop in proposals:
                        p_id="proposal:"+prop["proposal_id"]
                        found_skills[p_id]={
                            "name": f"⚡ PROPOSAL: {prop['skill_name']}",
                            "path": Path(prop["target_path"]),
                            "scope": "PROPOSAL (pending approval)",
                            "proposal_data": prop,
                        }
                except Exception:
                    pass
                self.after(0,lambda:self._show_skills(found_skills))
            except Exception:
                pass
            try:
                trace_runs=self._trace_reader.list_runs(limit=50)
                self.after(0,lambda r=trace_runs:self._show_trace_runs(r))
            except Exception:
                pass
            try:
                from tools.sabrina_evaluation_engine import SabrinaEvaluationEngine
                perf_snap=SabrinaEvaluationEngine().get_console_performance_snapshot()
                if self._perf_view:
                    self.after(0,lambda s=perf_snap:self._perf_view.update(s))
            except Exception:
                pass
            try:
                if getattr(self, '_devsup_view', None):
                    self.after(0, self._devsup_view.refresh)
            except Exception:
                pass
            self.after(0,lambda:setattr(self,'_refreshing',False))
        threading.Thread(target=work,daemon=True).start()
    def _show_reviews_error(self,error):
        self.reviews.configure(state='normal'); self.reviews.delete('1.0','end'); self.reviews.insert('end','Desktop review status unavailable: '+error); self.reviews.configure(state='disabled')
    def _show_visibility(self,vis):
        if not isinstance(vis,dict): return
        self._boost_24_7=bool(vis.get("boost_24_7"))
        rr=vis.get("round_robin") or {}
        jf=vis.get("job_funnel") or {}
        q=vis.get("quotas") or {}
        dc=q.get("daily_counts") or {}; dl=q.get("daily_limits") or {}
        ai=vis.get("ai_services") or {}
        rsi=vis.get("rsi") or {}
        ollama_ok=bool((ai.get("ollama") or {}).get("reachable"))
        boost_txt="24/7 BOOST ON" if self._boost_24_7 else "Standard 08:00-21:00"
        self.linkedin_funnel_status.set(
            f"Round-Robin Next: {rr.get('next_task','engage_feed')} (last: {rr.get('last_task','none')} -> {rr.get('last_status','idle')}) · "
            f"Jobs Funnel: {jf.get('applied',0)} applied / {jf.get('staged',0)} staged / {jf.get('total',0)} discovered · "
            f"RSI: Gen {rsi.get('generation',0)} (V={rsi.get('current_fitness',0)}, +{rsi.get('promoted_count',0)} promoted) · "
            f"Quotas: Apply {dc.get('job_apply',0)}/{dl.get('job_apply',4)} · "
            f"AI: {'Ollama OK' if ollama_ok else 'Ollama DOWN'} · Mode: {boost_txt}"
        )
    def _show_action_journal(self,operations):
        for item in self.action_journal.get_children(): self.action_journal.delete(item)
        for row in operations:
            result=row.get('result') or {}
            action_result=result.get('feed') if isinstance(result.get('feed'),dict) else result
            rr=result.get('round_robin') if isinstance(result.get('round_robin'),dict) else {}
            kind_label=row.get('kind','unknown')
            if rr.get('executed_task'): kind_label=f"{kind_label}->{rr['executed_task']}"
            attempted=int(action_result.get('actions_attempted',0) or 0)
            verified=int(action_result.get('actions_verified',0) or 0)
            scanned=int(action_result.get('posts_scanned',0) or 0)
            jobs_disc=action_result.get('jobs_discovered')
            err=row.get('error') or action_result.get('error') or action_result.get('reason')
            activity_id=row.get('linkedin_activity_id') or action_result.get('linkedin_activity_id')
            linkedin_url=row.get('linkedin_url') or action_result.get('linkedin_url')
            text_hash=row.get('verified_text_hash') or action_result.get('verified_text_hash')
            if attempted:
                is_verified=verified > 0 and bool((activity_id or linkedin_url) and text_hash)
                action_label='VERIFIED' if is_verified else 'ATTEMPTED · UNVERIFIED'
                evidence=(f"Activity {activity_id}" if activity_id else linkedin_url or "No LinkedIn activity ID or URL")
                if not text_hash: evidence += " · no verification hash"
                tag='verified' if is_verified else 'unverified'
            elif err:
                action_label='BLOCKED / ERROR'
                evidence=str(err)[:120]
                tag='unverified'
            elif jobs_disc is not None:
                action_label=f"JOBS: {action_result.get('jobs_staged',0)} STAGED / {jobs_disc} FOUND"
                evidence=f"Query: {action_result.get('query','LinkedIn Jobs')}"
                tag='verified' if int(action_result.get('jobs_staged',0) or 0) > 0 else 'scan'
            elif scanned > 0:
                action_label=f"SCANNED {scanned} POSTS · 0 ACTIONS"
                decisions=action_result.get('decisions') if isinstance(action_result.get('decisions'),list) else []
                reasons=[str(d.get('reason') or d.get('action') or 'skip') for d in decisions[:2] if isinstance(d,dict)]
                evidence=", ".join(reasons)[:120] if reasons else "No qualifying posts above substance threshold"
                tag='scan'
            else:
                action_label='SCAN ONLY · NO ACTION' if row.get('kind') in ('engage_feed','social_pulse') else 'Completed (Read/Draft)'
                evidence='No external write required'
                tag='scan'
            stamp=dt.datetime.fromtimestamp(row.get('created',0)).strftime('%Y-%m-%d %I:%M:%S %p')
            self.action_journal.insert('', 'end', iid=row.get('id'), values=(stamp,kind_label,row.get('status','unknown'),action_label,evidence),tags=(tag,))
    def _lab_unavailable(self,error):
        self.lab_status.set('Coordinator unavailable: '+error)
        self.activity.set('Live lab status unavailable. Previous queue rows may be stale.')
    def _show_reviews(self,rows,decisions,measurement=None):
        if measurement is not None:
            self._measurement=measurement
            self._render_capabilities(self._caps)
            if self._skills_data: self._show_skills(self._skills_data)
        self.reviews.configure(state='normal'); self.reviews.delete('1.0','end')
        if rows: self.desktop_step.set(f'Desktop: {rows[0][7]} · {rows[0][8]} · {rows[0][3]}')
        m_data=self._measurement or {}
        cat_counts=m_data.get('category_counts') or {}
        if cat_counts:
            summary_parts=[f"{k}={v}" for k,v in sorted(cat_counts.items())]
            self.reviews.insert('end',f"Run Measurement Summary ({m_data.get('total_evaluated_runs',0)} runs): {', '.join(summary_parts)}\n")
        for comp in m_data.get('comparisons',[]):
            self.reviews.insert('end',f"Paired-Replay Evidence [{comp.get('changed_variable')}: {comp.get('baseline_value')} -> {comp.get('candidate_value')}]: one_shot={comp.get('one_shot_rate')} · best_of_n={comp.get('best_of_n_rate')} · candidate={comp.get('candidate_rate')} · sampling_gain={format(comp.get('sampling_gain'), '+.3f') if isinstance(comp.get('sampling_gain'), (int, float)) else 'unavailable'} · intervention_gain={format(comp.get('intervention_gain'), '+.3f') if isinstance(comp.get('intervention_gain'), (int, float)) else 'unavailable'}\nRecommendation: {comp.get('recommendation')}\n\n")
        run_evals=m_data.get('runs') or {}
        for run_id,task,state,progress,answer,updated,messages,model,activity,verified,evidence in rows:
            stamp=dt.datetime.fromtimestamp(updated).strftime('%I:%M:%S %p')
            label='reasoning ended; inspect action evidence' if state=='completed' else state
            if verified: label='EXTERNAL RESULT VERIFIED'
            r_eval=run_evals.get(run_id) or {}
            attr_line=''
            if r_eval:
                cat_code=f"{r_eval.get('primary_category','unknown')}:{r_eval.get('sub_code','')}"
                comp_flag='counts toward competence' if r_eval.get('counts_toward_competence') else 'excluded from model competence (env/harness/replay)'
                prov_line=f"device={r_eval.get('requested_device','auto')}->{r_eval.get('confirmed_placement','unknown')} · think={r_eval.get('think_setting','unset')} · capability={r_eval.get('capability_name','unknown')}"
                attr_line=f"Attribution: {cat_code} ({comp_flag}) · {prov_line}\nDiagnostic: {r_eval.get('diagnostic_summary','')}\n"
            self.reviews.insert('end',f'{stamp} · {label} · {run_id}\nModel: {model} · {activity}\n{attr_line}Task: {task}\n{progress}\nModel report (not independently verified):\n{answer}\n\n')
            if verified: self.reviews.insert('end',f'Supervisor verification: {verified}\nEvidence: {evidence}\n\n')
            for tool,outcome,result,reason in decisions.get(run_id,[]):
                self.reviews.insert('end',f'Action record · {tool} · {outcome}\n{reason}\n{result[:1500]}\n\n')
            for message in json.loads(messages)[-4:]:
                if message.get('role')=='tool':
                    self.reviews.insert('end',f"Actual tool result · {message.get('tool_name')}\n{message.get('content','')[:2000]}\n\n")
        self.reviews.configure(state='disabled')
    def _show_lab(self,lab):
        active=[r for r in lab.get('requests',[]) if r.get('state') in ('running','queued','cancelling','uncertain')]
        running=next((r for r in active if r.get('state')=='running'),active[0] if active else None)
        self.activity.set(f"{running['state'].upper()} · {running.get('model','unknown model')} · {running.get('placement','pending')} · {running.get('task_label') or running.get('client')}" if running else 'No model inference currently running. Desktop reviews shows the saved tool progress and pending actions.')
        leases=lab.get('leases',[])
        summary=', '.join(f"{x.get('resource')}: {x.get('state')} ({x.get('owner')})" for x in leases) or 'No active reservations'
        self.lab_status.set(dt.datetime.now().strftime('%I:%M:%S %p')+' · '+summary)
        for item in self.lab_queue.get_children(): self.lab_queue.delete(item)
        for row in lab.get('requests',[]):
            if row.get('state') in ('queued','running','cancelling','uncertain','failed') or row in lab.get('requests',[])[:8]:
                placement=f"{row.get('requested_device','auto')} → {row.get('placement','unavailable')}"
                self.lab_queue.insert('','end',values=(row.get('task_label') or row.get('client'),row.get('model','unavailable'),placement,row.get('state'),row.get('error')))
    def _show_models(self,installed,loaded):
        resident={x.get('name'):x for x in loaded}
        self.model_status.set(f"{len(installed)} installed · {len(loaded)} loaded")
        for item in self.model_list.get_children(): self.model_list.delete(item)
        for model in installed:
            active=resident.get(model.get('name')); vram=(active or {}).get('size_vram',0)
            self.model_list.insert('','end',values=(model.get('name'),f"{model.get('size',0)/1024**3:.1f} GiB",'GPU' if vram else 'CPU' if active else 'Not loaded',f"{vram/1024**3:.1f} GiB" if vram else '—'))
    def _show(self,h,jobs,caps):
        s,q,b=h.get("supervisor",{}),h.get("scheduler",{}),h.get("bridge",{}); supervisor_state=s.get("status","unknown"); self.cards["Supervisor"].configure(text=supervisor_state.upper() if s.get("heartbeat_fresh") else "STALE"); self.cards["Scheduler"].configure(text="ACTIVE" if q.get("active") else "DOWN"); self.cards["Bridge"].configure(text=b.get("status","unknown")); self.cards["Jobs"].configure(text=f"{q.get('running',0)} running / {q.get('queued',0)} queued"); self.status.set("Connected · live state refreshed"); self._jobs=jobs; self.jobs.delete(0,tk.END)
        attention=sum(j.get('status') in {'paused','failed','blocked'} for j in jobs)
        self.cards['Jobs'].configure(text=f"{q.get('running',0)} running / {q.get('queued',0)} queued\n{attention} paused/blocked/failed")
        self.status.set('Operations API refreshed '+dt.datetime.now().strftime('%I:%M:%S %p'))
        last=(s.get("last_result") or {}).get("status","none recorded"); next_cycle=s.get("next_cycle_seconds")
        wait_text=f" · next cycle in about {max(0,int(next_cycle))//60} min" if next_cycle is not None else ""
        saved_note=" · saved result may predate this wait" if supervisor_state=="waiting" and last!="none recorded" else ""
        self.social_activity.set(f"Social supervisor: {supervisor_state.upper()}{wait_text} · last saved result: {last}{saved_note}")
        availability = h.get('availability') or {}
        if availability:
            blockers = ', '.join(availability.get('blocking_dependencies', [])) or 'none'
            retries = [r.get('cooldown_until') or r.get('next_try') for r in availability.get('recovery', [])
                       if (r.get('cooldown_until') or r.get('next_try') or 0) > time.time()]
            next_try = dt.datetime.fromtimestamp(min(retries)).strftime('%I:%M:%S %p') if retries else 'next health check'
            successful = availability.get('last_successful_social_run')
            last_ok = dt.datetime.fromtimestamp(successful).strftime('%m-%d %I:%M %p') if successful else 'not recorded'
            self.social_activity.set(f"Social availability: {availability.get('status', 'unknown').upper()} · blockers: {blockers} · last successful run: {last_ok} · recovery: {next_try}" +
                                     (f" · {availability['operator_action']}" if availability.get('operator_action') else ''))
        for job in jobs:
            result=job.get('result') or {}
            detail=job.get('last_error') or result.get('summary') or ''
            stamp=dt.datetime.fromtimestamp(job['updated']).strftime('%m-%d %I:%M:%S %p')
            self.jobs.insert(tk.END,f"{job['id']} | {job['capability']} | {job['status']} | {stamp} | {detail}")
        for item in self.calendar.get_children(): self.calendar.delete(item)
        for job in jobs:
            when=dt.datetime.fromtimestamp(job["next_run"]).strftime("%Y-%m-%d %I:%M %p"); repeat="—" if job.get("interval_min") is None else f"{job['interval_min']//60}–{(job.get('interval_max') or job['interval_min'])//60} min"; self.calendar.insert("","end",values=(when,job["capability"],job["status"],repeat))
        names=[c["name"] for c in caps]; self.capability["values"]=names
        if names and not self.capability.get(): self.capability.set(names[0])
        self._caps=caps; self._render_capabilities(caps)
    def _render_capabilities(self,caps):
        for item in self.capabilities.get_children(): self.capabilities.delete(item)
        measured_caps=(self._measurement or {}).get("capabilities") or {}
        seen=set()
        for cap in caps:
            cname=cap["name"]; seen.add(cname)
            m=measured_caps.get(cname) or {}
            comp=m.get("competence_label","unmeasured")
            blocks=str(m.get("harness_or_env_blocks","—"))
            self.capabilities.insert("","end",values=(cname,cap["owner"],", ".join(cap["actions"]),cap["availability"],comp,blocks))
        for cname,m in sorted(measured_caps.items()):
            if cname in seen or cname=="desktop.paired_replay": continue
            owner="sabrina-lab-assistant"
            actions="desktop_propose_action, verify_external_result" if "desktop" in cname else "sabrina_research_task"
            state="measured"
            self.capabilities.insert("","end",values=(cname,owner,actions,state,m.get("competence_label","0/0"),str(m.get("harness_or_env_blocks",0))))
    def selected(self):
        index=self.jobs.curselection(); return self._jobs[index[0]] if index and self._jobs else None
    def show_job_details(self,event=None):
        job=self.selected()
        if not job:return
        window=tk.Toplevel(self); window.title(f"Scheduled job · {job['capability']}"); window.geometry("780x540")
        detail=tk.Text(window,bg="#111827",fg="#e5e7eb",wrap="word",font=("Consolas",10))
        detail.pack(fill="both",expand=True,padx=10,pady=10)
        detail.insert("1.0",json.dumps(job,indent=2,ensure_ascii=False)); detail.configure(state="disabled")
    def job_action(self,action):
        job=self.selected()
        if not job:return
        try: request(f"/api/schedule/{job['id']}/{action}","POST"); self.refresh()
        except Exception as exc: messagebox.showerror("Operation failed",str(exc))
    def schedule(self):
        try:
            if self.capability.get() in {'kraken.inspect','kraken.objective'}:
                self._main_book.select(self._kraken_view)
                return
            capability=self.capability.get(); delay=max(0,int(self.delay.get() or 0)); low=int(self.interval_min.get())*60 if self.interval_min.get().strip() else None; high=int(self.interval_max.get())*60 if self.interval_max.get().strip() else low; payload=({} if capability=="system.health_report" else {"max_threads":5} if capability=="linkedin.replies" else {"max_posts":3}); request("/api/schedule","POST",{"capability":capability,"payload":payload,"next_run":time.time()+delay*60,"interval_min":low,"interval_max":high,"max_attempts":3}); self.refresh()
        except Exception as exc: messagebox.showerror("Schedule failed",str(exc))
    def _show_skills(self,skills_dict):
        self._skills_data=skills_dict
        measured_skills=(self._measurement or {}).get("skills") or {}
        existing=set(self.skills_list.get_children())
        for item in existing:
            if item not in skills_dict: self.skills_list.delete(item)
        for name,info in skills_dict.items():
            m=measured_skills.get(name) or {}
            comp=m.get("competence_label","unmeasured")
            if name not in existing:
                self.skills_list.insert("","end",iid=name,values=(info["name"],info["scope"],comp))
            else:
                self.skills_list.item(name,values=(info["name"],info["scope"],comp))
        if not self.skills_list.selection() and skills_dict:
            first=list(skills_dict.keys())[0]
            self.skills_list.selection_set(first)
            self._show_skill_content(first)
        elif self.skills_list.selection():
            self._show_skill_content(self.skills_list.selection()[0])
    def _on_skill_selected(self,event):
        sel=self.skills_list.selection()
        if not sel: return
        item_id=sel[0]
        if item_id.startswith("proposal:"):
            self.skill_validate_btn.pack_forget()
            self.skill_reject_btn.pack(side="right",padx=3)
            self.skill_approve_btn.pack(side="right",padx=3)
        else:
            self.skill_approve_btn.pack_forget()
            self.skill_reject_btn.pack_forget()
            self.skill_validate_btn.pack(side="right")
        self._show_skill_content(item_id)
    def _show_skill_content(self,name):
        info=self._skills_data.get(name)
        if not info: return
        if name.startswith("proposal:"):
            p_data=info.get("proposal_data") or {}
            content=p_data.get("skill_content","")
            m_ver = re.search(r'(?m)^version:\s*[\'"]?([0-9A-Za-z_.-]+)[\'"]?', content)
            disp_version = m_ver.group(1).strip() if m_ver else "1.0.0"
            disp_sha256 = hashlib.sha256(content.encode("utf-8")).hexdigest().lower()
            p_data["displayed_version"] = disp_version
            p_data["displayed_sha256"] = disp_sha256
            self.skill_title.set(f"Proposal: {p_data.get('skill_name')} | Status: {p_data.get('status','pending')}")
            header=(f"[AUTONOMOUS SKILL PROPOSAL - OPERATOR REVIEW REQUIRED]\n"
                    f"Proposal ID: {p_data.get('proposal_id')}\n"
                    f"Exact Proposal Version: {disp_version}\n"
                    f"Full Content SHA-256: {disp_sha256}\n"
                    f"Trigger Reason: {p_data.get('trigger_reason')}\n"
                    f"Query Used: {p_data.get('query_used')} · Created: {dt.datetime.fromtimestamp(p_data.get('created',0)).strftime('%Y-%m-%d %I:%M %p')}\n"
                    f"Target Installation: {p_data.get('target_path')}\n"
                    f"Search Sources: {p_data.get('search_sources')}\n"
                    f"{'='*72}\n\n")
            self.skill_detail.configure(state="normal")
            self.skill_detail.delete("1.0","end")
            self.skill_detail.insert("end",header+content)
            self.skill_detail.configure(state="disabled")
            return
        self.skill_title.set(f"{info['name']} ({info['scope']}) · {info['path']}")
        try:
            raw_bytes=info["path"].read_bytes()
            file_sha=hashlib.sha256(raw_bytes).hexdigest()[:16]
            content=raw_bytes.decode("utf-8",errors="replace")
        except Exception as exc:
            file_sha="unavailable"
            content=f"Error reading skill: {exc}"
        m=((self._measurement or {}).get("skills") or {}).get(name)
        if m:
            header=(f"[Skill Measurement & Competence]\n"
                    f"SKILL.md SHA-256: {file_sha} · Total Runs: {m.get('total_runs',0)} · "
                    f"Verified Competence: {m.get('competence_label','0/0')}\n"
                    f"Valid Attempts: {m.get('valid_attempts',0)} (Verified Passes: {m.get('verified_passes',0)}, Model Errors: {m.get('model_errors',0)}) · "
                    f"Excluded Env/App Blocks: {m.get('harness_or_env_blocks',0)}\n"
                    f"Last Run: {m.get('last_run_id','none')} -> {m.get('last_outcome','none')}\n"
                    f"{'-'*72}\n\n")
        else:
            header=(f"[Skill Measurement & Competence]\n"
                    f"SKILL.md SHA-256: {file_sha} · Status: No recorded Sabrina runs have referenced this skill yet.\n"
                    f"{'-'*72}\n\n")
        self.skill_detail.configure(state="normal")
        self.skill_detail.delete("1.0","end")
        self.skill_detail.insert("end",header+content)
        self.skill_detail.configure(state="disabled")
    def _approve_selected_proposal(self):
        sel=self.skills_list.selection()
        if not sel or not sel[0].startswith("proposal:"): return
        info=self._skills_data.get(sel[0], {})
        p_data=info.get("proposal_data", {})
        prop_id=p_data.get("proposal_id")
        if not prop_id: return
        content=p_data.get("skill_content", "")
        m_ver = re.search(r'(?m)^version:\s*[\'"]?([0-9A-Za-z_.-]+)[\'"]?', content)
        disp_version = p_data.get("displayed_version") or (m_ver.group(1).strip() if m_ver else "1.0.0")
        disp_sha256 = p_data.get("displayed_sha256") or hashlib.sha256(content.encode("utf-8")).hexdigest().lower()
        try:
            from tools.sabrina_skill_hydration import SabrinaSkillHydration
            res=SabrinaSkillHydration().approve_proposal(
                prop_id,
                expected_version=disp_version,
                expected_sha256=disp_sha256,
                reviewer="operator_console",
            )
            msg = f"Skill '{res.get('skill_name')}' v{disp_version} approved and installed to {res.get('installed_path')}\nSHA-256: {disp_sha256}"
            if res.get("archived_prior_path"):
                msg += f"\nReplaced version preserved in archive: {res.get('archived_prior_path')}"
            messagebox.showinfo("Proposal Approved", msg)
            self.refresh()
        except Exception as exc:
            messagebox.showerror("Approval Failed", str(exc))
    def _reject_selected_proposal(self):
        sel=self.skills_list.selection()
        if not sel or not sel[0].startswith("proposal:"): return
        info=self._skills_data.get(sel[0], {})
        p_data=info.get("proposal_data", {})
        prop_id=p_data.get("proposal_id")
        if not prop_id: return
        try:
            from tools.sabrina_skill_hydration import SabrinaSkillHydration
            res=SabrinaSkillHydration().reject_proposal(prop_id, reason="Rejected via Operator Console", reviewer="operator_console")
            messagebox.showinfo("Proposal Rejected", f"Proposal '{prop_id}' marked as rejected.")
            self.refresh()
        except Exception as exc:
            messagebox.showerror("Rejection Failed", str(exc))
    def _jump_to_trace_run(self, run_id: str):
        if self._main_book and self._trace_tab_frame:
            self._main_book.select(self._trace_tab_frame)
        if run_id in self.trace_runs_list.get_children():
            self.trace_runs_list.selection_set(run_id)
            self._on_trace_run_selected()
    def _validate_selected_skill(self):
        sel=self.skills_list.selection()
        if not sel: return
        name=sel[0]
        info=self._skills_data.get(name)
        if not info: return
        script=Path.home()/".codex/skills/.system/skill-creator/scripts/quick_validate.py"
        target_dir=info["path"].parent
        if not script.exists():
            messagebox.showinfo("Validation",f"Skill file exists at {info['path']}")
            return
        res=subprocess.run([sys.executable,str(script),str(target_dir)],capture_output=True,text=True)
        out=(res.stdout+res.stderr).strip()
        messagebox.showinfo(f"Validation: {name}",out or f"Return code: {res.returncode}")
    def _build_decision_trace_tab(self,parent):
        panes=ttk.PanedWindow(parent,orient="horizontal")
        panes.pack(fill="both",expand=True,padx=8,pady=8)
        left_pane=ttk.Frame(panes); panes.add(left_pane,weight=1)
        ttk.Label(left_pane,text="Recent Runs (Latest 50)",font=("Segoe UI",10,"bold")).pack(anchor="w",pady=(0,4))
        self.trace_runs_list=ttk.Treeview(left_pane,columns=("run_id","status","decisions","desktop","updated"),show="headings")
        for col,label,width in (("run_id","Run ID",130),("status","Trace status",85),("decisions","Decisions",65),("desktop","Desktop",65),("updated","Updated",130)):
            self.trace_runs_list.heading(col,text=label); self.trace_runs_list.column(col,width=width)
        self.trace_runs_list.pack(fill="both",expand=True)
        self.trace_runs_list.bind("<<TreeviewSelect>>",self._on_trace_run_selected)

        right_panes=ttk.PanedWindow(panes,orient="vertical")
        panes.add(right_panes,weight=3)

        upper_pane=ttk.Frame(right_panes); right_panes.add(upper_pane,weight=2)
        top_bar=ttk.Frame(upper_pane); top_bar.pack(fill="x",pady=(0,4))
        self.trace_run_header=tk.StringVar(value="Select a run to view trace")
        ttk.Label(top_bar,textvariable=self.trace_run_header,font=("Segoe UI",10,"bold")).pack(side="left")

        paging_bar=ttk.Frame(top_bar); paging_bar.pack(side="right")
        self.trace_page_label=tk.StringVar(value="Page 1 (100 / batch)")
        ttk.Label(paging_bar,textvariable=self.trace_page_label).pack(side="left",padx=6)
        ttk.Button(paging_bar,text="< Prev 100",command=self._trace_prev_page).pack(side="left",padx=2)
        ttk.Button(paging_bar,text="Next 100 >",command=self._trace_next_page).pack(side="left",padx=2)

        ttk.Label(upper_pane,text="Model choices and desktop operations are separate unless an explicit run-scoped handoff ID links them.",foreground="#9ca3af",font=("Segoe UI",8)).pack(anchor="w",pady=(0,4))

        self.trace_events_list=ttk.Treeview(upper_pane,columns=("type","summary","status","time"),show="headings")
        for col,label,width in (("type","Type",75),("summary","Event summary",380),("status","Status",95),("time","Time",145)):
            self.trace_events_list.heading(col,text=label); self.trace_events_list.column(col,width=width)
        self.trace_events_list.pack(fill="both",expand=True)
        self.trace_events_list.tag_configure("decision",foreground="#93c5fd")
        self.trace_events_list.tag_configure("desktop",foreground="#86efac")
        self.trace_events_list.tag_configure("replay",foreground="#c084fc")
        self.trace_events_list.tag_configure("error",foreground="#fca5a5")
        self.trace_events_list.tag_configure("incomplete",foreground="#fcd34d")
        self.trace_events_list.bind("<<TreeviewSelect>>",self._on_trace_event_selected)

        lower_pane=ttk.Frame(right_panes); right_panes.add(lower_pane,weight=3)
        self.trace_detail_book=ttk.Notebook(lower_pane); self.trace_detail_book.pack(fill="both",expand=True)
        self.trace_tab_overview=ttk.Frame(self.trace_detail_book)
        self.trace_tab_input=ttk.Frame(self.trace_detail_book)
        self.trace_tab_schemas=ttk.Frame(self.trace_detail_book)
        self.trace_tab_output=ttk.Frame(self.trace_detail_book)
        self.trace_tab_desktop=ttk.Frame(self.trace_detail_book)

        self.trace_detail_book.add(self.trace_tab_overview,text="Overview & Timing")
        self.trace_detail_book.add(self.trace_tab_input,text="Exact Model Input")
        self.trace_detail_book.add(self.trace_tab_schemas,text="Tool Schemas")
        self.trace_detail_book.add(self.trace_tab_output,text="Public Model Output")
        self.trace_detail_book.add(self.trace_tab_desktop,text="Desktop Observation")

        def make_text(tab):
            t=tk.Text(tab,bg="#1f2937",fg="#e5e7eb",wrap="word",font=("Consolas",9),relief="flat",state="disabled")
            t.pack(fill="both",expand=True,padx=4,pady=4); return t
        self.trace_text_overview=make_text(self.trace_tab_overview)
        self.trace_text_input=make_text(self.trace_tab_input)
        self.trace_text_schemas=make_text(self.trace_tab_schemas)
        self.trace_text_output=make_text(self.trace_tab_output)
        self.trace_text_desktop=make_text(self.trace_tab_desktop)

    def _show_trace_runs(self,runs):
        self._trace_runs=runs
        existing=set(self.trace_runs_list.get_children())
        incoming_ids={r["run_id"] for r in runs}
        for item in existing:
            if item not in incoming_ids: self.trace_runs_list.delete(item)
        for r in runs:
            rid=r["run_id"]
            status_disp=r["trace_status"].upper()
            stamp=dt.datetime.fromtimestamp(r["updated"]).strftime("%Y-%m-%d %I:%M %p") if r.get("updated") else "—"
            vals=(rid[:14]+"…",status_disp,r["decision_count"],r["desktop_event_count"],stamp)
            if rid not in existing:
                self.trace_runs_list.insert("","end",iid=rid,values=vals)
            else:
                self.trace_runs_list.item(rid,values=vals)
        if not self.trace_runs_list.selection() and runs:
            first=runs[0]["run_id"]
            self.trace_runs_list.selection_set(first)
            self._on_trace_run_selected()

    def _on_trace_run_selected(self,event=None):
        sel=self.trace_runs_list.selection()
        if not sel: return
        run_id=sel[0]
        self._selected_run_id=run_id
        self._trace_events_page=0
        run_info=next((r for r in self._trace_runs if r["run_id"]==run_id),None)
        if run_info:
            req=run_info.get("requested_device") or "auto"
            status=run_info.get("trace_status","unknown").upper()
            header=f"Run: {run_id} | State: {run_info.get('state')} | Model: {run_info.get('model') or '—'} | Requested Device: {req} | Status: {status}"
            self.trace_run_header.set(header)
        else:
            self.trace_run_header.set(f"Run: {run_id}")
        self._load_trace_events(run_id,page=0)

    def _load_trace_events(self,run_id,page=0):
        if not run_id: return
        def work():
            try:
                data=self._trace_reader.list_events(run_id,offset=page*self._trace_page_size,limit=self._trace_page_size)
                self.after(0,lambda:self._show_trace_events(data,page))
            except Exception as exc:
                self.after(0,lambda err=str(exc):self._clear_trace_detail(f"Error loading events: {err}"))
        threading.Thread(target=work,daemon=True).start()

    def _trace_prev_page(self):
        if self._trace_events_page>0:
            self._trace_events_page-=1
            self._load_trace_events(self._selected_run_id,page=self._trace_events_page)

    def _trace_next_page(self):
        self._trace_events_page+=1
        self._load_trace_events(self._selected_run_id,page=self._trace_events_page)

    def _show_trace_events(self,data,page):
        for item in self.trace_events_list.get_children(): self.trace_events_list.delete(item)
        total=data.get("total",0)
        offset=data.get("offset",0)
        limit=data.get("limit",100)
        events=data.get("events",[])
        is_expired=data.get("is_expired",False)
        start_idx=offset+1 if total>0 else 0
        end_idx=min(offset+limit,total)
        self.trace_page_label.set(f"Showing {start_idx}-{end_idx} of {total} (Batch 100)")
        if is_expired:
            self._clear_trace_detail("Desktop audit retention expired; raw content erased")
        for ev in events:
            ev_id=str(ev["id"])
            ev_type=ev["event_type"]
            iid=f"{ev_type}:{ev_id}"
            stamp=dt.datetime.fromtimestamp(ev["created"]).strftime("%Y-%m-%d %I:%M:%S %p") if ev.get("created") else "—"
            tag="replay" if ev.get("is_replay") else ("error" if ev.get("has_error") else ev_type)
            self.trace_events_list.insert("", "end", iid=iid, values=(ev_type.upper(),ev["summary"],ev["status"],stamp), tags=(tag,))
        if events and not self.trace_events_list.selection():
            first=f"{events[0]['event_type']}:{events[0]['id']}"
            self.trace_events_list.selection_set(first)
            self._on_trace_event_selected()
        elif not events:
            self._clear_trace_detail("No decision or desktop trace events recorded for this run.")

    def _on_trace_event_selected(self,event=None):
        sel=self.trace_events_list.selection()
        if not sel: return
        parts=sel[0].split(":",1)
        if len(parts)!=2: return
        ev_type,ev_id_str=parts[0],parts[1]
        if ev_type == "cross_app_handoff":
            ev_id=ev_id_str
        else:
            try: ev_id=int(ev_id_str)
            except ValueError: return
        def work():
            try:
                if ev_type in {"decision","decision_replay"}:
                    detail=self._trace_reader.get_decision_detail(ev_id)
                elif ev_type.startswith("cross_app_"):
                    detail=self._trace_reader.get_cross_app_event_detail(ev_type,ev_id)
                else:
                    detail=self._trace_reader.get_desktop_event_detail(ev_id)
                self.after(0,lambda:self._show_trace_detail(detail,ev_type))
            except Exception as exc:
                self.after(0,lambda err=str(exc):self._clear_trace_detail(f"Error reading detail: {err}"))
        threading.Thread(target=work,daemon=True).start()

    def _set_text_widget(self,widget,text):
        widget.configure(state="normal")
        widget.delete("1.0","end")
        widget.insert("end",text)
        widget.configure(state="disabled")

    def _clear_trace_detail(self,message):
        for w in (self.trace_text_overview,self.trace_text_input,self.trace_text_schemas,self.trace_text_output,self.trace_text_desktop):
            self._set_text_widget(w,message)

    def _show_trace_detail(self,detail,ev_type):
        if detail.get("metadata_only"):
            for w in (self.trace_text_overview,self.trace_text_input,self.trace_text_schemas,self.trace_text_output,self.trace_text_desktop):
                self._set_text_widget(w,"")
            self._set_text_widget(self.trace_text_overview,json.dumps(detail,indent=2,ensure_ascii=False))
            self._set_text_widget(self.trace_text_schemas,"Raw prompts, replies, snapshots, and report text were erased at retention expiry.")
            return
        if detail.get("raw_hidden") or detail.get("is_expired"):
            self._clear_trace_detail("Desktop audit retention expired; raw content erased")
            return
        if ev_type.startswith("cross_app_"):
            for w in (self.trace_text_overview,self.trace_text_input,self.trace_text_schemas,self.trace_text_output,self.trace_text_desktop):
                self._set_text_widget(w,"")
            if ev_type == "cross_app_handoff":
                measurement=detail.get("measurement") or {}
                overview=[
                    f"=== Cross-App Handoff {detail.get('handoff_id')} ===",
                    f"App: {detail.get('app')} | State: {detail.get('status')} | Route ID: {detail.get('route_id')}",
                    f"Prompt event: {detail.get('prompt_event_id')} | Submit event: {detail.get('submit_event_id')}",
                    f"Reply event: {detail.get('reply_event_id')} | Fresh snapshot: {detail.get('reply_snapshot_id')}",
                    f"Conversation: {detail.get('conversation_key') or '—'} | Contribution: {detail.get('contribution') or '—'}",
                    f"Elapsed: {measurement.get('elapsed_seconds')} s | Desktop calls: {measurement.get('desktop_call_count')}",
                    f"Linked model decisions: {measurement.get('local_model_decision_ids')}",
                    f"Local token counts: {measurement.get('local_model_token_counts')} | External tokens: {measurement.get('external_token_count', 'unavailable')}",
                    f"Error/stop detail: {detail.get('error_detail') or '—'}",
                    f"Linked desktop event IDs: {[item.get('id') for item in detail.get('linked_desktop_events', [])]}",
                ]
                self._set_text_widget(self.trace_text_overview,"\n".join(overview))
                self._set_text_widget(self.trace_text_input,detail.get("prompt") or "No prompt was submitted.")
                self._set_text_widget(self.trace_text_output,json.dumps({"summary":detail.get("summary"),"contribution":detail.get("contribution"),"reply_snapshot_id":detail.get("reply_snapshot_id")},indent=2))
            elif ev_type == "cross_app_route":
                self._set_text_widget(self.trace_text_overview,f"=== Route Choice #{detail.get('id')} ===\nApp: {detail.get('app')}\nChoice: {detail.get('choice')}\nHandoff ID: {detail.get('handoff_id') or '—'}\nDecision ID: {detail.get('decision_id')}")
                self._set_text_widget(self.trace_text_input,detail.get("rationale") or "")
                self._set_text_widget(self.trace_text_output,detail.get("expected_contribution") or "")
            else:
                coverage=detail.get("evidence_coverage") or {}
                self._set_text_widget(self.trace_text_overview,f"=== {str(detail.get('kind','checkpoint')).upper()} Checkpoint #{detail.get('id')} ===\nDecision ID: {detail.get('decision_id')}\nSHA-256: {detail.get('content_sha256')}\nChecklist evidence coverage: {coverage.get('referenced_claims', detail.get('checklist_with_evidence', 0))}/{coverage.get('total_claims', detail.get('checklist_count', 0))}")
                self._set_text_widget(self.trace_text_input,json.dumps(detail.get("evidence_refs",[]),indent=2))
                self._set_text_widget(self.trace_text_output,json.dumps({"content":detail.get("content"),"evidence_checklist":detail.get("evidence_checklist")},indent=2,ensure_ascii=False))
            self._set_text_widget(self.trace_text_schemas,"Cross-app route/handoff/checkpoint event. Causal links use stored IDs; chronological neighbors alone are not attributed.")
            self._set_text_widget(self.trace_text_desktop,json.dumps(detail.get("linked_desktop_events",[]),indent=2,ensure_ascii=False))
            return
        if ev_type in {"decision","decision_replay"}:
            timing=detail.get("timing") or {}
            tokens=detail.get("token_counts") or {}
            fps=detail.get("fingerprints") or {}
            req_p=detail.get("requested_placement") or "—"
            rec_p=detail.get("recorded_placement") or "—"
            overview_lines=[
                f"=== Model Decision Event #{detail.get('event_id')} (Run: {detail.get('run_id')}) ===",
                f"Model: {detail.get('model')} | Thinking Setting: {detail.get('thinking_setting')}",
                f"Requested GPU Placement: {req_p}  [Independently Recorded Placement: {rec_p}]",
                f"Generation Options: {json.dumps(detail.get('generation_options',{}))}",
                f"Replay / Experiment Record: {detail.get('is_replay')}",
                "",
                "--- Timing Breakdown ---",
                f"Wall Clock Elapsed: {timing.get('wall_clock_seconds')} s | Ollama Total: {timing.get('total_duration_s')} s",
                f"Model Load: {timing.get('load_duration_s')} s | Prompt Eval: {timing.get('prompt_eval_duration_s')} s | Eval: {timing.get('eval_duration_s')} s",
                "",
                "--- Token Counts ---",
                f"Prompt Tokens: {tokens.get('prompt_eval_count')} | Completion Tokens: {tokens.get('eval_count')}",
                "",
                "--- Prompt & Version Fingerprints ---",
                f"Pinned System Prompt SHA-256: {fps.get('pinned_system_prompt_sha256') or '—'}",
                f"Current System Prompt SHA-256: {fps.get('current_system_prompt_sha256') or '—'}",
                f"Prompt Changed: {fps.get('prompt_changed')}",
                f"Source Hashes: {json.dumps(fps.get('source_hashes',{}),indent=2)}",
                "",
                "--- Errors ---",
                f"Error: {detail.get('error') or 'None'}",
                f"Incomplete Response: {detail.get('is_incomplete')}",
            ]
            self._set_text_widget(self.trace_text_overview,"\n".join(overview_lines))
            inputs=detail.get("model_input_messages") or []
            self._set_text_widget(self.trace_text_input,json.dumps(inputs,indent=2))
            schemas=detail.get("available_tool_schemas") or []
            self._set_text_widget(self.trace_text_schemas,json.dumps(schemas,indent=2))
            output=detail.get("public_model_output") or {}
            self._set_text_widget(self.trace_text_output,json.dumps(output,indent=2))
            self._set_text_widget(self.trace_text_desktop,"This event is a Model Decision. Select an Executed Desktop event from the timeline to view desktop observations.")
        else:
            args=detail.get("arguments") or {}
            res=detail.get("result") or {}
            overview_lines=[
                f"=== Executed Desktop Event #{detail.get('event_id')} (Run: {detail.get('run_id')}) ===",
                f"Tool: {detail.get('tool')} | Target App: {detail.get('app')}",
                f"Elapsed Time: {detail.get('elapsed_seconds')} s",
                f"Outcome OK: {detail.get('outcome_ok')}",
                f"Error: {json.dumps(detail.get('error')) if detail.get('error') else 'None'}",
                "",
                "Executed desktop operations are recorded independently from model choices.",
            ]
            self._set_text_widget(self.trace_text_overview,"\n".join(overview_lines))
            self._set_text_widget(self.trace_text_input,"Recorded desktop tool arguments:\n"+json.dumps(args,indent=2,ensure_ascii=False))
            self._set_text_widget(self.trace_text_schemas,f"Schemas belong to decision requests; desktop action executed tool: {detail.get('tool')}")
            self._set_text_widget(self.trace_text_output,"Public model choices belong to decision requests. This is a recorded tool call; inspect native delivery, errors, and live/simulation attribution before claiming execution.")
            self._set_text_widget(self.trace_text_desktop,json.dumps(res,indent=2,ensure_ascii=False))



if __name__ == "__main__": Console().mainloop()
