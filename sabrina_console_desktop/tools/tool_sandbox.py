"""Bounded offline execution for generated Python; never fall back to the host."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
OUTPUT_LIMIT = 256 * 1024


def execute_candidate(code, timeout=60, *, image='python:3.11-slim', receipt_root=None, stdin_text=None):
    if not isinstance(code, str) or not code.strip() or len(code.encode('utf-8')) > 128*1024:
        raise ValueError('Candidate must contain 1..128 KiB of Python')
    if type(timeout) is not int or not 1 <= timeout <= 120:
        raise ValueError('Sandbox timeout must be 1..120 seconds')
    if stdin_text is not None and (not isinstance(stdin_text, str) or len(stdin_text.encode('utf-8')) > 128*1024):
        raise ValueError('Sandbox input must be UTF-8 text of at most 128 KiB')
    ident = 'sandbox_' + uuid.uuid4().hex
    root = Path(receipt_root or ROOT/'artifacts/tool_sandbox_runs')/ident
    root.mkdir(parents=True, exist_ok=False)
    result = {'run_id': ident, 'code_sha256': hashlib.sha256(code.encode()).hexdigest(),
              'stdout': '', 'stderr': '', 'exit_code': -1, 'status': 'unavailable',
              'isolated': False, 'network': 'none', 'host_fallback': False,
              'behavioral_qualification': False, 'started_at': time.time(),
              'receipt_path': str(root/'receipt.json')}
    (root/'candidate.py').write_bytes(code.encode('utf-8'))
    if stdin_text is not None:
        result['stdin_sha256'] = hashlib.sha256(stdin_text.encode('utf-8')).hexdigest()
    process = None
    try:
        found = subprocess.run(['docker', 'image', 'inspect', image, '--format', '{{.Id}}'],
                               capture_output=True, text=True, timeout=10)
        digest = found.stdout.strip()
        if found.returncode or not re.fullmatch(r'sha256:[a-f0-9]{64}', digest):
            raise RuntimeError('Approved sandbox image is not installed locally; no automatic pull')
        result['image_digest'] = digest
        # Only the candidate file is mounted. No repository, credentials, or Docker socket.
        cmd = ['docker', 'run', '--rm', '--name', ident, '--pull', 'never', '--network', 'none',
               '--read-only', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
               '--memory', '256m', '--memory-swap', '256m', '--cpus', '1', '--pids-limit', '64',
               '--user', '65534:65534', '--log-driver', 'none', '--tmpfs', '/tmp:rw,size=32m,mode=1777',
               '-v', str((root/'candidate.py').resolve())+':/input/run_script.py:ro', '-w', '/tmp',
               '--entrypoint', 'python', digest, '-I', '-B', '/input/run_script.py']
        os.chmod(root/'candidate.py', 0o644)
        if stdin_text is not None:
            cmd.insert(2, '--interactive')
        with tempfile.TemporaryDirectory() as temp, tempfile.TemporaryFile() as stdin_file:
            stdout, stderr = Path(temp)/'stdout', Path(temp)/'stderr'
            if stdin_text is not None:
                stdin_file.write(stdin_text.encode('utf-8'))
                stdin_file.seek(0)
            stdin = subprocess.DEVNULL if stdin_text is None else stdin_file
            with stdout.open('wb') as out, stderr.open('wb') as err:
                process = subprocess.Popen(cmd, stdout=out, stderr=err, stdin=stdin)
                result['isolated'] = True
                deadline = time.monotonic()+timeout
                while process.poll() is None:
                    if stdout.stat().st_size+stderr.stat().st_size > OUTPUT_LIMIT:
                        result['status'] = 'output_limit'; break
                    if time.monotonic() >= deadline:
                        result['status'] = 'timeout'; break
                    time.sleep(.05)
                if process.poll() is None:
                    # Kill only this uniquely named ephemeral test container.
                    try:
                        subprocess.run(['docker', 'rm', '--force', ident], capture_output=True, timeout=10)
                    finally:
                        process.terminate()
                    try: process.wait(timeout=5)
                    except subprocess.TimeoutExpired: process.kill(); process.wait(timeout=5)
                result['exit_code'] = process.returncode
            with stdout.open('rb') as out, stderr.open('rb') as err:
                result['stdout'] = out.read(OUTPUT_LIMIT).decode('utf-8', errors='replace')
                result['stderr'] = err.read(OUTPUT_LIMIT).decode('utf-8', errors='replace')
            if stdout.stat().st_size+stderr.stat().st_size > OUTPUT_LIMIT:
                result['status'] = 'output_limit'
            elif result['status'] not in ('timeout', 'output_limit'):
                result['status'] = 'script_completed' if result['exit_code'] == 0 else 'script_failed'
        # Script exit success is not proof of sufficient tests or permission to deploy.
    except Exception as exc:
        result['stderr'] = type(exc).__name__ + ': ' + str(exc)[:1000]
        result['status'] = 'sandbox_error' if result['isolated'] else 'unavailable'
    finally:
        if process is not None and process.poll() is None:
            try:
                subprocess.run(['docker', 'rm', '--force', ident], capture_output=True, timeout=10)
            finally:
                process.kill(); process.wait(timeout=5)
        result['finished_at'] = time.time()
        (root/'receipt.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    return result
