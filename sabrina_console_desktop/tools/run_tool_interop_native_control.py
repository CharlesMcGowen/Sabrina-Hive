"""Native Qwen tool-message control, with unchanged simulator and task semantics."""
import argparse
import json
from pathlib import Path
import time

from dm_weight_lab import DMLab,digest,dump
from tool_interop_identity import gold_actions
from run_tool_interop_schema_control import SchemaWorld


def parse_native(answer):
    text=answer.strip()
    if not (text.startswith('<tool_call>') and text.endswith('</tool_call>')):
        return None,'Expected exactly one native tool_call block'
    try:
        value=json.loads(text[len('<tool_call>'):-len('</tool_call>')].strip())
        if not isinstance(value,dict) or set(value)!={'name','arguments'}:raise ValueError('Expected name and arguments')
        return {'tool':value['name'],'arguments':value['arguments']},None
    except (ValueError,TypeError) as error:return None,str(error)


class NativeWorker:
    def __init__(self,lab):
        self.lab=lab;lab._load();self.generations=0;self.forwards=0;self.started=time.monotonic()
        def count(module,inputs):
            self.forwards+=1
            if self.forwards>12288:raise RuntimeError('Forward budget exhausted')
        self.counter=lab.model.register_forward_pre_hook(count)

    def generate(self,world,folder,explicit=None):
        import torch
        if self.generations>=128 or time.monotonic()-self.started>900:raise RuntimeError('Native control budget exhausted')
        self.generations+=1;folder.mkdir(parents=True,exist_ok=False)
        goal=explicit or f'Read the record titled {world.case["title"]!r}, save its exact contents as an artifact, and verify the saved artifact.'
        messages=[{'role':'user','content':'Use the available tools to fulfill the goal. Execute one call at a time. Use actual returned values. Do not guess IDs.\nGOAL: '+goal}]
        for event in world.trace:
            action=event['action']
            if isinstance(action,dict) and set(action)=={'tool','arguments'}:
                messages.append({'role':'assistant','content':'','tool_calls':[{'type':'function','function':{'name':action['tool'],'arguments':action['arguments']}}]})
                messages.append({'role':'tool','name':action['tool'],'content':json.dumps(event['result'])})
            else:
                messages.append({'role':'assistant','content':event.get('raw_answer','')})
                messages.append({'role':'user','content':'No tool call was executed. Return exactly one native tool_call block; wait for its real tool response.'})
        tools=[{'type':'function','function':d} for d in world.descriptions()]
        rendered=self.lab.tokenizer.apply_chat_template(messages,tools=tools,tokenize=False,add_generation_prompt=True)
        ids=self.lab.tokenizer.encode(rendered,add_special_tokens=False)
        if len(ids)>3072:raise ValueError('Context budget exceeded')
        started=time.monotonic();before=self.forwards
        with torch.inference_mode():
            output=self.lab.model.generate(input_ids=torch.tensor([ids],device='cuda'),max_new_tokens=96,
                do_sample=False,use_cache=True,pad_token_id=self.lab.tokenizer.eos_token_id)
        tokens=output[0,len(ids):].tolist();answer=self.lab.tokenizer.decode(tokens,skip_special_tokens=True)
        action,error=parse_native(answer);event=world.execute(action);event['raw_answer']=answer
        result={'answer':answer,'action':action,'parse_error':error,'event':event,
                'handoff_correct':event['operation']=='fetch' and event['target_identity'] is True,
                'token_ids':tokens,'output_tokens':len(tokens),'hit_token_limit':len(tokens)==96,'prompt_tokens':len(ids),
                'messages':messages,'tools':tools,'rendered_prompt':rendered,'forwards':self.forwards-before,
                'seconds':time.monotonic()-started}
        dump(folder/'generation.json',result);return result

    def close(self):self.counter.remove()


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--baseline-root',required=True,type=Path)
    parser.add_argument('--root',required=True,type=Path);args=parser.parse_args()
    import fcntl
    lock=(args.root.parent/'.dm_weight_lab.lock').open('a');fcntl.flock(lock.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
    root=args.root;root.mkdir(parents=True,exist_ok=False);base=args.baseline_root
    original=json.loads((base/'protocol.json').read_text());assert digest(base/'cases.json')==original['datasets_sha256']
    cases=[c for c in json.loads((base/'cases.json').read_text()) if c['variant'] in ['canonical','rename']]
    source=json.loads((Path.home()/'.config/sabrina/weight_space_models.json').read_text())['models']['qwen2.5-coder-1.5b']
    assert digest(Path(source)/'model.safetensors')==original['source_sha256']
    dump(root/'cases.json',cases)
    dump(root/'protocol.json',{'type':'post-hoc native protocol control on all16 canonical/rename cases, not a fresh heldout benchmark',
        'changes':'Use pinned tokenizer tools branch, assistant tool_calls and tool response roles. Same JSON Schema tool descriptions, arguments, simulator and task content. Native output name key mapped deterministically to dispatcher tool key.',
        'interpretation':'Protocol/template condition changes several presentation details together; cannot attribute differences to one formatting detail or weight learning.',
        'output_parser':'Exactly one tool_call block containing one JSON object with name and arguments. No extra text or fabricated tool response accepted.',
        'controls':'Same16 explicit-argument single-call controls. Fresh model cache for each generation.',
        'max_generations':128,'max_new_tokens':96,'max_forwards':12288,'max_wall_seconds':900,
        'source_sha256':original['source_sha256'],'baseline_case_sha256':original['datasets_sha256'],
        'training_steps':0,'weight_edits':0})
    lab=DMLab(source,root/'source_session');worker=None;results=[];controls=[]
    try:
        worker=NativeWorker(lab)
        for case in cases:
            folder=root/case['id'];world=SchemaWorld(case);world.seed_lookup()
            fixed=worker.generate(world,folder/'decision')
            world=SchemaWorld(case);rows=[]
            for step in range(6):
                rows.append(worker.generate(world,folder/'chain'/f'step-{step}'))
                if world.success():break
            result={'case':case['id'],'variant':case['variant'],'base':case['base'],
                    'native_handoff_correct':fixed['handoff_correct'],'native_goal_success':world.success(),
                    'calls':len(rows),'trace':world.trace}
            results.append(result);dump(folder/'outcome.json',result)
            print(json.dumps({k:v for k,v in result.items() if k!='trace'}),flush=True)
        for case in [c for c in cases if c['base'] in [0,4]]:
            for op,action in zip(['lookup','fetch','save','verify'],gold_actions(case)):
                world=SchemaWorld(case)
                if op=='verify':world.artifacts[f'art{case["base"]}_1']=case['body']
                goal='Make exactly this requested operation: call '+json.dumps(action['tool'])+' with arguments '+json.dumps(action['arguments'])+'.'
                row=worker.generate(world,root/'controls'/case['id']/op,explicit=goal)
                controls.append({'case':case['id'],'operation':op,
                    'correct':row['event']['action']==action and 'error' not in row['event']['result']})
        dump(root/'controls.json',controls)
        outcome={'status':'completed','cases':len(results),'native_handoffs':sum(r['native_handoff_correct'] for r in results),
                 'native_chains':sum(r['native_goal_success'] for r in results),'chain_calls':sum(r['calls'] for r in results),
                 'explicit_controls_correct':sum(r['correct'] for r in controls),'explicit_controls_total':len(controls),
                 'generations':worker.generations,'actual_forwards':worker.forwards,'weight_edits':0,'training_steps':0,
                 'interpretation':'Native protocol diagnostic on reused cases; no weight learning or independent final benchmark claim.'}
        dump(root/'outcome.json',outcome);print(json.dumps(outcome),flush=True)
    finally:
        if worker:worker.close()
        lab.close()


if __name__=='__main__':main()
