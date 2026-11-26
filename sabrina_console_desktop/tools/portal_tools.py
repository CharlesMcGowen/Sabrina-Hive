"""Bounded execution bridge. Tool calls are data until the host broker approves them."""
from __future__ import annotations
import json
from tools.portal_policy import digest

SCHEMAS = {'lookup_record': {'record_id': int},
           'record_note': {'record_id': int, 'note': str},
           'check_operation': {'operation_id': str}}


def execute_qualified_call(policy, response, call, *, operation_id, record_scope, broker, cancelled=False):
    """broker is the trusted controller callback, never a model-selected module/function.

    The host supplies record_scope and operation_id. This bridge supports only the
    three fixture-tested research tools; it cannot expand to shell/desktop actions.
    """
    pid = response.get('portal_id')
    name = call.get('tool')
    args = call.get('arguments')
    schema = SCHEMAS.get(name)
    if cancelled:
        return {'ok':False,'status':'cancelled'}
    if not operation_id or not schema or set(call) != {'tool','arguments'} or not isinstance(args,dict) or set(args)!=set(schema):
        raise ValueError('Invalid or unsupported tool call')
    if any(type(args[k]) is not t for k,t in schema.items()):
        raise ValueError('Tool argument schema mismatch')
    if 'record_id' in args and args['record_id'] not in record_scope:
        raise PermissionError('Record outside host-authorized scope')
    evidence=policy.eligibility(pid,'tool_use',[name])
    if not evidence['eligible'] or response.get('cache_hit') or response.get('requires_controller_review'):
        raise PermissionError('Current tool qualification required')
    if name not in response.get('tool_allowlist',[]):
        raise PermissionError('Tool was not authorized for this request')
    with policy.db() as c:
        c.execute('BEGIN IMMEDIATE')
        issued=c.execute("SELECT 1 FROM portal_requests WHERE request_id=? AND portal_id=? AND purpose='tool' AND state='complete'",(response.get('request_id'),pid)).fetchone()
        if not issued:
            raise PermissionError('No completed, authorized tool proposal request')
        h=digest([pid,call,sorted(record_scope)])
        previous=c.execute('SELECT * FROM portal_tool_operations WHERE operation_id=?',(operation_id,)).fetchone()
        if previous:
            if previous['action_hash']!=h:
                raise ValueError('Operation ID reused with different action')
            return {'ok':previous['state']=='complete','status':previous['state'],'result':json.loads(previous['result_json'] or 'null')}
        c.execute('INSERT INTO portal_tool_operations VALUES (?,?,?,?,?,?,?)',
                  (operation_id,pid,response['request_id'],h,'uncertain',None,policy.clock()))
    # The existing broker must still enforce its own authorization and lifecycle rules.
    try:
        result=broker(name,args,operation_id=operation_id)
    except Exception:
        return {'ok':False,'status':'uncertain','operation_id':operation_id}
    with policy.db() as c:
        c.execute("UPDATE portal_tool_operations SET state='complete',result_json=? WHERE operation_id=?",(json.dumps(result),operation_id))
    return {'ok':True,'status':'complete','result':result}
