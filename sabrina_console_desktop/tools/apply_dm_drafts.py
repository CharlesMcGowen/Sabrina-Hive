"""Operator-only application of explicitly selected DM drafts to isolated candidates.

Not exposed as a DM tool. Immutable drafts are retained; separate receipts record
application. Identical manifests share one candidate and matched evaluation.
"""
import argparse
import datetime
import hashlib
import json
from pathlib import Path
import sys
import weight_space_lab as lab


def digest(path):
    h=hashlib.sha256()
    with path.open('rb') as f:
        for b in iter(lambda:f.read(4*1024*1024),b''):h.update(b)
    return h.hexdigest()


def write(path,value):path.write_text(json.dumps(value,indent=2,allow_nan=False)+'\n')


def tensor_audit(source,candidate):
    from safetensors import safe_open
    import torch
    def index(folder):
        tensors={}
        for path in folder.glob('*.safetensors'):
            with safe_open(path,framework='pt',device='cpu') as f:
                for name in f.keys():tensors[name]=path
        return tensors
    a,b=index(source),index(candidate)
    if set(a)!=set(b):raise RuntimeError('Candidate stored tensor names differ from source')
    count=0;changes=0
    for name in a:
        with safe_open(a[name],framework='pt',device='cpu') as x,safe_open(b[name],framework='pt',device='cpu') as y:
            original=x.get_tensor(name);edited=y.get_tensor(name)
            if original.shape!=edited.shape or original.dtype!=edited.dtype:raise RuntimeError('Tensor schema changed: '+name)
            if not torch.equal(original,edited):changes+=1
            count+=1
    return {'stored_tensors_compared':count,'changed_tensors':changes,'all_tensor_values_identical':changes==0}


def main():
    p=argparse.ArgumentParser();p.add_argument('--campaign-root',type=Path,required=True)
    p.add_argument('--draft',action='append',required=True);a=p.parse_args()
    root=a.campaign_root.resolve(strict=True)
    registry=json.loads((Path.home()/'.config/sabrina/weight_space_models.json').read_text())['models']
    source=Path(registry['qwen2.5-coder-1.5b']).resolve(strict=True)
    groups={};hash_cache={};selected=[]
    for relative in a.draft:
        path=(root/relative).resolve(strict=True)
        if root not in path.parents or not path.name.endswith('.manifest.json'):raise ValueError('Invalid draft path')
        draft_id=path.name.removesuffix('.manifest.json')
        record_path=path.with_name(draft_id+'.json');record=json.loads(record_path.read_text())
        receipt=path.with_name(draft_id+'.application.json')
        if receipt.exists():raise ValueError('Draft already has an application receipt: '+draft_id)
        manifest=json.loads(path.read_text())
        if record.get('kind')!='draft' or record.get('model_alias')!='qwen2.5-coder-1.5b' or record.get('manifest')!=manifest:
            raise ValueError('Draft provenance does not match manifest')
        if not record.get('source_hashes'):raise ValueError('Draft lacks pinned source hashes')
        for name,expected in record['source_hashes'].items():
            if Path(name).name!=name:raise ValueError('Invalid source hash filename')
            if name not in hash_cache:hash_cache[name]=digest(source/name)
            if hash_cache[name]!=expected:raise RuntimeError('Source hash mismatch: '+name)
        for evidence in record.get('evidence_ids',[]):
            if Path(evidence).name!=evidence or not (path.parent/(evidence+'.json')).is_file():raise ValueError('Missing evidence record')
        lab._validate_manifest(manifest)
        key=hashlib.sha256(json.dumps(manifest,sort_keys=True,separators=(',',':')).encode()).hexdigest()
        entry={'draft_id':draft_id,'manifest_path':str(path),'manifest_sha256':digest(path),'record_sha256':digest(record_path),'receipt':receipt}
        groups.setdefault(key,{'manifest':manifest,'drafts':[]})['drafts'].append(entry);selected.append(entry)
    stamp=datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    out=root/'applications'/stamp;out.mkdir(parents=True,exist_ok=False)
    from transformers import AutoTokenizer
    tokenizer=AutoTokenizer.from_pretrained(source,local_files_only=True,trust_remote_code=False)
    prompts=[{'name':name,'prompt':tokenizer.apply_chat_template([{'role':'user','content':prompt}],tokenize=False,add_generation_prompt=True)} for name,prompt in [
        ('head_fact','Does an HTTP HEAD response include a response body? Answer only YES or NO.'),
        ('body_check','A test sends HTTP HEAD to /metrics and checks its response body for HELP lines. Is this a valid body check? Answer only YES or NO.'),
        ('arithmetic_control','What is 2 + 3? Answer with the number only.')]]
    reports=[]
    for key,group in groups.items():
        print('Applying unique manifest '+key,flush=True)
        result=lab._experiment(source,group['manifest'],prompts,'apply-dm-drafts',16,out)
        candidate=Path(result['run_dir'])/'edited_model'
        audit=tensor_audit(source,candidate)
        outputs_equal=all(not x['changed'] for x in result['comparisons'])
        report={'manifest_group':key,'candidate_dir':str(candidate),'experiment':result,'tensor_audit':audit,'matched_outputs_identical':outputs_equal,
            'applied_to_isolated_candidate':True,'serving_model_replaced':False,'training_steps':0,
            'drafts':[e['draft_id'] for e in group['drafts']]}
        write(out/(key+'.json'),report);reports.append(report)
        for entry in group['drafts']:
            receipt={k:v for k,v in entry.items() if k!='receipt'}
            receipt.update(status='applied_to_isolated_candidate',candidate_dir=str(candidate),report_path=str(out/(key+'.json')),
                tensor_audit=audit,matched_outputs_identical=outputs_equal,serving_model_replaced=False,training_steps=0)
            write(entry['receipt'],receipt)
    write(out/'application_index.json',{'applied_drafts':len(selected),'unique_candidates':len(reports),
        'source':str(source),'reports':[str(out/(r['manifest_group']+'.json')) for r in reports]})
    print(json.dumps({'applied_drafts':len(selected),'unique_candidates':len(reports),'output':str(out)}),flush=True)


if __name__=='__main__':main()
