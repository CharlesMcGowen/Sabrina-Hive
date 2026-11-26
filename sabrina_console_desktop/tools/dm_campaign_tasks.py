"""Versioned executable software-interoperability tasks, independent of the DM.

The DM selects family/variant; this module fixes requirements and hidden oracles.
These are Sabrina research tasks, not the published ExploitGym benchmark.
"""
import csv
import io
import random

FAMILIES = ['metrics', 'pagination', 'retries', 'csv', 'dependencies', 'redaction']
VERSION = 1


def make_task(family, variant, seed):
    if family not in FAMILIES or type(variant) is not int or not 0 <= variant <= 9999:
        raise ValueError('Unknown family or variant')
    rng = random.Random(seed)
    tests = []
    def add(args, expected):
        tests.append({'args': args, 'expected': expected})
    if family == 'metrics':
        metric = ['requests_total', 'scrapes_total', 'queue_depth', 'jobs_total'][variant % 4]
        spec = (f'Implement solve(response). response is a dict with status (int), content_type (str), body (str). '
                f'Return True only if status is 200, the media type before a semicolon is text/plain (strip whitespace and ignore case), '
                f'and body has both a line starting exactly "# HELP {metric} " and a line starting exactly "# TYPE {metric} ". '
                f'Otherwise return False. Empty or missing fields are invalid. Do not make network requests.')
        body = f'# HELP {metric} Count\n# TYPE {metric} counter\n{metric} 7\n'
        add([{}], False)
        for status in [200, 201, 403, 500]:
            for ctype in ['text/plain; version=0.0.4', ' TEXT/PLAIN ; charset=utf-8', 'text/html']:
                add([{'status': status, 'content_type': ctype, 'body': body}], status == 200 and ctype != 'text/html')
        for text in ['', '<html>HELP TYPE</html>', body.replace('# HELP ', '# HELPER '), body.replace('# TYPE ', '# TYPEX '), body.replace(metric, metric+'extra'), body.replace('# HELP ', ' # HELP ')]:
            add([{'status': 200, 'content_type': 'text/plain', 'body': text}], False)
        add([{'status': 200, 'content_type': 'text/plain', 'body': body.replace('\n','\r\n')}], True)
    elif family == 'pagination':
        key = ['items', 'results', 'records', 'entries'][variant % 4]
        spec = (f'Implement solve(pages). Each page is a dict whose optional "{key}" field is a list of records. '
                'Each record has an integer id and a value. Merge all pages into a list of complete records, deduplicated by id. '
                'A later record replaces an earlier record with the same id, but output order stays the order each id first appeared. '
                'A missing page field is an empty page. Do not sort by id and do not mutate the inputs.')
        add([[]], [])
        add([[{}, {key: []}]], [])
        add([[{key: [{'id': 3,'value':'old'}, {'id':1,'value':'b'}]}, {key:[{'id':3,'value':'new'}]}]], [{'id':3,'value':'new'},{'id':1,'value':'b'}])
        for _ in range(20):
            pages=[]; order=[]; latest={}
            for __ in range(rng.randrange(1, 7)):
                records=[]
                for ___ in range(rng.randrange(0, 8)):
                    ident=rng.randrange(-3, 8); row={'id':ident,'value':rng.randrange(1000)}
                    records.append(row)
                    if ident not in latest: order.append(ident)
                    latest[ident]=dict(row)
                pages.append({key:records})
            add([pages], [latest[x] for x in order])
    elif family == 'retries':
        spec = ('Implement solve(responses, base, cap). Return the delay before each retry while scanning response dicts in order. '
                'Stop at the first 2xx status or any status other than 429 or 500..599. For retryable responses, '
                'a retry_after string consisting only of ASCII digits supplies the delay; other values use base * 2**n, '
                'where n is the number of retries already emitted, including retries with an explicit delay. '
                'Clamp every delay to cap. Inputs base and cap are nonnegative integers. Do not sleep or perform requests.')
        add([[], 1, 10], [])
        add([[{'status':200},{'status':500}],1,10], [])
        add([[{'status':500},{'status':429,'retry_after':'3'},{'status':503},{'status':200}],2,10], [2,3,8])
        add([[{'status':429,'retry_after':'-2'},{'status':429,'retry_after':'1.5'},{'status':403}],1,10], [1,2])
        add([[{'status':429,'retry_after':'\u0662'},{'status':429,'retry_after':' 2 '}],3,10], [3,6])
        for _ in range(20):
            base=rng.randrange(0,5); cap=rng.randrange(0,20); responses=[]; expected=[]
            for n in range(rng.randrange(1,8)):
                header=rng.choice([None,'bad','',str(rng.randrange(30))]); status=rng.choice([429,500,502,599])
                responses.append({'status':status,'retry_after':header})
                expected.append(min(cap,int(header) if header is not None and header.isascii() and header.isdigit() else base*2**n))
            responses += [{'status':rng.choice([200,204,400,404])},{'status':500}]
            add([responses,base,cap],expected)
    elif family == 'csv':
        delim = ',' if variant % 2 == 0 else ';'
        spec = (f'Implement solve(text). Read CSV with delimiter {delim!r} and headers sku,qty. Return a dict mapping stripped, nonempty sku values '
                'to the sum of integer quantities. Skip a row if sku is empty or qty cannot be converted by int() after stripping. '
                'Negative quantities are allowed. Respect CSV quoting, including delimiters inside a quoted sku. Empty input returns {}.')
        add([''], {})
        add([f'sku{delim}qty\nA{delim}2\nA{delim}-1\n {delim}3\nB{delim}bad\n'], {'A':1})
        for _ in range(22):
            output=io.StringIO(); writer=csv.writer(output,delimiter=delim); writer.writerow(['sku','qty']); expected={}
            for __ in range(rng.randrange(1,14)):
                sku=rng.choice(['A','B','part'+delim+'extra','x"y',''])
                valid=rng.choice([True,True,False]); qty=rng.randrange(-10,20)
                writer.writerow([' '+sku+' ',str(qty) if valid else 'N/A'])
                if sku and valid: expected[sku]=expected.get(sku,0)+qty
            add([output.getvalue()],expected)
    elif family == 'dependencies':
        spec = ('Implement solve(dependencies). Input maps every task name to a list of prerequisite names, all present as keys. '
                'Return parallel execution batches: first all tasks with no prerequisites, then all tasks whose prerequisites '
                'were completed in EARLIER batches, and so on. Sort each batch alphabetically. Duplicate prerequisites count once. '
                'Return None if any cycle exists, even in a disconnected component. An empty mapping returns []. Do not mutate input.')
        add([{}],[])
        add([{'c':['a','a','b'],'b':[],'a':[]}],[['a','b'],['c']])
        add([{'a':['b'],'b':['a'],'c':[]}],None)
        add([{'a':['a']}],None)
        for i in range(20):
            size=rng.randrange(3,10); names=[f't{x}' for x in range(size)]; deps={}; levels={}
            for j,name in enumerate(names):
                prereq=rng.sample(names[:j],rng.randrange(min(j,3)+1))
                deps[name]=prereq; levels[name]=max([levels[x]+1 for x in prereq],default=0)
            batches=[sorted(k for k,v in levels.items() if v==level) for level in range(max(levels.values())+1)]
            if i%5==0:
                deps[names[0]]=[names[0]]; batches=None
            add([deps],batches)
    else:
        mask=['[REDACTED]','***','<hidden>','MASKED'][variant%4]
        spec = (f'Implement solve(value). Recursively copy a JSON-compatible value, replacing the entire value under a dict key '
                f'whose case-insensitive name is exactly password, token, or authorization with {mask!r}. '
                'Preserve other dict keys, lists and scalar values. Substring matches such as token_count must not be redacted. '
                'Do not mutate input. Strings mentioning password are ordinary values and stay unchanged.')
        add([None],None)
        add([{'Token':{'nested':'secret'},'token_count':2,'note':'password'}],{'Token':mask,'token_count':2,'note':'password'})
        def tree(depth):
            if depth==0:
                value=rng.choice([None,True,17,'password',False]);return value,value
            if rng.randrange(3)==0:
                pairs=[tree(depth-1) for _ in range(rng.randrange(4))]
                return [p[0] for p in pairs],[p[1] for p in pairs]
            original={};expected={}
            for key in rng.sample(['password','TOKEN','Authorization','safe','token_count','note'],rng.randrange(1,7)):
                raw,clean=tree(depth-1);original[key]=raw;expected[key]=mask if key.lower() in ['password','token','authorization'] else clean
            return original,expected
        for _ in range(22):
            raw,clean=tree(3);add([raw],clean)
    return {'family':family,'variant':variant,'seed':seed,'spec':spec,'tests':tests,'version':VERSION}


REFERENCES = {
    'metrics': '''def solve(response):
    c = str(response.get('content_type', '')).split(';')[0].strip().lower()
    lines = str(response.get('body', '')).splitlines()
    metric = METRIC
    return response.get('status') == 200 and c == 'text/plain' and any(x.startswith('# HELP '+metric+' ') for x in lines) and any(x.startswith('# TYPE '+metric+' ') for x in lines)
''',
    'pagination': '''def solve(pages):
    out = {}
    for page in pages:
        for row in page.get(KEY, []): out[row['id']] = dict(row)
    return list(out.values())
''',
    'retries': '''def solve(responses, base, cap):
    out = []
    for row in responses:
        status = row['status']
        if status != 429 and not 500 <= status <= 599: break
        value = row.get('retry_after')
        delay = int(value) if isinstance(value, str) and value and all('0' <= x <= '9' for x in value) else base * 2**len(out)
        out.append(min(delay, cap))
    return out
''',
    'csv': '''import csv, io
def solve(text):
    result = {}
    for row in csv.DictReader(io.StringIO(text), delimiter=DELIM):
        key = row['sku'].strip()
        try: qty = int(row['qty'].strip())
        except (ValueError, AttributeError): continue
        if key: result[key] = result.get(key, 0) + qty
    return result
''',
    'dependencies': '''def solve(dependencies):
    remaining = {k:set(v) for k,v in dependencies.items()}
    done = set(); result = []
    while remaining:
        batch = sorted(k for k,v in remaining.items() if v <= done)
        if not batch: return None
        result.append(batch); done.update(batch)
        for k in batch: del remaining[k]
    return result
''',
    'redaction': '''def solve(value):
    if isinstance(value, dict):
        return {k: MASK if k.lower() in ['password','token','authorization'] else solve(v) for k,v in value.items()}
    if isinstance(value, list): return [solve(v) for v in value]
    return value
''',
}


def reference(task):
    code=REFERENCES[task['family']];v=task['variant']
    replacements={'METRIC':repr(['requests_total','scrapes_total','queue_depth','jobs_total'][v%4]),
                  'KEY':repr(['items','results','records','entries'][v%4]),
                  'DELIM':repr(',' if v%2==0 else ';'),
                  'MASK':repr(['[REDACTED]','***','<hidden>','MASKED'][v%4])}
    for key,value in replacements.items(): code=code.replace(key,value)
    return code
