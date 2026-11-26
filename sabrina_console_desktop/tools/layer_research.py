"""Autonomous model-authored sequence layers; untrusted code stays in Docker."""
from __future__ import annotations
import argparse
import ast
import base64
import hashlib
import json
import os
from pathlib import Path
import random
import re
import secrets
import subprocess
import tempfile
import time
from tools.control_client import configured

ROOT = Path(__file__).resolve().parents[1]
RUNS = ROOT/'data/architecture_research/layers'
DRIVER = ROOT/'tools/layer_sandbox_driver.py'
DRIVER_SHA256 = '947fd9fb3ecd5e34988902241b03300a698bfe5cb1e4d1720b493c4abe8bd368'
IMAGE = 'sha256:dff7ee71c00fa75770d14b6866c5c032779b20115fd11d2b2a2e6f2b55f38299'
MODEL = 'granite-code:8b'
ALTERNATE_MODEL = 'qwen2.5:7b'
REFERENCE = '''class CandidateLayer(nn.Module):
    def __init__(self, width):
        super().__init__()
        self.gate = nn.Linear(width, width)
        self.value = nn.Linear(width, width)
        self.out = nn.Linear(width, width)
    def forward(self, x):
        h = torch.zeros_like(x[:, 0, :])
        states = []
        for i in range(x.shape[1]):
            a = torch.sigmoid(self.gate(x[:, i, :]))
            h = a*h + (1-a)*torch.tanh(self.value(x[:, i, :]))
            states.append(h)
        return self.out(torch.stack(states, dim=1))
'''


def save(path, data):
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(data, sort_keys=True, indent=2), encoding='utf-8')
    os.replace(temp, path)


def files(directory):
    return [p for p in sorted(directory.glob('run-*/record.json')) if p.is_file()]


def valid_source(source):
    if not isinstance(source, str) or not 1 <= len(source) <= 12000:
        raise ValueError('Candidate source must be 1..12000 characters')
    tree = ast.parse(source)
    if len(tree.body) != 1 or not isinstance(tree.body[0], ast.ClassDef) or tree.body[0].name != 'CandidateLayer':
        raise ValueError('Provide exactly class CandidateLayer')
    cls = tree.body[0]
    if cls.decorator_list or cls.keywords or len(cls.bases) != 1 or ast.unparse(cls.bases[0]) != 'nn.Module':
        raise ValueError('CandidateLayer must inherit only nn.Module')
    if any(not isinstance(x, ast.FunctionDef) for x in cls.body):
        raise ValueError('Class body may contain methods only')
    methods = {f.name for f in cls.body}
    if methods != {'__init__','forward'} or len(cls.body) != 2 or any(f.decorator_list for f in cls.body):
        raise ValueError('Only undecorated __init__ and forward are supported')
    for method in cls.body:
        wanted = ['self','width'] if method.name == '__init__' else ['self','x']
        args = method.args
        if ([a.arg for a in args.args] != wanted or args.posonlyargs or args.kwonlyargs or args.vararg
                or args.kwarg or args.defaults or method.returns or any(a.annotation for a in args.args)):
            raise ValueError('Method signatures must be __init__(self,width) and forward(self,x)')
    initialized = set()
    for method in cls.body:
        if method.name == '__init__':
            for n in ast.walk(method):
                if isinstance(n,ast.Assign):
                    initialized.update(t.attr for t in n.targets if isinstance(t,ast.Attribute)
                                       and isinstance(t.value,ast.Name) and t.value.id == 'self')
    forbidden = (ast.Import,ast.ImportFrom,ast.Global,ast.Nonlocal,ast.With,ast.AsyncWith,ast.AsyncFor,
                 ast.AsyncFunctionDef,ast.Lambda,ast.Try,ast.Raise,ast.While,ast.Yield,ast.YieldFrom,ast.GeneratorExp)
    modules = {'nn':{'Module','Linear','GRU','GRUCell','Conv1d','Parameter','LayerNorm','Dropout','ModuleList','Sequential','SiLU','GELU','Sigmoid','Tanh'},
               'torch':{'sigmoid','tanh','zeros_like','ones_like','zeros','ones','stack','cat','arange','exp','clamp','matmul','einsum','softmax'},
               'F':{'silu','gelu','softmax','sigmoid','tanh','linear'}}
    allowed_builtins = {'range','len','super','enumerate','zip','int','float','min','max'}
    tensor_methods = {'size','reshape','view','transpose','permute','unsqueeze','squeeze','expand','clone',
                      'contiguous','new_zeros','new_ones','mean','sum','chunk','split','repeat','flatten','unbind','append','clamp','tanh','sigmoid'}
    parents = {child:parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    for node in ast.walk(tree):
        if isinstance(node,ast.ClassDef) and node is not cls:
            raise ValueError('Nested classes are not supported')
        if isinstance(node,forbidden):
            raise ValueError('Unsupported syntax: '+type(node).__name__)
        if isinstance(node,ast.FunctionDef) and node not in cls.body:
            raise ValueError('Nested functions are not supported')
        if (isinstance(node,ast.Name) and isinstance(node.ctx,(ast.Store,ast.Del))
                and node.id in ({'self'} | set(modules) | allowed_builtins)):
            raise ValueError('Rebinding protected names is not allowed')
        if isinstance(node,ast.Name) and node.id.startswith('_') and node.id != '_':
            raise ValueError('Private names are not allowed')
        if isinstance(node,ast.Name) and node.id in {'open','exec','eval','compile','globals','locals','vars',
                'print','input','breakpoint','type','object','getattr','setattr','delattr','hasattr','help','exit','quit'}:
            raise ValueError('Introspection or I/O names are not allowed')
        if isinstance(node,ast.Name) and node.id in modules:
            parent = parents.get(node)
            if not isinstance(parent,ast.Attribute) or parent.value is not node:
                raise ValueError('Module aliases are not allowed')
        if isinstance(node,ast.Attribute):
            parent = parents.get(node)
            is_called = isinstance(parent,ast.Call) and parent.func is node
            direct_self = isinstance(node.value,ast.Name) and node.value.id=='self'
            direct_module = isinstance(node.value,ast.Name) and node.value.id in modules
            is_super = node.attr=='__init__' and isinstance(node.value,ast.Call) and isinstance(node.value.func,ast.Name) and node.value.func.id=='super'
            if node.attr.startswith('_') and not (node.attr == '__init__' and isinstance(node.value,ast.Call)
                    and isinstance(node.value.func,ast.Name) and node.value.func.id == 'super'):
                raise ValueError('Private attributes are not allowed')
            if node.attr == 'training':
                raise ValueError('Execution-mode inspection is not allowed')
            if direct_module and node.attr not in modules[node.value.id]:
                raise ValueError('Unsupported torch operation: '+ast.unparse(node))
            if direct_module and not is_called and node not in cls.bases:
                raise ValueError('Torch callable aliases are not allowed')
            if not (direct_module or direct_self or is_super or (is_called and node.attr in tensor_methods)
                    or (not is_called and node.attr in {'shape','dtype','device'})):
                raise ValueError('Unsupported attribute access: '+ast.unparse(node))
            if isinstance(node.ctx,ast.Store) and not direct_self:
                raise ValueError('Only layer attributes can be assigned')
        if isinstance(node,ast.Call):
            fn=node.func
            if isinstance(fn,ast.Name) and fn.id not in allowed_builtins:
                raise ValueError('Unsupported function: '+fn.id)
            if isinstance(fn,ast.Attribute):
                is_self = isinstance(fn.value,ast.Name) and fn.value.id=='self' and fn.attr in initialized
                is_module = isinstance(fn.value,ast.Name) and fn.value.id in modules and fn.attr in modules[fn.value.id]
                is_super = fn.attr=='__init__' and isinstance(fn.value,ast.Call) and isinstance(fn.value.func,ast.Name) and fn.value.func.id=='super'
                if not (is_self or is_module or is_super or fn.attr in tensor_methods):
                    raise ValueError('Unsupported method: '+ast.unparse(fn))
            elif not isinstance(fn,ast.Name):
                raise ValueError('Unsupported call target')
        if isinstance(node,(ast.Assign,ast.AugAssign,ast.AnnAssign)):
            targets = node.targets if isinstance(node,ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target,ast.Attribute) and not (isinstance(target.value,ast.Name) and target.value.id=='self'):
                    raise ValueError('Only layer attributes can be assigned')
    compile(tree, '<candidate>', 'exec')
    return source


def normalize_constructors(source):
    """Qualify unambiguous bare nn constructors; retain the original proposal separately."""
    if not isinstance(source,str) or len(source) > 12000:
        raise ValueError('Invalid proposal source')
    tree=ast.parse(source)
    names={'Linear','LayerNorm','GRUCell','Dropout','Conv1d','Parameter'}
    bound={n.id for n in ast.walk(tree) if isinstance(n,ast.Name) and isinstance(n.ctx,(ast.Store,ast.Del))}
    bound.update(n.arg for n in ast.walk(tree) if isinstance(n,ast.arg))
    changes=[]
    class Qualify(ast.NodeTransformer):
        def visit_Name(self,node):
            if node.id == 'input' and 'input' in bound and 'step_input_value' not in bound:
                if 'input -> step_input_value' not in changes:
                    changes.append('input -> step_input_value')
                return ast.copy_location(ast.Name(id='step_input_value',ctx=node.ctx),node)
            return node
        def visit_Call(self,node):
            self.generic_visit(node)
            if isinstance(node.func,ast.Name) and node.func.id in names and node.func.id not in bound:
                name=node.func.id
                node.func=ast.copy_location(ast.Attribute(value=ast.Name(id='nn',ctx=ast.Load()),attr=name,ctx=ast.Load()),node.func)
                changes.append(name+' -> nn.'+name)
            return node
    tree=Qualify().visit(tree)
    if not changes:
        return source,changes
    ast.fix_missing_locations(tree)
    return ast.unparse(tree)+'\n',changes


def source_fingerprint(source):
    """Compare candidate structure after the documented constructor normalization."""
    normalized, _ = normalize_constructors(source)
    tree = ast.parse(normalized)
    return hashlib.sha256(ast.dump(tree, include_attributes=False).encode()).hexdigest()


def previous_source(path, record):
    candidate = path.parent/'candidate.py'
    if candidate.exists():
        source = candidate.read_text(encoding='utf-8')
        if hashlib.sha256(source.encode()).hexdigest() != record.get('source_sha256'):
            raise ValueError('Previous candidate source changed')
        return source
    try:
        source = json.loads(record['response']['message']['content'])['source']
        return source if isinstance(source, str) else None
    except (KeyError, ValueError, TypeError, AttributeError):
        return None


def layer_constructors(source):
    normalized, _ = normalize_constructors(source)
    tree = ast.parse(normalized)
    return sorted({node.func.attr for node in ast.walk(tree) if isinstance(node, ast.Call)
                   and isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name)
                   and node.func.value.id == 'nn'})


def choose_planner_model(records):
    """Switch available model families after consecutive invalid proposals."""
    recent = []
    for _, record in reversed(records):
        model = record.get('request', {}).get('payload', {}).get('model')
        if model not in (MODEL, ALTERNATE_MODEL) or record['state'] not in ('failed','completed'):
            continue
        recent.append((model, record['state'] == 'failed' and
                       record.get('error','').startswith('Invalid proposal:')))
        if len(recent) == 2:
            break
    if not recent:
        return MODEL
    model = recent[0][0]
    if len(recent) == 2 and all(m == model and invalid for m, invalid in recent):
        return ALTERNATE_MODEL if model == MODEL else MODEL
    return model


def repeated_family_to_avoid(records):
    """Temporarily move away from a repeated recurrent/convolutional family."""
    for path, old in reversed(records):
        if old['state'] not in ('completed', 'failed'):
            continue
        error = old.get('error', '')
        if old['state'] == 'completed' or error.startswith('Experiment:'):
            return []
        if not (error.startswith('Invalid proposal: Duplicate candidate') or
                error.startswith('Invalid proposal: Repeated architecture uses')):
            continue
        source = previous_source(path, old)
        if not source:
            return []
        try:
            constructors = layer_constructors(source)
        except (ValueError, TypeError, SyntaxError):
            return []
        for name in ('GRUCell', 'GRU', 'Conv1d'):
            if name in constructors:
                return [name]
        return []
    return []


def design_history(records):
    """Retain distinct outcomes, preferring actual errors over duplicate rejections."""
    history, seen = [], set()
    for path, old in reversed(records):
        if old['state'] not in ('completed', 'failed'):
            continue
        if old['state'] == 'failed' and old.get('error', '').startswith('Invalid proposal: Duplicate candidate'):
            continue
        source = previous_source(path, old)
        if not source:
            continue
        try:
            key = source_fingerprint(source)
        except (ValueError, TypeError, SyntaxError):
            key = hashlib.sha256(source.encode()).hexdigest()
        if key in seen:
            continue
        seen.add(key)
        try:
            constructors = layer_constructors(source)
        except (ValueError, TypeError, SyntaxError):
            constructors = []
        entry = {'hypothesis':old.get('hypothesis',''), 'constructors':constructors}
        if old['state'] == 'completed':
            entry.update(development=old['result']['candidate']['development'],
                         untrained_development=old['result']['candidate']['untrained_development'],
                         reference_development=old['result']['reference']['development'])
        else:
            entry['failure'] = old.get('error','')[-1000:]
        history.append(entry)
        if len(history) == 3:
            break
    return list(reversed(history))


def examples(seed, count):
    rng = random.Random(seed)
    rows = []
    for _ in range(count):
        x = [rng.randint(1, 8) for _ in range(11)] + [9]
        rows.append({'x': x, 'y': x[0]-1})
    return rows


def accuracy(predictions, expected):
    if (not isinstance(predictions, list) or len(predictions) != len(expected)
            or any(type(p) is not int or not 0 <= p < 8 for p in predictions)):
        raise ValueError('Invalid prediction vector')
    return sum(p == e['y'] for p, e in zip(predictions, expected))/len(expected)


def sandbox(name, payload, timeout=240):
    """Wait for the named container to stop before returning or raising."""
    driver = DRIVER.read_text(encoding='utf-8')
    if hashlib.sha256(driver.encode()).hexdigest() != DRIVER_SHA256:
        raise RuntimeError('Frozen sandbox driver changed; retain compute lease')
    # The Docker client inherits a hard per-file output limit, even if the model floods stdout.
    argv = ['prlimit','--fsize=2097152:2097152','--','docker','run','--rm','-i','--name',name,'--pull','never','--network','none',
            '--read-only','--user','65534:65534','--cap-drop','ALL','--security-opt','no-new-privileges',
            '--memory','3g','--memory-swap','3g','--cpus','2','--pids-limit','96',
            '--log-driver','none','--tmpfs','/tmp:rw,nosuid,nodev,noexec,size=64m',
            '--entrypoint','timeout',IMAGE,'--signal=TERM','--kill-after=10',str(timeout)+'s','python','-I','-B','-c',driver]
    with tempfile.TemporaryFile() as inp, tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        inp.write(json.dumps(payload).encode()); inp.seek(0)
        process = subprocess.Popen(argv, stdin=inp, stdout=out, stderr=err)
        deadline = time.monotonic()+timeout
        try:
            while process.poll() is None:
                if time.monotonic() > deadline or os.fstat(out.fileno()).st_size + os.fstat(err.fileno()).st_size > 2_000_000:
                    raise TimeoutError('Sandbox timed out or exceeded output budget')
                time.sleep(.2)
        finally:
            # A terminated Docker CLI does not prove the daemon's container has stopped.
            check = subprocess.run(['docker','inspect','--format','{{.State.Running}}',name], capture_output=True, text=True, timeout=15)
            if check.returncode and 'no such object' not in check.stderr.lower():
                raise RuntimeError('Cannot inspect sandbox; retain compute lease')
            if check.returncode and process.poll() is None:
                raise RuntimeError('Sandbox creation outcome unknown; retain compute lease')
            if check.returncode == 0:
                kill = subprocess.run(['docker','rm','--force',name], capture_output=True, timeout=30)
                if kill.returncode:
                    raise RuntimeError('Unable to confirm sandbox stopped; retain compute lease')
            if process.poll() is None:
                process.terminate()
                try: process.wait(timeout=5)
                except subprocess.TimeoutExpired: process.kill(); process.wait()
            check = subprocess.run(['docker','inspect','--format','{{.State.Running}}',name], capture_output=True, text=True, timeout=15)
            if check.returncode and 'no such object' not in check.stderr.lower():
                raise RuntimeError('Cannot verify sandbox stopped; retain compute lease')
            if check.returncode == 0:
                raise RuntimeError('Sandbox still exists; retain compute lease')
        out.seek(0); err.seek(0)
        stdout, stderr = out.read(2_000_000).decode(errors='replace'), err.read(4000).decode(errors='replace')
        if process.returncode:
            raise ValueError('Sandbox failed: ' + stderr[-1000:])
        lines = [s[len('LAYER_RESPONSE='):] for s in stdout.splitlines() if s.startswith('LAYER_RESPONSE=')]
        if not lines:
            raise ValueError('Sandbox returned no prediction/checkpoint')
        return json.loads(lines[-1])


def evaluate(run, record):
    """Host owns labels and scoring; containers receive inputs only at evaluation."""
    source = (run/'candidate.py').read_text(encoding='utf-8')
    if hashlib.sha256(source.encode()).hexdigest() != record['source_sha256']:
        raise ValueError('Candidate source changed after design')
    seeds = record['fixture_seeds']
    train = examples(seeds['train'], 3200)
    dev = examples(seeds['development'], 256)
    holdout = examples(seeds['holdout'], 256)
    batches = [train[i:i+32] for i in range(0, len(train), 32)]
    # Catch malformed tensor dimensions before spending time training the reference.
    preflight = sandbox(f"layer-{record['id']}-candidate-preflight", {
        'mode':'predict', 'source':source, 'checkpoint':None,
        'inputs':[row['x'] for row in dev[:2]]})
    accuracy(preflight.get('predictions'), dev[:2])
    result = {}
    for name, code in [('reference',REFERENCE), ('candidate',source)]:
        tag = f"layer-{record['id']}-{name}"
        trained = sandbox(tag+'-train', {'mode':'train','source':code,'batches':batches})
        checkpoint = trained['checkpoint']
        if not isinstance(checkpoint,str) or len(checkpoint) > 1_000_000:
            raise ValueError('Invalid checkpoint')
        try:
            decoded = base64.b64decode(checkpoint,validate=True)
        except (ValueError,TypeError) as exc:
            raise ValueError('Invalid checkpoint encoding') from exc
        if not decoded or len(decoded) > 750_000:
            raise ValueError('Checkpoint exceeds the sandbox transfer budget')
        if type(trained.get('parameters')) is not int or not 0 < trained['parameters'] <= 1_000_000:
            raise ValueError('Invalid reported parameter count')
        # Never supply y or the fixture seed to the candidate in prediction calls.
        scores = {}
        for label, sample, weights in [('untrained_development',dev,None), ('development',dev,checkpoint), ('holdout',holdout,checkpoint)]:
            output = sandbox(tag+'-'+label.replace('_','-'), {'mode':'predict','source':code,'checkpoint':weights,
                                             'inputs':[r['x'] for r in sample]})
            scores[label] = accuracy(output.get('predictions'), sample)
        result[name] = {'parameters': trained['parameters'], **scores}
        (run/(name+'-checkpoint.b64')).write_text(checkpoint,encoding='ascii')
    return result


def request_body(record, history):
    schema = {'type':'object','properties':{'hypothesis':{'type':'string'},'source':{'type':'string'}},
              'required':['hypothesis','source'],'additionalProperties':False}
    message = ('Design a novel small selective state-space / Mamba-like PyTorch layer. '
        'Return JSON with hypothesis and source. Source must contain exactly class CandidateLayer(nn.Module), '
        'with __init__(self,width) and forward(self,x), input/output shape [batch, sequence, width]. '
        'torch, nn and F are already provided; do not import. Produce a causal differentiable layer, '
        'using a rolling state and input-dependent dynamics. No I/O, file or network access. '
        'Only methods __init__(self,width) and forward(self,x) are allowed, with no annotations or decorators. '
        'The class must be named CandidateLayer. Use super().__init__(). Prefix all layer constructors with nn. '
        'Allowed nn constructors: Linear, GRU, GRUCell, Conv1d, Parameter, LayerNorm, Dropout, ModuleList, Sequential, SiLU, GELU, Sigmoid, Tanh. '
        'Allowed torch calls: sigmoid, tanh, zeros_like, ones_like, zeros, ones, stack, cat, arange, exp, clamp, matmul, einsum, softmax. '
        'Allowed F calls: silu, gelu, softmax, sigmoid, tanh, linear. Do not alias torch, nn, F, or their functions. '
        'Allowed tensor methods: size, reshape, view, transpose, permute, unsqueeze, squeeze, expand, clone, contiguous, '
        'new_zeros, new_ones, mean, sum, chunk, split, repeat, flatten, unbind, clamp, tanh, sigmoid; lists may use append. '
        'Allowed builtins: range, len, super, enumerate, zip, int, float, min, max. '
        'If concatenating two width-sized vectors, the result has 2*width features: Linear input sizes must match. '
        'Gates combined elementwise with width-sized states must themselves have width features. '
        'Study prior failures, fix their root causes, and propose structurally different code. '
        'Follow the avoid_constructors list in the request; do not use those nn constructors. '
        'It will be trained from scratch on a delayed-symbol recall task and evaluated on new inputs. '
        'Keep the code concise, at most 20 lines and one million parameters; return actual Python code.')
    payload = {'model':record.get('planner_model', MODEL),'stream':False,'think':False,'format':schema,
               'options':{'temperature':0.65,'seed':record['seed'],'num_ctx':4096,'num_predict':600},
               'messages':[{'role':'system','content':message},
                           {'role':'user','content':json.dumps({'previous_development_results':history[-4:],
                                                               'avoid_constructors':record.get('avoid_constructors',[])})}]}
    return {'client':'sabrina-layer-research','project':'autonomous-mamba-layers',
            'operation_id':'sabrina-layer-'+record['id'], 'path':'/api/chat','payload':payload,
            'request_seconds':300,'device':'gpu','task_label':'Design new Mamba-like layer'}


def recover_interrupted(path, record, client):
    """Never replay an interrupted run until all its containers and lease are gone."""
    if not re.fullmatch(r'[0-9a-f]{20}',record['id']):
        raise ValueError('Invalid interrupted run identity')
    names = [f"layer-{record['id']}-{arm}-{phase}" for arm in ('reference','candidate')
             for phase in ('train','untrained-development','development','holdout')]
    names.append(f"layer-{record['id']}-candidate-preflight")
    for name in names:
        inspected = subprocess.run(['docker','inspect','--format','{{.State.Running}}',name],
                                   capture_output=True,text=True,timeout=15)
        if inspected.returncode == 0:
            if inspected.stdout.strip() == 'true':
                return {'state':'waiting_old_container','run':str(path.parent)}
            removed = subprocess.run(['docker','rm','--force',name],capture_output=True,text=True,timeout=30)
            if removed.returncode:
                return {'state':'interrupted_requires_review','error':'Could not remove stopped container'}
        elif 'no such object' not in inspected.stderr.lower():
            return {'state':'interrupted_requires_review','error':'Cannot inspect container'}
    processes = subprocess.check_output(['ps','-eo','args'],text=True,timeout=15).splitlines()
    if any('docker run' in line and any(name in line for name in names) for line in processes):
        return {'state':'waiting_old_docker_client','run':str(path.parent)}
    leases = client.request('/v1/status').get('leases',[])
    if any(x['resource']=='compute:lab' and x['owner'].startswith('sabrina-layer-research:') for x in leases):
        return {'state':'waiting_lease_reconciliation','run':str(path.parent)}
    record.update(state='failed',error='Interrupted; container absence and lease release verified',finished=time.time())
    save(path,record)
    return {'state':'interrupted_recorded','run':str(path.parent)}


def step(directory=RUNS, client=None, *, normalize_candidate=normalize_constructors):
    directory.mkdir(parents=True,exist_ok=True)
    paths = files(directory)
    records = sorted(((p,json.loads(p.read_text())) for p in paths), key=lambda item:item[1]['created'])
    pending = [(p,r) for p,r in records if r['state'] not in ('completed','failed')]
    if pending:
        p, record = pending[0]
        run = p.parent
    else:
        ident = secrets.token_hex(10)
        run = directory/('run-'+ident); run.mkdir()
        record = {'id':ident,'created':time.time(),'seed':secrets.randbelow(2**31), 'driver_sha256':DRIVER_SHA256,
                  'fixture_seeds':{k:secrets.randbits(128) for k in ('train','development','holdout')},'state':'planned'}
        record['planner_model'] = choose_planner_model(records)
        record['avoid_constructors'] = repeated_family_to_avoid(records)
        record['request'] = request_body(record, design_history(records))
        p = run/'record.json'; save(p,record)
    client = client or configured()
    if client is None:
        return {'state':'waiting_coordinator'}
    if record['state'] == 'running':
        return recover_interrupted(p,record,client)
    if record['state'] == 'planned':
        try:
            reply = (client.request('/v1/inference/'+record['request_id']) if record.get('request_id')
                     else client.request('/v1/inference', record['request']))
        except (OSError, TimeoutError) as exc:
            return {'state':'waiting_transport','error':str(exc)[:200]}
        record['request_id'] = reply['id']
        if reply['state'] == 'completed':
            record['response'] = reply['result']
            record['state'] = 'received'
        elif reply['state'] in ('failed','cancelled'):
            record.update(state='failed',error='Planner: '+str(reply.get('error',''))[:400])
        else:
            save(p,record)
            return {'state':reply['state'],'request_id':reply['id']}
        save(p,record)
    if record['state'] == 'received':
        try:
            raw = record['response']
            if raw.get('done_reason') == 'length': raise ValueError('Planner output truncated')
            proposal = json.loads(raw['message']['content'])
            source,normalizations = normalize_candidate(proposal['source'])
            source = valid_source(source)
            avoided = set(record.get('avoid_constructors',[])) & set(layer_constructors(source))
            if avoided:
                raise ValueError('Repeated architecture uses '+', '.join(sorted(avoided)))
            hypothesis = proposal['hypothesis']
            if not isinstance(hypothesis,str) or not 5 <= len(hypothesis) <= 1000: raise ValueError('Invalid hypothesis')
            fingerprint = source_fingerprint(source)
            for old_path, old in records:
                # Only candidates actually accepted for testing are duplicates. A proposal
                # rejected by an older validator may become valid after a validator fix.
                if not old.get('source_sha256'):
                    continue
                old_source = previous_source(old_path, old)
                if not old_source:
                    continue
                try:
                    duplicate = source_fingerprint(old_source) == fingerprint
                except (ValueError, TypeError, SyntaxError):
                    continue
                if duplicate:
                    raise ValueError('Duplicate candidate source')
            (run/'candidate.py').write_text(source,encoding='utf-8')
            record.update(state='designed',hypothesis=hypothesis,normalizations=normalizations,
                          source_sha256=hashlib.sha256(source.encode()).hexdigest())
        except (ValueError,KeyError,TypeError,SyntaxError,AttributeError) as exc:
            record.update(state='failed',error='Invalid proposal: '+str(exc)[:400])
        save(p,record)
    if record['state'] == 'designed':
        client.client,client.project='sabrina-layer-research','autonomous-mamba-layers'
        try:
            with client.lease('compute:lab',wait_seconds=86400):
                record.update(state='running',started=time.time()); save(p,record)
                try:
                    result=evaluate(run,record)
                except (ValueError,KeyError,TypeError,TimeoutError,json.JSONDecodeError) as exc:
                    record.update(state='failed',error='Experiment: '+str(exc)[-1400:],finished=time.time())
                else:
                    record.update(state='completed',result=result,finished=time.time())
                save(p,record)
        except (OSError,TimeoutError) as exc:
            return {'state':'waiting_coordinator','error':str(exc)[:200]}
    return {'state':record['state'],'run':str(run),'error':record.get('error','')}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--loop',action='store_true'); parser.add_argument('--status',action='store_true')
    args=parser.parse_args(); RUNS.mkdir(parents=True,exist_ok=True)
    if args.status:
        print(json.dumps([json.loads(p.read_text()) for p in files(RUNS)],indent=2)); return
    import fcntl
    with (RUNS/'worker.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        last=None
        while True:
            result=step()
            if result != last: print(json.dumps(result),flush=True); last=result
            if not args.loop: break
            time.sleep(10)


if __name__=='__main__': main()
