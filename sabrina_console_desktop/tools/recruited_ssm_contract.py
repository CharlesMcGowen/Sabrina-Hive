"""Data-only job and submission contracts for recruited SSM research."""
import ast
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile


REVISION = '1e76775f628fbf1350fbe4dbb3d971ba64af25a1'
MODEL = 'state-spaces/mamba-130m-hf'
WIDTH = 128
MAX_DELTA = .25
LAYERS = range(20, 24)
TENSOR_SIZE = {'A_log': 1536 * 16, 'dt_proj.bias': 1536}
KEY = re.compile(r'[a-z0-9][a-z0-9-]{0,79}\Z')
HEXDIGEST = re.compile(r'[a-f0-9]{64}\Z')

WEIGHT_BRIEF = '''Design a direct causal state-space weight intervention for the public
state-spaces/mamba-130m-hf checkpoint. No training, tools or external actions.
Return one JSON object with exactly hypothesis (a short falsifiable explanation),
direction ("increase" or "decrease", the expected mean effective carry), layer
(integer 20 to 23), parameter ("A_log" shape [1536,16] or "dt_proj.bias" shape
[1536]), offset (zero-based index of the first element of a contiguous row-major
slice of 128 FP32 weights), and source (runnable Python code). A_log sets the
negative continuous decay A=-exp(A_log); dt=softplus(input term+dt_proj.bias).
The runtime passes the slice as a list of 128 floats to your def edit(values).
Return a list of [slice_index, additive_delta] pairs, unique indices 0..127,
nonzero finite abs(delta)<=0.25. You may use math or torch. Choose an offset
within the tensor. The lab applies the deltas to a disposable checkpoint copy
and compares original and edited outputs. Do not include markdown or other keys.'''


def check_key(key):
    if not isinstance(key, str) or not KEY.fullmatch(key):
        raise ValueError('invalid run or job identifier')
    return key


def sha(data):
    return hashlib.sha256(data).hexdigest()


def file_sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def canonical(obj):
    return json.dumps(obj, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False).encode()


def save(path, obj):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.' + path.name + '-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(canonical(obj) + b'\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)


def load(path):
    return json.loads(Path(path).read_bytes())


def compact_source(reply):
    if not isinstance(reply, str) or len(reply) > 18000:
        raise ValueError('reply is empty or exceeds the submission size')
    lines = reply.strip().splitlines()
    if len(lines) >= 2 and lines[0].startswith('```') and lines[-1].strip() == '```':
        return '\n'.join(lines[1:-1]).strip()
    return reply.strip()


def json_reply(reply):
    text = compact_source(reply)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find('{'), text.rfind('}')
        if 0 <= start < end and (start > 0 or end + 1 < len(text)):
            return json.loads(text[start:end + 1])
        raise


def validate_submission(doc):
    if not isinstance(doc, dict) or set(doc) != {'hypothesis', 'direction', 'layer', 'parameter', 'offset', 'source'}:
        raise ValueError('expected six exact JSON fields: hypothesis, direction, layer, parameter, offset, source')
    hypothesis, layer = doc['hypothesis'], doc['layer']
    parameter, offset, code = doc['parameter'], doc['offset'], doc['source']
    if not isinstance(hypothesis, str) or not hypothesis.strip() or len(hypothesis) > 700:
        raise ValueError('hypothesis must be a nonempty statement of at most 700 characters')
    if doc['direction'] not in ('increase', 'decrease'):
        raise ValueError('direction must be increase or decrease')
    if type(layer) is not int or layer not in LAYERS or not isinstance(parameter, str) or parameter not in TENSOR_SIZE:
        raise ValueError('choose A_log or dt_proj.bias in layer 20 through 23')
    if type(offset) is not int or not 0 <= offset <= TENSOR_SIZE[parameter] - WIDTH:
        raise ValueError('the entire 128-element slice must fit inside the selected tensor')
    if not isinstance(code, str) or not code.strip() or len(code) > 6000:
        raise ValueError('edit source must contain at most 6000 characters')
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        raise ValueError(f'Python syntax error on line {exc.lineno}: {exc.msg}') from exc
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef)]
    modules = [node for node in tree.body if isinstance(node, ast.Import)]
    if len(functions) != 1 or len(functions) + len(modules) != len(tree.body) or functions[0].name != 'edit':
        raise ValueError('source must define one top-level edit(values) and may import math or torch')
    args = functions[0].args
    if len(args.args) != 1 or args.args[0].arg != 'values' or any((args.posonlyargs, args.vararg, args.kwarg,
                                                                  args.kwonlyargs, args.defaults, functions[0].decorator_list)):
        raise ValueError('use def edit(values) with no defaults or decorators')
    for node in ast.walk(tree):
        if isinstance(node, ast.Import) and any(n.name not in ('math', 'torch') for n in node.names):
            raise ValueError('only math or torch may be imported')
        if isinstance(node, (ast.ImportFrom, ast.Global, ast.Nonlocal, ast.ClassDef, ast.AsyncFunctionDef)):
            raise ValueError('unsupported Python statement in edit')
        if isinstance(node, (ast.Name, ast.Attribute)) and (node.id if isinstance(node, ast.Name) else node.attr).startswith('_'):
            raise ValueError('private names are unavailable to edit')
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in {'open', 'exec', 'eval', 'compile', 'input', 'globals', 'locals', 'getattr', 'setattr', 'vars'}:
            raise ValueError('file access and dynamic execution are unavailable to edit')
    return doc


def validate_deltas(pairs):
    if not isinstance(pairs, list) or not 1 <= len(pairs) <= WIDTH:
        raise ValueError('edit must return between 1 and 128 index/delta pairs')
    clean = {}
    for item in pairs:
        if not isinstance(item, list) or len(item) != 2:
            raise ValueError('each edit must be a two-item index/delta pair')
        index, value = item
        if type(index) is not int or not 0 <= index < WIDTH or index in clean:
            raise ValueError('delta indices must be distinct integers from 0 to 127')
        if type(value) not in (int, float) or not math.isfinite(value) or not 0 < abs(value) <= MAX_DELTA:
            raise ValueError('each delta must be finite, nonzero and within ±0.25')
        clean[index] = float(value)
    return sorted(clean.items())


def decision(base, candidate, control, base_loss, candidate_loss):
    if not len(base) == len(candidate) == len(control) == 8:
        raise ValueError('expected eight paired panels')
    delta = sum(b - a for a, b in zip(base, candidate)) / 8
    wins = sum(b > a for a, b in zip(base, candidate))
    above_control = sum(b - c for c, b in zip(control, candidate)) / 8
    ratio = candidate_loss / base_loss
    return {'mean_delta': delta, 'wins': wins, 'mean_delta_vs_control': above_control, 'retention_loss_ratio': ratio,
            'improved': delta >= .03 and wins >= 6 and above_control > 0 and ratio <= 1.01}
