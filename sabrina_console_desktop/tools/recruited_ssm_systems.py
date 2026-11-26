"""Offline compiler and reference checks for recruited C++20 and Go submissions."""
import json
from pathlib import Path
import subprocess
import tempfile
import uuid

from tools import recruited_ssm_contract as c


CPP = '''Write a single C++20 translation unit, without third-party dependencies.
Implement the vector selective recurrence h[j] = a[j] * h[j] + b[j]. Read
width and steps (positive integers) from stdin, then width initial state
floats, then steps frames. Each frame contains a reset flag (0 or 1), width
carry coefficients, and width additive drives. Clear the entire state just
before applying that frame when reset is 1. Print one line containing width
state floats per frame with enough precision to round-trip double. Accept
chunked runs by taking the previous final state as the next initial state.
Exit nonzero on malformed input. Use no network, tools or external actions.
Return only this JSON object: {"source":"complete C++20 code"}.'''
GOLANG = '''Write a single Go 1.22 standard-library-only source file for
a lab request CLI. It builds without network access. Usage is
program submit RUN JOB FILE or program status RUN. RUN and JOB must match
[a-z0-9][a-z0-9-]{0,79}. submit reads a local file containing exactly one JSON
object and writes one JSON envelope to stdout with fields action="accept",
run, job, and payload equal to the object. status writes action="overview"
and run. Refuse invalid identifiers, invalid JSON or wrong argument counts
with nonzero exit. The trusted lab relay sends envelopes separately.
Return only this JSON object: {"source":"complete Go source"}.'''
BRIEFS = {'cpp20': CPP, 'go': GOLANG}

DRIVER = r'''
import json
import math
import random
import subprocess
import sys
import time
from pathlib import Path

def call(args,text='',timeout=5):
    p=subprocess.run(args,input=text,text=True,capture_output=True,timeout=timeout)
    return p.returncode,p.stdout,p.stderr

def insist(test,message):
    if not test:
        raise ValueError(message)

def refs(state, frames):
    answer=[]
    for reset,a,b in frames:
        if reset:
            state=[0.0]*len(state)
        state=[w*x+y for w,x,y in zip(state,a,b)]
        answer.append(state)
    return answer

def input_for(state, frames):
    lines=[f'{len(state)} {len(frames)}',' '.join(map(str,state))]
    for reset,a,b in frames:
        lines.append(' '.join(map(str,[reset,*a,*b])))
    return '\n'.join(lines)+'\n'

def recurrence(state,frames):
    code,out,err=call(['/tmp/worker'],input_for(state,frames))
    insist(code==0,'valid recurrence input was rejected')
    rows=[[float(x) for x in line.split()] for line in out.strip().splitlines()]
    expected=refs(state,frames)
    insist(len(rows)==len(expected),'wrong count of emitted states')
    insist(all(len(x)==len(y) and all(math.isclose(a,b,rel_tol=1e-12,abs_tol=1e-12) for a,b in zip(x,y))
                 for x,y in zip(rows,expected)),'numeric parity failed')
    return rows

def cpp(source):
    Path('/tmp/code.cpp').write_text(source)
    code,_,err=call(['g++','-std=c++20','-O3','-ffp-contract=off','-Wall','-Wextra','-Werror','/tmp/code.cpp','-o','/tmp/worker'],timeout=28)
    if code:
        return {'state':'rejected','diagnostic':'compiler: '+err[:210]}
    for width in (1,4,16,31):
        seed=random.Random(340+width)
        initial=[seed.uniform(-1,1) for _ in range(width)]
        fs=[(int(i in (0,4,12)),[seed.uniform(.05,.999) for _ in range(width)],
              [seed.uniform(-.5,.5) for _ in range(width)]) for i in range(17)]
        whole=recurrence(initial,fs)
        part=recurrence(initial,fs[:7]); tail=recurrence(part[-1],fs[7:])
        insist(part+tail==whole,'chunked and continuous states differ')
    insist(call(['/tmp/worker'],'2 1\n0 0\n1 0.9\n')[0]!=0,'malformed frame was accepted')
    width=16
    frames=[(0,[.9]*width,[.01]*width) for _ in range(1500)]
    stdin=input_for([0.0]*width,frames)
    start=time.perf_counter(); refs([0.0]*width,frames); python_s=time.perf_counter()-start
    start=time.perf_counter(); code,_,_=call(['/tmp/worker'],stdin); native_s=time.perf_counter()-start
    insist(code==0,'benchmark input failed')
    return {'state':'passed','parity_widths':[1,4,16,31],'frames_benchmarked':len(frames),
            'python_kernel_seconds':python_s,'native_cli_seconds':native_s,'native_timing_includes_process_and_io':True}

def golang(source):
    Path('/tmp/main.go').write_text(source)
    code,_,err=call(['/usr/lib/go-1.22/bin/go','build','-trimpath','-o','/tmp/worker','/tmp/main.go'],timeout=95)
    if code:
        return {'state':'rejected','diagnostic':'compiler: '+err[:210]}
    p=Path('/tmp/edit.json'); payload={'submission':{'layer':23,'source':'a\nb','offset':7}}
    p.write_text(json.dumps(payload))
    code,out,_=call(['/tmp/worker','submit','example-run','example-job',str(p)])
    insist(code==0 and json.loads(out)=={'action':'accept','run':'example-run','job':'example-job','payload':payload},'submit JSON envelope differs')
    code,out,_=call(['/tmp/worker','status','example-run'])
    insist(code==0 and json.loads(out)=={'action':'overview','run':'example-run'},'status JSON envelope differs')
    for argv in (['status','../bad'],['submit','good','a/b',str(p)],['status'],['unknown','ok']):
        insist(call(['/tmp/worker',*argv])[0]!=0,'invalid CLI arguments were accepted')
    for invalid in ('null','[]','{"x":1} {"y":2}','{'):
        p.write_text(invalid)
        insist(call(['/tmp/worker','submit','good','job',str(p)])[0]!=0,'invalid or nonobject JSON was accepted')
    return {'state':'passed','checks':10,'relay_contract':'recruited-ssm-relay-v1'}

x=json.load(sys.stdin)
try:
    result=(cpp if x['language']=='cpp20' else golang)(x['source'])
except (ValueError,subprocess.SubprocessError) as exc:
    result={'state':'rejected','diagnostic':str(exc)[:240]}
print('RESULT='+json.dumps(result,allow_nan=False))
'''


def validate(language, source):
    if language not in BRIEFS or not isinstance(source, str) or not source.strip() or len(source) > 12000:
        raise ValueError('expected C++20 or Go source up to 12000 characters')


def evaluate(payload, image):
    language, source = payload['language'], payload['source']
    validate(language, source)
    name = 'sabrina-recruited-systems-' + uuid.uuid4().hex
    cmd = ['prlimit', '--fsize=67108864:67108864', '--', 'docker', 'run', '--rm', '-i', '--name', name,
           '--pull', 'never', '--network', 'none', '--read-only', '--user', '65534:65534', '--cap-drop', 'ALL',
           '--security-opt', 'no-new-privileges', '--memory', '1g', '--memory-swap', '1g', '--cpus', '2',
           '--pids-limit', '128', '--log-driver', 'none', '--tmpfs', '/tmp:rw,nosuid,nodev,exec,size=448m',
           '--env', 'HOME=/tmp']
    if language == 'go':
        cmd += ['--mount', 'type=bind,src=/usr/lib/go-1.22,dst=/usr/lib/go-1.22,readonly',
                '--mount', 'type=bind,src=/usr/share/go-1.22,dst=/usr/share/go-1.22,readonly', '--env', 'GOROOT=/usr/lib/go-1.22',
                '--env', 'GOTOOLCHAIN=local', '--env', 'GOPROXY=off', '--env', 'GOSUMDB=off', '--env', 'CGO_ENABLED=0',
                '--env', 'GOCACHE=/tmp/go-cache', '--env', 'GOMAXPROCS=2']
    cmd += ['--entrypoint', 'timeout', image, '--kill-after=4', '115s', 'python', '-I', '-B', '-c', DRIVER]
    with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as error:
        try:
            completed = subprocess.run(cmd, input=c.canonical({'language': language, 'source': source}), stdout=output,
                                       stderr=error, timeout=125, check=False)
        except subprocess.TimeoutExpired:
            subprocess.run(['docker','rm','--force',name], capture_output=True, timeout=10, check=False)
            return {'state': 'rejected', 'diagnostic': 'offline compilation or tests timed out'}
        output.seek(0); error.seek(0)
        lines = [line[7:] for line in output.read(65536).splitlines() if line.startswith(b'RESULT=')]
        if completed.returncode or len(lines) != 1:
            return {'state': 'rejected', 'diagnostic': 'offline compiler sandbox returned no result'}
    return {**json.loads(lines[0]), 'source_sha256': c.sha(source.encode()), 'language': language}
