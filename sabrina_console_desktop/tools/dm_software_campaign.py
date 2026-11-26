"""Bounded DM campaigns with executable worker artifacts and external grading.

Creates exploratory practice data, not a paper benchmark score or weight update.
The model writes code only into an isolated, networkless Python container.
"""
import argparse
import ast
import gc
import hashlib
import json
from pathlib import Path
import re
import subprocess
import time
import urllib.request
import uuid

from dm_campaign_tasks import FAMILIES, make_task, reference
from dm_weight_lab import DMLab, digest, dump

DM = 'huihui_ai/qwen3-abliterated:14b'


class SandboxInfrastructureError(RuntimeError):
    """Unavailable execution infrastructure must not become a model failure score."""


SCHEMA = {'type':'object','properties':{
    'title':{'type':'string'},'hypothesis':{'type':'string'},
    'tasks':{'type':'array','minItems':6,'maxItems':6,'items':{'type':'object','properties':{
        'family':{'type':'string','enum':FAMILIES},'variant':{'type':'integer','minimum':0,'maximum':3},
        'quest_title':{'type':'string'}},'required':['family','variant','quest_title'],'additionalProperties':False}}},
    'required':['title','hypothesis','tasks'],'additionalProperties':False}
REPAIR_SCHEMA = {'type':'object','properties':{'advice':{'type':'array','maxItems':2,'items':{
    'type':'object','properties':{'task_id':{'type':'string'},'guidance':{'type':'string'}},
    'required':['task_id','guidance'],'additionalProperties':False}}},'required':['advice'],'additionalProperties':False}
CHILD = '''import contextlib, copy, io, json, runpy, sys
with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
    namespace = runpy.run_path('/task/solution.py')
data = json.load(sys.stdin)
out = []
for args in data:
    before = copy.deepcopy(args)
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            value = namespace['solve'](*args)
        out.append({'value':value,'mutated':args != before})
    except Exception as error:
        out.append({'error':type(error).__name__+': '+str(error)[:300]})
print(json.dumps(out, allow_nan=False))
'''


def release(lab):
    lab.model=None; lab.tokenizer=None
    gc.collect()
    import torch
    torch.cuda.empty_cache()


def ask_dm(root, name, messages, schema):
    """Save actual requests/responses; do not silently substitute operator prose."""
    for attempt in range(2):
        payload={'model':DM,'messages':messages,'format':schema,'stream':False,'think':False,
                 'keep_alive':0,'options':{'temperature':0,'num_ctx':4096,'num_predict':900,'seed':1701}}
        dump(root/f'{name}-request-{attempt}.json',payload)
        request=urllib.request.Request('http://127.0.0.1:11434/api/chat',data=json.dumps(payload).encode(),headers={'Content-Type':'application/json'})
        with urllib.request.urlopen(request,timeout=180) as response: raw=json.load(response)
        dump(root/f'{name}-response-{attempt}.json',raw)
        try:
            value=json.loads(raw['message']['content'])
            if not isinstance(value,dict): raise ValueError('Expected JSON object')
            return value
        except (ValueError,KeyError) as error:
            messages=messages+[{'role':'user','content':f'No valid JSON object received: {error}. Return exactly the requested JSON object.'}]
    raise RuntimeError('DM failed twice; campaign did not silently continue')


def sandbox(task, code_path, image, output):
    """Expected outputs never enter the model's execution container."""
    code=code_path.read_text()
    try:
        tree=ast.parse(code)
        if not any(isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)) and n.name=='solve' for n in tree.body):
            raise ValueError('Missing top-level solve function')
    except (SyntaxError,ValueError) as error:
        result={'passed':False,'passed_tests':0,'total_tests':len(task['tests']),'error':str(error),'failures':[]}
        dump(output,result);return result
    ident='sabrina-campaign-'+uuid.uuid4().hex[:12]
    cmd=['docker','run','--rm','--pull=never','--name',ident,'--network=none','--read-only',
         '--cap-drop=ALL','--security-opt=no-new-privileges','--pids-limit=32','--memory=192m',
         '--cpus=1','--user=65534:65534','--log-driver=none','--tmpfs=/tmp:rw,noexec,nosuid,size=32m',
         '--mount',f'type=bind,src={code_path.resolve()},dst=/task/solution.py,readonly',
         '-i',image,'python','-I','-B','-c',CHILD]
    inputs=[t['args'] for t in task['tests']]
    try:
        completed=subprocess.run(cmd,input=json.dumps(inputs),capture_output=True,text=True,timeout=15)
        if completed.returncode in [125,126,127]:
            raise SandboxInfrastructureError(f'Docker could not start the fixed grader: {completed.stderr[-1000:]}')
        if completed.returncode:
            raise RuntimeError(f'Container exit {completed.returncode}: {completed.stderr[-1000:]}')
        if len(completed.stdout)>1000000: raise ValueError('Output limit exceeded')
        rows=json.loads(completed.stdout)
        if not isinstance(rows,list) or len(rows)!=len(inputs): raise ValueError('Invalid grader output shape')
        failures=[]
        forbid_mutation=task['family'] in ['pagination','dependencies','redaction']
        for i,(test,row) in enumerate(zip(task['tests'],rows)):
            # Strict type equality prevents True from passing an expected integer 1.
            same='value' in row and json.dumps(row['value'],sort_keys=True)==json.dumps(test['expected'],sort_keys=True)
            if not same or (forbid_mutation and row.get('mutated')):
                failures.append({'test':i,'args':test['args'],'expected':test['expected'],'actual':row})
        result={'passed':not failures,'passed_tests':len(inputs)-len(failures),'total_tests':len(inputs),
                'failures':failures[:3],'failed_test_count':len(failures),'container_image':image}
    except subprocess.TimeoutExpired:
        subprocess.run(['docker','kill',ident],capture_output=True,timeout=10)
        result={'passed':False,'passed_tests':0,'total_tests':len(inputs),'error':'Execution timeout','failures':[]}
    except (SandboxInfrastructureError,FileNotFoundError):
        raise
    except Exception as error:
        result={'passed':False,'passed_tests':0,'total_tests':len(inputs),'error':str(error),'failures':[]}
    dump(output,result);return result


def worker(lab, messages, folder):
    import torch
    # Ollama's keep_alive=0 response can precede CUDA memory release.
    # Wait for room without unloading any unrelated lab service.
    needed=sum(p.stat().st_size for p in lab.files)*1.5
    if lab.model is None:
        deadline=time.monotonic()+30
        while torch.cuda.mem_get_info()[0]<needed and time.monotonic()<deadline:
            time.sleep(1)
    lab._load()
    rendered=lab.tokenizer.apply_chat_template(messages,tokenize=False,add_generation_prompt=True)
    ids=lab.tokenizer.encode(rendered,add_special_tokens=False)
    if len(ids)>3072: raise ValueError('Worker context budget exceeded')
    started=time.monotonic()
    with torch.inference_mode():
        generated=lab.model.generate(input_ids=torch.tensor([ids],device='cuda'),max_new_tokens=768,
                                     do_sample=False,use_cache=True,pad_token_id=lab.tokenizer.eos_token_id)
    tokens=generated[0,len(ids):].tolist();answer=lab.tokenizer.decode(tokens,skip_special_tokens=True)
    fenced=re.findall(r'```(?:python|py)?\s*\n(.*?)```',answer,re.S)
    code=fenced[0].strip() if fenced else answer.strip()
    folder.mkdir(parents=True,exist_ok=False)
    (folder/'solution.py').write_text(code+'\n')
    dump(folder/'generation.json',{'messages':messages,'answer':answer,'tokens':len(tokens),
                                  'hit_token_limit':len(tokens)==768,'seconds':time.monotonic()-started,
                                  'prompt_tokens':len(ids),'training_steps':0,'weight_edits':0})
    return folder/'solution.py'


def freeze_benchmark(root):
    """Freeze unique task specifications, not inflated counts of seed variants."""
    tasks=[]; seen=set()
    for i,family in enumerate(FAMILIES):
        for variant in range(4):
            task=make_task(family,variant,880000+i*100+variant)
            if task['spec'] in seen: continue
            seen.add(task['spec']);task['id']=f'{family}-v{variant}';tasks.append(task)
    dump(root/'frozen_evaluation.json',{'tasks':tasks,'label':'custom family-level pilot, not a public benchmark',
        'independence':'Task families overlap development. Inputs are held out; this does not establish generalization to new task families.',
        'state':'not_run; do not send test fixtures or expected outputs to the DM'})
    return tasks


def run(root, cycles, resume=False):
    import fcntl
    root.parent.mkdir(parents=True,exist_ok=True)
    lock=(root.parent/'.dm_weight_lab.lock').open('a');fcntl.flock(lock.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
    root.mkdir(exist_ok=resume)
    source=json.loads((Path.home()/'.config/sabrina/weight_space_models.json').read_text())['models']['qwen2.5-coder-1.5b']
    image=subprocess.check_output(['docker','image','inspect','python:3.11-slim','--format','{{.Id}}'],text=True).strip()
    if resume:
        protocol=json.loads((root/'protocol.json').read_text())
        assert protocol['cycles']==cycles and protocol['container_image']==image
        assert protocol['source_sha256']==digest(Path(source)/'model.safetensors')
        assert protocol['frozen_evaluation_sha256']==digest(root/'frozen_evaluation.json')
        benchmark=json.loads((root/'frozen_evaluation.json').read_text())['tasks']
    else:
        benchmark=freeze_benchmark(root)
        protocol={'cycles':cycles,'max_worker_generations':8*cycles,'max_dm_requests':4*cycles,'max_new_tokens':768,
              'max_wall_seconds':1500,'development_seed':61000,'worker_source':source,'source_sha256':digest(Path(source)/'model.safetensors'),
              'container_image':image,'frozen_evaluation_sha256':digest(root/'frozen_evaluation.json'),
              'claim':'Executable exploratory task success and feedback-assisted repair only. No weight improvement claim.',
              'frozen_evaluation':'Not consumed by the DM, and not evaluated during this exploratory batch.',
              'training_steps':0,'weight_edits':0}
        dump(root/'protocol.json',protocol)
    # Test the grading path before collecting worker measurements.
    controls=root/'controls';controls.mkdir(exist_ok=resume)
    for task in benchmark:
        existing=controls/(task['id']+'.json')
        if resume and existing.exists() and json.loads(existing.read_text())['passed']: continue
        p=controls/(task['id']+'.py');p.write_text(reference(task))
        check=sandbox(task,p,image,controls/(task['id']+'.json'))
        if not check['passed']: raise RuntimeError(f'Reference failed for {task["id"]}: {check}')
    wrong=controls/'wrong.py';wrong.write_text('def solve(*args): return None\n')
    for family in FAMILIES:
        task=next(t for t in benchmark if t['family']==family)
        existing=controls/(family+'-negative.json')
        if resume and existing.exists() and not json.loads(existing.read_text())['passed']: continue
        if sandbox(task,wrong,image,controls/(family+'-negative.json'))['passed']:
            raise RuntimeError('Broken implementation passed grader')
    print(json.dumps({'phase':'grader_verified','unique_frozen_specs':len(benchmark),'families':FAMILIES}),flush=True)
    history_path=root.parent/'software_campaign_history.json'
    history=json.loads(history_path.read_text()) if history_path.exists() else []
    history=[h for h in history if h.get('run')!=root.name]
    session_name='worker_source_session' if not resume else 'worker_source_recovery_'+uuid.uuid4().hex[:8]
    lab=DMLab(source,root/session_name)
    began=time.monotonic()-max(0,time.time()-(root/'protocol.json').stat().st_mtime)
    all_results=[]
    generations=len(list(root.glob('campaign-*/*/*/generation.json')))
    try:
        for cycle in range(cycles):
            if time.monotonic()-began>protocol['max_wall_seconds']: raise TimeoutError('Campaign wall budget exceeded')
            release(lab)
            folder=root/f'campaign-{cycle+1:02d}';folder.mkdir(exist_ok=resume)
            plan=json.loads((folder/'plan.json').read_text()) if (folder/'plan.json').exists() else ask_dm(folder,'plan',[{'role':'system','content':
                'You are Sabrina, the Dungeon Master of a software-building research campaign. Invent a campaign title and six quest titles; select exactly one task from EACH family with variant 0..3. '
                'Give a falsifiable hypothesis about worker failure. These families have externally validated executable graders. You cannot create arbitrary grading rules or declare success. '
                'Use prior outcomes to select variants and progression. This is exploration, not a frozen benchmark or training. Return the JSON schema.'},
                {'role':'user','content':json.dumps({'families':FAMILIES,'prior_development_outcomes':history[-3:],
                    'variants':'metrics: metric name; pagination: collection key; csv: comma/semicolon; redaction: replacement marker. retries/dependencies have one canonical spec; variant changes fixture seed only.'})}],SCHEMA)
            tasks=plan.get('tasks',[])
            if len(tasks)!=6 or {x.get('family') for x in tasks}!=set(FAMILIES): raise ValueError('DM must cover all six families once')
            if any(type(x.get('variant')) is not int or not 0<=x['variant']<=3 for x in tasks): raise ValueError('Invalid DM variant')
            dump(folder/'plan.json',plan)
            print(json.dumps({'phase':'campaign_planned','cycle':cycle+1,'title':plan['title']}),flush=True)
            records=[]
            for index,selected in enumerate(tasks):
                task=make_task(selected['family'],selected['variant'],61000+100*len(history)+index)
                task_id=f'{selected["family"]}-v{selected["variant"]}'
                task_root=folder/task_id;task_root.mkdir(exist_ok=resume)
                if (task_root/'task.json').exists(): task=json.loads((task_root/'task.json').read_text())
                else: dump(task_root/'task.json',task)
                prompt=task['spec']+' Return a complete Python implementation in one python code block. Standard library only. Define solve exactly; no demonstration calls.'
                messages=[{'role':'user','content':prompt}]
                if (task_root/'baseline'/'score.json').exists():
                    score=json.loads((task_root/'baseline'/'score.json').read_text())
                else:
                    if (task_root/'baseline'/'solution.py').exists(): path=task_root/'baseline'/'solution.py'
                    else: path=worker(lab,messages,task_root/'baseline');generations+=1
                    score=sandbox(task,path,image,task_root/'baseline'/'score.json')
                row={'task_id':task_id,'family':task['family'],'variant':task['variant'],'spec_sha256':hashlib.sha256(task['spec'].encode()).hexdigest(),
                     'baseline_passed':score['passed'],'after_feedback_passed':score['passed'],'baseline_tests_passed':score['passed_tests'],
                     'total_tests':score['total_tests'],'attempts':1}
                records.append(row)
                print(json.dumps({'phase':'baseline','cycle':cycle+1,**row}),flush=True)
                dump(folder/'results.json',records)
            failures=[row for row in records if not row['baseline_passed']]
            release(lab)
            if failures:
                supplied=[]
                for row in failures[:2]:
                    task_root=folder/row['task_id']; task=json.loads((task_root/'task.json').read_text())
                    score=json.loads((task_root/'baseline'/'score.json').read_text())
                    supplied.append({'task_id':row['task_id'],'spec':task['spec'],
                        'code':(task_root/'baseline'/'solution.py').read_text()[:4000],
                        'failed_example':score.get('failures',[])[:1],'error':score.get('error')})
                saved_review=sorted(folder.glob('review-response-*.json'))
                advice=json.loads(json.loads(saved_review[-1].read_text())['message']['content']) if saved_review else ask_dm(folder,'review',[{'role':'system','content':
                    'You are Sabrina, the campaign DM. Review these measured development failures and give concise actionable repair guidance for each supplied task. '
                    'Do not claim success. Do not propose training or changing grading rules. Return advice for at most the two supplied task IDs.'},
                    {'role':'user','content':json.dumps(supplied)}],REPAIR_SCHEMA)
                allowed={x['task_id'] for x in supplied}; used=set()
                for item in advice.get('advice',[])[:2]:
                    task_id=item.get('task_id');guidance=item.get('guidance')
                    if task_id not in allowed or task_id in used or not isinstance(guidance,str) or len(guidance)>2000:
                        raise ValueError('Invalid DM repair advice')
                    used.add(task_id);row=next(r for r in records if r['task_id']==task_id)
                    task_root=folder/task_id;task=json.loads((task_root/'task.json').read_text())
                    generation=json.loads((task_root/'baseline'/'generation.json').read_text())
                    example=next(x for x in supplied if x['task_id']==task_id)
                    messages=generation['messages']+[{'role':'assistant','content':generation['answer']},
                        {'role':'user','content':'Your code failed executable development tests. Evidence: '+json.dumps(example['failed_example'])+
                         '. Error: '+str(example['error'])+'. DM guidance: '+guidance+'. Correct the implementation. Return the complete replacement in one python code block.'}]
                    if (task_root/'feedback'/'score.json').exists():
                        score=json.loads((task_root/'feedback'/'score.json').read_text())
                    else:
                        if (task_root/'feedback'/'solution.py').exists(): path=task_root/'feedback'/'solution.py'
                        else: path=worker(lab,messages,task_root/'feedback');generations+=1
                        score=sandbox(task,path,image,task_root/'feedback'/'score.json')
                    row.update(after_feedback_passed=score['passed'],attempts=2,feedback_tests_passed=score['passed_tests'])
                    print(json.dumps({'phase':'feedback','cycle':cycle+1,**row}),flush=True)
                    dump(folder/'results.json',records)
            dump(folder/'results.json',records);all_results.extend(records)
            summary={'run':root.name,'cycle':cycle+1,'title':plan['title'],'results':records}
            history.append(summary)
            tmp=history_path.with_suffix('.tmp');dump(tmp,history);tmp.replace(history_path)
        result={'status':'completed','campaigns':cycles,'task_attempts':len(all_results),
                'unique_specs':len({r['spec_sha256'] for r in all_results}),
                'first_attempt_passes':sum(r['baseline_passed'] for r in all_results),
                'after_feedback_passes':sum(r['after_feedback_passed'] for r in all_results),
                'rescued_tasks':[r['task_id'] for r in all_results if not r['baseline_passed'] and r['after_feedback_passed']],
                'worker_generations':generations,'training_steps':0,'weight_edits':0,
                'interpretation':'Exploratory code artifact repair; not weight learning, not a published benchmark score.',
                'elapsed_seconds':time.monotonic()-began,'results':all_results}
        assert generations<=protocol['max_worker_generations']
        dump(root/'outcome.json',result);print(json.dumps({k:v for k,v in result.items() if k!='results'}),flush=True)
    except Exception as error:
        dump(root/('interruption-'+uuid.uuid4().hex[:8]+'.json'),
             {'status':'execution_error','error':f'{type(error).__name__}: {error}',
              'worker_generations_saved':generations,'resume_available':True})
        raise
    finally:
        release(lab);lab.close()
        dump(root/'worker_accounting.json',{'generated_attempts':generations,
            'note':'Generation calls counted here; DMLab forward counter covers its intervention dispatch only, which this runner does not use.'})
        assert digest(root/'frozen_evaluation.json')==protocol['frozen_evaluation_sha256']


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--root',required=True,type=Path)
    parser.add_argument('--cycles',type=int,default=2,choices=range(1,5))
    parser.add_argument('--resume',action='store_true');args=parser.parse_args()
    run(args.root,args.cycles,args.resume)


if __name__=='__main__':main()
