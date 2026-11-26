"""Bounded, no-training research tools for the D&D DM. No shell or apply operation.

The operator pins a local HF source and session directory. Model-authored calls
cannot select filesystem paths, expand budgets, modify source, or promote edits.
"""
from __future__ import annotations
import contextlib
import hashlib
import json
import math
from pathlib import Path
import re
import threading
import time
import uuid

MAX_CALLS = 12
MAX_FORWARDS = 48
MAX_VALUES = 1024
MAX_CASES = 4
MAX_PROMPT_TOKENS = 512
MAX_CHANGED = 262144
MAX_REQUEST_BYTES = 65536


def dump(path, value):
    path.write_text(json.dumps(value, indent=2, allow_nan=False)+'\n',encoding='utf-8')


def digest(path):
    h=hashlib.sha256()
    with path.open('rb') as f:
        for b in iter(lambda:f.read(4*1024*1024),b''):h.update(b)
    return h.hexdigest()


def integer(value, low, high, name):
    if type(value) is not int or not low<=value<=high:raise ValueError(f'{name} must be integer {low}..{high}')
    return value


def number(value, low, high, name):
    if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or not low<=value<=high:
        raise ValueError(f'{name} must be finite {low}..{high}')
    return float(value)


def text(value, low, high, name):
    if not isinstance(value,str) or not low<=len(value.strip())<=high:raise ValueError(f'{name} must be {low}..{high} characters')
    return value


def identifier(value):
    if not isinstance(value,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,40}',value):raise ValueError('Invalid identifier')
    return value


def obj(properties, required):
    return {'type':'object','properties':properties,'required':required,'additionalProperties':False}


STR={'type':'string'}
INT={'type':'integer'}
SCHEMAS = [
    ('dm_list_tensors','List parameter tensor names, shapes and dtypes from the pinned checkpoint. No model load. The filter is a literal substring of tensor names, not a behavioral concept, task label, player ID, or activation label. Start with filter="" when names are unknown.',
     obj({'filter':{'type':'string','description':'Optional literal substring of a parameter name, e.g. model.layers.0 or down_proj. Use an empty string to list any tensor.'},
          'limit':{'type':'integer','minimum':1,'maximum':32}},[])),
    ('dm_tensor_slice','Read at most 1024 tensor values. Each slice is [inclusive_start, exclusive_stop]; specify every axis.',
     obj({'tensor':STR,'slices':{'type':'array','items':{'type':'array','items':INT,'minItems':2,'maxItems':2}}},['tensor','slices'])),
    ('dm_intervene','Run bounded first-next-token baseline and causal intervention. patch copies a last-token activation from source_prompt; ablate zeros it; project removes a direction from a previous result. Temporary hooks only. 1-4 cases, <=512 tokens each.',
     obj({'site':{'enum':['mlp','head']},'layer':INT,'head':INT,'mode':{'enum':['observe','patch','ablate','project']},
          'alpha':{'type':'number','minimum':0,'maximum':1},
          'direction_ref':obj({'result_id':STR,'case_id':STR},['result_id','case_id']),
          'cases':{'type':'array','minItems':1,'maxItems':4,'items':obj({'id':STR,'prompt':STR,'source_prompt':STR},['id','prompt'])}},['site','layer','mode','cases'])),
    ('dm_inspect_result','Inspect an immutable result authored in this session. A result_id is an opaque ID, not a path.',
     obj({'result_id':STR,'section':{'enum':['summary','cases','manifest']},'offset':INT,'limit':INT},['result_id'])),
    ('dm_rank_mlp_writers','Given a measured activation direction, rank up to 64 neuron columns in one pinned MLP down-projection by absolute contribution along that direction. Read-only; does not edit weights.',
     obj({'tensor':STR,'direction_ref':obj({'result_id':STR,'case_id':STR},['result_id','case_id']),
          'limit':{'type':'integer','minimum':1,'maximum':64}},['tensor','direction_ref'])),
    ('dm_draft_manifest','Author a validated direct-edit manifest with evidence and hypothesis. Does NOT apply edits. Up to four scalar edits or <=64 selected columns per projection. Native format_version 1.',
     obj({'hypothesis':STR,'validation_plan':STR,'evidence_ids':{'type':'array','items':STR,'minItems':1,'maxItems':4},
          'manifest':obj({'format_version':{'const':1},'edits':{'type':'array','minItems':1,'maxItems':4,'items':obj({
              'tensor':STR,'op':{'enum':['add','multiply','set','project']},'index':{'type':'array','items':INT},'value':{'type':'number'},
              'columns':{'type':'array','items':INT},'direction':{'type':'array','items':{'type':'number'}},
              'direction_ref':obj({'result_id':STR,'case_id':STR},['result_id','case_id']),
              'alpha':{'type':'number'}},['tensor','op'])}},['format_version','edits'])},['hypothesis','validation_plan','evidence_ids','manifest'])),
]
TOOLS=[{'type':'function','function':{'name':name,'description':desc,'parameters':schema}} for name,desc,schema in SCHEMAS]


class DMLab:
    def __init__(self, source, root, alias='qwen2.5-coder-1.5b', device='gpu'):
        from safetensors import safe_open
        self.source=Path(source).resolve(strict=True);self.root=Path(root).resolve()
        if self.source==self.root or self.source in self.root.parents:raise ValueError('Results cannot be inside source')
        self.config=json.loads((self.source/'config.json').read_text())
        if self.config.get('model_type')!='qwen2':raise ValueError('This initial interface supports Qwen2 checkpoints only')
        self.hidden=integer(self.config['hidden_size'],1,4096,'hidden_size')
        self.layers=integer(self.config['num_hidden_layers'],1,40,'layers')
        self.heads=integer(self.config['num_attention_heads'],1,64,'heads')
        if self.hidden%self.heads:raise ValueError('Unsupported head width')
        self.files=sorted(self.source.glob('*.safetensors'))
        if not self.files or sum(p.stat().st_size for p in self.files)>4*1024**3:raise ValueError('Source must have <=4 GiB safetensors')
        self.catalog={}
        for path in self.files:
            with safe_open(path,framework='pt',device='cpu') as f:
                for key in f.keys():
                    if key in self.catalog:raise ValueError('Duplicate tensor')
                    s=f.get_slice(key)
                    self.catalog[key]={'file':path,'shape':list(s.get_shape()),'dtype':s.get_dtype()}
        self.root.mkdir(parents=True,exist_ok=False)
        self.source_files=self.files+[self.source/name for name in ['config.json','tokenizer.json','tokenizer_config.json',
            'generation_config.json','merges.txt','vocab.json','chat_template.jinja'] if (self.source/name).is_file()]
        self.hashes={p.name:digest(p) for p in self.source_files}
        if device not in ['cpu','gpu']:raise ValueError('device must be cpu or gpu')
        self.device=device
        self.alias=alias;self.calls=0;self.forwards=0;self.results={};self.model=None;self.tokenizer=None
        self._lock=threading.Lock();self.closed=False
        dump(self.root/'session.json',{'model_alias':alias,'source':str(self.source),'source_hashes':self.hashes,
            'limits':{'calls':MAX_CALLS,'forwards':MAX_FORWARDS,'slice_values':MAX_VALUES,'prompt_tokens':MAX_PROMPT_TOKENS},
            'training_allowed':False,'weight_apply_exposed':False,'device':device,'tools':TOOLS})

    def dispatch(self, name, arguments):
        with self._lock:
            if self.closed:raise RuntimeError('Session is closed')
            if self.calls>=MAX_CALLS:raise RuntimeError('Session tool-call budget exhausted')
            self.calls+=1
            started=time.monotonic();event={'call':self.calls,'tool':name,'arguments':arguments}
            try:
                raw=json.dumps(arguments,allow_nan=False).encode()
                if len(raw)>MAX_REQUEST_BYTES or not isinstance(arguments,dict):raise ValueError('Request must be bounded JSON object')
                allowed={n for n,_,_ in SCHEMAS}
                if name not in allowed:raise ValueError('Unknown tool; shell, training and apply are unavailable')
                result=getattr(self,name)(**arguments)
                result['budget']={'calls_remaining':MAX_CALLS-self.calls,'forwards_remaining':MAX_FORWARDS-self.forwards}
                if len(json.dumps(result,allow_nan=False).encode())>40000:raise ValueError('Result exceeds response budget')
                event['result']=result
                return result
            except Exception as error:
                event['error']=f'{type(error).__name__}: {error}'
                raise
            finally:
                event['seconds']=time.monotonic()-started
                # Store arguments only after validating serialization; never execute their contents.
                with (self.root/'audit.jsonl').open('a',encoding='utf-8') as f:
                    f.write(json.dumps(event,allow_nan=True,default=str)+'\n')

    def _save(self,kind,record,arrays=None):
        result_id=uuid.uuid4().hex
        value={'result_id':result_id,'kind':kind,'model_alias':self.alias,**record}
        dump(self.root/(result_id+'.json'),value)
        if arrays:
            import numpy as np
            np.savez(self.root/(result_id+'.npz'),**arrays)
        self.results[result_id]=value
        return value

    def _result(self,result_id):
        identifier(result_id)
        if result_id not in self.results:raise ValueError('Result is not from this session')
        return self.results[result_id]

    def dm_list_tensors(self,filter='',limit=16):
        if not isinstance(filter,str) or len(filter)>100:raise ValueError('Invalid filter')
        integer(limit,1,32,'limit')
        names=[n for n in sorted(self.catalog) if filter in n]
        result={'tensors':[{'name':n,'shape':self.catalog[n]['shape'],'dtype':self.catalog[n]['dtype']} for n in names[:limit]],
                'matching_count':len(names),'truncated':len(names)>limit}
        if not names and filter:
            result['hint']='No literal parameter-name match. Use filter="" to discover names; do not search for task, path-card, player, or behavior labels.'
        return result

    def dm_tensor_slice(self,tensor,slices):
        from safetensors import safe_open
        if tensor not in self.catalog:raise ValueError('Unknown tensor')
        meta=self.catalog[tensor];shape=meta['shape']
        if not isinstance(slices,list) or len(slices)!=len(shape):raise ValueError('Specify one [start,stop] per axis')
        selected=[];count=1
        for bounds,size in zip(slices,shape):
            if not isinstance(bounds,list) or len(bounds)!=2:raise ValueError('Slice must be [start,stop]')
            lo=integer(bounds[0],0,size-1,'start');hi=integer(bounds[1],lo+1,size,'stop')
            selected.append(slice(lo,hi));count*=hi-lo
        if count>MAX_VALUES:raise ValueError('Tensor slice exceeds 1024 values')
        with safe_open(meta['file'],framework='pt',device='cpu') as f:
            values=f.get_slice(tensor)[tuple(selected)].float()
        import torch
        if not torch.isfinite(values).all():raise ValueError('Nonfinite tensor values')
        return {'tensor':tensor,'shape':shape,'slices':slices,'values':values.tolist(),'count':count}

    def _load(self):
        if self.model is not None:return
        import torch
        from transformers import AutoModelForCausalLM,AutoTokenizer
        torch.set_num_threads(8)
        size=sum(p.stat().st_size for p in self.files)
        if self.device=='gpu' and not torch.cuda.is_available():raise RuntimeError('GPU required for requested live interventions')
        if self.device=='gpu' and torch.cuda.mem_get_info()[0]<size*1.5:raise RuntimeError('Insufficient free GPU memory; no eviction attempted')
        self.tokenizer=AutoTokenizer.from_pretrained(self.source,local_files_only=True,trust_remote_code=False)
        self.model=AutoModelForCausalLM.from_pretrained(self.source,local_files_only=True,trust_remote_code=False,
            use_safetensors=True,dtype=torch.bfloat16 if self.device=='gpu' else torch.float32,
            attn_implementation='eager').to(self.device).eval()
        self.model.requires_grad_(False)

    def _direction(self,ref,width):
        import numpy as np
        if not isinstance(ref,dict) or set(ref)!={'result_id','case_id'}:raise ValueError('Invalid direction_ref')
        record=self._result(ref['result_id']);case=identifier(ref['case_id'])
        if record['kind']!='intervention' or record['mode']!='patch':raise ValueError('Direction requires a completed patch result')
        with np.load(self.root/(record['result_id']+'.npz'),allow_pickle=False) as data:
            d=data[case+'_source']-data[case+'_baseline']
        if d.shape!=(width,) or not np.isfinite(d).all() or np.linalg.norm(d)==0:raise ValueError('Direction has wrong width, zero norm or nonfinite values')
        return d/np.linalg.norm(d)

    def dm_intervene(self,site,layer,mode,cases,head=None,alpha=1.0,direction_ref=None):
        import torch
        import numpy as np
        integer(layer,0,self.layers-1,'layer');alpha=number(alpha,0,1,'alpha')
        if site not in ['mlp','head'] or mode not in ['observe','patch','ablate','project']:raise ValueError('Unsupported intervention')
        if site=='head':integer(head,0,self.heads-1,'head')
        elif head is not None:raise ValueError('head only valid for head site')
        if mode=='project' and site!='mlp':raise ValueError('Projection currently supports MLP output only')
        if mode!='project' and direction_ref is not None:raise ValueError('Unexpected direction_ref')
        if not isinstance(cases,list) or not 1<=len(cases)<=MAX_CASES:raise ValueError('Use 1-4 cases')
        seen=set()
        for c in cases:
            if not isinstance(c,dict) or set(c)-{'id','prompt','source_prompt'}:raise ValueError('Invalid case fields')
            cid=identifier(c.get('id'))
            if cid in seen:raise ValueError('Duplicate case ID')
            seen.add(cid);text(c.get('prompt'),1,4000,'prompt')
            if mode=='patch':text(c.get('source_prompt'),1,4000,'source_prompt')
            elif 'source_prompt' in c:raise ValueError('source_prompt only valid for patch')
        cost=len(cases)*({'observe':1,'patch':3,'ablate':2,'project':2}[mode])
        if self.forwards+cost>MAX_FORWARDS:raise RuntimeError('Forward budget exceeded')
        direction=self._direction(direction_ref,self.hidden) if mode=='project' else None
        self._load()
        encoded={}
        for c in cases:
            for field in ['prompt']+(['source_prompt'] if mode=='patch' else []):
                s=self.tokenizer.apply_chat_template([{'role':'user','content':c[field]}],tokenize=False,add_generation_prompt=True)
                ids=self.tokenizer.encode(s,add_special_tokens=False)
                if len(ids)>MAX_PROMPT_TOKENS:raise ValueError('Prompt exceeds 512 tokens; not truncated')
                encoded[(c['id'],field)]=ids
        self.forwards+=cost  # Reserve all passes, including if an inference later fails.
        module=self.model.model.layers[layer].mlp.down_proj if site=='mlp' else self.model.model.layers[layer].self_attn.o_proj
        width=self.hidden//self.heads
        bounds=slice(None) if site=='mlp' else slice(head*width,(head+1)*width)
        arrays={};rows=[]
        def forward(ids,replacement=None,intervene=False):
            captured={}
            def transform(value):
                original=value[0,-1,bounds]
                captured['vector']=original.detach().float().cpu().numpy().copy()
                if not intervene:return value
                before=original.float()
                if mode=='patch':after=(1-alpha)*before+alpha*torch.as_tensor(replacement,device=before.device)
                elif mode=='ablate':after=(1-alpha)*before
                else:
                    u=torch.as_tensor(direction,device=before.device);after=before-alpha*(before@u)*u
                changed=value.clone();changed[0,-1,bounds]=after.to(value.dtype)
                return changed
            if site=='mlp':handle=module.register_forward_hook(lambda m,inputs,output:transform(output))
            else:handle=module.register_forward_pre_hook(lambda m,inputs:(transform(inputs[0]),)+inputs[1:])
            try:
                with torch.inference_mode():
                    out=self.model(input_ids=torch.tensor([ids],device=self.device),use_cache=False,logits_to_keep=1)
                    logits=out.logits[0,-1].float()
                    if not torch.isfinite(logits).all():raise RuntimeError('Nonfinite logits')
                    top=logits.topk(8)
                    scores=[{'id':i,'text':self.tokenizer.decode([i]),'logit':v} for i,v in zip(top.indices.tolist(),top.values.tolist())]
                    return {'next_token':scores[0]['text'],'next_token_id':scores[0]['id'],'top_tokens':scores,'prompt_tokens':len(ids)},captured['vector']
            finally:handle.remove()
        for c in cases:
            cid=c['id'];base,vector=forward(encoded[(cid,'prompt')]);arrays[cid+'_baseline']=vector
            row={'id':cid,'prompt':c['prompt'],'baseline':base}
            if mode=='patch':
                source,vector=forward(encoded[(cid,'source_prompt')]);arrays[cid+'_source']=vector
                row.update(source_prompt=c['source_prompt'],source=source)
            if mode!='observe':
                patched,_=forward(encoded[(cid,'prompt')],replacement=vector,intervene=True);row['intervened']=patched
            rows.append(row)
        record=self._save('intervention',{'site':site,'layer':layer,'head':head,'mode':mode,'alpha':alpha,
            'direction_ref':direction_ref,'cases':rows,'forwards':cost,'training_steps':0,'weight_edits':0,
            'summary':{'cases':len(rows),'forwards':cost,'interpretation':'First-token causal diagnostic, not evidence of generalized skill or edit success.'}},arrays)
        return {'result_id':record['result_id'],'summary':record['summary'],'cases':rows,'remaining_forwards':MAX_FORWARDS-self.forwards}

    def dm_inspect_result(self,result_id,section='summary',offset=0,limit=4):
        r=self._result(result_id);integer(offset,0,100,'offset');integer(limit,1,4,'limit')
        if section=='summary':return {'result_id':result_id,'kind':r['kind'],'summary':r['summary']}
        if section=='cases' and r['kind']=='intervention':return {'result_id':result_id,'cases':r['cases'][offset:offset+limit]}
        if section=='manifest' and r['kind']=='draft':
            # Return direction hashes rather than thousands of floating-point values.
            edits=[]
            for edit in r['manifest']['edits']:
                edit=dict(edit)
                if 'direction' in edit:
                    values=edit.pop('direction');edit['direction_length']=len(values)
                    edit['direction_sha256']=hashlib.sha256(json.dumps(values).encode()).hexdigest()
                edits.append(edit)
            return {'result_id':result_id,'manifest':{'format_version':1,'edits':edits},'applied':False}
        raise ValueError('Section unavailable for result')

    def dm_rank_mlp_writers(self,tensor,direction_ref,limit=64):
        """Rank down-projection input columns that write along a measured direction."""
        from safetensors import safe_open
        integer(limit,1,64,'limit')
        if tensor not in self.catalog or not re.fullmatch(r'model\.layers\.\d+\.mlp\.down_proj\.weight',tensor):
            raise ValueError('tensor must be one pinned model.layers.N.mlp.down_proj.weight')
        meta=self.catalog[tensor];shape=meta['shape']
        if len(shape)!=2 or shape[0]!=self.hidden:raise ValueError('Unsupported down-projection shape')
        direction=self._direction(direction_ref,self.hidden)
        with safe_open(meta['file'],framework='pt',device='cpu') as f:
            weights=f.get_tensor(tensor).float()
        import torch
        scores=torch.as_tensor(direction,dtype=torch.float32)@weights
        if not torch.isfinite(scores).all():raise ValueError('Nonfinite writer scores')
        count=min(limit,scores.numel())
        indices=scores.abs().topk(count).indices.tolist()
        writers=[{'column':int(index),'signed_score':float(scores[index]),'absolute_score':float(scores[index].abs())}
                 for index in indices]
        record=self._save('writer_ranking',{'tensor':tensor,'direction_ref':direction_ref,'writers':writers,
            'summary':{'ranked_columns':len(writers),'read_only':True,'interpretation':'Contribution ranking for a measured direction; not causal or promotion evidence.'}})
        return {'result_id':record['result_id'],'tensor':tensor,'writers':writers,'summary':record['summary']}

    def dm_draft_manifest(self,hypothesis,validation_plan,evidence_ids,manifest):
        text(hypothesis,10,2000,'hypothesis');text(validation_plan,10,2000,'validation_plan')
        if not isinstance(evidence_ids,list) or not 1<=len(evidence_ids)<=4:raise ValueError('Need 1-4 evidence IDs')
        for ref in evidence_ids:
            if self._result(ref)['kind']!='intervention':raise ValueError('Evidence must be a completed intervention')
        if not isinstance(manifest,dict) or set(manifest)!={'format_version','edits'} or type(manifest['format_version']) is not int or manifest['format_version']!=1:
            raise ValueError('Use manifest format_version 1')
        edits=manifest['edits']
        if not isinstance(edits,list) or not 1<=len(edits)<=4:raise ValueError('Use 1-4 edits')
        cleaned=[];selected=0
        for edit in edits:
            if not isinstance(edit,dict):raise ValueError('Edit must be an object')
            name=edit.get('tensor');op=edit.get('op')
            if name not in self.catalog or not name.startswith('model.layers.'):raise ValueError('Only known transformer-block tensors may be drafted')
            shape=self.catalog[name]['shape']
            if op=='project':
                if set(edit)-{'tensor','op','columns','direction','direction_ref','alpha'}:raise ValueError('Unknown projection field')
                cols=edit.get('columns')
                if len(shape)!=2 or not isinstance(cols,list) or not 1<=len(cols)<=64:raise ValueError('Project 1-64 columns of a matrix')
                if any(type(c) is not int or not 0<=c<shape[1] for c in cols) or len(set(cols))!=len(cols):raise ValueError('Invalid columns')
                alpha=number(edit.get('alpha',1.0),0.000001,1,'alpha')
                if ('direction' in edit)==('direction_ref' in edit):raise ValueError('Supply one direction or direction_ref')
                d=self._direction(edit['direction_ref'],shape[0]).tolist() if 'direction_ref' in edit else edit['direction']
                if not isinstance(d,list) or len(d)!=shape[0] or len(d)>4096:raise ValueError('Direction must match output width <=4096')
                d=[number(v,-1e6,1e6,'direction value') for v in d]
                if not any(d):raise ValueError('Zero direction')
                cleaned.append({'tensor':name,'op':op,'columns':cols,'direction':d,'alpha':alpha});selected+=shape[0]*len(cols)
            elif op in ['add','multiply','set']:
                if set(edit)!={'tensor','op','index','value'}:raise ValueError('Scalar edits need exactly tensor/op/index/value')
                index=edit['index']
                if not isinstance(index,list) or len(index)!=len(shape):raise ValueError('Only individual scalar edits are allowed')
                for i,size in zip(index,shape):integer(i,0,size-1,'index')
                limits={'add':(-.1,.1),'multiply':(.5,1.5),'set':(-1,1)}
                value=number(edit['value'],*limits[op],'value')
                cleaned.append({'tensor':name,'op':op,'index':index,'value':value});selected+=1
            else:raise ValueError('Unsupported edit operation')
        if selected>MAX_CHANGED:raise ValueError('Manifest selects too many elements')
        record=self._save('draft',{'hypothesis':hypothesis,'validation_plan':validation_plan,'evidence_ids':evidence_ids,
            'source_hashes':self.hashes,'manifest':{'format_version':1,'edits':cleaned},
            'summary':{'selected_elements_upper_bound':selected,'edits':len(cleaned),'applied':False,'status':'unvalidated_draft'},'applied':False})
        # Export only the compatible manifest for external, separately controlled application.
        dump(self.root/(record['result_id']+'.manifest.json'),record['manifest'])
        return {'result_id':record['result_id'],'summary':record['summary']}

    def close(self):
        if self.closed:return
        self.closed=True;self.model=None;self.tokenizer=None
        if self.forwards and self.device=='gpu':
            import torch
            torch.cuda.empty_cache()
        after={p.name:digest(p) for p in self.source_files}
        report={'calls':self.calls,'forwards_reserved':self.forwards,'source_unchanged':after==self.hashes,'device':self.device,
                'weight_edits':0,'training_steps':0,'results':list(self.results)}
        dump(self.root/'closed.json',report)
        if after!=self.hashes:raise RuntimeError('Source checkpoint changed during session')
        return report
