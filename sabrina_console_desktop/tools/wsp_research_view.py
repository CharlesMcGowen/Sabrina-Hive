"""Read-only, sanitized WSP receipts for the Control Center. No inference here."""
from __future__ import annotations

import json
import math
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT = ROOT / 'data' / 'wsp_research' / 'latest.json'


def score_pair(label, before, after, *, lower_is_better=False):
    def valid(value):
        if not isinstance(value, dict):return None
        passed,total=value.get('passed'),value.get('total')
        if any(type(x) not in (int,float) or not math.isfinite(x) for x in [passed,total]):return None
        if total<=0 or not 0<=passed<=total:return None
        return {'passed':passed,'total':total}
    return {'label':label,'before':valid(before),'after':valid(after),'lower_is_better':lower_is_better}


def metrics(summary):
    def mapping(value):return value if isinstance(value,dict) else {}
    summary=mapping(summary)
    parent,candidate=mapping(summary.get('parent')),mapping(summary.get('candidate'))
    rows=[score_pair('Complete state tasks',mapping(parent.get('state')).get('campaigns'),mapping(candidate.get('state')).get('campaigns'))]
    for name,label in [('math','Math: strict answers'),('coding','Coding: full tasks')]:
        rows.append(score_pair(label,parent.get(name),candidate.get(name)))
    for name,label in [('coding_format','Coding: valid Python format'),('anchor_format','Controls: valid Python format'),('metrics_contract','Metrics task: full contract'),('missing_fields','Missing fields: correct rejection'),('exact_whitespace','Exact whitespace: correct rejection')]:
        if name in parent or name in candidate:
            rows.append(score_pair(label,parent.get(name),candidate.get(name)))
    if 'total_checks' in mapping(parent.get('coding')) or 'total_checks' in mapping(candidate.get('coding')):
        def checks(side):
            value=mapping(side.get('coding'))
            return {'passed':value.get('passed_checks'),'total':value.get('total_checks')}
        rows.append(score_pair('Coding: individual checks',checks(parent),checks(candidate)))
    for name,label in [('math','Preservation: basic arithmetic'),('coding','Preservation: basic Python')]:
        rows.append(score_pair(label,mapping(parent.get('preservation_controls')).get(name),mapping(candidate.get('preservation_controls')).get(name)))
    for kind in ['wrong_argument','wrong_operation','format']:
        values=[]
        for side in [parent,candidate]:
            state=mapping(side.get('state'));value=mapping(state.get('failure_kinds')).get(kind)
            values.append({'passed':value,'total':state.get('turns')} if value is not None else None)
        rows.append(score_pair(kind.replace('_',' ')+' calls',*values,lower_is_better=True))
    preservation=mapping(summary.get('independent_preservation'))
    if preservation:
        rows.append(score_pair('Previously passing coding checks retained',preservation.get('before'),preservation.get('after')))
    return rows


def snapshot(path=SNAPSHOT):
    result={'available':False,'read_at':time.time(),'experiments':[]}
    if not path.is_file():return {**result,'message':'No lab receipt has been synchronized yet.'}
    try:
        if path.stat().st_size>4_000_000:raise ValueError('Receipt exceeds display budget')
        value=json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(value,dict) or not isinstance(value.get('experiments'),list):raise ValueError('Invalid receipt structure')
        experiments=[]
        for item in value['experiments'][:50]:
            if not isinstance(item,dict):continue
            # The synchronization contract excludes model text, tokens and activations.
            clean={k:item.get(k) for k in ['id','status','source_alias','run_path','comparison_id','progress','failure','summary','discovery','trace','calibration','inference','diagnostics','integrity','queue','deployment','comparison_kind','labels','study']}
            if clean['summary'] is not None and not isinstance(clean['summary'],dict):
                clean['summary']=None;clean['parse_error']='Summary is not an object'
            clean['metrics']=metrics(item.get('summary') or {})
            diagnostic=clean.get('diagnostics')
            if isinstance(diagnostic,dict):
                for key,label in [('basic','Basic Python: extracted functions (diagnostic)'),('full_task','Coding tasks: extracted functions (diagnostic)')]:
                    clean['metrics'].append(score_pair(label,diagnostic.get('parent',{}).get(key),diagnostic.get('candidate',{}).get(key)))
            experiments.append(clean)
        return {**result,'available':True,'synced_at':value.get('synced_at'),'source':value.get('source'),
            'stale':time.time()-float(value.get('synced_at',0))>300,'experiments':experiments}
    except (OSError,ValueError,TypeError) as error:return {**result,'message':f'Invalid research receipt: {error}'}
