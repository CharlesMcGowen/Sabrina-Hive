"""OS evidence for stopped workers; a missing heartbeat is never death evidence."""
from __future__ import annotations

from contextlib import contextmanager
import contextvars
import ctypes
from functools import lru_cache
import os
from pathlib import Path
import socket
import uuid

OPERATION_ID = contextvars.ContextVar("availability_operation_id", default=None)
_INSTANCE = uuid.uuid4().hex


class DependencyUnavailable(TimeoutError):
    """A dependency failed before the caller's operation began."""


@lru_cache(maxsize=1)
def boot_identity():
    if os.name != "nt":
        try:
            return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        except OSError:
            return None
    try:
        # SystemTimeOfDayInformation exposes a stable Windows boot FILETIME.
        buffer = ctypes.create_string_buffer(48)
        status = ctypes.windll.ntdll.NtQuerySystemInformation(3, buffer, len(buffer), None)
        return str(ctypes.c_longlong.from_buffer(buffer).value) if status == 0 else None
    except (AttributeError, OSError):
        return None


def process_start(pid):
    if os.name != "nt":
        try:
            # comm can contain spaces and parentheses; fields after its final ')' start at #3.
            return Path(f"/proc/{int(pid)}/stat").read_text().rsplit(")", 1)[1].split()[19]
        except FileNotFoundError:
            return "absent"
        except (OSError, ValueError, IndexError):
            return None
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.c_void_p] * 4
    kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x1000, False, int(pid))
    if not handle:
        return "absent" if ctypes.get_last_error() == 87 else None
    try:
        exit_code = wintypes.DWORD()
        if kernel.GetExitCodeProcess(handle, ctypes.byref(exit_code)) and exit_code.value != 259:
            return 'absent'
        times = [ctypes.c_ulonglong() for _ in range(4)]
        if kernel.GetProcessTimes(handle, *[ctypes.byref(t) for t in times]):
            return str(times[0].value)
        return None
    finally:
        kernel.CloseHandle(handle)


def executor_identity(operation_id=None):
    return {"host": socket.gethostname().lower(), "boot_id": boot_identity(),
            "pid": os.getpid(), "process_start": process_start(os.getpid()),
            "instance": _INSTANCE, "operation_id": operation_id or OPERATION_ID.get()}


def stopped_evidence(identity):
    """Only inspect this host. Remote, legacy, and inaccessible owners remain unknown."""
    evidence = {"stopped": False, "reason": "identity_unavailable"}
    if not isinstance(identity, dict) or not all(identity.get(k) for k in
                                                ("host", "boot_id", "pid", "process_start")):
        return evidence
    if identity["host"] != socket.gethostname().lower():
        return {**evidence, "reason": "owner_host_unreachable"}
    boot = boot_identity()
    if not boot:
        return evidence
    if boot != identity["boot_id"]:
        return {"stopped": True, "reason": "host_rebooted", "observer_boot_id": boot}
    observed = process_start(identity["pid"])
    if observed == "absent" or observed and observed != str(identity["process_start"]):
        return {"stopped": True, "reason": "process_absent_or_replaced", "observed_start": observed}
    return {"stopped": False, "reason": "worker_alive" if observed else "inspection_unavailable"}


@contextmanager
def operation_identity(operation_id):
    token = OPERATION_ID.set(operation_id)
    try:
        yield
    finally:
        OPERATION_ID.reset(token)
