"""Forward-only, complete-action identity-binding research on the lab HF worker.

Interventions occur at a semantically aligned tool-result token during prefill.
The whole next JSON action is then generated and executed with a fresh KV cache.
No trained probe, gradient, checkpoint edit, or production agent operation occurs.
"""
import argparse
import hashlib
import json
from pathlib import Path
import time

from dm_weight_lab import DMLab, digest, dump
from tool_interop_identity import IdentityWorld, make_cases, gold_actions, parse_action


class Worker:
    def __init__(self,lab):
        self.lab=lab;lab._load();self.generations=0;self.forwards=0;self.started=time.monotonic()
        def count(module,inputs):
            self.forwards+=1
            if self.forwards>43200:raise RuntimeError('Forward-pass cap exceeded')
        self.counter=lab.model.register_forward_pre_hook(count)

    def generate(self,world,folder,explicit=None,capture=False,intervention=None):
        import numpy as np
        import torch
        if self.generations>=450 or time.monotonic()-self.started>1800:raise RuntimeError('Bounded generation/wall budget exhausted')
        self.generations+=1;folder.mkdir(parents=True,exist_ok=False)
        prompt,spans=world.prompt(explicit)
        rendered=self.lab.tokenizer.apply_chat_template([{'role':'user','content':prompt}],tokenize=False,add_generation_prompt=True)
        encoded=self.lab.tokenizer(rendered,add_special_tokens=False,return_offsets_mapping=True)
        ids=encoded['input_ids'];offsets=encoded['offset_mapping']
        if len(ids)>3072:raise ValueError('Prompt exceeds 3072 tokens')
        positions={'decision':len(ids)-1}; alignment={}
        lookup=[s for s in spans if s[2]['operation']=='lookup' and 'error' not in s[2]['result']]
        if lookup:
            start,end,event=lookup[-1];base=rendered.index(prompt)
            text=prompt[start:end];literal=json.dumps(world.case['target_id'])
            local=text.index(literal)+1;id_start=base+start+local;id_end=id_start+len(world.case['target_id'])
            positions['identity']=max(i for i,(s,e) in enumerate(offsets) if s<id_end and e>id_start)
            positions['response_end']=max(i for i,(s,e) in enumerate(offsets) if s<base+end and e>base+start)
            alignment={'id_character_span':[id_start,id_end],'id_token_offset':list(offsets[positions['identity']]),
                       'id_token_text':self.lab.tokenizer.decode([ids[positions['identity']]]),
                       'target_id':world.case['target_id'],'positions':positions}
        if (capture or intervention) and 'identity' not in positions:raise ValueError('Intervention needs a real lookup result')
        handles=[];vectors={};applied=[]
        def collector(layer):
            def hook(module,inputs):
                values=inputs[0]
                if values.shape[1]!=len(ids):return
                for name,pos in positions.items():vectors[f'{name}_{layer}']=values[0,pos].detach().float().cpu().numpy().copy()
            return hook
        if capture:
            for layer,block in enumerate(self.lab.model.model.layers):
                handles.append(block.self_attn.o_proj.register_forward_pre_hook(collector(layer)))
        if intervention:
            layer=intervention['layer'];head=intervention['head'];alpha=intervention.get('alpha',1.)
            assert 0<=layer<27 and 0<=head<12 and 0<=alpha<=1
            bounds=slice(head*128,(head+1)*128);pos=positions['identity']
            def patch(module,inputs):
                values=inputs[0]
                # Exactly once, on the full prefill. Decode calls are not patched.
                if values.shape[1]!=len(ids):return
                before=values[0,pos,bounds].float()
                if intervention['mode']=='ablate':after=(1-alpha)*before
                else:after=(1-alpha)*before+alpha*torch.tensor(intervention['vector'],device=before.device)
                changed=values.clone();changed[0,pos,bounds]=after.to(values.dtype)
                applied.append({'layer':layer,'head':head,'position':pos,'delta_l2':float((after-before).norm())})
                return (changed,)+inputs[1:]
            handles.append(self.lab.model.model.layers[layer].self_attn.o_proj.register_forward_pre_hook(patch))
        started=time.monotonic();before_count=self.forwards
        try:
            with torch.inference_mode():
                output=self.lab.model.generate(input_ids=torch.tensor([ids],device='cuda'),max_new_tokens=96,
                    do_sample=False,use_cache=True,pad_token_id=self.lab.tokenizer.eos_token_id)
            tokens=output[0,len(ids):].tolist();answer=self.lab.tokenizer.decode(tokens,skip_special_tokens=True)
        finally:
            for handle in handles:handle.remove()
        if intervention and len(applied)!=1:raise RuntimeError('Patch did not apply exactly once at prefill')
        action,error=parse_action(answer)
        event=world.execute(action)
        row={'answer':answer,'token_ids':tokens,'output_tokens':len(tokens),'hit_token_limit':len(tokens)==96,
             'parse_error':error,'event':event,'handoff_correct':event['operation']=='fetch' and event['target_identity'] is True,
             'prompt':prompt,'rendered_prompt':rendered,'input_token_ids':ids,'prompt_tokens':len(ids),
             'alignment':alignment,'intervention':({k:v for k,v in intervention.items() if k!='vector'} if intervention else None),
             'patch_applications':applied,'forwards':self.forwards-before_count,'seconds':time.monotonic()-started}
        dump(folder/'generation.json',row)
        if vectors:np.savez_compressed(folder/'heads.npz',**vectors)
        return row

    def close(self):
        self.counter.remove()


def decision(worker,case,folder,capture=False,intervention=None):
    world=IdentityWorld(case);world.seed_lookup()
    return worker.generate(world,folder,capture=capture,intervention=intervention)


def rollout(worker,case,folder,intervention=None):
    world=IdentityWorld(case);rows=[];patched=False
    for step in range(6):
        active=None
        if intervention and not patched and world.trace and world.trace[-1]['operation']=='lookup' and 'error' not in world.trace[-1]['result']:
            active=intervention;patched=True
        row=worker.generate(world,folder/f'step-{step}',intervention=active);rows.append(row)
        if world.success():break
    result={'case_id':case['id'],'goal_success':world.success(),'calls':len(rows),
            'valid_schema_calls':sum(r['event']['schema_valid'] for r in rows),
            'fetch_correct':sum(r['event']['target_identity'] is True for r in rows),
            'fetch_wrong':sum(r['event']['target_identity'] is False for r in rows),
            'parse_errors':sum(r['parse_error'] is not None for r in rows),
            'patch_applied':patched,'artifacts':world.artifacts,'verified':world.verified,'trace':world.trace}
    dump(folder/'outcome.json',result)
    return result


def control(worker,case,root):
    results=[]
    for op,action in zip(['lookup','fetch','save','verify'],gold_actions(case)):
        world=IdentityWorld(case)
        if op=='verify':world.artifacts[f'art{case["base"]}_1']=case['body']
        explicit='Make exactly this requested operation: call '+json.dumps(action['tool'])+' with arguments '+json.dumps(action['arguments'])+'.'
        row=worker.generate(world,root/op,explicit=explicit)
        results.append({'operation':op,'correct':row['event']['action']==action and 'error' not in row['event']['result']})
    dump(root/'controls.json',results);return results


def ranking(worker,cases,root):
    import numpy as np
    base_ids=sorted({c['base'] for c in cases});vectors={}
    for base in base_ids:
        for variant in ['canonical','reorder','swap']:
            with np.load(root/'baseline'/f'b{base}_{variant}'/'decision'/'heads.npz',allow_pickle=False) as data:
                vectors[(base,variant)]=[data[f'identity_{layer}'].copy() for layer in range(27)]
    rows=[]
    for layer in range(27):
        weight=worker.lab.model.model.layers[layer].self_attn.o_proj.weight.detach().float().cpu().numpy()
        for head in range(12):
            bounds=slice(head*128,(head+1)*128);order=[];identity=[]
            for base in base_ids:
                original=vectors[(base,'canonical')][layer][bounds]
                order.append(float(np.linalg.norm(weight[:,bounds]@(vectors[(base,'reorder')][layer][bounds]-original))))
                identity.append(float(np.linalg.norm(weight[:,bounds]@(vectors[(base,'swap')][layer][bounds]-original))))
            rows.append({'layer':layer,'head':head,'order_contrast_l2':float(np.mean(order)),
                         'identity_value_contrast_l2':float(np.mean(identity))})
    rows.sort(key=lambda r:(-r['order_contrast_l2'],r['layer'],r['head']))
    selected=[]
    for row in rows:
        if all(row['layer']!=x['layer'] for x in selected):selected.append(row)
        if len(selected)==2:break
    dump(root/'head_ranking.json',{'correlational_only':True,'ranked':rows,'nominated':selected})
    return selected


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--root',required=True,type=Path);args=parser.parse_args();root=args.root
    import fcntl
    root.parent.mkdir(parents=True,exist_ok=True)
    lock=(root.parent/'.dm_weight_lab.lock').open('a');fcntl.flock(lock.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
    root.mkdir(exist_ok=False)
    cases=make_cases();dump(root/'cases.json',cases)
    source=json.loads((Path.home()/'.config/sabrina/weight_space_models.json').read_text())['models']['qwen2.5-coder-1.5b']
    protocol={'question':'Causal contribution to preserving record identity between lookup and fetch tools.',
        'datasets_sha256':digest(root/'cases.json'),'source_sha256':digest(Path(source)/'model.safetensors'),
        'base_scenarios':8,'development_bases':4,'heldout_bases':4,'variants':4,
        'variants_definition':{'canonical':'ordinary names/order','reorder':'only lookup result field order reversed',
             'rename':'documented tool and field aliases, preserving field order','swap':'target ID and cursor values exchange semantic roles; backend contents are remapped consistently'},
        'heldout_scope':'New base identities/titles and tool namespaces, same chain structure; not unseen compositions.',
        'controls':'Four individual-tool controls in canonical and renamed interfaces for base0 dev and base4 heldout;16 generations.',
        'baseline':'Full generated next action on fixed lookup history plus autonomous lookup/fetch/save/verify, maximum6 calls per chain.',
        'ranking':'Development-only mean o_proj-weighted L2 canonical/reorder contrast at last token overlapping target ID, heads in layers0..26. Top2 distinct layers. Last layer excluded because prefill outputs there have no downstream attention path to later tokens.',
        'selection':'Ablate each nominated head at aligned ID token during prefill, alpha1. Rank by number of complete correct fetch calls on16 development decisions, then schema-valid count, then lower layer/head. Freeze before heldout inference.',
        'intervention_scope':'One prefill head vector only. Fresh cache for every generated action; full JSON generated and executed. Does not intervene at every argument-generation token.',
        'causal_controls':'On canonical bases0,1,4,5: identity patch, zero ablation, reorder-donor patch, reverse patch, norm-matched random patch, cross-value canonical donor from next base in same split. All use primary head; no test-based retuning.',
        'conditional_rollout':'Only if primary improves development decision count, replay all heldout full chains with that head ablated once at first lookup handoff.',
        'advance_gate':'Strict development AND heldout decision improvement, no decline in individual-tool control accuracy, exact identity/zero replay, no donor-ID copying, and improved heldout full-chain success. Passing only permits further weight-edit research, not automatic edit or promotion.',
        'limits':{'generations':450,'new_tokens_per_action':96,'prompt_tokens':3072,'actual_forwards':43200,'wall_seconds':1800},
        'training_steps':0,'weight_edits':0}
    dump(root/'protocol.json',protocol)
    # Validate semantic grading before any inference.
    for case in cases:
        world=IdentityWorld(case)
        for action in gold_actions(case):world.execute(action)
        assert world.success()
        wrong=IdentityWorld(case);wrong.seed_lookup();f=case['fields'];n=case['tools']
        event=wrong.execute({'tool':n['fetch'],'arguments':{f['fetch_arg']:case['decoy_id']}})
        assert event['schema_valid'] and event['target_identity'] is False and 'error' not in event['result']
        wrong.execute({'tool':n['save'],'arguments':{f['save_arg']:case['decoy_body']}})
        wrong.execute(gold_actions(case)[3]);assert not wrong.success()
    dump(root/'simulator_checks.json',{'gold_chains_pass':32,'valid_decoy_chains_fail':32})
    lab=DMLab(source,root/'source_session');worker=None;results={};controls={};candidate_rows={}
    try:
        worker=Worker(lab)
        for split in ['development','heldout']:
            group=[c for c in cases if c['split']==split]
            for case in group:
                folder=root/'baseline'/case['id']
                fixed=decision(worker,case,folder/'decision',capture=True)
                chain=rollout(worker,case,folder/'chain')
                results[case['id']]={'case':case['id'],'base':case['base'],'variant':case['variant'],'split':split,
                    'handoff_correct':fixed['handoff_correct'],'schema_valid':fixed['event']['schema_valid'],
                    'goal_success':chain['goal_success'],'calls':chain['calls']}
                print(json.dumps({'phase':'baseline',**results[case['id']]}),flush=True)
            for variant in ['canonical','rename']:
                case=next(c for c in group if c['base']==(0 if split=='development' else 4) and c['variant']==variant)
                controls[case['id']]=control(worker,case,root/'single_tool_controls'/case['id'])
            dump(root/'baseline_results.json',list(results.values()));dump(root/'single_tool_controls.json',controls)
            if split=='development':
                nominated=ranking(worker,group,root)
            for head in nominated:
                label=f'L{head["layer"]}H{head["head"]}'
                candidate_rows.setdefault(label,[])
                for case in group:
                    result=decision(worker,case,root/'ablations'/label/case['id'],
                        intervention={'mode':'ablate','layer':head['layer'],'head':head['head'],'alpha':1.})
                    candidate_rows[label].append({'case':case['id'],'split':split,'handoff_correct':result['handoff_correct'],
                                                  'schema_valid':result['event']['schema_valid']})
                print(json.dumps({'phase':'ablation','head':label,'split':split,
                     'correct':sum(r['handoff_correct'] for r in candidate_rows[label] if r['split']==split),'total':len(group)}),flush=True)
                dump(root/'ablation_results.json',candidate_rows)
            if split=='development':
                nominated.sort(key=lambda h:(-sum(r['handoff_correct'] for r in candidate_rows[f'L{h["layer"]}H{h["head"]}']),
                    -sum(r['schema_valid'] for r in candidate_rows[f'L{h["layer"]}H{h["head"]}']),h['layer'],h['head']))
                primary=nominated[0];label=f'L{primary["layer"]}H{primary["head"]}'
                gain=sum(r['handoff_correct'] for r in candidate_rows[label])-sum(r['handoff_correct'] for r in results.values())
                dump(root/'selection.json',{'primary':primary,'nominated':nominated,'development_gain':gain,
                    'frozen_before_heldout_inference':True,'protocol_sha256':digest(root/'protocol.json')})
                print(json.dumps({'phase':'selection_frozen','primary':primary,'development_gain':gain}),flush=True)
        # Controls use fixed source/recipient cases, not heldout performance to select them.
        import numpy as np
        layer=primary['layer'];head=primary['head'];bounds=slice(head*128,(head+1)*128);patch_rows=[]
        def vector(case_id):
            with np.load(root/'baseline'/case_id/'decision'/'heads.npz',allow_pickle=False) as arrays:
                return arrays[f'identity_{layer}'][bounds].copy()
        for base in [0,1,4,5]:
            recipient=next(c for c in cases if c['base']==base and c['variant']=='canonical')
            reordered=next(c for c in cases if c['base']==base and c['variant']=='reorder')
            other_base=(base//4)*4+(base+1)%4
            other=next(c for c in cases if c['base']==other_base and c['variant']=='canonical')
            original=vector(recipient['id']);donor=vector(reordered['id']);delta=donor-original
            noise=np.random.default_rng(1701+base).standard_normal(128).astype('float32')
            random_vector=original+noise/np.linalg.norm(noise)*np.linalg.norm(delta)
            experiments=[('identity',recipient,original,1.),('zero',recipient,None,0.),
                         ('reorder_patch',recipient,donor,1.),('reverse_patch',reordered,original,1.),
                         ('random_patch',recipient,random_vector,1.),('cross_value_patch',recipient,vector(other['id']),1.)]
            for name,target,replacement,alpha in experiments:
                intervention={'mode':'ablate' if replacement is None else 'patch','layer':layer,'head':head,'alpha':alpha,
                              'control_name':name,'donor_case':other['id'] if name=='cross_value_patch' else reordered['id'] if name=='reorder_patch' else recipient['id']}
                if replacement is not None:intervention['vector']=replacement.tolist()
                out=decision(worker,target,root/'patch_controls'/f'b{base}_{name}',intervention=intervention)
                old=json.loads((root/'baseline'/target['id']/'decision'/'generation.json').read_text())
                action=out['event']['action'];args=(action or {}).get('arguments',{})
                copied=name=='cross_value_patch' and isinstance(args,dict) and other['target_id'] in args.values()
                row={'base':base,'control':name,'case':target['id'],'handoff_correct':out['handoff_correct'],
                     'identical_token_ids':out['token_ids']==old['token_ids'],'donor_id_copied':copied}
                patch_rows.append(row)
                if name in ['identity','zero']:assert row['identical_token_ids'],'No-op replay failed'
        dump(root/'patch_controls.json',patch_rows)
        rollout_results=[]
        if gain>0:
            for case in [c for c in cases if c['split']=='heldout']:
                rollout_results.append(rollout(worker,case,root/'primary_chains'/case['id'],
                    intervention={'mode':'ablate','layer':layer,'head':head,'alpha':1.}))
        dump(root/'primary_chain_results.json',rollout_results)
        primary_label=f'L{layer}H{head}';summary={}
        for split in ['development','heldout']:
            rows=[r for r in results.values() if r['split']==split]
            after=[r for r in candidate_rows[primary_label] if r['split']==split]
            summary[split]={'cases':len(rows),'baseline_handoffs':sum(r['handoff_correct'] for r in rows),
                'baseline_chains':sum(r['goal_success'] for r in rows),'primary_ablation_handoffs':sum(r['handoff_correct'] for r in after)}
        # With this one-token prefill intervention, single-tool controls contain no lookup
        # result and are intentionally unmodified. A broad edit needs additional regressions.
        improve=gain>0 and summary['heldout']['primary_ablation_handoffs']>summary['heldout']['baseline_handoffs']
        chain_gain=bool(rollout_results) and sum(r['goal_success'] for r in rollout_results)>summary['heldout']['baseline_chains']
        gate=improve and chain_gain and not any(r['donor_id_copied'] for r in patch_rows)
        outcome={'status':'completed','summary':summary,'primary':primary,'advance_gate_passed':bool(gate),
                 'single_tool_controls_correct':sum(r['correct'] for v in controls.values() for r in v),
                 'single_tool_controls_total':sum(len(v) for v in controls.values()),
                 'generations':worker.generations,'actual_forward_passes':worker.forwards,'elapsed_seconds':time.monotonic()-worker.started,
                 'identity_zero_controls_equal':True,'cross_value_donor_copy_count':sum(r['donor_id_copied'] for r in patch_rows),
                 'training_steps':0,'weight_edits':0,
                 'limits':'One identity concept and one chain structure; heldout names/values do not establish unseen-composition generalization. Causal contribution is not a universal concept neuron.'}
        dump(root/'outcome.json',outcome);print(json.dumps(outcome),flush=True)
    finally:
        if worker:
            worker.close();dump(root/'generation_accounting.json',{'generation_calls':worker.generations,'actual_forward_passes':worker.forwards,
                'note':'DMLab intervention dispatch is unused; generation/forward accounting is separate.'})
        lab.close()
        assert digest(root/'cases.json')==protocol['datasets_sha256']


if __name__=='__main__':main()
