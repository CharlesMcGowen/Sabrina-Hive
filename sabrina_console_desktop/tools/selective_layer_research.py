"""Autonomous candidate projections inside a real rolling-state Mamba block."""
from __future__ import annotations
import argparse
import ast
import hashlib
import json
from pathlib import Path
import time
from tools import layer_research as base

ROOT = Path(__file__).resolve().parents[1]
RUNS = ROOT/'data/architecture_research/selective-mamba-v1'
DRIVER = ROOT/'tools/selective_layer_driver.py'
# Pinned separately from the source and verified by the shared sandbox runner.
DRIVER_SHA256 = '30795d1acff7f6ffba07059a685a0570deafeb592d67bccdaf8baaabf131c190'
REFERENCE = '''class CandidateLayer(nn.Module):
    def __init__(self, width):
        super().__init__()
        self.proj = nn.Linear(width, 18, bias=False)
    def forward(self, x):
        return self.proj(x)
'''

_original_request_body = base.request_body
_original_evaluate = base.evaluate
_original_history = base.design_history


def normalize_candidate(source):
    """Repair the known extra-axis join while retaining the original model response."""
    source, changes = base.normalize_constructors(source)
    tree = ast.parse(source)
    if len(tree.body) != 1 or not isinstance(tree.body[0], ast.ClassDef):
        return source, changes
    forward = next((method for method in tree.body[0].body
                    if isinstance(method, ast.FunctionDef) and method.name == 'forward'), None)
    if forward is None or not isinstance(forward.body[-1], ast.Return):
        return source, changes
    join = forward.body[-1].value
    if (not isinstance(join, ast.Call) or ast.unparse(join.func) != 'torch.cat'
            or len(join.args) != 1 or not isinstance(join.args[0], (ast.List, ast.Tuple))
            or len(join.args[0].elts) != 3 or len(join.keywords) != 1
            or join.keywords[0].arg != 'dim' or ast.unparse(join.keywords[0].value) != '2'):
        return source, changes

    # Only handle straight-line assignments of three names from an explicit unsqueeze(2).
    # Custom reshapes and control flow still go through the normal sandbox checks.
    if not all(isinstance(stmt, ast.Assign) and len(stmt.targets) == 1
               and isinstance(stmt.targets[0], ast.Name) for stmt in forward.body[:-1]):
        return source, changes
    last_assignment = {stmt.targets[0].id: stmt.value for stmt in forward.body[:-1]}
    for item in join.args[0].elts:
        value = last_assignment.get(item.id) if isinstance(item, ast.Name) else None
        if (not isinstance(value, ast.Call) or not isinstance(value.func, ast.Attribute)
                or value.func.attr != 'unsqueeze' or len(value.args) != 1 or value.keywords
                or ast.unparse(value.args[0]) != '2'):
            return source, changes

    # Keep upstream computations intact. Squeeze only the extra axes at the output join.
    join.args[0].elts = [
        ast.Call(func=ast.Attribute(value=item, attr='squeeze', ctx=ast.Load()),
                 args=[ast.Constant(2)], keywords=[])
        for item in join.args[0].elts
    ]
    join.keywords[0].value = ast.UnaryOp(op=ast.USub(), operand=ast.Constant(1))
    return ast.unparse(tree)+'\n', changes + ['Removed explicit singleton axes at the three-head output join']


def request_body(record, history):
    body = _original_request_body(record, history)
    body['project'] = 'autonomous-real-mamba-projections'
    body['task_label'] = 'Design selective Mamba projection'
    body['payload']['options']['num_predict'] = 800
    body['payload']['messages'][0]['content'] = (
        'Design a novel PyTorch module for the selective parameter projection INSIDE a real Mamba layer. '
        'Return JSON with hypothesis and source; source must contain exactly class CandidateLayer(nn.Module), '
        'with __init__(self,width) and forward(self,x). No imports or extra methods. '
        'x has shape [batch, sequence, width] where width is 48. Return one finite tensor '
        'of shape [batch, sequence, 18]. Its first 2 channels drive the Mamba time step, '
        'the next 8 drive state input B, and the final 8 drive state readout C. '
        'For separate heads, keep each output three-dimensional, with last dimensions 2, 8, and 8. '
        'Use torch.cat([dt, b, c], dim=-1) directly. Unsqueeze and stack add an incorrect fourth axis. '
        'nn.Linear already processes the last dimension for each token and needs no reshaping. '
        'All three groups must depend on x. This module must operate token by token; '
        'do not mix or aggregate across batch or sequence dimensions, and do not store mutable state. '
        'Tensors may become noncontiguous after slicing or transposing; use reshape rather than view. '
        'The trusted Mamba layer owns causal convolution, the rolling state, and cache. '
        'Use nn.Linear(width,18) or novel token-local combinations of allowed nn.Linear, LayerNorm, '
        'SiLU, GELU, Sigmoid and Tanh modules. Prefix constructors with nn. '
        'torch, nn, F are already provided; do not alias them. Allowed torch calls include sigmoid, tanh, '
        'zeros_like, ones_like, stack, cat, exp, clamp, matmul and einsum. '
        'Allowed tensor methods include reshape, view, unsqueeze, squeeze, chunk, split, clamp, tanh and sigmoid. '
        'Allowed builtins: range, len, super, enumerate, zip, int, float, min, max. '
        'Follow avoid_constructors; consider the previous development results but do not duplicate a design. '
        'Use no I/O, file or network access. Keep code concise and return actual Python, not markdown.')
    return body


def history(records):
    recent = _original_history(records)
    completed = [(p,r) for p,r in records if r.get('state') == 'completed' and
                 r.get('result',{}).get('validation',{}).get('streaming_cache_matches') is True]
    if completed:
        p, best = max(completed, key=lambda item:item[1]['result']['candidate']['development'] - item[1]['result']['reference']['development'])
        source = base.previous_source(p, best)
        entry = {'role':'best_development_so_far', 'hypothesis':best.get('hypothesis',''),
                 'constructors':base.layer_constructors(source),
                 'development':best['result']['candidate']['development'],
                 'reference_development':best['result']['reference']['development']}
        return recent[-2:] + [entry]
    return recent


def evaluate(run, record):
    source = (run/'candidate.py').read_text(encoding='utf-8')
    if hashlib.sha256(source.encode()).hexdigest() != record['source_sha256']:
        raise ValueError('Candidate source changed after design')
    validation = base.sandbox(f"layer-{record['id']}-candidate-preflight", {'mode':'validate','source':source})
    required = ('mamba_block','input_dependent_delta_b_c','causal','token_local_projection','streaming_cache_matches','state_affects_output')
    if any(validation.get(key) is not True for key in required):
        raise ValueError('Mamba rolling-state qualification failed')
    result = _original_evaluate(run, record)
    result['validation'] = validation
    return result


def configure():
    base.DRIVER = DRIVER
    base.DRIVER_SHA256 = DRIVER_SHA256
    base.REFERENCE = REFERENCE
    base.request_body = request_body
    base.design_history = history
    base.evaluate = evaluate


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--loop',action='store_true'); parser.add_argument('--status',action='store_true')
    args = parser.parse_args()
    RUNS.mkdir(parents=True,exist_ok=True)
    if args.status:
        print(json.dumps([json.loads(p.read_text()) for p in base.files(RUNS)],indent=2)); return
    configure()
    import fcntl
    with (RUNS/'worker.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        last = None
        while True:
            result = base.step(directory=RUNS, normalize_candidate=normalize_candidate)
            if result != last: print(json.dumps(result),flush=True); last=result
            if not args.loop: break
            time.sleep(10)

if __name__ == '__main__': main()
