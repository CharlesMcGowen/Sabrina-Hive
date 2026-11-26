"""Explicit, attributable research dispatch and fixed sealed replay from desktop chat."""
from __future__ import annotations
import hashlib,json,os,re,subprocess,sys,time,urllib.error,urllib.request,uuid
from pathlib import Path

CONSOLE=Path(__file__).resolve().parents[1]
CONFIG=CONSOLE/'research_console_config.json'
_config=json.loads(CONFIG.read_text(encoding='utf-8')) if CONFIG.is_file() else {}
PROJECT=Path(_config.get('project_root',str(CONSOLE.parent/'Astra'/'polymarket-btc-offline'))).resolve()
if not PROJECT.is_dir(): raise RuntimeError('Research project_root is not an existing directory')
HELP='Commands: /research codex QUESTION; /research hf:PORTAL_ID QUESTION; /research council QUESTION; /codex QUESTION; /experiment polymarket. Research council asks OpenAI Codex and local Qwen3 and one admitted public HF endpoint independently; explicit targets: /research codex,local,hf:PORTAL_ID QUESTION; /research roster. Receipts are in Polymarket console_runs. The experiment reruns only sealed local inputs; it places no orders.'

def write(path,value):
    path.write_text(json.dumps(value,indent=2,ensure_ascii=False)+'\n',encoding='utf-8')

def rpc(path,payload=None,timeout=15):
    base=os.environ.get('SABRINA_SWARM_API_BASE','http://127.0.0.1:8095').rstrip('/')
    req=urllib.request.Request(base+path,data=None if payload is None else json.dumps(payload).encode(),headers={'Content-Type':'application/json'})
    with urllib.request.urlopen(req,timeout=timeout) as response:return json.load(response)

def codex_research(question,run,progress):
    from tools.sabrina_delegation_bridge import CodexDelegationBridge,find_codex_binary
    binary=find_codex_binary()
    if not binary:return {'ok':False,'platform':'OpenAI Codex','failure':'cli_unavailable','reply':''}
    auth=subprocess.run([binary,'login','status'],capture_output=True,text=True,timeout=15,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    authenticated=auth.returncode==0
    if not authenticated:return {'ok':False,'platform':'OpenAI Codex','failure':'authentication_unavailable','reply':''}
    work=run/'public_workspace';work.mkdir(exist_ok=True)
    (work/'AGENTS.md').write_text('Use only the public prompt. Do not inspect files, run tools, access services, or place orders. Return the requested research note.\n')
    def actual_runner(command,timeout):
        # This is the existing bridge's real subprocess extension, not a model stub.
        command=[command[0],'--disable','shell_tool',*command[1:]]
        write(run/'codex_process.json',{'command_prefix':command[:-1],'cwd':str(work),'sandbox':'read-only','shell_tool':False,'auth_verified':authenticated})
        result=subprocess.run(command,cwd=work,stdin=subprocess.DEVNULL,capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=timeout,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        return result.returncode,result.stdout,result.stderr
    bridge=CodexDelegationBridge(db_path=PROJECT/'console_runs'/'delegations.sqlite3',codex_bin=binary,runner=actual_runner)
    operation=run.name+'-codex'
    progress('OpenAI Codex: submitting '+operation)
    contract={'requirement':'Provide a concise public research critique (at most 180 words). Do not claim you executed experiments. '+question,
              'files':['research_note.txt'],'artifact_format':'text','timeout':180}
    assignment=bridge.submit(contract,operation)
    write(run/'codex_assignment.json',assignment)
    if assignment['state'] in {'failed','blocked'}:
        return {'ok':False,'platform':'OpenAI Codex','assignment_id':assignment['id'],'worker_id':assignment.get('conversation_key'),
                'failure':assignment.get('failure_kind'),'error':assignment.get('error_detail'),'delivery':assignment.get('delivery'),'reply':''}
    collected=bridge.collect(assignment['id']);write(run/'codex_collected.json',collected)
    return {'ok':True,'platform':'OpenAI Codex','provider':'OpenAI via authenticated Codex CLI',
            'model':'not_reported_by_cli','assignment_id':assignment['id'],'worker_id':collected['conversation_key'],
            'artifact_hash':collected['artifact_hash'],'reply':collected['artifact']['research_note.txt'],'metrics':collected['metrics']}

def hf_research(question,portal,run,progress):
    operation=run.name+'-'+hashlib.sha256(portal.encode()).hexdigest()[:10];payload={'operation_id':operation,'message':question,'mode':'direct_node','target':'portal:'+portal}
    write(run/('hf_'+portal+'_submission.json'),payload);progress('Public HF: submitting '+operation)
    receipt=None
    try:receipt=rpc('/api/swarm-chat/jobs',payload)
    except urllib.error.HTTPError as exc:
        if exc.code<500:return {'ok':False,'platform':'Public HF','worker_id':portal,'job_id':operation,'failure':'http_'+str(exc.code),'reply':''}
    except (OSError,ValueError):pass
    deadline=time.monotonic()+180
    while time.monotonic()<deadline:
        if receipt and receipt.get('state') in {'complete','failed','interrupted'}:
            write(run/('hf_'+portal+'_receipt.json'),receipt)
            responses=(receipt.get('result') or {}).get('responses',[])
            result=responses[0] if responses else {}
            return {'ok':bool(result.get('ok')),'platform':'Public HF','model':'underlying_model_unverified',
                    'worker_id':portal,'job_id':operation,'request_id':result.get('request_id'),
                    'state':receipt['state'],'failure':'' if result.get('ok') else result.get('failure') or result.get('error') or receipt.get('progress'),
                    'reply':result.get('reply',''),'source':result.get('source')}
        try:receipt=rpc('/api/swarm-chat/jobs/'+operation)
        except urllib.error.HTTPError as exc:
            if exc.code==404:return {'ok':False,'platform':'Public HF','worker_id':portal,'job_id':operation,'failure':'submission_outcome_unknown_no_resubmit','reply':''}
        except (OSError,ValueError):pass
        time.sleep(1)
    return {'ok':False,'platform':'Public HF','worker_id':portal,'job_id':operation,'failure':'receipt_wait_timeout_no_resubmit','reply':''}

def pick_hf():
    nodes=rpc('/api/swarm-chat/roster').get('external_nodes',[])
    preferred=['hf-gradio-himakesh-parth-ai-tutor','hf-gradio-affanaiengr-rag','hf-gradio-dkt3-digital-twin','hf-gradio-jayeshmurthi49-byte-agentic-rag-chatbot-api']
    eligible={n['portal_id'] for n in nodes if n.get('chat_eligible')}
    return next((p for p in preferred if p in eligible),next(iter(sorted(eligible)),None))

def local_research(question,run,progress):
    progress('Local Qwen3: public research request')
    payload={'model':'qwen3:14b','stream':False,'think':False,'messages':[{'role':'user','content':'Public research only; do not claim experiments. Answer in at most 180 words. '+question}],'options':{'num_predict':384,'num_ctx':4096,'temperature':0}}
    cmd=['ssh','-o','BatchMode=yes','-o','ConnectTimeout=5','ego','curl -sS --max-time 160 -H "Content-Type: application/json" --data-binary @- http://127.0.0.1:11434/api/chat']
    result=subprocess.run(cmd,input=json.dumps(payload),capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=180,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    write(run/'local_process.json',{'exit_code':result.returncode,'stdout':result.stdout,'stderr':result.stderr})
    if result.returncode:return {'ok':False,'platform':'Ego Ollama','failure':'transport_exit_'+str(result.returncode),'reply':''}
    data=json.loads(result.stdout);write(run/'local_receipt.json',data)
    reply=data.get('message',{}).get('content','')
    return {'ok':bool(reply),'platform':'Ego Ollama','model':data.get('model'),'worker_id':'ego/ollama/'+data.get('model','unknown'),'reply':reply,'done':data.get('done')}

def execute_command(command,progress=None,cancel_event=None):
    progress=progress or (lambda message:None)
    clean=command.strip()
    if clean=='/research roster':return {'ok':True,'reply':json.dumps(rpc('/api/swarm-chat/roster'),indent=2)}
    if clean in {'/research','/research help','/codex','/experiment'}:return {'ok':True,'reply':HELP}
    if clean.startswith('/experiment'):
        if clean!='/experiment polymarket':return {'ok':False,'error':'Only /experiment polymarket is supported. No user shell or paths are accepted.'}
        driver=PROJECT/'run_sealed_replay.py'
        if not driver.is_file():return {'ok':False,'error':'The sealed Polymarket replay driver is not installed yet.'}
        run=PROJECT/'console_runs'/('experiment-'+uuid.uuid4().hex);run.mkdir(parents=True)
        progress('Running sealed offline Polymarket replay; receipt '+str(run/'launcher_receipt.json'))
        intent={'command':[sys.executable,str(driver)],'cwd':str(PROJECT),'started':time.time(),'run_id':run.name,'data_only':True}
        write(run/'intent.json',intent);env=dict(os.environ);env['SABRINA_CONSOLE_RUN_ID']=run.name
        try:
            result=subprocess.run(intent['command'],cwd=PROJECT,env=env,capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=1800,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            write(run/'stdout.json',{'stdout':result.stdout,'stderr':result.stderr})
            receipt={**intent,'finished':time.time(),'exit_code':result.returncode,'ok':result.returncode==0,'driver_receipt':str(run/'receipt.json')}
        except subprocess.TimeoutExpired:receipt={**intent,'finished':time.time(),'ok':False,'failure':'1800_second_process_budget_exhausted'}
        write(run/'launcher_receipt.json',receipt)
        return {'ok':receipt['ok'],'reply':('Sealed replay completed.' if receipt['ok'] else 'Sealed replay failed; inspect receipt.')+'\n'+str(run/'launcher_receipt.json'),'receipt_path':str(run/'launcher_receipt.json')}
    if clean.startswith('/codex '):target='codex';question=clean.split(' ',1)[1]
    elif clean.startswith('/research '):
        parts=clean.split(' ',2)
        if len(parts)!=3:return {'ok':False,'error':HELP}
        _,target,question=parts
    else:raise ValueError('Unsupported research command')
    if len(question)>8000 or re.search(r'\bsk-[A-Za-z0-9_-]{12,}|-----BEGIN .*PRIVATE KEY|\bBearer\s+\S+',question):
        return {'ok':False,'error':'Use a bounded public research question without credentials.'}
    targets=['codex','local','hf:auto'] if target=='council' else target.split(',')
    if len(targets)>6 or len(set(targets))!=len(targets) or any(t not in {'codex','openai','local'} and not re.fullmatch(r'hf:[a-zA-Z0-9_-]+',t) for t in targets):return {'ok':False,'error':HELP}
    run=PROJECT/'console_runs'/('research-'+uuid.uuid4().hex);run.mkdir(parents=True)
    write(run/'intent.json',{'question':question,'question_sha256':hashlib.sha256(question.encode()).hexdigest(),'targets':targets,'created':time.time()})
    results=[]
    for destination in targets:
        if cancel_event and cancel_event.is_set():
            results.append({'ok':False,'platform':destination,'failure':'cancelled_before_dispatch','reply':''});continue
        try:
            if destination in {'codex','openai'}:result=codex_research(question,run,progress)
            elif destination=='local':result=local_research(question,run,progress)
            else:
                portal=pick_hf() if destination=='hf:auto' else destination[3:]
                result=hf_research(question,portal,run,progress) if portal else {'ok':False,'platform':'Public HF','failure':'no_admitted_endpoint','reply':''}
            result['target']=destination;results.append(result)
        except Exception as exc:results.append({'ok':False,'platform':destination,'failure':type(exc).__name__,'error':str(exc),'reply':''})
    succeeded=sum(bool(r.get('ok')) for r in results)
    reply=f'{succeeded}/{len(results)} platforms answered.\n\n'+'\n\n'.join('['+r['platform']+']\n'+(r.get('reply') or r.get('failure') or r.get('error','No response')) for r in results)
    receipt={'ok':succeeded>0,'complete':succeeded==len(results),'run_id':run.name,'question':question,'target':target,'responses':results,'reply':reply,'receipt_path':str(run/'receipt.json')}
    write(run/'receipt.json',receipt);return receipt
