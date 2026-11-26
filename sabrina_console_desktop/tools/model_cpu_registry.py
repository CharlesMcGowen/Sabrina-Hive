"""Measured CPU model profiles and bounded skill routing for Sabrina's swarm."""
from __future__ import annotations

import json
from pathlib import Path

DEFAULT_PATH = Path(__file__).resolve().parents[1] / 'config/model_cpu_registry.json'


def load_cpu_registry(path: str | Path = DEFAULT_PATH) -> dict:
    registry = json.loads(Path(path).read_text(encoding='utf-8'))
    if registry.get('schema_version') != 1 or registry.get('evidence_type') != 'measured_cpu_only_benchmark':
        raise ValueError('Unsupported CPU model registry')
    for model in registry['models']:
        if not model.get('digest') or len(model.get('thread_sweep', [])) != 4:
            raise ValueError('Incomplete CPU model benchmark')
    return registry


def recommend_cpu_skill(task: str, *, path: str | Path = DEFAULT_PATH) -> dict:
    """Select a measured preset; never confer execution or approval authority."""
    registry = load_cpu_registry(path)
    route = registry['skill_routes'].get(task)
    if not route:
        return {'action': 'escalate', 'reason': 'No measured CPU preset for this task'}
    if route.get('action') == 'escalate':
        return {'action': 'escalate', 'reason': route['reason']}
    model = next((m for m in registry['models'] if m['name'] == route['model']), None)
    if model is None or route['threads'] != model['recommended_threads_for_measured_throughput']:
        raise ValueError('CPU skill route has no matching measured model')
    return {'action': 'cpu_inference', 'model': model['name'], 'digest': model['digest'],
            'num_gpu': 0, 'num_thread': route['threads'], 'condition': route['condition'],
            'measured_tokens_per_second': next(r['median_tokens_per_second'] for r in model['thread_sweep'] if r['threads'] == route['threads']),
            'structured_json_smoke': model['structured_json_smoke'], 'evidence_type': registry['evidence_type']}


def measured_cpu_threads(name: str, digest: str | None, *, path: str | Path = DEFAULT_PATH) -> int | None:
    """Use a measured thread count only for the same installed model weights."""
    if not digest:
        return None
    for model in load_cpu_registry(path)['models']:
        if model['name'] == name and model['digest'] == digest:
            return model['recommended_threads_for_measured_throughput']
    return None


def resolve_num_threads(model_name: str, digest: str | None = None, registry: dict | None = None, *, path: str | Path = DEFAULT_PATH) -> int | None:
    """Resolve recommended thread count for a model if present in measured registry."""
    if registry is None:
        try:
            registry = load_cpu_registry(path)
        except Exception:
            return None
    for model in registry.get('models', []):
        if model.get('name') == model_name:
            if digest is None or model.get('digest') == digest:
                return model.get('recommended_threads_for_measured_throughput')
    return None


load_registry = load_cpu_registry

