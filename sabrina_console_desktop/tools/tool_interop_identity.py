"""Deterministic stateful tools for the identity-handoff research pilot.

No shell, network, model-authored code, filesystem writes or real credentials are
exposed to the worker. Success is determined from executed simulator state.
"""
import copy
import hashlib
import json
import random


VARIANTS = ('canonical', 'reorder', 'rename', 'swap')


def make_cases():
    cases=[]
    titles=['Forecast','Inventory','Schedule','Readings','Manifest','Catalog','Ledger','Checklist']
    for index,title in enumerate(titles):
        rng=random.Random(731000+index)
        ids=[f'k{v}' for v in rng.sample(range(100000,999999),2)]
        for variant in VARIANTS:
            renamed=variant=='rename'
            namespace=('a','b','c','d','m','n','p','q')[index]
            tools=({'lookup':f'{namespace}_scan','fetch':f'{namespace}_read','save':f'{namespace}_store','verify':f'{namespace}_check'}
                   if renamed else {'lookup':'lookup_record','fetch':'fetch_record','save':'save_artifact','verify':'verify_artifact'})
            fields=({'id':'locator','name':'caption','cursor':'bookmark','fetch_arg':'key','body':'payload','save_arg':'text','artifact':'receipt','verify_arg':'receipt'}
                    if renamed else {'id':'record_id','name':'display_name','cursor':'next_cursor','fetch_arg':'object_id','body':'content','save_arg':'text','artifact':'artifact_id','verify_arg':'artifact_id'})
            target,decoy=(ids[::-1] if variant=='swap' else ids)
            cases.append({'id':f'b{index}_{variant}','base':index,'split':'development' if index<4 else 'heldout',
                          'variant':variant,'title':title,'target_id':target,'decoy_id':decoy,
                          'body':f'{title}: verified value {40+index}.','decoy_body':f'{title}: obsolete value {90+index}.',
                          'tools':tools,'fields':fields})
    return cases


class IdentityWorld:
    def __init__(self,case):
        self.case=copy.deepcopy(case);self.trace=[];self.artifacts={};self.fetched=[];self.verified=[]

    def descriptions(self):
        c=self.case;f=c['fields'];n=c['tools']
        return [
            {'name':n['lookup'],'arguments':{'query':'string: exact record title'},
             'returns':{f['id']:'string: ID of the requested record; use this ID with the record-reading tool',
                        f['name']:'string: display label, not an ID',f['cursor']:'string: pagination cursor, not the requested record ID'}},
            {'name':n['fetch'],'arguments':{f['fetch_arg']:'string: ID of the record to read'},
             'returns':{f['body']:'string: exact record contents'}},
            {'name':n['save'],'arguments':{f['save_arg']:'string: exact contents to save'},
             'returns':{f['artifact']:'string: identifier of the saved artifact'}},
            {'name':n['verify'],'arguments':{f['verify_arg']:'string: saved artifact identifier'},
             'returns':{'exists':'boolean','sha256':'string: digest of stored content','stored_text':'string: actual stored content'}},
        ]

    def execute(self,action):
        c=self.case;f=c['fields'];n=c['tools']
        schema=False;semantic=None;operation=None
        try:
            if not isinstance(action,dict) or set(action)!={'tool','arguments'}:raise ValueError('Use exactly tool and arguments')
            if action['tool'] not in n.values():raise ValueError('Unknown tool')
            operation=next(k for k,v in n.items() if v==action['tool'])
            key={'lookup':'query','fetch':f['fetch_arg'],'save':f['save_arg'],'verify':f['verify_arg']}[operation]
            args=action['arguments']
            if not isinstance(args,dict) or set(args)!={key} or not isinstance(args[key],str) or len(args[key])>300:
                raise ValueError('Arguments must contain the one documented string field')
            schema=True;value=args[key]
            if operation=='lookup':
                if value!=c['title']:raise ValueError('No record with this exact title')
                order=[('id',c['target_id']),('name',c['title']),('cursor',c['decoy_id'])]
                if c['variant']=='reorder':order=order[::-1]
                result={f[k]:v for k,v in order}
            elif operation=='fetch':
                if value not in [c['target_id'],c['decoy_id']]:raise ValueError('Unknown record ID')
                semantic=value==c['target_id'];self.fetched.append(value)
                result={f['body']:c['body'] if semantic else c['decoy_body']}
            elif operation=='save':
                ident=f'art{c["base"]}_{len(self.artifacts)+1}'
                self.artifacts[ident]=value;result={f['artifact']:ident}
            else:
                if value not in self.artifacts:raise ValueError('Unknown artifact identifier')
                stored=self.artifacts[value];self.verified.append(value)
                result={'exists':True,'sha256':hashlib.sha256(stored.encode()).hexdigest(),'stored_text':stored}
        except (ValueError,TypeError,KeyError) as error:
            result={'error':str(error)}
        event={'action':copy.deepcopy(action),'result':result,'schema_valid':schema,'operation':operation,'target_identity':semantic}
        self.trace.append(event)
        return event

    def success(self):
        return self.case['target_id'] in self.fetched and any(self.artifacts[x]==self.case['body'] for x in self.verified)

    def seed_lookup(self):
        return self.execute({'tool':self.case['tools']['lookup'],'arguments':{'query':self.case['title']}})

    def prompt(self,explicit=None):
        goal=(f'Read the record titled {self.case["title"]!r}, save its exact contents as an artifact, and verify the saved artifact.'
              if explicit is None else explicit)
        text=('Use the available tools to fulfill the goal. Return ONLY one JSON object with exactly two keys: '
              '"tool" (one available tool name) and "arguments" (the documented arguments object). '
              'Execute one call at a time. Use actual returned values. Do not guess IDs.\nGOAL: '+goal+
              '\nTOOLS: '+json.dumps(self.descriptions(),ensure_ascii=False)+'\nEXECUTED HISTORY:\n')
        response_spans=[]
        for event in self.trace:
            text+='CALL: '+json.dumps(event['action'],ensure_ascii=False)+'\nRESULT: '
            start=len(text);text+=json.dumps(event['result'],ensure_ascii=False);end=len(text)
            response_spans.append((start,end,event));text+='\n'
        text+='Return the next tool call now.'
        return text,response_spans


def parse_action(answer):
    value=answer.strip()
    if value.startswith('```json\n') and value.endswith('```'):value=value[8:-3].strip()
    elif value.startswith('```\n') and value.endswith('```'):value=value[4:-3].strip()
    try:
        result=json.loads(value)
        if not isinstance(result,dict):raise ValueError('Expected one JSON object')
        return result,None
    except (ValueError,TypeError) as error:return None,str(error)


def gold_actions(case):
    n=case['tools'];f=case['fields']
    return [
        {'tool':n['lookup'],'arguments':{'query':case['title']}},
        {'tool':n['fetch'],'arguments':{f['fetch_arg']:case['target_id']}},
        {'tool':n['save'],'arguments':{f['save_arg']:case['body']}},
        {'tool':n['verify'],'arguments':{f['verify_arg']:f'art{case["base"]}_1'}},
    ]
