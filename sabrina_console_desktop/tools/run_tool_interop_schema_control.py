"""Post-hoc format control: same tool semantics, standard JSON Schema contracts.

Uses all canonical/renamed scenarios from the identity pilot, not just failures.
No intervention or edit selection is made from this diagnostic follow-up.
"""
import argparse
import json
from pathlib import Path

from dm_weight_lab import DMLab, digest, dump
from tool_interop_identity import IdentityWorld, gold_actions
from run_tool_interop_identity import Worker


class SchemaWorld(IdentityWorld):
    def descriptions(self):
        def object_schema(mapping):
            properties={}
            for key,value in mapping.items():
                kind,description=value.split(': ',1) if ': ' in value else (value,value)
                properties[key]={'type':kind,'description':description}
            return {'type':'object','properties':properties,'required':list(properties),'additionalProperties':False}
        return [{'name':tool['name'],'parameters':object_schema(tool['arguments']),
                 'returns':object_schema(tool['returns'])} for tool in super().descriptions()]


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--baseline-root',type=Path,required=True)
    parser.add_argument('--root',type=Path,required=True);args=parser.parse_args()
    import fcntl
    lock=(args.root.parent/'.dm_weight_lab.lock').open('a');fcntl.flock(lock.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
    root=args.root;root.mkdir(parents=True,exist_ok=False)
    base=args.baseline_root
    if not (base/'outcome.json').exists():raise ValueError('Primary experiment must finish first')
    original=json.loads((base/'protocol.json').read_text())
    assert digest(base/'cases.json')==original['datasets_sha256']
    cases=[c for c in json.loads((base/'cases.json').read_text()) if c['variant'] in ['canonical','rename']]
    source=json.loads((Path.home()/'.config/sabrina/weight_space_models.json').read_text())['models']['qwen2.5-coder-1.5b']
    assert digest(Path(source)/'model.safetensors')==original['source_sha256']
    protocol={'status':'post-hoc diagnostic; prior heldout cases now reused for diagnosis, not a fresh final test',
        'question':'Does standard JSON Schema presentation reduce description-copying and goal-grounding failures?',
        'change':'Replace flat field-to-description mappings with type/properties/description/required objects. Same semantics, names, simulator, tasks and greedy decoding. Token lengths differ.',
        'selection':'All16 canonical/rename cases, no selection by success; same16 explicit-call controls.',
        'baseline_case_hash':digest(base/'cases.json'),'baseline_source_sha256':original['source_sha256'],
        'max_generations':128,'max_new_tokens':96,'no_activation_interventions':True,'training_steps':0,'weight_edits':0}
    dump(root/'protocol.json',protocol);dump(root/'cases.json',cases)
    lab=DMLab(source,root/'source_session');worker=None;rows=[];controls=[]
    try:
        worker=Worker(lab)
        for case in cases:
            folder=root/case['id'];world=SchemaWorld(case);world.seed_lookup()
            fixed=worker.generate(world,folder/'decision')
            world=SchemaWorld(case);chain=[]
            for step in range(6):
                chain.append(worker.generate(world,folder/'chain'/f'step-{step}'))
                if world.success():break
            old=json.loads((base/'baseline'/case['id']/'chain'/'outcome.json').read_text())
            row={'case':case['id'],'variant':case['variant'],'base':case['base'],'prior_goal_success':old['goal_success'],
                 'schema_goal_success':world.success(),'schema_handoff_correct':fixed['handoff_correct'],
                 'calls':len(chain),'trace':world.trace}
            rows.append(row);dump(folder/'outcome.json',row)
            print(json.dumps({k:v for k,v in row.items() if k!='trace'}),flush=True)
        for case in [c for c in cases if c['base'] in [0,4]]:
            for op,action in zip(['lookup','fetch','save','verify'],gold_actions(case)):
                world=SchemaWorld(case)
                if op=='verify':world.artifacts[f'art{case["base"]}_1']=case['body']
                goal='Make exactly this requested operation: call '+json.dumps(action['tool'])+' with arguments '+json.dumps(action['arguments'])+'.'
                row=worker.generate(world,root/'controls'/case['id']/op,explicit=goal)
                controls.append({'case':case['id'],'operation':op,
                    'correct':row['event']['action']==action and 'error' not in row['event']['result']})
        assert worker.generations<=128
        dump(root/'controls.json',controls)
        outcome={'status':'completed','cases':len(rows),'prior_chains':sum(r['prior_goal_success'] for r in rows),
                 'schema_chains':sum(r['schema_goal_success'] for r in rows),
                 'schema_handoffs':sum(r['schema_handoff_correct'] for r in rows),
                 'explicit_controls_correct':sum(r['correct'] for r in controls),'explicit_controls_total':len(controls),
                 'generations':worker.generations,'actual_forwards':worker.forwards,'weight_edits':0,'training_steps':0,
                 'interpretation':'Formatting diagnostic on reused cases. Any difference is a harness/prompt effect, not weight learning.'}
        dump(root/'outcome.json',outcome);print(json.dumps(outcome),flush=True)
    finally:
        if worker:worker.close()
        lab.close()


if __name__=='__main__':main()
