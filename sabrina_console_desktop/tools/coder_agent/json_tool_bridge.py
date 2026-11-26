"""Optional research transport adapter; validates but never executes a tool call."""
import json
import re


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate JSON key')
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError('Non-finite JSON constant')


def bridge_json_call(answer, tools):
    """Wrap one complete declared JSON call after schema validation.

    Accept only a whole object or one whole JSON code fence. No prose extraction,
    identifier repair, added arguments, or fabricated execution results.
    Native envelopes are left to the existing native parser.
    """
    from jsonschema import Draft202012Validator, ValidationError
    if not isinstance(answer, str) or len(answer) > 65536 or not tools:
        return answer, False
    fenced = re.fullmatch(r'\s*```(?:json)?\s*\n(.*?)\n?```\s*', answer, re.S)
    payload = fenced.group(1) if fenced else answer
    try:
        value = json.loads(payload, object_pairs_hook=_unique_object,
                           parse_constant=_reject_constant)
    except (ValueError, TypeError):
        return answer, False
    if not isinstance(value, dict) or set(value) != {'name', 'arguments'}:
        return answer, False
    if not isinstance(value['name'], str) or not isinstance(value['arguments'], dict):
        return answer, False
    declared = {tool['function']['name']: tool['function'] for tool in tools}
    if len(declared) != len(tools):
        raise ValueError('Duplicate declared tool name')
    if value['name'] not in declared:
        return answer, False
    schema = declared[value['name']]['parameters']
    Draft202012Validator.check_schema(schema)
    try:
        Draft202012Validator(schema).validate(value['arguments'])
    except ValidationError:
        return answer, False
    return '<tool_call>' + json.dumps(value, ensure_ascii=False, allow_nan=False) + '</tool_call>', True
