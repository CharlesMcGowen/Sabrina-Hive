"""Run-owned catalog execution, physical input, artifacts and native sessions.

This module extends (never bypasses) AgentDesktopBroker's scope and audit checks.
An injected runner is simulation; its records are labeled accordingly.
"""
from __future__ import annotations

import base64
import ctypes
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import threading
import uuid

from tools import agent_desktop_registry as catalog

_LOCK = threading.RLock()


def ensure_schema(db):
    db.executescript('''
    CREATE TABLE IF NOT EXISTS assistant_desktop_runtime(
      run_id TEXT PRIMARY KEY, permissions TEXT NOT NULL, fingerprint TEXT NOT NULL,
      session_id TEXT NOT NULL DEFAULT '', state_root TEXT NOT NULL DEFAULT '',
      expires_at REAL NOT NULL, expired INTEGER NOT NULL DEFAULT 0);
    CREATE TABLE IF NOT EXISTS assistant_desktop_artifacts(
      id TEXT PRIMARY KEY, run_id TEXT NOT NULL, kind TEXT NOT NULL, app TEXT NOT NULL,
      window_id TEXT NOT NULL, created REAL NOT NULL, expires_at REAL NOT NULL,
      metadata TEXT NOT NULL, content BLOB, sha256 TEXT NOT NULL, valid INTEGER NOT NULL DEFAULT 1);
    CREATE TABLE IF NOT EXISTS assistant_desktop_ref_windows(
      run_id TEXT NOT NULL, snapshot_id TEXT NOT NULL, window_id TEXT NOT NULL,
      PRIMARY KEY(run_id,snapshot_id));
    ''')
    columns={r[1] for r in db.execute('PRAGMA table_info(assistant_desktop_runtime)')}
    if 'loaded_tools' not in columns:
        db.execute("ALTER TABLE assistant_desktop_runtime ADD COLUMN loaded_tools TEXT NOT NULL DEFAULT '[]'")
    if 'scope' not in columns:
        db.execute("ALTER TABLE assistant_desktop_runtime ADD COLUMN scope TEXT NOT NULL DEFAULT '{}' ")


def expire_artifacts(db, run_ids):
    tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for run_id in run_ids:
        if 'assistant_desktop_artifacts' in tables:
            db.execute("UPDATE assistant_desktop_artifacts SET content=NULL,metadata='{}',valid=0 WHERE run_id=?",(run_id,))
        if 'assistant_desktop_runtime' in tables:
            row=db.execute('SELECT state_root FROM assistant_desktop_runtime WHERE run_id=?',(run_id,)).fetchone()
            if row and row[0]:
                # The path must be the deterministic private child, never an arbitrary DB path.
                expected=private_root(run_id)
                if Path(row[0]).resolve() != expected.resolve(): raise ValueError('invalid desktop cleanup root')
                if expected.exists(): shutil.rmtree(expected)
            db.execute("UPDATE assistant_desktop_runtime SET expired=1,session_id='' WHERE run_id=?",(run_id,))


def private_root(run_id):
    from tools.agent_desktop_broker import _desktop_environment
    base=Path(_desktop_environment().get('AGENT_DESKTOP_HOME',str(Path.home()/'.agent-desktop')))
    return base/'sabrina-runs'/hashlib.sha256(run_id.encode()).hexdigest()


def win_window(window, points=(), foreground=True):
    if os.name != 'nt': raise ValueError('physical window verification requires Windows')
    user=ctypes.WinDLL('user32',use_last_error=True)
    user.SetThreadDpiAwarenessContext.argtypes=[ctypes.c_void_p]
    user.SetThreadDpiAwarenessContext.restype=ctypes.c_void_p
    previous=user.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
    if not previous: raise ValueError('cannot establish physical-pixel DPI context')
    try: return _win_window(window,points,foreground)
    finally: user.SetThreadDpiAwarenessContext(previous)


def _win_window(window, points=(), foreground=True):
    """Verify actual HWND/PID, geometry, DPI and hit-tested target ownership."""
    if os.name != 'nt': raise ValueError('physical window verification requires Windows')
    from ctypes import wintypes as w
    user=ctypes.WinDLL('user32',use_last_error=True)
    user.GetForegroundWindow.restype=w.HWND
    user.GetAncestor.argtypes=[w.HWND,w.UINT];user.GetAncestor.restype=w.HWND
    user.WindowFromPoint.argtypes=[w.POINT];user.WindowFromPoint.restype=w.HWND
    user.GetWindowThreadProcessId.argtypes=[w.HWND,ctypes.POINTER(w.DWORD)]
    user.GetWindowRect.argtypes=[w.HWND,ctypes.POINTER(w.RECT)]
    user.IsWindowVisible.argtypes=[w.HWND];user.IsIconic.argtypes=[w.HWND]
    user.GetDpiForWindow.argtypes=[w.HWND];user.GetDpiForWindow.restype=w.UINT
    hwnd=int(str(window['id']).removeprefix('w-'))
    pid=w.DWORD();user.GetWindowThreadProcessId(hwnd,ctypes.byref(pid))
    if pid.value != int(window['pid']) or not user.IsWindowVisible(hwnd) or user.IsIconic(hwnd):
        raise ValueError('window identity changed or window is minimized/invisible')
    if foreground and user.GetForegroundWindow() != hwnd: raise ValueError('focus interference: target is not foreground')
    rect=w.RECT()
    if not user.GetWindowRect(hwnd,ctypes.byref(rect)): raise ValueError('cannot verify window geometry')
    frame=w.RECT()
    dwm=ctypes.WinDLL('dwmapi')
    dwm.DwmGetWindowAttribute.argtypes=[w.HWND,w.DWORD,ctypes.c_void_p,w.DWORD]
    if dwm.DwmGetWindowAttribute(hwnd,9,ctypes.byref(frame),ctypes.sizeof(frame)) != 0:
        raise ValueError('cannot verify screenshot frame bounds')
    rect=frame  # agent-desktop screenshots exclude invisible resize borders
    geometry={'x':rect.left,'y':rect.top,'width':rect.right-rect.left,'height':rect.bottom-rect.top,'dpi':user.GetDpiForWindow(hwnd)}
    for x,y in points:
        if not rect.left <= x < rect.right or not rect.top <= y < rect.bottom: raise ValueError('point outside target window')
        target=user.WindowFromPoint(w.POINT(x,y))
        if user.GetAncestor(target,2) != hwnd: raise ValueError('target point is occluded by another window')
    return geometry


class ExtendedDesktop:
    def __init__(self, broker, native=False):
        self.b=broker
        self.native=native
        self.guard=win_window
        with self.b.store.connect(True) as db: ensure_schema(db)

    def row(self, run_id):
        with self.b.store.connect() as db:
            r=db.execute('SELECT * FROM assistant_desktop_runtime WHERE run_id=?',(run_id,)).fetchone()
        return dict(r) if r else None

    def fingerprint(self):
        if not self.native: return {'execution_kind':'simulation','catalog':catalog.FINGERPRINT}
        from tools.agent_desktop_campaign import agent_desktop_binary
        binary=Path(agent_desktop_binary())
        version=subprocess.run([str(binary),'--version'],capture_output=True,text=True,timeout=10,check=True).stdout.strip()
        if version != 'agent-desktop '+catalog.VERSION: raise ValueError('adapter version requires compatibility review: '+version)
        return {'version':version,'binary':str(binary),'sha256':hashlib.sha256(binary.read_bytes()).hexdigest(),
                'catalog':catalog.FINGERPRINT,'execution_kind':'live'}

    def configure(self, run_id, permissions=None):
        current=self.row(run_id)
        if permissions is None and current is None: return None
        if permissions is not None:
            if not isinstance(permissions,dict) or set(permissions)-set(catalog.PERMISSIONS) or any(type(v) is not bool for v in permissions.values()):
                raise ValueError('desktop_permissions accepts only known boolean permissions')
            selected={k:permissions.get(k,False) for k in catalog.PERMISSIONS}
            if current and json.loads(current['permissions']) != selected: raise ValueError('desktop permissions are frozen; start a fresh run')
        else: selected=json.loads(current['permissions'])
        fingerprint=self.fingerprint()
        policy=self.b.policy(run_id);scope={'access':policy['access'],'apps':policy['apps']}
        if current:
            if current['expired'] or current['expires_at'] <= self.b.clock(): raise ValueError('Desktop audit retention expired; resume rejected')
            if json.loads(current['fingerprint']) != fingerprint: raise ValueError('desktop binary/schema changed; start a reviewed fresh run')
            if json.loads(current['scope']) != scope: raise ValueError('desktop app scope is frozen; start a fresh run')
            return current
        with self.b.store.connect(True) as db:
            tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            session=db.execute('SELECT desktop_audit_expires FROM assistant_sessions WHERE id=?',(run_id,)).fetchone() if 'assistant_sessions' in tables else None
            if self.native and (not session or not session[0]): raise ValueError('full desktop access requires a retained research run')
            expires=float(session[0]) if session and session[0] else self.b.clock()+7*86400
            db.execute('INSERT INTO assistant_desktop_runtime(run_id,permissions,fingerprint,expires_at,scope) VALUES(?,?,?,?,?)',
                       (run_id,json.dumps(selected),json.dumps(fingerprint),expires,json.dumps(scope)))
        return self.row(run_id)

    def check(self, run_id):
        row=self.row(run_id)
        if row is None: raise ValueError('full desktop catalog is not enabled for this run')
        if row['expired'] or row['expires_at'] <= self.b.clock(): raise ValueError('Desktop audit retention expired; resume rejected')
        if json.loads(row['fingerprint']) != self.fingerprint(): raise ValueError('desktop binary/schema changed; resume rejected')
        policy=self.b.policy(run_id)
        if json.loads(row['scope'])!={'access':policy['access'],'apps':policy['apps']}: raise ValueError('desktop app scope changed; resume rejected')
        return row

    def schemas(self, run_id):
        row=self.row(run_id)
        if not row: return []
        perms=json.loads(row['permissions']);access=self.b.policy(run_id)['access']
        selected=set(json.loads(row['loaded_tools'])) | {'desktop_capabilities','desktop_load_tools'}
        if access=='propose': selected.add('desktop_propose_command')
        result=[]
        for name,entry in catalog.REGISTRY.items():
            if name not in selected: continue
            if name in catalog.LEGACY or entry['state'] in {'controller-owned','unsupported'} or name in catalog.RESTRICTED: continue
            if entry['permission'] and not perms.get(entry['permission']): continue
            if entry['mutation'] and access != 'autonomous': continue
            result.append(entry['schema'])
        return result

    def native_call(self, run_id, name, args, *, headed=False):
        """Private transport. Trusted args only; never callable as a model tool."""
        if not self.native: return self.b.runner(name,args)
        from tools.agent_desktop_broker import _desktop_environment,_run_desktop_cli
        from tools.agent_desktop_campaign import agent_desktop_binary
        row=self.check(run_id);root=private_root(run_id);root.mkdir(parents=True,exist_ok=True)
        env=_desktop_environment();env['AGENT_DESKTOP_HOME']=str(root)
        env.pop('AGENT_DESKTOP_SESSION',None);env.pop('AGENT_DESKTOP_AGENT_ID',None)
        binary=agent_desktop_binary()
        if not row['session_id']:
            started=_run_desktop_cli([binary,'session','start','--no-trace'],timeout=15,env=env)
            envelope=json.loads(started.stdout)
            data=envelope.get('data',{});sid=data.get('id') or data.get('session_id')
            if not envelope.get('ok') or not sid: raise ValueError('native session setup failed')
            with self.b.store.connect(True) as db:
                db.execute('UPDATE assistant_desktop_runtime SET session_id=?,state_root=? WHERE run_id=?',(sid,str(root),run_id))
            row=self.row(run_id)
        args=dict(args);stdin=None
        if name == 'desktop_type':
            stdin=args.pop('value');cmd=['type',args.pop('ref_id'),'--stdin']
            cmd += ['--timeout-ms='+str(args.pop('timeout_ms',30000))]
        elif name == 'desktop_set_value':
            stdin=args.pop('value');cmd=['set-value',args.pop('ref_id'),'--stdin']
        elif name == 'desktop_clipboard_set': stdin=args.pop('text');cmd=['clipboard-set','--stdin']
        elif name == 'desktop_press': cmd=['press',args.pop('keys'),'--window-id='+args.pop('window_id')]
        elif name == 'desktop_restore_window': cmd=catalog.command_args('desktop_restore',args)
        elif name == 'desktop_launch':
            # Retain exact configured launch targets; no arbitrary launch flags.
            return self.b.runner(name,args)
        else: cmd=catalog.command_args(name,args)
        argv=[binary,'--session',row['session_id'],'--trace',str(root/'audit.jsonl'),'--trace-strict']
        if headed: argv.append('--headed')
        try:
            response=_run_desktop_cli(argv+cmd,input_text=stdin,timeout=45,env=env)
            result=json.loads(response.stdout)
            if not isinstance(result,dict) or not isinstance(result.get('ok'),bool): raise ValueError('invalid native JSON envelope')
            if response.returncode and result.get('ok'): raise ValueError('native exit code contradicts successful JSON envelope')
            return result
        except subprocess.TimeoutExpired:
            return {'ok':False,'error':{'code':'CLI_ACTION_TIMEOUT' if name not in catalog.READS else 'CLI_PROCESS_TIMEOUT','details':{'layer':'cli_watchdog','seconds':45},'disposition':{'delivery':'unknown','retry':'unsafe'}}}
        except (ValueError,TypeError) as exc:
            return {'ok':False,'error':{'code':'INVALID_NATIVE_RESPONSE','message':str(exc),'disposition':{'delivery':'unknown','retry':'unsafe'}},
                    'native_response':{'stdout':response.stdout,'stderr':response.stderr,'returncode':response.returncode}}

    def invoke_legacy(self, run_id, name, args):
        if not self.row(run_id): return self.b.runner(name,args)
        row=self.check(run_id)
        catalog.validate(name,args)
        policy=self.b.policy(run_id)
        if args.get('surface') and args['surface'] not in {'window','focused','menu','menubar','sheet','popover','alert'}:
            raise ValueError('protected system surface is not in app scope')
        bound_app=args.get('app')
        for field in ('ref_id','root','element'):
            if args.get(field):
                observed_app=self.b._ref_app(run_id,args[field])
                self.b._allowed_app(policy,observed_app)
                if bound_app and observed_app.casefold()!=bound_app.casefold(): raise ValueError('reference belongs to another app')
                bound_app=observed_app
        if args.get('window_id'):
            observed_app=self.b._window_app(run_id,args['window_id'])
            if bound_app and observed_app.casefold()!=bound_app.casefold(): raise ValueError('window belongs to another app')
            self.b._allowed_app(policy,observed_app)
        headed=name in {'desktop_type','desktop_press'}
        if headed:
            if not json.loads(row['permissions'])['physical_input']: raise ValueError('physical_input permission required on Windows')
            if name=='desktop_press' and args.get('keys') in {'ctrl+c','ctrl+v','ctrl+x'} and not json.loads(row['permissions'])['clipboard']:
                raise ValueError('clipboard permission required for copy, cut or paste')
            app=self.b._ref_app(run_id,args['ref_id']) if args.get('ref_id') else self.b._window_app(run_id,args['window_id'])
            window=self.window(run_id,app,args.get('window_id'))
            self.safe_surface(run_id,app,window['id'])
            self.guard(window)
        result=self.native_call(run_id,name,args,headed=headed)
        if name in {'desktop_snapshot','desktop_find'} and result.get('ok') and args.get('window_id'):
            sid=result.get('data',{}).get('snapshot_id')
            if sid:
                with self.b.store.connect(True) as db:
                    db.execute('INSERT OR REPLACE INTO assistant_desktop_ref_windows VALUES(?,?,?)',(run_id,sid,args['window_id']))
        result.setdefault('broker',{}).update(execution_kind='live' if self.native else 'simulation',requested_mechanism='physical' if headed else 'semantic',registry_fingerprint=catalog.FINGERPRINT)
        if name in catalog.LEGACY_ACTIONS: self.invalidate(run_id)
        return result

    def window(self, run_id, app, window_id=None):
        if window_id and self.b._window_app(run_id,window_id).casefold()!=app.casefold(): raise ValueError('window belongs to another app')
        result=self.native_call(run_id,'desktop_list_windows',{'app':app})
        result,verified=self.b._verify_inventory_items(app,result)
        windows=result.get('data',[])
        if isinstance(windows,dict): windows=windows.get('windows',[])
        found=[x for x in windows if (not window_id or x.get('id')==window_id) and x.get('visible',True)]
        if len(found)!=1: raise ValueError('ambiguous or missing current target window')
        w=found[0]
        if self.native and w.get('id') not in verified: raise ValueError('unverified executable identity')
        return w

    def safe_surface(self, run_id, app, window_id):
        from tools.agent_desktop_broker import SENSITIVE_CONTROL,PERSISTENT_GRANT_CONTROL,ONE_TIME_APPROVAL_CONTROL,PROMPT_TEXT_INDICATOR
        result=self.native_call(run_id,'desktop_snapshot',{'app':app,'window_id':window_id,'include_bounds':True,'timeout_ms':30000})
        data=result.get('data',{})
        if not result.get('ok') or data.get('complete') is not True: raise ValueError('protected-surface check incomplete; narrow observation or wait')
        text=json.dumps(data,ensure_ascii=False)
        if any(pattern.search(text) for pattern in (SENSITIVE_CONTROL,PERSISTENT_GRANT_CONTROL,ONE_TIME_APPROVAL_CONTROL,PROMPT_TEXT_INDICATOR)):
            raise ValueError('protected authentication or command approval surface')
        event=self.b._audit_start(run_id,app,'desktop_snapshot',{'app':app,'window_id':window_id,'purpose':'pre_action_surface_check'})
        self.b._audit_finish(event,result)
        return result

    def invalidate(self, run_id):
        with self.b.store.connect(True) as db:
            db.execute('UPDATE assistant_desktop_artifacts SET valid=0 WHERE run_id=?',(run_id,))

    def artifact(self, run_id, kind, app, window_id, metadata, content=b''):
        row=self.check(run_id);identity=uuid.uuid4().hex
        with self.b.store.connect(True) as db:
            db.execute('INSERT INTO assistant_desktop_artifacts VALUES(?,?,?,?,?,?,?,?,?,?,1)',
                       (identity,run_id,kind,app,window_id,self.b.clock(),row['expires_at'],json.dumps(metadata),content,hashlib.sha256(content).hexdigest()))
        return identity

    def load_artifact(self, run_id, identity, kind):
        with self.b.store.connect() as db:
            row=db.execute('SELECT * FROM assistant_desktop_artifacts WHERE id=? AND run_id=? AND kind=?',(identity,run_id,kind)).fetchone()
        if not row or not row['valid'] or row['expires_at']<=self.b.clock() or row['created']<self.b.clock()-60:
            raise ValueError('stale, expired or foreign observation; capture again')
        return dict(row)

    def execute(self, run_id, name, arguments, decision_id=None):
        with _LOCK:
            return self._execute(run_id,name,arguments,decision_id)

    def _execute(self, run_id, name, arguments, decision_id, supervised=False):
        row=self.check(run_id);entry=catalog.validate(name,arguments)
        policy=self.b._require_access(run_id,('propose' if supervised else 'autonomous') if entry['mutation'] else 'observe')
        permissions=json.loads(row['permissions'])
        if entry['permission'] and not permissions[entry['permission']]: raise ValueError(entry['permission']+' permission is not enabled')
        if name=='desktop_propose_command':
            self.b._require_access(run_id,'propose')
            target=catalog.validate(arguments['tool'],arguments['arguments'])
            if not target['mutation'] or arguments['tool']=='desktop_batch' or arguments['tool'] in catalog.LEGACY:
                raise ValueError('propose a single new catalog mutation; use desktop_propose_action for legacy tools')
            if target['permission'] and not permissions[target['permission']]: raise ValueError('proposal lacks run permission')
            identity=uuid.uuid4().hex;now=self.b.clock()
            with self.b.store.connect(True) as db:
                db.execute('''INSERT INTO assistant_desktop_proposals(id,run_id,tool,arguments,reason,state,created,updated)
                    VALUES(?,?,?,?,?,'pending',?,?)''',(identity,run_id,arguments['tool'],json.dumps(arguments['arguments']),arguments['reason'],now,now))
            return json.dumps({'ok':True,'proposal_id':identity,'state':'pending','executed':False})
        if name=='desktop_load_tools':
            for tool in arguments['tools']:
                item=catalog.REGISTRY[tool]
                if item['state'] in {'unsupported','controller-owned'} or tool in catalog.RESTRICTED:
                    raise ValueError(tool+' is not model-accessible')
                if item['permission'] and not permissions[item['permission']]: raise ValueError(tool+' permission is not enabled')
                if item['mutation'] and policy['access']!='autonomous': raise ValueError('mutation requires autonomous access')
            event=self.b._audit_start(run_id,None,name,arguments,decision_id)
            with self.b.store.connect(True) as db:
                db.execute('UPDATE assistant_desktop_runtime SET loaded_tools=? WHERE run_id=?',(json.dumps(arguments['tools']),run_id))
            result={'ok':True,'data':{'loaded_tools':arguments['tools'],'effective':'next model request','permissions_unchanged':True}}
            self.b._audit_finish(event,result)
            return json.dumps(result)
        # Existing workflow contracts only authorize their original submission
        # protocol. New mutation tools cannot bypass its frozen prompt checks.
        if entry['mutation']:
            from tools.sabrina_workflow import workflow_attempt
            with self.b.store.connect() as db: workflow=workflow_attempt(db,run_id)
            if workflow is not None and name not in {'desktop_scroll','desktop_scroll_to','desktop_expand','desktop_collapse','desktop_hover','desktop_mouse_move'}:
                raise ValueError('workflow contract does not authorize this new mutation; use its recorded submission protocol')
        if name=='desktop_capabilities':
            offset=arguments.get('offset',0);limit=arguments.get('limit',20)
            entries=[{'tool':n,'classification':e['state'],'permission':e['permission'],
                      'enabled':e['state'] not in {'unsupported','controller-owned'} and n not in catalog.RESTRICTED and (not e['permission'] or permissions[e['permission']])} for n,e in catalog.REGISTRY.items()]
            return json.dumps({'ok':True,'data':{'version':catalog.VERSION,'fingerprint':json.loads(row['fingerprint']),
                'permissions':permissions,'commands':entries[offset:offset+limit],'total':len(entries),'next_offset':offset+limit if offset+limit<len(entries) else None,
                'visual_interpreter':'unavailable until an operator-configured, tested vision provider is supplied'}})
        self.b.acquire_desktop_lease(run_id,ttl_seconds=180,purpose=name)
        if name=='desktop_batch':
            results=[]
            for step in arguments['steps']:
                tool=step['tool']
                if tool=='desktop_batch': raise ValueError('nested batches prohibited')
                result=json.loads(self.b.dispatch(run_id,tool,step['arguments'],decision_id))
                results.append(result)
                disposition=result.get('data',{}).get('disposition',{}) if isinstance(result.get('data'),dict) else {}
                if not result.get('ok') or disposition.get('delivery') in {'unknown','delivery_uncertain','delivered_unverified'}: break
            return json.dumps({'ok':len(results)==len(arguments['steps']) and all(r.get('ok') for r in results),'data':{'results':results,'non_atomic':True}})
        args=dict(arguments);app=args.get('app');window_id=args.get('window_id');window=None
        if args.get('ref_id'):
            app=self.b._ref_app(run_id,args['ref_id'])
            if entry['mutation']: self.b._validate_element_action(run_id,'desktop_click',args['ref_id'])
        if (args.get('observation_id') and name.startswith('desktop_mouse')) or name=='desktop_drag':
            saved=self.load_artifact(run_id,args['observation_id'],'screenshot');app=saved['app']
            if saved['window_id']!=window_id: raise ValueError('screenshot belongs to another window')
        if app: app=self.b._allowed_app(policy,app)
        if app or name not in {'desktop_version','desktop_permissions','desktop_list_displays','desktop_skills_list','desktop_skills_get','desktop_trace_show'}:
            self.b._require_cross_app_scope(run_id,app,name,args)
        if window_id:
            app=app or self.b._window_app(run_id,window_id)
            window=self.window(run_id,app,window_id)
        if entry['mutation'] and app and window_id: self.safe_surface(run_id,app,window_id)
        if name in catalog.REF_ACTIONS and not window: raise ValueError('fresh window_id required')
        if entry['physical']:
            if not window: raise ValueError('physical input requires a verified target window')
            self.guard(window)
        if name=='desktop_screenshot':
            self.safe_surface(run_id,app,window_id)
            geometry=self.guard(window)
        event=self.b._audit_start(run_id,app,name,arguments,decision_id)
        operation=uuid.uuid4().hex
        native_result=None
        try:
            if name in {'desktop_mouse_move','desktop_mouse_click','desktop_mouse_wheel','desktop_drag'}:
                metadata=json.loads(saved['metadata']);geometry=self.guard(window)
                if geometry!=metadata['geometry']: raise ValueError('window geometry or DPI changed; recapture')
                points=[(geometry['x']+args['x'],geometry['y']+args['y'])]
                if name=='desktop_drag': points.append((geometry['x']+args['to_x'],geometry['y']+args['to_y']))
                self.guard(window,points)
                # Coordinates cannot bypass control-level safety: require a fresh
                # complete tree and reject protected labels covering either point.
                snapshot=self.safe_surface(run_id,app,window_id)
                self._safe_points(snapshot,points)
                self.guard(window,points)  # focus can change while reading the tree
                args.pop('observation_id');args.pop('window_id');args.pop('x');args.pop('y')
                if name=='desktop_drag':
                    args.pop('to_x');args.pop('to_y');args.update(from_xy='%d,%d'%points[0],to_xy='%d,%d'%points[1])
                elif name=='desktop_mouse_wheel': args.update(x=points[0][0],y=points[0][1])
                else: args['xy']='%d,%d'%points[0]
            elif name in catalog.REF_ACTIONS:
                # Refs have their own native source-window identity. Confirm it.
                current=self.safe_surface(run_id,app,window_id)
                prior=self.b.native_ref_window(run_id,arguments['ref_id'])
                if prior != window_id: raise ValueError('reference is not bound to the requested window')
                args.pop('window_id')
                if entry['physical']: self.guard(window)
            elif name in {'desktop_dismiss_notification','desktop_notification_action'}:
                saved=self.load_artifact(run_id,args.pop('observation_id'),'notifications')
                if saved['app'].casefold()!=app.casefold(): raise ValueError('notification belongs to another app')
                items=json.loads(saved['metadata']).get('items',[])
                target=next((x for x in items if x.get('index')==args['index']),None)
                if not target or str(target.get('app','')).casefold()!=app.casefold(): raise ValueError('unverified notification ownership')
                args['expected_app']=app;args['expected_title']=str(target.get('title',''))
                if name=='desktop_notification_action': args.pop('app',None)
            elif name=='desktop_close_app': args.pop('window_id')
            elif name=='desktop_screenshot':
                # 1.2.0 resolves --app before --window-id for screenshot and
                # rejects multiple processes. The broker has already verified
                # the exact window's executable; send that identity alone.
                args.pop('app',None)
            result=self.native_call(run_id,name,args,headed=entry['physical'])
            native_result=result
            if name=='desktop_screenshot' and result.get('ok'):
                data=result.get('data',{});encoded=data if isinstance(data,str) else data.get('data') or data.get('base64') or data.get('image_base64') or data.get('png_base64')
                if not encoded: raise ValueError('screenshot returned no PNG data')
                content=base64.b64decode(encoded,validate=True)
                if not content.startswith(b'\x89PNG\r\n\x1a\n'): raise ValueError('invalid screenshot PNG')
                if self.native and (data.get('width')!=geometry['width'] or data.get('height')!=geometry['height']):
                    raise ValueError('captured image dimensions differ from verified frame; coordinate input blocked')
                identity=self.artifact(run_id,'screenshot',app,window_id,{'geometry':geometry},content)
                # Complete native response remains in audit; model gets a bounded descriptor.
                result['artifact']={'observation_id':identity,'sha256':hashlib.sha256(content).hexdigest(),'geometry':geometry,'vision_status':'unavailable'}
            elif name=='desktop_list_notifications' and result.get('ok'):
                data=result.get('data',{});items=data if isinstance(data,list) else data.get('notifications',[])
                items=[x for x in items if str(x.get('app','')).casefold()==app.casefold()]
                result['data']={'notifications':items,'observation_id':self.artifact(run_id,'notifications',app,'',{'items':items})}
        except Exception as exc:
            result={'ok':False,'error':{'code':'DESKTOP_OPERATION_BLOCKED','message':str(exc),'disposition':{'delivery':'unknown','retry':'unsafe'}}}
            if native_result is not None: result['native_result']=native_result
        result['broker']={'operation_id':operation,'registry_fingerprint':catalog.FINGERPRINT,'session_id':self.row(run_id)['session_id'],
                          'requested_mechanism':'physical' if entry['physical'] else 'semantic','execution_kind':'live' if self.native else 'simulation'}
        self.b._audit_finish(event,result)
        if entry['mutation']:
            self.invalidate(run_id)
            with self.b.store.connect(True) as db:
                for table in ('assistant_desktop_refs','assistant_desktop_elements','assistant_desktop_windows'):
                    db.execute(f'DELETE FROM {table} WHERE run_id=?',(run_id,))
            if app and window_id and name!='desktop_close_app':
                try:
                    self.b.observe(run_id,'desktop_list_windows',{'app':app},decision_id)
                    verification=json.loads(self.b.observe(run_id,'desktop_snapshot',{'app':app,'window_id':window_id},decision_id))
                    result['post_observation']={'ok':verification.get('ok'),'snapshot_id':verification.get('data',{}).get('snapshot_id')}
                except Exception as exc: result['post_observation']={'ok':False,'error':str(exc)}
        if name=='desktop_screenshot' and result.get('ok'):
            return json.dumps({'ok':True,'data':result['artifact'],'broker':result['broker']})
        from tools.agent_desktop_broker import bounded_desktop_json
        return bounded_desktop_json(result,12000)

    def decide(self, run_id, proposal_id, approve, reason):
        self.b._require_access(run_id,'propose');self.check(run_id)
        with _LOCK:
            with self.b.store.connect(True) as db:
                row=db.execute('SELECT * FROM assistant_desktop_proposals WHERE id=? AND run_id=?',(proposal_id,run_id)).fetchone()
                if not row or row['state']!='pending': raise ValueError('proposal unavailable or already decided')
                state='executing' if approve else 'rejected'
                changed=db.execute("UPDATE assistant_desktop_proposals SET state=?,decision_reason=?,updated=? WHERE id=? AND state='pending'",(state,reason,self.b.clock(),proposal_id)).rowcount
                if changed!=1: raise ValueError('proposal decision conflict')
            if not approve: return {'proposal_id':proposal_id,'state':'rejected','executed':False}
            try: result=json.loads(self._execute(run_id,row['tool'],json.loads(row['arguments']),None,supervised=True))
            except Exception as exc: result={'ok':False,'error':{'code':'PROPOSAL_BLOCKED','message':str(exc)}}
            state='executed' if result.get('ok') else 'failed'
            with self.b.store.connect(True) as db:
                db.execute('UPDATE assistant_desktop_proposals SET state=?,result=?,updated=? WHERE id=?',(state,json.dumps(result),self.b.clock(),proposal_id))
            return {'proposal_id':proposal_id,'state':state,'executed':bool(result.get('ok')),'result':result}

    @staticmethod
    def _safe_points(snapshot, points):
        from tools.agent_desktop_broker import SENSITIVE_CONTROL,DESTRUCTIVE_CONVERSATION_CONTROL,PERSISTENT_GRANT_CONTROL,ONE_TIME_APPROVAL_CONTROL
        def walk(node):
            if isinstance(node,dict):
                yield node
                for child in node.values(): yield from walk(child)
            elif isinstance(node,list):
                for child in node: yield from walk(child)
        for node in walk(snapshot):
            bounds=node.get('bounds',{})
            if not all(k in bounds for k in ('x','y','width','height')): continue
            if any(bounds['x']<=x<bounds['x']+bounds['width'] and bounds['y']<=y<bounds['y']+bounds['height'] for x,y in points):
                label=' '.join(str(node.get(k,'')) for k in ('name','role','description'))
                if any(p.search(label) for p in (SENSITIVE_CONTROL,DESTRUCTIVE_CONVERSATION_CONTROL,PERSISTENT_GRANT_CONTROL,ONE_TIME_APPROVAL_CONTROL)):
                    raise ValueError('protected control at pointer target')
