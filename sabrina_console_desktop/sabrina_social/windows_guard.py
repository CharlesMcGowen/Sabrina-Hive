"""One bounded Task Scheduler guard for the operator's existing Windows session."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from .availability_support import (IncidentLedger, atomic_json, backup_database,
                                   instance_lock, read_json)
from .availability import get_json, reconcile_local_leases
from .recovery import RecoveryJournal
from tools.worker_identity import executor_identity, process_start, stopped_evidence

ROOT = Path(__file__).resolve().parents[1]


def desktop_notice(component, phase, reason):
    if os.name != 'nt':
        return
    message = 'Sabrina social workflow recovered.' if phase == 'recovered' else 'Sabrina needs attention. Open the Operator Console for the blocking dependency.'
    script = ("Add-Type -AssemblyName System.Windows.Forms; $n=New-Object System.Windows.Forms.NotifyIcon; "
              "$n.Icon=[System.Drawing.SystemIcons]::Information; $n.Visible=$true; "
              f"$n.ShowBalloonTip(5000,'Sabrina availability','{message}',[System.Windows.Forms.ToolTipIcon]::Info); "
              "Start-Sleep -Seconds 6; $n.Dispose()")
    subprocess.Popen(['powershell.exe', '-NoProfile', '-Command', script],
                     creationflags=subprocess.CREATE_NO_WINDOW, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def terminate_owned(identity):
    """An owned PID plus its creation time must still match at the time of termination."""
    if stopped_evidence(identity).get('stopped'):
        return
    if not identity or identity.get('host') != executor_identity()['host'] or process_start(identity.get('pid', 0)) != identity.get('process_start'):
        raise RuntimeError('Watcher process identity changed')
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(1, False, identity['pid'])
    if not handle:
        raise OSError('Cannot stop owned stalled watcher')
    try:
        if not kernel.TerminateProcess(handle, 1):
            raise OSError('Watcher termination failed')
    finally:
        kernel.CloseHandle(handle)


def ensure_watcher(root=ROOT, *, repair=True, probe=get_json, launch=subprocess.Popen, terminate=terminate_owned):
    root = Path(root)
    folder = root / 'data/control'
    folder.mkdir(parents=True, exist_ok=True)
    journal = RecoveryJournal(folder / 'guard-recovery.sqlite3', infrastructure=True)
    record_path = folder / 'managed-bridge-watcher.json'
    try:
        health = probe('http://127.0.0.1:19225/health')
    except Exception:
        health = {}
    if health and 0 <= time.time() - health.get('updated', 0) < 120:
        identity = health.get('executor')
        if identity and identity.get('host') == executor_identity()['host'] and process_start(identity.get('pid', 0)) == identity.get('process_start'):
            atomic_json(record_path, identity)
        journal.resolve('watcher-process')
        return health
    config_ok = (root / 'data/control/bridge.token').is_file() and (Path(os.environ.get('LOCALAPPDATA', '')) / 'SabrinaDebugProfile').is_dir()
    if not config_ok:
        return {'status': 'operator_required', 'reason': 'Existing browser profile or bridge token is absent'}
    previous = read_json(record_path)
    if health and not previous:
        return {'status': 'operator_required', 'reason': 'Unmanaged stalled watcher requires inspection'}
    if not repair:
        return {'status': 'would_restore_watcher'}
    receipt = journal.begin('watcher-process', 'restore', 'watcher_missing_or_stalled')
    if receipt['status'] != 'approved':
        return receipt
    if previous and not stopped_evidence(previous).get('stopped'):
        terminate(previous)
        # Never overlap an old repair process with its replacement.
        end = time.monotonic() + 8
        while not stopped_evidence(previous).get('stopped') and time.monotonic() < end:
            time.sleep(0.2)
        if not stopped_evidence(previous).get('stopped'):
            raise RuntimeError('Previous watcher shutdown is not verified')
    if os.name == 'nt' and launch is subprocess.Popen:
        # A separate long-lived task avoids inheriting the short guard task's
        # execution limit/job lifetime. Its IgnoreNew policy is a second singleton.
        subprocess.run(['powershell.exe', '-NoProfile', '-Command',
                        "Start-ScheduledTask -TaskName 'Sabrina Bridge Watcher'"],
                       capture_output=True, text=True, timeout=8, check=True,
                       creationflags=subprocess.CREATE_NO_WINDOW)
        value = {'status': 'watcher_start_requested', 'updated': time.time()}
        journal.finish('watcher-process', receipt, value)
        return value
    log_dir = root / 'logs/availability'
    log_dir.mkdir(parents=True, exist_ok=True)
    with (log_dir / 'bridge-watcher.log').open('ab') as log:
        child = launch([sys.executable, '-m', 'sabrina_social.bridge_watcher', '--host', '127.0.0.1',
                        '--port', '19225', '--token-file', str(root / 'data/control/bridge.token')],
                       cwd=str(root), stdout=log, stderr=log,
                       creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    identity = {**executor_identity('bridge-watcher'), 'pid': child.pid, 'process_start': process_start(child.pid)}
    atomic_json(record_path, identity)
    value = {'status': 'watcher_started', 'updated': time.time()}
    journal.finish('watcher-process', receipt, value)
    return value


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--observe', action='store_true')
    args = parser.parse_args()
    folder = ROOT / 'data/control'
    with instance_lock(folder / 'windows-availability-guard.lock'):
        from tools.control_client import configured
        try:
            client = configured()
            if client:
                reconcile_local_leases(client, client.request('/v1/status').get('leases', []), not args.observe)
        except Exception:
            pass  # Host unreachable is not permission to discard its reservations.
        try:
            health = ensure_watcher(repair=not args.observe)
        except Exception as exc:
            health = {'status': 'guard_error', 'error_type': type(exc).__name__}
        availability = health.get('availability') or {}
        ready = health.get('status') == 'bridge_ready' and availability.get('ready') is True
        reason = ', '.join(availability.get('blocking_dependencies', [])) or health.get('reason') or health.get('status', 'unavailable')
        ledger = IncidentLedger(folder / 'guard-availability.sqlite3')
        ledger.observe('social_workflow', ready, reason, notify=None if args.observe else desktop_notice)
        for name in ['bridge-recovery.sqlite3', 'guard-recovery.sqlite3', 'guard-availability.sqlite3']:
            backup_database(folder / name)
        atomic_json(folder / 'windows-availability-status.json', {
            'status': 'ready' if ready else 'degraded', 'updated': time.time(),
            'watcher': health, **ledger.snapshot()})
        print(json.dumps({'status': health.get('status'), 'ready': ready}))


if __name__ == '__main__':
    main()
