"""Trusted ego-side checkpoint writer and paired Mamba research evaluator."""
import argparse
from collections import defaultdict
import contextlib
import fcntl
import json
import os
from pathlib import Path
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid

from tools import recruited_ssm_contract as c


ROOT = Path(__file__).resolve().parents[1]
RUNS = ROOT / 'data/architecture_research/recruited-ssm'
MODEL_DIR = Path.home() / '.cache/huggingface/hub/models--state-spaces--mamba-130m-hf/snapshots' / c.REVISION
RETENTION = ROOT / 'work/architecture-research-staging-20261121/language_data.json'
IMAGE = 'sha256:dff7ee71c00fa75770d14b6866c5c032779b20115fd11d2b2a2e6f2b55f38299'
CODESTRAL = 'mistralai/Mamba-Codestral-7B-v0.1'
CODESTRAL_REV = 'ab5ea2a2342c616ca1e29b325df2cf51cd286211'
CODESTRAL_MIN_FREE_MIB = 15200
SANDBOX = '''import json, sys
payload = json.load(sys.stdin)
ns = {}
exec(compile(payload['source'], '<recruited-edit>', 'exec'), ns)
print('RESULT=' + json.dumps(ns['edit'](payload['values']), allow_nan=False))
'''


def emit(data):
    print(c.canonical(data).decode(), flush=True)


def fixtures(random_seed, retention):
    seeds = random.Random(random_seed)
    result = {}
    filler = ['stone', 'cloud', 'lake', 'field', 'road', 'forest', 'rain', 'light']
    for split in ('development', 'holdout'):
        panels = []
        for _ in range(8):
            seed = seeds.getrandbits(63)
            rng = random.Random(seed)
            labels = list(range(8)); rng.shuffle(labels)
            cases = []
            for index, label in enumerate(labels):
                tag = rng.randrange(10000, 100000)
                delay = 16 if index % 2 == 0 else 96
                text = ' '.join(rng.choices(filler, k=delay))
                prompt = f'Key {tag} has value {chr(65+label)}.\n{text}\nWhat is the value of key {tag}?\nAnswer:'
                cases.append({'prompt': prompt, 'target': label, 'delay': delay})
            panels.append({'seed': seed, 'cases': cases})
        result[split] = {'panels': panels, 'retention': retention['general_dev' if split == 'development' else 'general_test'][:8]}
    return result


def initialize(run):
    run.mkdir(parents=True, exist_ok=True, mode=0o700)
    manifest = run / 'manifest.json'
    if not manifest.exists():
        variants = fixtures(int(c.sha(run.name.encode())[:15], 16), c.load(RETENTION))
        for split, data in variants.items():
            c.save(run / f'{split}.json', data)
        c.save(manifest, {'model': c.MODEL, 'revision': c.REVISION,
                          'base_sha256': c.file_sha(MODEL_DIR / 'model.safetensors'),
                          'fixture_hashes': {key: c.file_sha(run / f'{key}.json') for key in variants},
                          'protocol': 'sparse-mamba-edits-v1'})
    return c.load(manifest)


def corpus(run, split):
    path = run / f'{split}.json'
    if c.file_sha(path) != c.load(run / 'manifest.json')['fixture_hashes'][split]:
        raise RuntimeError('the frozen evaluation fixtures changed')
    return c.load(path)


def source_output(submission, values):
    name = 'sabrina-recruited-edit-' + uuid.uuid4().hex
    command = ['prlimit', '--fsize=65536:65536', '--', 'docker', 'run', '--rm', '-i', '--name', name,
               '--pull', 'never', '--network', 'none', '--read-only', '--user', '65534:65534',
               '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges', '--memory', '2g', '--memory-swap',
               '2g', '--cpus', '1', '--pids-limit', '48', '--log-driver', 'none',
               '--tmpfs', '/tmp:rw,nosuid,nodev,noexec,size=16m', '--env', 'OMP_NUM_THREADS=1',
               '--entrypoint', 'timeout', IMAGE, '--kill-after=2', '22s', 'python', '-I', '-B', '-c', SANDBOX]
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        try:
            result = subprocess.run(command, input=c.canonical({'source': submission['source'], 'values': values}),
                                    stdout=out, stderr=err, timeout=32, check=False)
        except subprocess.TimeoutExpired as exc:
            subprocess.run(['docker', 'rm', '--force', name], capture_output=True, timeout=12, check=False)
            raise ValueError('edit exceeded the offline sandbox time limit') from exc
        out.seek(0); err.seek(0)
        if result.returncode:
            diagnostic = err.read(6000).decode(errors='replace')
            error = re.search(r'(?m)^(ValueError|TypeError|NameError|IndexError|RuntimeError|ImportError|ZeroDivisionError):', diagnostic)
            line = re.search(r'<recruited-edit>", line (\d+)', diagnostic)
            reason = error.group(1) if error else f'exit {result.returncode}'
            raise ValueError(f'offline edit failed: {reason}' + (f' on edit line {line.group(1)}' if line else ''))
        lines = [line[7:] for line in out.read(65536).splitlines() if line.startswith(b'RESULT=')]
    if len(lines) != 1:
        raise ValueError('sandbox must return exactly one result')
    return c.validate_deltas(json.loads(lines[0]))


def tensor_name(submission):
    return f'backbone.layers.{submission["layer"]}.mixer.{submission["parameter"]}'


def atomic_tensor(path, name, tensor):
    from safetensors.torch import save_file
    pending = path.with_suffix('.next')
    save_file({name: tensor.contiguous()}, pending)
    os.replace(pending, path)


def prepare_overlay(run, job, submission):
    import torch
    from safetensors import safe_open
    output = run / 'evaluations' / job
    output.mkdir(parents=True, exist_ok=True)
    manifest = output / 'edits.json'
    if manifest.exists():
        data = c.load(manifest)
        for arm in ('candidate', 'control'):
            if c.file_sha(output / f'{arm}.safetensors') != data['overlay_hashes'][arm]:
                raise RuntimeError('saved overlay has changed')
        return data
    name = tensor_name(submission)
    with safe_open(MODEL_DIR / 'model.safetensors', framework='pt', device='cpu') as file:
        base = file.get_tensor(name).contiguous()
    if base.dtype != torch.float32 or base.numel() != c.TENSOR_SIZE[submission['parameter']]:
        raise RuntimeError('pinned checkpoint tensor does not match the edit contract')
    offset = submission['offset']
    values = base.flatten()[offset:offset + c.WIDTH].tolist()
    pairs = source_output(submission, values)
    if pairs != source_output(submission, values):
        raise ValueError('edit must return identical deltas on a fresh sandbox replay')
    actual = [(offset + index, delta) for index, delta in pairs]
    used = {index for index, _ in actual}
    fingerprint = c.sha(c.canonical([name, actual]))
    controls = random.Random(int(fingerprint[:16], 16)).sample([i for i in range(base.numel()) if i not in used], len(actual))
    control = list(zip(controls, [delta for _, delta in actual]))
    changed = {}
    for arm, edits in (('candidate', actual), ('control', control)):
        tensor = base.clone()
        flat = tensor.view(-1)
        indices, deltas = zip(*edits)
        previous = flat[list(indices)].clone()
        flat[list(indices)] += torch.tensor(deltas, dtype=torch.float32)
        if not torch.isfinite(tensor).all() or not (flat[list(indices)] != previous).all():
            raise ValueError('delta overflows or rounds away at FP32 precision')
        atomic_tensor(output / f'{arm}.safetensors', name, tensor)
        changed[arm] = c.file_sha(output / f'{arm}.safetensors')
    data = {'tensor': name, 'parameter': submission['parameter'], 'layer': submission['layer'],
            'direction': submission['direction'], 'weight_effect_sha256': fingerprint,
            'candidate_edits': actual, 'control_edits': control, 'overlay_hashes': changed,
            'source_sha256': c.sha(submission['source'].encode()), 'base_sha256': c.load(run / 'manifest.json')['base_sha256']}
    c.save(manifest, data)
    return data


class Evaluator:
    def __init__(self, run, device):
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer
        torch.set_num_threads(4)
        torch.backends.cuda.matmul.allow_tf32 = False
        torch.backends.cudnn.allow_tf32 = False
        self.torch, self.device, self.run = torch, device, run
        if c.file_sha(MODEL_DIR / 'model.safetensors') != c.load(run / 'manifest.json')['base_sha256']:
            raise RuntimeError('base checkpoint bytes changed')
        self.model = AutoModelForCausalLM.from_pretrained(MODEL_DIR, local_files_only=True, dtype=torch.float32).to(device).eval()
        self.tokens = AutoTokenizer.from_pretrained(MODEL_DIR, local_files_only=True)
        alphabet = [self.tokens.encode(' ' + chr(65 + i), add_special_tokens=False) for i in range(8)]
        if any(len(item) != 1 for item in alphabet) or len({x[0] for x in alphabet}) != 8:
            raise RuntimeError('the pinned tokenizer must give eight distinct one-token labels')
        self.labels = [x[0] for x in alphabet]

    def inputs(self, texts):
        return self.torch.tensor([self.tokens.encode(text, add_special_tokens=False) for text in texts], device=self.device)

    def logits(self, hidden):
        return self.torch.nn.functional.linear(hidden, self.model.lm_head.weight[self.labels])

    def score(self, data):
        t = self.torch
        by_length = defaultdict(list)
        for index, panel in enumerate(data['panels']):
            for item in panel['cases']:
                ids = self.tokens.encode(item['prompt'], add_special_tokens=False)
                by_length[len(ids)].append((index, ids, item['target'], item['delay']))
        outcomes = [[] for _ in range(8)]
        nll, count = 0., 0
        with t.inference_mode():
            for group in by_length.values():
                for start in range(0, len(group), 4):
                    rows = group[start:start + 4]
                    ids = t.tensor([row[1] for row in rows], device=self.device)
                    hs = self.model.backbone(input_ids=ids, use_cache=False).last_hidden_state[:, -1]
                    scores = self.logits(hs)
                    if not t.isfinite(scores).all():
                        raise ValueError('nonfinite recall scores')
                    for (index, _, target, delay), predicted in zip(rows, scores.argmax(-1).tolist()):
                        outcomes[index].append({'correct': predicted == target, 'delay': delay})
            for text in data['retention']:
                ids = self.inputs([text])[:, :128]
                hs = self.model.backbone(input_ids=ids, use_cache=False).last_hidden_state[:, :-1]
                logits = self.model.lm_head(hs)
                nll += t.nn.functional.cross_entropy(logits.flatten(0, 1), ids[:, 1:].flatten(), reduction='sum').item()
                count += ids.shape[1] - 1
        return {'panels': [sum(entry['correct'] for entry in row) / len(row) for row in outcomes],
                'delay_accuracy': {str(delay): sum(x['correct'] for row in outcomes for x in row if x['delay'] == delay)
                                   / sum(x['delay'] == delay for row in outcomes for x in row) for delay in (16, 96)},
                'retention_loss': nll / count}

    @contextlib.contextmanager
    def overlay(self, path, expected_name):
        from safetensors.torch import load_file
        contents = load_file(path, device='cpu')
        if set(contents) != {expected_name}:
            raise ValueError('overlay replaces an unexpected tensor')
        parameter = self.model.get_parameter(expected_name)
        replacement = contents[expected_name]
        if parameter.shape != replacement.shape or parameter.dtype != replacement.dtype:
            raise ValueError('overlay shape or dtype differs from original')
        with self.torch.no_grad():
            previous = parameter.detach().clone()
            parameter.copy_(replacement)
            try:
                yield
            finally:
                parameter.copy_(previous)

    def parity(self, data):
        t = self.torch
        case = data['panels'][0]['cases'][0]
        ids = self.inputs([case['prompt']])
        with t.inference_mode():
            full = self.model.backbone(input_ids=ids, use_cache=False).last_hidden_state
            first = self.model.backbone(input_ids=ids[:, :-2], use_cache=True)
            cache = first.cache_params
            steps = [first.last_hidden_state]
            for position in (-2, -1):
                token = ids[:, position:position + 1] if position != -1 else ids[:, -1:]
                item = self.model.backbone(input_ids=token, cache_params=cache, use_cache=True)
                steps.append(item.last_hidden_state)
        error = (t.cat(steps, dim=1) - full).abs().max().item()
        prefix_error = (steps[0] - full[:, :-2]).abs().max().item()
        return {'passes': bool(t.allclose(t.cat(steps, dim=1), full, atol=3e-4, rtol=3e-4)),
                'max_abs': error, 'prefix_max_abs': prefix_error}

    def ablation(self, data, edits):
        t = self.torch
        layer = edits['layer']
        mixer = self.model.backbone.layers[layer].mixer
        def state_indices(pairs):
            indices = [i for i, _ in pairs]
            return indices if edits['parameter'] == 'A_log' else [i*16 + j for i in indices for j in range(16)]
        targeted = state_indices(edits['candidate_edits'])
        unrelated = state_indices(edits['control_edits'])
        channels = t.tensor([i // 16 for i in targeted], device=self.device)
        modes = t.tensor([i % 16 for i in targeted], device=self.device)
        observations = []
        dt_outputs = []
        hook = mixer.dt_proj.register_forward_hook(lambda _m, _x, y: dt_outputs.append(y.detach()))
        saved_forward = mixer.forward
        mixer.forward = mixer.slow_forward
        try:
            with t.inference_mode():
                for panel in data['panels']:
                    item = next(case for case in panel['cases'] if case['delay'] == 96)
                    ids = self.inputs([item['prompt']]); margins = []
                    for reset in ((), targeted, unrelated):
                        dt_outputs.clear()
                        cache = self.model.backbone(input_ids=ids[:, :-1], use_cache=True).cache_params
                        state_layer = cache.layers[layer]
                        state = state_layer.recurrent_states[0]
                        conv_before = state_layer.conv_states[0].clone()
                        carry = t.exp(-mixer.A_log[channels, modes].exp() * t.nn.functional.softplus(dt_outputs[0][..., channels])).mean().item()
                        norm = state.flatten(1)[:, targeted].norm().item()
                        if reset:
                            state.view(state.shape[0], -1)[:, reset] = 0
                        if not t.equal(conv_before, state_layer.conv_states[0]):
                            raise RuntimeError('reset changed convolution state')
                        result = self.model.backbone(input_ids=ids[:, -1:], cache_params=cache, use_cache=True)
                        scores = self.logits(result.last_hidden_state[0, -1])
                        target = item['target']
                        margins.append((scores[target] - t.cat((scores[:target], scores[target + 1:])).max()).item())
                        if not reset:
                            observed_carry, observed_norm = carry, norm
                    observations.append({'mean_carry': observed_carry, 'state_norm': observed_norm,
                                         'margin': margins[0], 'target_reset_margin': margins[1], 'unrelated_reset_margin': margins[2]})
        finally:
            mixer.forward = saved_forward
            hook.remove()
        return observations


def evaluate(run, job, device, split):
    output = run / 'evaluations' / job
    edits = c.load(output / 'edits.json')
    data = corpus(run, split)
    evaluator = Evaluator(run, device)
    baseline_file = run / f'{split}-baseline-{device}.json'
    base = c.load(baseline_file) if baseline_file.exists() else evaluator.score(data)
    if not baseline_file.exists():
        c.save(baseline_file, base)
    base_trace = evaluator.ablation(data, edits) if split == 'holdout' else None
    results = {'base': base, 'device': device}
    for arm in ('candidate', 'control'):
        path = output / f'{arm}.safetensors'
        if c.file_sha(path) != edits['overlay_hashes'][arm]:
            raise RuntimeError('the checkpoint overlay changed')
        with evaluator.overlay(path, edits['tensor']):
            results[arm] = evaluator.score(data)
            if arm == 'candidate':
                results['parity'] = evaluator.parity(data)
                if split == 'holdout':
                    results['candidate_trace'] = evaluator.ablation(data, edits)
    b, a, shuffled = (results[key] for key in ('base', 'candidate', 'control'))
    results['comparison'] = c.decision(b['panels'], a['panels'], shuffled['panels'], b['retention_loss'], a['retention_loss'])
    if base_trace is not None:
        results['base_trace'] = base_trace
        results['mechanism'] = mechanism(base_trace, results['candidate_trace'], edits['direction'])
    return results


def mechanism(base, edited, direction):
    carry = sum(b['mean_carry'] - a['mean_carry'] for a, b in zip(base, edited)) / len(base)
    def remaining(arm):
        return sum(abs(b[arm] - a[arm]) for a, b in zip(base, edited)) / len(base)
    intact, target, unrelated = (remaining(key) for key in ('margin', 'target_reset_margin', 'unrelated_reset_margin'))
    return {'carry_delta': carry, 'predicted_direction': direction, 'intact_effect': intact,
            'target_reset_effect': target, 'unrelated_reset_effect': unrelated,
            'supported': (carry > 0 if direction == 'increase' else carry < 0) and target < min(intact, unrelated)}


def checkout_device():
    return 'cuda' if int(subprocess.check_output(['nvidia-smi', '--query-gpu=memory.free', '--format=csv,noheader,nounits'],
                                                  text=True, timeout=8).splitlines()[0]) >= 3400 else None


def compute(run, job, split):
    from tools.control_client import configured
    client = configured()
    if client is None:
        raise RuntimeError('coordinator client is unavailable')
    client.client, client.project = 'sabrina-recruited-ssm', 'recruited-ssm'
    leases = client.request('/v1/status').get('leases', [])
    hold = next((lease for lease in leases if lease['resource'] == 'compute:lab' and lease['state'] == 'uncertain'), None)
    if hold:
        c.save(run / 'compute-status.json', {'state': 'waiting_coordinator_reconciliation', 'resource': 'compute:lab',
                                              'owner': hold['owner'], 'checked_at': time.time()})
        return None
    with client.lease('compute:lab', wait_seconds=1200):
        device = checkout_device()
        if device is None:
            c.save(run / 'compute-status.json', {'state': 'waiting_gpu_capacity', 'checked_at': time.time()})
            return None
        c.save(run / 'compute-status.json', {'state': 'scoring', 'job': job, 'split': split, 'checked_at': time.time()})
        args = [sys.executable, '-B', '-m', 'tools.recruited_ssm_eval', 'score', run.name, job, split, device]
        result = subprocess.run(args, text=True, capture_output=True, timeout=780, check=False)
        if result.returncode:
            raise RuntimeError('trusted evaluator failed: ' + result.stderr[-1300:])
        outcome = json.loads(result.stdout.splitlines()[-1])
        c.save(run / 'compute-status.json', {'state': 'scored', 'job': job, 'split': split, 'checked_at': time.time()})
        return outcome


def process_job(run, path):
    job = path.stem
    result = run / 'public' / f'{job}.json'
    if result.exists():
        return False
    payload = c.load(path)
    try:
        proposal = c.validate_submission(payload['submission'])
        edits = prepare_overlay(run, job, proposal)
    except (ValueError, TypeError, SyntaxError, IndexError) as exc:
        c.save(result, {'state': 'rejected', 'diagnostic': str(exc)[:300]})
        return True
    receipt = run / 'development-effects' / (edits['weight_effect_sha256'] + '.json')
    if receipt.exists():
        report = c.load(receipt)
    else:
        report = compute(run, job, 'development')
        if report is None:
            return False
        c.save(receipt, report)
    c.save(result, {'state': 'evaluated', 'development': report, 'weight_effect_sha256': edits['weight_effect_sha256']})
    return True


def select(run):
    candidates = [(path.stem, c.load(path)) for path in sorted((run / 'public').glob('*.json'))]
    return min(((job, report['development']['comparison']) for job, report in candidates
                if report['state'] == 'evaluated' and report['development']['parity']['passes']),
               key=lambda item: (-item[1]['mean_delta'], -item[1]['mean_delta_vs_control'],
                                 item[1]['retention_loss_ratio'], item[0]), default=None)


def finalize(run):
    if (run / 'verdict.json').exists():
        return False
    shortlisted = run / 'shortlist.json'
    if not shortlisted.exists():
        chosen = select(run)
        if chosen is None:
            c.save(run / 'verdict.json', {'state': 'no_candidate_passed_development_checks'})
            return True
        c.save(shortlisted, {'job': chosen[0], 'development': chosen[1], 'chosen_at': time.time()})
    job = c.load(shortlisted)['job']
    scored = run / 'holdout-results.json'
    if not scored.exists():
        first = compute(run, job, 'holdout')
        if first is None:
            return False
        c.save(scored, first)
    report = c.load(scored)
    reload = compute(run, job, 'holdout')
    if reload is None:
        return False
    same = all(report[key]['panels'] == reload[key]['panels'] and abs(report[key]['retention_loss'] - reload[key]['retention_loss']) < 1e-5
               for key in ('base', 'candidate', 'control'))
    positive = report['comparison']['improved'] and same and report['parity']['passes'] and reload['parity']['passes']
    c.save(run / 'verdict.json', {'state': 'completed', 'job': job, 'fresh_process_reload': same,
                                 'improved': positive, 'explained_improvement': positive and report['mechanism']['supported'],
                                 'production_promoted': False, 'holdout': report})
    return True


def verify_first_edit(run):
    receipt = run / 'bootstrap-verified.json'
    if receipt.exists():
        return
    candidate = select(run)
    if candidate is None:
        return
    job = candidate[0]
    original = c.load(run / 'public' / f'{job}.json')['development']
    fresh = compute(run, job, 'development')
    if fresh is None:
        return
    same = fresh['parity']['passes'] and all(
        fresh[key]['panels'] == original[key]['panels'] and
        abs(fresh[key]['retention_loss'] - original[key]['retention_loss']) < 1e-5
        for key in ('base', 'candidate', 'control'))
    if not same:
        c.save(run / 'bootstrap-reload-mismatch.json', {'job': job, 'fresh': fresh})
        raise RuntimeError('fresh-process development evaluation disagreed')
    c.save(receipt, {'job': job, 'device': fresh['device'], 'development': fresh,
                     'source_receipt': c.load(run / 'jobs' / f'{job}.json')['author']})


def codestral_status(run):
    import importlib.util
    from transformers import Mamba2ForCausalLM
    root = Path.home() / '.cache/huggingface/hub'
    snapshot = root / 'models--mistralai--Mamba-Codestral-7B-v0.1/snapshots' / CODESTRAL_REV
    index = snapshot / 'model.safetensors.index.json'
    ready = index.is_file() and (snapshot / 'config.json').is_file() and all(
        (snapshot / name).is_file() for name in c.load(index)['weight_map'].values())
    free = int(subprocess.check_output(['nvidia-smi', '--query-gpu=memory.free', '--format=csv,noheader,nounits'],
                                       text=True, timeout=8).splitlines()[0])
    report = {'model': CODESTRAL, 'revision': CODESTRAL_REV, 'min_gpu_headroom_mib': CODESTRAL_MIN_FREE_MIB, 'current_gpu_free_mib': free,
              'official_checkpoint_bytes': 14570874432, 'free_cache_disk_bytes': shutil.disk_usage(root).free,
              'loader_class': Mamba2ForCausalLM.__name__, 'native_mamba_ssm_installed': importlib.util.find_spec('mamba_ssm') is not None,
              'official_local_snapshots': [str(snapshot)] if ready else [],
              'state': 'waiting_mamba_130m_recruited_reload' if not (run / 'bootstrap-verified.json').exists()
              else ('waiting_codestral_gpu_headroom' if free < CODESTRAL_MIN_FREE_MIB else
                    ('checkpoint_available_for_loader_smoke' if ready else 'waiting_official_checkpoint'))}
    c.save(run / 'codestral-status.json', report)
    return report


def codestral_probe(snapshot):
    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer
    model = AutoModelForCausalLM.from_pretrained(snapshot, dtype=torch.bfloat16, local_files_only=True,
                                               device_map={'': 0}).eval()
    tokenizer = AutoTokenizer.from_pretrained(snapshot, local_files_only=True)
    ids = tokenizer('def add(a, b):', return_tensors='pt').input_ids.to('cuda')
    with torch.inference_mode():
        full = model.backbone(input_ids=ids, use_cache=False).last_hidden_state[:, -1]
        prefix = model.backbone(input_ids=ids[:, :-1], use_cache=True)
        stream = model.backbone(input_ids=ids[:, -1:], cache_params=prefix.cache_params,
                                use_cache=True).last_hidden_state[:, -1]
    parity = bool(torch.isfinite(full).all() and torch.allclose(full.float(), stream.float(), rtol=.03, atol=.03))
    params = {name: list(param.shape) for name, param in model.named_parameters()
              if name.split('.')[-1] in ('A_log', 'dt_bias') and any(f'.layers.{i}.' in name for
                  i in range(model.config.num_hidden_layers - 4, model.config.num_hidden_layers))}
    if not parity or not params:
        raise ValueError('Codestral streaming output or editable tensor mapping failed qualification')
    return {'state': 'loader_qualified', 'model': CODESTRAL, 'revision': CODESTRAL_REV, 'dtype': 'bf16',
            'cached_streaming_parity': parity, 'runtime_peak_cuda_bytes': torch.cuda.max_memory_reserved(),
            'candidate_tensor_shapes': params}


def qualify_codestral(run):
    status = codestral_status(run)
    if status['state'] not in ('checkpoint_available_for_loader_smoke', 'waiting_official_checkpoint'):
        return status
    if status['free_cache_disk_bytes'] < 2 * status['official_checkpoint_bytes']:
        return {**status, 'state': 'waiting_codestral_disk'}
    from huggingface_hub import snapshot_download
    snapshot = snapshot_download(CODESTRAL, revision=CODESTRAL_REV, allow_patterns=[
        'config.json', 'generation_config.json', 'tokenizer.json', 'tokenizer_config.json', 'special_tokens_map.json',
        'model-*.safetensors', 'model.safetensors.index.json'])
    from tools.control_client import configured
    client = configured()
    if client is None:
        raise RuntimeError('coordinator client is unavailable')
    client.client, client.project = 'sabrina-recruited-codestral', 'recruited-ssm'
    with client.lease('compute:lab', wait_seconds=1200):
        status = codestral_status(run)
        if status['current_gpu_free_mib'] < CODESTRAL_MIN_FREE_MIB:
            return status
        outcome = codestral_probe(snapshot)
    c.save(run / 'codestral-qualified.json', outcome)
    return outcome


def system_jobs(run):
    from tools import recruited_ssm_systems as systems
    for job in sorted((run / 'systems-jobs').glob('*.json')):
        receipt = run / 'systems-results' / job.name
        if not receipt.exists():
            c.save(receipt, systems.evaluate(c.load(job), IMAGE))
            return True
    return False


def systems_work():
    RUNS.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (RUNS / '.systems-worker.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        while True:
            try:
                for run in sorted(RUNS.iterdir()):
                    if (run / 'manifest.json').is_file():
                        system_jobs(run)
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
                print(str(exc)[:500], file=sys.stderr, flush=True)
            time.sleep(12)


def work(once=False):
    RUNS.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (RUNS / '.worker.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        while True:
            try:
                pending = []
                for run in sorted(RUNS.iterdir()):
                    if (run / 'manifest.json').is_file():
                        if not (run / 'bootstrap-verified.json').exists():
                            verify_first_edit(run)
                        elif not (run / 'codestral-status.json').exists() or c.load(run / 'codestral-status.json')['state'] == 'waiting_mamba_130m_recruited_reload':
                            codestral_status(run)
                        pending.extend((run, job) for job in sorted((run / 'jobs').glob('*.json'))
                                       if not (run / 'public' / job.name).exists())
                        if (run / 'closed.json').exists() and not (run / 'verdict.json').exists() and not any(r == run for r, _ in pending):
                            finalize(run)
                for run, job in pending[:4]:
                    if process_job(run, job):
                        break
            except (OSError, RuntimeError, TimeoutError, subprocess.SubprocessError) as exc:
                print(str(exc)[:1700], file=sys.stderr, flush=True)
                if once:
                    raise
            if once:
                return
            time.sleep(10 if pending else 20)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('init', 'accept', 'overview', 'close', 'worker', 'once', 'score',
                                           'systems-accept', 'systems-status', 'systems-worker', 'codestral-status', 'codestral-qualify'))
    parser.add_argument('run', nargs='?')
    parser.add_argument('job', nargs='?')
    parser.add_argument('split', nargs='?', choices=('development', 'holdout'))
    parser.add_argument('device', nargs='?', choices=('cpu', 'cuda'))
    args = parser.parse_args()
    if args.action in ('worker', 'once'):
        work(once=args.action == 'once')
        return
    if args.action == 'systems-worker':
        systems_work()
        return
    run = RUNS / c.check_key(args.run)
    if args.action == 'init':
        emit(initialize(run))
        return
    if not (run / 'manifest.json').is_file():
        raise ValueError('run has no frozen manifest')
    if args.action == 'accept':
        job = c.check_key(args.job)
        payload = json.loads(sys.stdin.buffer.read(65536))
        c.validate_submission(payload['submission'])
        path = run / 'jobs' / (job + '.json')
        if path.exists():
            if c.load(path) != payload:
                raise ValueError('job identifier already used with different content')
        else:
            if (run / 'closed.json').exists():
                raise ValueError('the development panel is closed')
            c.save(path, payload)
        emit({'state': 'accepted', 'job': job})
    elif args.action == 'overview':
        status = run / 'compute-status.json'
        emit({'development': {p.stem: c.load(p) for p in (run / 'public').glob('*.json')},
              'finalized': (run / 'verdict.json').exists(), 'bootstrap_verified': (run / 'bootstrap-verified.json').exists(),
              'compute_status': c.load(status) if status.exists() else None})
    elif args.action == 'close':
        if not (run / 'closed.json').exists():
            c.save(run / 'closed.json', {'closed_at': time.time()})
        emit({'closed': True})
    elif args.action == 'score':
        emit(evaluate(run, c.check_key(args.job), args.device, args.split))
    elif args.action == 'systems-accept':
        from tools import recruited_ssm_systems as systems
        job = c.check_key(args.job)
        payload = json.loads(sys.stdin.buffer.read(100000))
        systems.validate(payload['language'], payload['source'])
        path = run / 'systems-jobs' / (job + '.json')
        if path.exists() and c.load(path) != payload:
            raise ValueError('job identifier already used with different content')
        if not path.exists():
            c.save(path, payload)
        emit({'state': 'accepted', 'job': job})
    elif args.action == 'systems-status':
        emit({p.stem: c.load(p) for p in (run / 'systems-results').glob('*.json')})
    elif args.action == 'codestral-status':
        emit(codestral_status(run))
    elif args.action == 'codestral-qualify':
        emit(qualify_codestral(run))


if __name__ == '__main__':
    main()
