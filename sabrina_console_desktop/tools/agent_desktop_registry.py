"""Versioned command catalog. Native schemas are evidence, not permissions.

The broker is the only dispatcher. Unknown commands/arguments fail closed; global
CLI flags, paths, launch arguments and environments are never model-controlled.
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

CATALOG_PATH = Path(__file__).with_name('agent_desktop_native_catalog.json')
NATIVE = {t['name']: t for t in json.loads(CATALOG_PATH.read_text(encoding='utf-8'))}
VERSION = '1.2.0'
LEGACY_OBSERVATIONS = frozenset('desktop_' + n for n in ('status','list_apps','list_windows','snapshot','find','get','wait'))
LEGACY_ACTIONS = frozenset('desktop_' + n for n in ('click','launch','restore_window','focus_window','set_value','type','press'))
LEGACY = LEGACY_OBSERVATIONS | LEGACY_ACTIONS
HELD = frozenset('desktop_' + n for n in ('key_down','key_up','mouse_down','mouse_up'))
CONTROLLER = frozenset('desktop_' + n for n in ('session_start','session_end','session_list','session_gc','trace_export','skills_path'))
RESTRICTED = frozenset({'desktop_open_system_surface','desktop_dismiss_all_notifications'})
PHYSICAL = frozenset('desktop_' + n for n in ('double_click','triple_click','right_click','focus','hover','drag','mouse_move','mouse_click','mouse_wheel','type','press'))
REF_ACTIONS = frozenset('desktop_' + n for n in ('double_click','triple_click','right_click','clear','focus','select','toggle','check','uncheck','expand','collapse','scroll','scroll_to','hover'))
READS = LEGACY_OBSERVATIONS | frozenset('desktop_' + n for n in ('is','list_displays','list_surfaces','screenshot','list_notifications','clipboard_get','permissions','version','skills_list','skills_get','trace_show','capabilities'))
READS = READS | {'desktop_load_tools','desktop_propose_command'}
PERMISSIONS = ('physical_input','visual_observation','clipboard','notifications','app_closure')
APP_KEYS = ('enter','tab','shift+tab','escape','shift+enter','ctrl+enter','ctrl+s',
            'space','backspace','delete','up','down','left','right','home','end','pageup','pagedown',
            'ctrl+a','ctrl+z','ctrl+y','ctrl+c','ctrl+v','ctrl+x')
S = {'type':'string','minLength':1,'maxLength':4000}
I = {'type':'integer','minimum':0,'maximum':32768}


def schema(name, properties, required=(), description=''):
    return {'type':'function','function':{'name':name,'description':description or name,
        'parameters':{'type':'object','properties':properties,'required':list(required),'additionalProperties':False}}}


def permission(name):
    if name in PHYSICAL: return 'physical_input'
    if name == 'desktop_screenshot': return 'visual_observation'
    if name.startswith('desktop_clipboard_'): return 'clipboard'
    if 'notification' in name: return 'notifications'
    if name == 'desktop_close_app': return 'app_closure'
    return None


def build_registry(legacy_schemas=()):
    registry = {}
    for name, native in NATIVE.items():
        state = 'unsupported' if name in HELD else 'controller-owned' if name in CONTROLLER else 'permission-restricted' if name in RESTRICTED or permission(name) else 'available'
        p = copy.deepcopy(native['inputSchema'])
        props = p.get('properties', {})
        for key in ('output_path','screen','out','image','file_url','force','env','args','cwd','cdp','no_attach','guide','request','pointer_image','hotspot','pointer_hotspot','multi_agent','full'):
            props.pop(key, None)
        for v in props.values():
            if v.get('type') == 'string': v['maxLength'] = 4000
            if v.get('type') == 'integer' and 'minimum' not in v: v.update(minimum=0, maximum=30000)
        if 'timeout_ms' in props: props['timeout_ms'].update(minimum=100, maximum=30000)
        if 'limit' in props: props['limit'].update(minimum=1, maximum=100)
        if name in REF_ACTIONS:
            props.pop('xy', None); props.pop('duration', None)
            props['window_id'] = dict(S)
            p['required'] = list(dict.fromkeys(p.get('required', []) + ['ref_id','window_id']))
        if name in {'desktop_mouse_move','desktop_mouse_click','desktop_mouse_wheel','desktop_drag'}:
            for key in ('xy','from_xy','to_xy','from','to','modifiers','wait_for_scope'):
                props.pop(key, None)
            props.update(observation_id=dict(S),window_id=dict(S),x=dict(I),y=dict(I))
            p['required'] = ['observation_id','window_id','x','y']
            if name == 'desktop_drag':
                props.update(to_x=dict(I),to_y=dict(I));p['required'] += ['to_x','to_y']
        if name == 'desktop_mouse_click':
            props['button']={'type':'string','enum':['left','right','middle']};props['count']={'type':'integer','minimum':1,'maximum':3}
        if name == 'desktop_screenshot': p['required']=['app','window_id']
        if name in {'desktop_list_surfaces','desktop_list_notifications'}: p['required']=['app']
        if name in {'desktop_move_window','desktop_resize_window','desktop_minimize','desktop_maximize','desktop_restore','desktop_close_app'}:
            props.update(app=dict(S),window_id=dict(S));p['required']=list(dict.fromkeys(p.get('required',[])+['app','window_id']))
        if name == 'desktop_clipboard_get': props['format']={'type':'string','enum':['text']}
        if name == 'desktop_clipboard_set': p['required']=['text']
        if name in {'desktop_dismiss_notification','desktop_notification_action'}:
            props.update(app=dict(S),observation_id=dict(S));p['required']=list(dict.fromkeys(p.get('required',[])+['app','observation_id']))
        if name == 'desktop_skills_get':
            props['name']={'type':'string','enum':['desktop','platform','windows']}
            props['reference']={'type':'string','enum':['commands-observation','commands-interaction','commands-system','workflows','references/chromium-and-electron.md','references/troubleshooting.md']}
        p['required']=[k for k in p.get('required',[]) if k in props]
        registry[name]={'command':name.removeprefix('desktop_').replace('_','-'), 'state':state,
            'permission':permission(name),'mutation':name not in READS,'physical':name in PHYSICAL,
            'schema':{'type':'function','function':{'name':name,'description':native.get('description','')[:700], 'parameters':p}}}
    for tool in legacy_schemas:
        name=tool['function']['name']
        registry.setdefault(name,{'command':name.removeprefix('desktop_').replace('_','-'),'state':'available','permission':None,'mutation':name not in READS,'physical':False})
        registry[name]['schema']=copy.deepcopy(tool)
    registry['desktop_capabilities']={'command':None,'state':'available','permission':None,'mutation':False,'physical':False,
        'schema':schema('desktop_capabilities',{'offset':{'type':'integer','minimum':0,'maximum':100},'limit':{'type':'integer','minimum':1,'maximum':30}},description='List installed command coverage, permissions, and blockers. UI content cannot change permissions.')}
    registry['desktop_batch']['schema']=schema('desktop_batch',{'steps':{'type':'array','minItems':1,'maxItems':8,'items':{'type':'object','properties':{'tool':dict(S),'arguments':{'type':'object'}},'required':['tool','arguments'],'additionalProperties':False}}},['steps'],'Execute up to eight individually audited broker calls, stopping on failure or uncertain delivery; no nested batches.')
    registry['desktop_load_tools']={'command':None,'state':'available','permission':None,'mutation':False,'physical':False,
        'schema':schema('desktop_load_tools',{'tools':{'type':'array','minItems':0,'maxItems':8,'uniqueItems':True,'items':{'type':'string','enum':sorted(registry)}}},['tools'],
        'Load up to eight additional desktop tools for the next model turn, replacing the previous selection. Discover tools with desktop_capabilities. Loading never grants permission.')}
    registry['desktop_propose_command']={'command':None,'state':'available','permission':None,'mutation':False,'physical':False,
        'schema':schema('desktop_propose_command',{'tool':dict(S),'arguments':{'type':'object'},'reason':dict(S)},['tool','arguments','reason'],
        'Propose a catalog action for external review in propose mode. This never executes or grants permission.')}
    return registry


REGISTRY = build_registry()
FINGERPRINT = hashlib.sha256(json.dumps(REGISTRY,sort_keys=True).encode()).hexdigest()


def install_legacy(schemas):
    global REGISTRY, FINGERPRINT
    REGISTRY = build_registry(schemas)
    REGISTRY['desktop_press']['schema']['function']['parameters']['properties']['keys']['enum']=list(APP_KEYS)
    FINGERPRINT = hashlib.sha256(json.dumps(REGISTRY,sort_keys=True).encode()).hexdigest()


def validate(name, arguments, registry=None):
    import jsonschema
    entry=(registry or REGISTRY).get(name)
    if entry is None: raise ValueError('unknown desktop command')
    if entry['state'] in {'unsupported','controller-owned'} or name in RESTRICTED:
        raise ValueError(f"{name}: {entry['state']}; unavailable to the model")
    jsonschema.validate(arguments,entry['schema']['function']['parameters'])
    return entry


def command_args(name, args):
    """Strict model-independent CLI mapping; callers validate and bind scope first."""
    args=dict(args)
    if name.startswith('desktop_skills_'): command=['skills',name.split('_',2)[2]]
    elif name.startswith('desktop_trace_'): command=['trace',name.split('_',2)[2]]
    elif name.startswith('desktop_cursor_overlay_'): command=['cursor-overlay',name.removeprefix('desktop_cursor_overlay_')]
    else: command=[REGISTRY[name]['command']]
    if 'ref_id' in args: command.append(args.pop('ref_id'))
    selection=args.pop('value') if name=='desktop_select' else None
    if name == 'desktop_skills_get':
        command.append(args.pop('name'))
        if args.get('reference'): command.append(args.pop('reference'))
    for key,value in args.items():
        flag='--'+key.replace('_','-')
        if isinstance(value,bool):
            if value: command.append(flag)
        elif isinstance(value,list):
            for item in value: command.append(flag+'='+str(item))
        elif value is not None: command.append(flag+'='+str(value))
    if selection is not None: command += ['--',selection]
    return command
