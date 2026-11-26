"""Bounded autonomous Mamba adapter-architecture experiments on shared compute."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time

from tools.control_client import configured

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / 'research/mamba_baseline'
RUNS = ROOT / 'data/architecture_research/mamba'
BASE_HASHES = {
    'train_mamba.py': 'd02c7776a42d14f0b53ba77aef6dbc1afcc4c4db182f40a4fc94a69a004e00c4',
    'preflight.json': 'a31b81daa714f6b9d5ff51f3db77f5c911a69e1c12cb9fc8f040225f15649517',
    'language_data.json': 'f9078176ebc146370b79b48c1a0fe0205e09f2399b3cc00db0780b398468acdb',
}
# Fixed, finite search space: edits only an isolated experiment's LoRA topology.
VARIANTS = [(8, 4), (4, 4), (8, 2), (8, 6), (16, 4), (4, 2), (16, 2), (4, 6), (16, 6)]
MAX_DAILY_STARTS = 4
TIMEOUT_SECONDS = 1800


def sha(data):
    return hashlib.sha256(data).hexdigest()


def save(path, value):
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(value, indent=2, sort_keys=True), encoding='utf-8')
    os.replace(temp, path)


def baseline():
    result = {}
    for name, expected in BASE_HASHES.items():
        data = (BASE / name).read_bytes()
        if sha(data) != expected:
            raise ValueError('Frozen baseline checksum changed: ' + name)
        result[name] = data
    return result


def replace_once(source, before, after):
    if source.count(before) != 1:
        raise ValueError('Baseline template mismatch: ' + before)
    return source.replace(before, after, 1)


def construct(source, rank, blocks):
    if (rank, blocks) not in VARIANTS:
        raise ValueError('Unknown architecture variant')
    text = source.decode('utf-8')
    text = replace_once(text, 'split = base.config.num_hidden_layers - 4',
                        f'split = base.config.num_hidden_layers - {blocks}')
    text = replace_once(text, 'range(split, split+4)', f'range(split, split+{blocks})')
    text = replace_once(text, 'LoraConfig(r=8, lora_alpha=16,',
                        f'LoraConfig(r={rank}, lora_alpha={rank * 2},')
    text = replace_once(text, "'trainable': 'rank-8 LoRA in_proj and x_proj in last four blocks'",
                        f"'trainable': 'rank-{rank} LoRA in_proj and x_proj in last {blocks} blocks'")
    return text.encode('utf-8')


def records(directory):
    return [json.loads(p.read_text(encoding='utf-8')) for p in sorted(directory.glob('*/run.json'))
            if not p.parent.name.startswith('.')]


def choose(history):
    seen = {(r['rank'], r['blocks']) for r in history}
    remaining = [v for v in VARIANTS if v not in seen]
    if not remaining:
        return None
    # Development measurements only. Never feed held-out results into selection.
    measured = [r for r in history if r.get('state') == 'completed' and 'development_score' in r]
    if not measured:
        return remaining[0]
    winner = max(measured, key=lambda r: r['development_score'])
    return min(remaining, key=lambda v: (abs(v[0]-winner['rank'])/4 + abs(v[1]-winner['blocks']), VARIANTS.index(v)))


def prepare(directory, variant, inputs):
    rank, blocks = variant
    path = directory / f'rank{rank}-blocks{blocks}'
    staging = Path(tempfile.mkdtemp(prefix='.preparing-', dir=directory))
    source = construct(inputs['train_mamba.py'], rank, blocks)
    (staging / 'train_mamba.py').write_bytes(source)
    for name in ('preflight.json', 'language_data.json'):
        (staging / name).write_bytes(inputs[name])
    record = {'rank': rank, 'blocks': blocks, 'state': 'prepared', 'created': time.time(),
              'source_sha256': sha(source), 'baseline_sha256': BASE_HASHES['train_mamba.py'],
              'description': 'Mamba-130m LoRA topology; base weights frozen',
              'production_promoted': False}
    save(staging / 'run.json', record)
    staging.rename(path)
    return path, record


def development(path):
    stages = [json.loads((path / f'stage_{i}_selection.json').read_text()) for i in range(3)]
    return {'passed_stages': sum(bool(s['passed']) for s in stages),
            'final_accuracy_sum': sum(f['accuracy'] for f in stages[-1]['facts']),
            'final_nll_ratio': stages[-1]['nll_ratio']}


def run_child(path, action):
    with (path / (action + '.log')).open('ab', buffering=0) as log:
        child = subprocess.Popen([sys.executable, '-u', str(path/'train_mamba.py'), action, str(path)],
                                 cwd=path, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        def terminate(_signal, _frame):
            os.killpg(child.pid, signal.SIGTERM)
            try:
                child.wait(timeout=15)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait()
            raise SystemExit('Interrupted after stopping experiment process')
        old = signal.signal(signal.SIGTERM, terminate)
        try:
            try:
                return child.wait(timeout=TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGTERM)
                try:
                    child.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    os.killpg(child.pid, signal.SIGKILL)
                    child.wait()
                return 124
        finally:
            signal.signal(signal.SIGTERM, old)


def execute(path, record, client):
    # Failures are recorded after the child has terminated and before releasing the lease.
    # Keep one reservation owner in the queue across long competing jobs.
    with client.lease('compute:lab', wait_seconds=86400):
        try:
            free = subprocess.check_output(['nvidia-smi', '--query-gpu=memory.free',
                     '--format=csv,noheader,nounits'], text=True, timeout=15)
            free_mib = int(free.splitlines()[0])
        except (OSError, ValueError, IndexError, subprocess.SubprocessError):
            return 'waiting_gpu_probe'
        if free_mib < 3500:
            return 'waiting_gpu_capacity'
        record.update(state='running', started=time.time())
        save(path/'run.json', record)
        for action in ('prepare', 'train', 'verify'):
            code = run_child(path, action)
            if code:
                record.update(state='failed', failed_action=action, exit_code=code, finished=time.time())
                save(path/'run.json', record)
                return 'failed'
        try:
            dev = development(path)
            result = json.loads((path/'restart_verification.json').read_text())
            if result.get('state') != 'verified':
                raise ValueError('Verifier did not report verified execution')
            verification = {key: result[key] for key in ('overall_gate_passed',
                            'restart_persistence_passed', 'new_learning_passed', 'sampled_retention_passed')}
        except (OSError, ValueError, KeyError, TypeError) as error:
            record.update(state='failed', failed_action='read_report', error=str(error)[:300], finished=time.time())
            save(path/'run.json', record)
            return 'failed'
        record.update(state='completed', finished=time.time(), development=dev,
                      development_score=[dev['passed_stages'], dev['final_accuracy_sum'], -dev['final_nll_ratio']],
                      verification=verification)
        save(path/'run.json', record)
        return 'completed'


def step(directory=RUNS, client=None, now=None):
    directory.mkdir(parents=True, exist_ok=True)
    inputs = baseline()
    history = records(directory)
    # A missing terminal receipt may mean prior execution is still alive. Never retry it.
    for record in history:
        if record['state'] == 'running':
            return {'state': 'interrupted_requires_review', 'rank': record['rank'], 'blocks': record['blocks']}
    prepared = [r for r in history if r['state'] == 'prepared']
    if prepared:
        record = prepared[0]
        path = directory / f"rank{record['rank']}-blocks{record['blocks']}"
        expected = construct(inputs['train_mamba.py'], record['rank'], record['blocks'])
        if (path/'train_mamba.py').read_bytes() != expected or sha(expected) != record['source_sha256']:
            raise ValueError('Prepared experiment source changed')
        for name in ('preflight.json', 'language_data.json'):
            if (path/name).read_bytes() != inputs[name]:
                raise ValueError('Prepared experiment input changed: ' + name)
    else:
        if sum(r.get('started', 0) > (now or time.time())-86400 for r in history) >= MAX_DAILY_STARTS:
            return {'state': 'waiting_daily_budget'}
        variant = choose(history)
        if variant is None:
            return {'state': 'search_complete'}
        path, record = prepare(directory, variant, inputs)
    client = client or configured()
    if client is None:
        return {'state': 'waiting_coordinator'}
    client.client, client.project = 'sabrina-architecture-research', 'autonomous-mamba'
    try:
        state = execute(path, record, client)
    except (TimeoutError, OSError) as error:
        return {'state': 'waiting_coordinator', 'error': str(error)[:300]}
    return {'state': state, 'rank': record['rank'], 'blocks': record['blocks'], 'run': str(path)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--loop', action='store_true')
    parser.add_argument('--status', action='store_true')
    args = parser.parse_args()
    RUNS.mkdir(parents=True, exist_ok=True)
    if args.status:
        print(json.dumps(records(RUNS), indent=2))
        return
    import fcntl
    with (RUNS/'worker.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        while True:
            result = step()
            print(json.dumps(result), flush=True)
            if not args.loop:
                break
            time.sleep(60)


if __name__ == '__main__':
    main()
