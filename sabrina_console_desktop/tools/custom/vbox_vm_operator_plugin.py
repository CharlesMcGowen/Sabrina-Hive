# -*- coding: utf-8 -*-
"""
Sabrina Custom Plugin: VirtualBox & Ubuntu Browser VM Operator (`vbox_vm_operator`)
Grounded in official VirtualBox 7.2 Ch.8, loginctl(1), GNOME GDM, gsettings(1), OpenSSH, & CDP docs.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import time
from typing import Any, Callable, Dict, List, Optional, Tuple
import urllib.request

TOOL_NAME = "vbox_vm_operator"
TOOL_PROMPT = (
    "Inspect, screenshot, power-harden, and recover Sabrina's headless VirtualBox Ubuntu browser VM "
    "('Sabrina Browser'). Actions: {'operation': 'inspect' | 'screenshot' | 'harden_guest_power' | 'recover'}."
)
PARALLEL_SAFE = False

DEFAULT_VM_NAME = "Sabrina Browser"
DEFAULT_VBOXMANAGE = r"C:\Program Files\Oracle\VirtualBox\VBoxManage.exe"
DEFAULT_SSH = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "System32", "OpenSSH", "ssh.exe")
DEFAULT_KEY = os.path.join(os.path.expanduser("~"), ".ssh", "id_ed25519")


def classify_vm_state(vm_state: str, greeter_on_seat0: bool, guest_cdp_listening: bool) -> Tuple[str, str]:
    """Pure doc-grounded state machine matching `virtualbox-headless-vm-ops`."""
    if vm_state != "running":
        return ("vm_stopped", "startvm_headless")
    if guest_cdp_listening and not greeter_on_seat0:
        return ("healthy", "noop")
    if greeter_on_seat0 and not guest_cdp_listening:
        return ("gdm_greeter_stall", "controlvm_reset")
    return ("browser_process_missing", "restart_guest_browser")


def build_gsettings_guard_commands(
    uid: int, sleep_ac_type: str, idle_delay: int, lock_enabled: bool
) -> List[str]:
    """Pure doc-grounded GNOME power & lock guard builder matching `ubuntu-gdm-session-and-power-ops`."""
    prefix = f"DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/{int(uid)}/bus gsettings set"
    cmds: List[str] = []
    if sleep_ac_type != "nothing":
        cmds.append(f"{prefix} org.gnome.settings-daemon.plugins.power sleep-inactive-ac-type 'nothing'")
    if int(idle_delay) != 0:
        cmds.append(f"{prefix} org.gnome.desktop.session idle-delay 0")
    if bool(lock_enabled):
        cmds.append(f"{prefix} org.gnome.desktop.screensaver lock-enabled false")
    return cmds


def _parse_machinereadable(text: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for line in (text or "").splitlines():
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip().strip('"')] = v.strip().strip('"')
    return out


def _guest_ssh(
    remote_cmd: str,
    ssh_bin: str = DEFAULT_SSH,
    key_path: str = DEFAULT_KEY,
    timeout: int = 10,
    runner: Callable[..., Any] = subprocess.run,
) -> subprocess.CompletedProcess:
    args = [
        ssh_bin, "-4", "-p", "2222", "-i", key_path,
        "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "-o", "StrictHostKeyChecking=accept-new",
        "sabrina@127.0.0.1", remote_cmd,
    ]
    try:
        return runner(args, capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(args, returncode=124, stdout="", stderr="ssh_timeout_vm_booting")


def _probe_http_cdp(url: str = "http://127.0.0.1:9222/json/version", timeout: float = 2.5) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return bool(data.get("webSocketDebuggerUrl"))
    except Exception:
        return False


def inspect_vm(
    vm_name: str = DEFAULT_VM_NAME,
    vboxmanage: str = DEFAULT_VBOXMANAGE,
    runner: Callable[..., Any] = subprocess.run,
    cdp_prober: Callable[[str], bool] = _probe_http_cdp,
) -> Dict[str, Any]:
    if not os.path.exists(vboxmanage):
        return {"ok": False, "error": f"VBoxManage not found at {vboxmanage}"}
    res = runner([vboxmanage, "showvminfo", vm_name, "--machinereadable"], capture_output=True, text=True, timeout=12, check=False)
    if res.returncode != 0:
        return {"ok": False, "error": (res.stderr or res.stdout or "showvminfo failed").strip()[:300]}

    info = _parse_machinereadable(res.stdout)
    vm_state = info.get("VMState", "unknown")
    greeter_on_seat0 = False
    guest_cdp_listening = False
    guest_ssh_ok = False
    sleep_ac = "unknown"

    if vm_state == "running":
        probe_script = (
            "loginctl list-sessions --no-legend 2>/dev/null; echo '===SS==='; "
            "ss -tln 2>/dev/null; echo '===GS==='; "
            "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus gsettings get "
            "org.gnome.settings-daemon.plugins.power sleep-inactive-ac-type 2>/dev/null || true"
        )
        gres = _guest_ssh(probe_script, runner=runner)
        if gres.returncode == 0:
            guest_ssh_ok = True
            parts = gres.stdout.split("===SS===")
            sessions_txt = parts[0] if parts else ""
            rest = parts[1] if len(parts) > 1 else ""
            ss_txt, _, gs_txt = rest.partition("===GS===")
            greeter_on_seat0 = bool(re.search(r"\bgdm(?:-greeter)?\b.*\bseat0\b", sessions_txt))
            guest_cdp_listening = ":9222" in ss_txt
            sleep_ac = gs_txt.strip().strip("'") or "unknown"

    host_cdp_ready = cdp_prober("http://127.0.0.1:9222/json/version")
    diagnosis, action = classify_vm_state(vm_state, greeter_on_seat0, guest_cdp_listening)
    return {
        "ok": True,
        "vm_name": vm_name,
        "vm_state": vm_state,
        "guest_os": info.get("ostype", ""),
        "guest_ssh_reachable": guest_ssh_ok,
        "greeter_on_seat0": greeter_on_seat0,
        "guest_cdp_listening": guest_cdp_listening,
        "host_cdp_ready": host_cdp_ready,
        "sleep_inactive_ac_type": sleep_ac,
        "diagnosis": diagnosis,
        "recommended_action": action,
    }


def execute(action: Dict[str, Any], loop: Any = None) -> str:
    payload = action if isinstance(action, dict) else {}
    op = str(payload.get("operation") or "inspect").strip().lower()
    vm_name = str(payload.get("vm_name") or DEFAULT_VM_NAME)
    vboxmanage = str(payload.get("vboxmanage") or DEFAULT_VBOXMANAGE)

    if payload.get("dry_run"):
        diag, rec = classify_vm_state(
            str(payload.get("vm_state") or "running"),
            bool(payload.get("greeter_on_seat0", True)),
            bool(payload.get("guest_cdp_listening", False)),
        )
        return json.dumps({"ok": True, "dry_run": True, "diagnosis": diag, "recommended_action": rec})

    if op == "inspect":
        return json.dumps(inspect_vm(vm_name=vm_name, vboxmanage=vboxmanage), ensure_ascii=False)

    if op == "screenshot":
        out_dir = Path(__file__).resolve().parents[2] / "artifacts" / "vm_screenshots"
        out_dir.mkdir(parents=True, exist_ok=True)
        dest = out_dir / f"sabrina_vm_{int(time.time())}.png"
        r = subprocess.run([vboxmanage, "controlvm", vm_name, "screenshotpng", str(dest)], capture_output=True, text=True, timeout=15, check=False)
        return json.dumps({"ok": r.returncode == 0 and dest.exists(), "screenshot_path": str(dest), "stderr": (r.stderr or "")[:200]})

    if op == "harden_guest_power":
        cmds = build_gsettings_guard_commands(1000, "suspend", 300, True)
        cmds.append("systemctl --user stop snap.snapd-desktop-integration.snapd-desktop-integration.service 2>/dev/null || true")
        res = _guest_ssh(" && ".join(cmds), timeout=15)
        return json.dumps({"ok": res.returncode == 0, "commands_applied": len(cmds), "stderr": (res.stderr or "")[:200]})

    if op == "recover":
        bridge_ps1 = Path(__file__).resolve().parents[2] / "launch_social_bridge.ps1"
        r = subprocess.run(
            ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(bridge_ps1), "-Action", "reconnect"],
            capture_output=True, text=True, timeout=120, check=False,
        )
        return json.dumps({"ok": r.returncode == 0, "returncode": r.returncode, "output": (r.stdout or r.stderr or "").strip()[:800]})

    return json.dumps({"ok": False, "error": f"Unsupported operation: {op}"})
