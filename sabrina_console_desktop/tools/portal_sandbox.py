"""Execute candidate functions only in an unprivileged, networkless Docker container."""
from __future__ import annotations
import json
import os
import shlex
import subprocess
import uuid

RUNNER = '''import json,sys,contextlib,io
p=json.load(sys.stdin)
ns={}
out=[]
try:
 with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
  exec(compile(p['code'],'<candidate>','exec'),ns)
  fn=ns[p['function']]
  for call in p['calls']:
   try:
    value=fn(*call['args'],**call.get('kwargs',{}))
    out.append({'value':value})
   except Exception as e: out.append({'exception':type(e).__name__})
 print(json.dumps({'results':out},allow_nan=True))
except BaseException as e: print(json.dumps({'error':type(e).__name__}))
'''


def run_candidate(code, function, calls):
    name = 'sabrina-eval-' + uuid.uuid4().hex
    image = os.environ.get('SABRINA_EVAL_IMAGE', 'python:3.11-slim')
    host = os.environ.get('SABRINA_EVAL_HOST', 'ego@ego.local')
    docker = ['docker', 'run', '--pull=never', '--rm', '--name', name, '--network=none',
              '--read-only', '--cap-drop=ALL', '--security-opt=no-new-privileges',
              '--pids-limit=32', '--memory=128m', '--memory-swap=128m', '--cpus=0.5',
              '--ulimit', 'cpu=3:3', '--ulimit', 'nofile=64:64', '--user=65534:65534',
              '--tmpfs', '/tmp:rw,noexec,nosuid,size=8m', '-i', image, 'python', '-I', '-c', RUNNER]
    def command(args):
        return ['ssh','-o','BatchMode=yes','-o','ConnectTimeout=8',host,shlex.join(args)] if host else args
    try:
        result = subprocess.run(command(docker), input=json.dumps({'code': code, 'function': function, 'calls': calls}),
                                capture_output=True, text=True, encoding='utf-8', timeout=15)
        if result.returncode:
            return {'status': 'sandbox_unavailable' if 'Unable to find image' in result.stderr or 'connect' in result.stderr.lower() else 'sandbox_failed', 'error': result.stderr[-1000:]}
        decoded = json.loads(result.stdout)
        return {'status': 'completed', **decoded}
    except subprocess.TimeoutExpired:
        return {'status': 'sandbox_timeout'}
    except (OSError, ValueError):
        return {'status': 'sandbox_unavailable'}
    finally:
        # Remove only this invocation's named container, including after SSH interruption.
        try:
            subprocess.run(command(['docker','rm','-f',name]), capture_output=True, timeout=12)
        except (OSError, subprocess.TimeoutExpired):
            pass


def grade_code(reply, case):
    result = run_candidate(reply, case['function'], case['calls'])
    if result['status'] != 'completed':
        return {'passed': False, 'status': result['status'], 'pending': result['status'] == 'sandbox_unavailable'}
    actual = result.get('results', [])
    expected = case['expected']
    checks = [i < len(actual) and actual[i] == target for i, target in enumerate(expected)]
    return {'passed': bool(checks) and all(checks), 'status': 'verified' if checks and all(checks) else 'failed', 'checks': checks}


def clamp_check(reply):
    from tools.portal_transport import normalize
    code, _ = normalize(reply)
    case = {'function': 'clamp_ratio', 'calls': [{'args': args} for args in
            [[.5,0,1],[-2,0,1],[5,0,1],[float('nan'),0,1],[.5,float('nan'),1],[.5,0,float('nan')],[.5,2,1]]],
            'expected': [{'value': .5},{'value':0},{'value':1}] + [{'exception':'ValueError'}] * 4}
    grade = grade_code(code, case)
    checks = grade.get('checks', [False] * 7)
    criteria = dict(zip(('normal_clamping','nan_value_rejected','nan_bounds_rejected','inverted_bounds_rejected'),
                        (all(checks[:3]), checks[3], all(checks[4:6]), checks[6])))
    return {**grade, 'criteria_results': criteria, 'failed_criteria': [k for k,v in criteria.items() if not v]}
