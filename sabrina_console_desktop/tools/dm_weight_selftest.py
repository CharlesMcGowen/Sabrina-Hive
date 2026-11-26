"""Real-model controls for tool wiring; produces only unvalidated no-op drafts."""
import json
from pathlib import Path
import sys
from dm_weight_lab import DMLab,dump

root=Path(sys.argv[1])
source=json.loads((Path.home()/'.config/sabrina/weight_space_models.json').read_text())['models']['qwen2.5-coder-1.5b']
lab=DMLab(source,root)
try:
    listing=lab.dispatch('dm_list_tensors',{'filter':'self_attn.o_proj.weight','limit':2})
    tensor=listing['tensors'][0]['name']
    lab.dispatch('dm_tensor_slice',{'tensor':tensor,'slices':[[0,2],[0,4]]})
    prompt='Does HTTP HEAD return a body? Answer only YES or NO.'
    patched=lab.dispatch('dm_intervene',{'site':'head','head':0,'layer':0,'mode':'patch',
        'cases':[{'id':'identity','prompt':prompt,'source_prompt':prompt}]})
    assert patched['cases'][0]['baseline']==patched['cases'][0]['intervened']
    lab.dispatch('dm_inspect_result',{'result_id':patched['result_id'],'section':'cases'})
    zero=lab.dispatch('dm_intervene',{'site':'mlp','layer':0,'mode':'ablate','alpha':0,
        'cases':[{'id':'zero','prompt':prompt}]})
    assert zero['cases'][0]['baseline']==zero['cases'][0]['intervened']
    contrast=lab.dispatch('dm_intervene',{'site':'mlp','layer':0,'mode':'patch',
        'cases':[{'id':'contrast','prompt':prompt,'source_prompt':'Does HTTP GET return a body? Answer only YES or NO.'}]})
    projection=lab.dispatch('dm_intervene',{'site':'mlp','layer':0,'mode':'project','alpha':.25,
        'direction_ref':{'result_id':contrast['result_id'],'case_id':'contrast'},
        'cases':[{'id':'projected','prompt':prompt}]})
    assert projection['cases'][0]['intervened']['top_tokens']
    # A deliberate forward failure must remove its temporary hook.
    module=lab.model.model.layers[0].mlp.down_proj
    before=len(module._forward_hooks)
    original=lab.model.forward
    def failing(*args,**kwargs):raise RuntimeError('injected self-test failure')
    lab.model.forward=failing
    try:
        try:lab.dispatch('dm_intervene',{'site':'mlp','layer':0,'mode':'ablate','cases':[{'id':'failure','prompt':prompt}]})
        except RuntimeError as e:assert str(e)=='injected self-test failure'
        else:raise AssertionError('Expected injected failure')
        assert len(module._forward_hooks)==before
    finally:lab.model.forward=original
    draft=lab.dispatch('dm_draft_manifest',{'hypothesis':'Interface self-test only: multiplying one scalar by 1 leaves it unchanged.',
        'validation_plan':'Independently check source hashes and validate the no-op on an isolated copy before any application.',
        'evidence_ids':[patched['result_id']],'manifest':{'format_version':1,'edits':[{'tensor':tensor,'op':'multiply','index':[0,0],'value':1.0}]}})
    assert draft['summary']['applied'] is False
    dump(root/'selftest.json',{'identity_head_patch_equal':True,'zero_alpha_mlp_equal':True,'hooks_removed_after_exception':True,
        'evidence_direction_projection_ran':True,'draft_only':True,'result_id':draft['result_id']})
finally:lab.close()
