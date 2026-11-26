"""Validate the complete export before ingestion; reject unexpected vocabulary."""
import argparse
import json
from pathlib import Path
import re


def validate(directory):
    root = Path(directory)
    summary = json.loads((root / 'summary.json').read_text())
    keys = {'schema_version', 'language', 'symbols', 'nodes', 'roots', 'children', 'ref',
            'scalar_type', 'id', 'parent', 'depth', 'kind',
            'base_count', 'async_function', 'parameters', 'positional_default_count',
            'keyword_default_count', 'return_annotation_present'}
    words = {'python', 'go', 'javascript', 'yaml', 'class', 'function', 'mapping',
             'sequence', 'scalar', 'str', 'int', 'float', 'bool', 'null',
             'timestamp', 'binary', 'other', 'positional_only', 'positional_or_keyword',
             'variadic_positional', 'keyword_only', 'variadic_keyword'}

    def check(value):
        if isinstance(value, dict):
            if not set(value) <= keys:
                raise ValueError('Unexpected field')
            for v in value.values():
                check(v)
        elif isinstance(value, list):
            for v in value:
                check(v)
        elif isinstance(value, str):
            if value not in words and not re.fullmatch(r'(symbol_|parameter_|node_)[1-9][0-9]*|[0-9a-f]{32}', value):
                raise ValueError('Unexpected string')
        elif value is not None and not isinstance(value, (bool, int)):
            raise ValueError('Unexpected value')

    seen = set()
    with (root / 'structures.jsonl').open() as stream:
        for line in stream:
            record = json.loads(line)
            check(record)
            if record.get('schema_version') != 1 or record.get('language') not in {'python','go','javascript','yaml'} or not {'id','schema_version','language'} <= set(record):
                raise ValueError('Invalid record')
            if record['id'] in seen:
                raise ValueError('Duplicate record')
            seen.add(record['id'])
    if len(seen) != summary.get('exported', 0):
        raise ValueError('Incomplete export')
    return {'validated_records': len(seen), 'unexpected_strings': 0}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory')
    print(json.dumps(validate(parser.parse_args().directory)))
