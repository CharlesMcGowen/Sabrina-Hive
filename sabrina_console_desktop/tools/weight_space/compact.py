"""Canonical compact-policy backend: registered aliases, bounded isolated edits.

Tensor mutation and manifest validation delegate to core.py. This backend only
adapts compact checkpoint serialization; it never serves or promotes a candidate.
"""
from __future__ import annotations

import json
from pathlib import Path

from tools.weight_space import core
from tools.kraken_strategy_engine import sha, save


def resolve(alias, registry):
    if not isinstance(alias, str) or alias not in registry:
        raise ValueError('unregistered_compact_alias')
    source = Path(registry[alias]).resolve()
    if source == core.SOURCE_ROOT or core.SOURCE_ROOT in source.parents:
        raise ValueError('checkpoint_must_be_outside_source_tree')
    if not (source/'manifest.json').is_file() or not (source/'weights.npz').is_file():
        raise ValueError('incomplete_compact_checkpoint')
    return source


def inspect(alias, registry):
    from tools.kraken_policy_model import load_checkpoint
    source = resolve(alias, registry); model = load_checkpoint(source)
    return {'alias': alias, 'weights_sha256': sha(source/'weights.npz'),
            'tensors': [{'name': k, 'shape': list(v.shape), 'norm': float(v.detach().norm())}
                        for k, v in model.named_parameters()]}


def experiment(alias, registry, destination, manifest, *, hypothesis, evidence_ids):
    import torch
    from tools.kraken_policy_model import load_checkpoint, save_checkpoint
    if not isinstance(hypothesis, str) or len(hypothesis) < 20 or len(set(evidence_ids)) < 2:
        raise ValueError('weight_edit_requires_specific_hypothesis_and_two_failures')
    source = resolve(alias, registry); destination = Path(destination).resolve()
    if (destination == source or source in destination.parents or destination.exists()
            or destination == core.SOURCE_ROOT or core.SOURCE_ROOT in destination.parents):
        raise ValueError('candidate_must_be_separate_outside_source_tree')
    hashes = {n: sha(source/n) for n in ('weights.npz', 'manifest.json')}
    edits = core._validate_manifest(manifest)
    if len(edits) != 1 or edits[0]['tensor'] != 'head.2.bias' or edits[0]['op'] != 'add' or edits[0]['index'] != [1]:
        raise ValueError('compact_recipe_allows_only_buy_value_bias_probe')
    if edits[0]['value'] not in (-.001, -.002):
        raise ValueError('compact_edit_magnitude_outside_recipe')
    model = load_checkpoint(source)
    before = {k: v.detach().clone() for k, v in model.named_parameters()}
    applied = core._apply_edits(model, edits)
    delta = sum(float((v.detach()-before[k]).square().sum()) for k, v in model.named_parameters())**.5
    norm = sum(float(v.square().sum()) for v in before.values())**.5
    if delta > max(1e-9, norm)*.01:
        raise ValueError('compact_edit_relative_norm_exceeded')
    result = save_checkpoint(model, destination, {'kind': 'direct_weight_edit_no_optimizer',
                'source_alias': alias, 'source_hashes': hashes, 'hypothesis': hypothesis,
                'evidence_ids': evidence_ids, 'edits': applied, 'delta_l2': delta})
    restored = load_checkpoint(destination)
    if any(not torch.equal(v, restored.state_dict()[k]) for k, v in model.state_dict().items()):
        raise ValueError('compact_candidate_reload_mismatch')
    if hashes != {n: sha(source/n) for n in hashes}:
        raise ValueError('compact_source_changed')
    save(destination/'edit-receipt.json', {'source_unchanged': True, 'candidate_activated': False,
         'manifest': manifest, 'hypothesis': hypothesis, 'evidence_ids': evidence_ids, 'delta_l2': delta})
    return result
