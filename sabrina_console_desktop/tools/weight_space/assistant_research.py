"""Matched local HF protocol probes, separate from trading qualification."""
from __future__ import annotations

import gc
import json
from pathlib import Path

from tools.weight_space import core
from tools.kraken_strategy_engine import save


def fixtures():
    cases = []
    for i, (mode, target, trail) in enumerate([
            ('take', 700, 0), ('take', 1000, 0), ('time_only', 0, 0), ('time_only', 0, 0),
            ('trail', 700, 150), ('trail', 1000, 200), ('take', 600, 0), ('time_only', 0, 0)]):
        expected = {'profit_mode': mode, 'profit_target_mid_bps': target, 'trail_giveback_bps': trail}
        prompt = ('Return ONLY a JSON object with exactly profit_mode, profit_target_mid_bps, trail_giveback_bps. '
                  'Take and trail are mutually exclusive. A take order has zero giveback. Time-only has both numeric fields zero. '
                  f'Configure profit_mode={mode}, target={target} bps, giveback={trail} bps. No prose.')
        cases.append({'id': f'constraint-{i}', 'category': 'exit_constraint', 'split': 'development' if i < 4 else 'held_out',
                      'prompt': prompt, 'expected': expected})
    for i, (a, b) in enumerate(((7, 8), (11, 9))):
        cases.append({'id': f'regression-{i}', 'category': 'unrelated_arithmetic', 'split': 'regression',
                      'prompt': f'Return only JSON with key answer and integer value {a} * {b}.', 'expected': {'answer': a*b}})
    return cases


def score(rows, cases):
    scored = []
    for row, case in zip(rows, cases):
        try:
            value = json.loads(row['response'])
            passed = value == case['expected'] and not row['truncated']
        except (ValueError, TypeError): passed = False
        scored.append({'id': case['id'], 'category': case['category'], 'split': case['split'], 'passed': passed,
                       'failure_fingerprint': None if passed else case['category']+':exact_json_contract'})
    return {'cases': scored, 'passed': sum(x['passed'] for x in scored), 'total': len(scored),
            'trading_qualification': False}


def generate(model, tokenizer, torch, cases):
    rows = []
    for case in cases:
        if not tokenizer.chat_template:
            raise ValueError('saved_chat_template_required_for_matched_probe')
        prompt = tokenizer.apply_chat_template([{'role': 'user', 'content': case['prompt']}],
                                               tokenize=False, add_generation_prompt=True)
        batch = tokenizer(prompt, return_tensors='pt', add_special_tokens=False)
        if batch.input_ids.shape[1] > 1024:
            raise ValueError('protocol_probe_context_budget')
        with torch.inference_mode():
            output = model.generate(**batch, max_new_tokens=160, do_sample=False, use_cache=True,
                                    pad_token_id=tokenizer.eos_token_id)
        tokens = output[0, batch.input_ids.shape[1]:]
        rows.append({'id': case['id'], 'rendered_prompt': prompt,
                     'response': tokenizer.decode(tokens, skip_special_tokens=True),
                     'truncated': len(tokens) >= 160, 'output_tokens': len(tokens)})
    return rows


def run_recipe(action, request, out):
    # Registry contains only host-verified aliases explicitly mounted read-only.
    alias, source = core._resolve_model(request['model_alias'])
    hashes = core._model_file_hashes(source)
    if hashes != request['expected_source_sha256']:
        raise ValueError('assistant_source_changed_since_program_approval')
    torch, model, tokenizer, device = core._load_model(source, 'cpu')
    torch.set_num_threads(2); torch.manual_seed(1729)
    cases = fixtures(); save(out/'fixtures.json', cases)
    baseline = generate(model, tokenizer, torch, cases)
    baseline_score = score(baseline, cases)
    save(out/'baseline.json', baseline); save(out/'baseline-score.json', baseline_score)
    failures = [r for r in baseline_score['cases'] if r['split'] == 'development' and not r['passed']]
    norm_names = [name for name, value in model.named_parameters()
                  if name.endswith('norm.weight') and value.ndim == 1 and value.numel() <= 8192]
    # Pin a single final normalization tensor; a modest magnitude probe is a falsifiable
    # calibration hypothesis, never an assertion that a semantic task vector was found.
    tensor_name = next((n for n in norm_names if n in ('model.norm.weight', 'transformer.norm_f.weight')), None)
    result = {'status': 'completed', 'model_alias': alias, 'baseline_score': baseline_score,
              'source_hashes': hashes, 'reproduced_failures': failures, 'tensor': tensor_name,
              'protocol': {'device': device, 'dtype': str(next(model.parameters()).dtype), 'chat_template': True,
                           'greedy': True, 'seed': 1729, 'max_new_tokens': 160},
              'trading_qualification': False, 'candidate_activated': False, 'edited': False}
    if action == 'assistant_weight':
        prior = request.get('failures', [])
        if len(prior) < 2 or len(failures) < 2 or not tensor_name:
            result['reason'] = 'mechanism_or_reproduction_gate_not_met'
        elif len(request.get('hypothesis', '')) < 30:
            raise ValueError('assistant_weight_requires_mechanism_hypothesis')
        else:
            manifest = {'format_version': 1, 'edits': [{'tensor': tensor_name, 'op': 'multiply', 'value': .99}]}
            save(out/'frozen-edit.json', {'manifest': manifest, 'hypothesis': request['hypothesis'],
                                         'evidence': prior, 'source_hashes': hashes})
            applied = core._apply_edits(model, core._validate_manifest(manifest))
            destination = Path('/models/assistant-candidate')
            model.save_pretrained(destination, safe_serialization=True); tokenizer.save_pretrained(destination)
            del model, tokenizer; gc.collect()
            torch, model, tokenizer, after_device = core._load_model(destination, 'cpu')
            if after_device != device or str(next(model.parameters()).dtype) != result['protocol']['dtype']:
                raise ValueError('matched_inference_protocol_changed')
            candidate = generate(model, tokenizer, torch, cases)
            save(out/'candidate.json', candidate)
            result.update(edited=True, edits=applied, candidate_score=score(candidate, cases),
                          candidate_hashes=core._model_file_hashes(destination))
    del model, tokenizer; gc.collect()
    if core._model_file_hashes(source) != hashes:
        raise ValueError('registered_assistant_source_changed')
    result['source_unchanged'] = True
    return result
