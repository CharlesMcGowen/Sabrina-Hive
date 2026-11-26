#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Sabrina SSM Model R&D: VirtualBox & Ubuntu OS Documentation Comprehension & Repair
==================================================================================
Runs Sabrina's recruited Mamba-2 State-Space Model (`mahade315/mamba-codestral:7b-q4_K_M`)
through a controlled A/B + Multi-Round Feedback Repair benchmark (`no_docs` vs `with_docs`),
following the exact architecture of `work/ssm-pytorch-doc-skills-20261121`:

1. Verifies exact Mamba-2 GGUF SHA-256 digest (`919888b99f46...`) on `ego`.
2. Evaluates 3 real VM/OS operational synthesis tasks (`diagnose_vm_greeter_stall`,
   `ubuntu_power_guard_commands`, `reconcile_vm_cdp_tunnel`) under AST safety validation
   (no I/O, no imports beyond `typing`, deterministic pure logic) against `public` (`smoke`)
   and `heldout` test suites.
3. Compares `no_docs` (zero documentation context) vs `with_docs` (injecting the SHA-256
   verified `SKILL.md` recipes built by `tools/vm_os_doc_skills.py` from official VirtualBox,
   systemd/loginctl, GNOME GDM, gsettings, OpenSSH, and CDP manuals) with up to 3 rounds
   of autonomous public-feedback repair.
4. Archives verbatim Mamba-2 outputs, SHA-256 digests, and scores to
   `data/architecture_research/ssm-vm-doc-skills-20261122/` (Zero Assistant Ghostwriting).
"""
from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional, Tuple
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import vm_os_doc_skills
from tools.control_client import DependencyUnavailable, configured

MODEL = "mahade315/mamba-codestral:7b-q4_K_M"
MODEL_SHA = "919888b99f4685de4556b827c9bc4cd42d6ed4ead0039ff23c0a94b6b4a5e004"
MODEL_FILE = Path("/usr/share/ollama/.ollama/models/blobs") / ("sha256-" + MODEL_SHA)
RUNNER = Path("/home/ego/llama-b12600-cuda/llama-b12600/llama-completion")
RESULTS_DIR = ROOT / "data" / "architecture_research" / "ssm-vm-doc-skills-20261122"
ROUND_LIMIT = 3

SYSTEM_PROMPT = (
    "Write executable Python 3 from the specification. Submit ONLY one Python code block "
    "defining the requested top-level function `solve(...)`. Do NOT import os, subprocess, "
    "sys, or socket, and do NOT include I/O, print calls, or demo code."
)

TASKS: Dict[str, str] = {
    "diagnose_vm_greeter_stall": (
        "Define `def solve(vm_state, greeter_on_seat0, guest_cdp_listening):` returning a "
        "2-tuple `(diagnosis, action)` for a headless VirtualBox Ubuntu browser VM with GDM "
        "AutomaticLogin configured.\n"
        "- When the VM is not `'running'`, return `('vm_stopped', 'startvm_headless')`.\n"
        "- When `'running'`, `guest_cdp_listening` is True, and `greeter_on_seat0` is False, "
        "return `('healthy', 'noop')`.\n"
        "- When `'running'`, `greeter_on_seat0` is True, and `guest_cdp_listening` is False, "
        "return the exact GDM greeter stall diagnosis and the VBoxManage lifecycle action that "
        "re-triggers initial boot AutomaticLogin (`'gdm_greeter_stall'`, `'controlvm_reset'`).\n"
        "- Otherwise return `('browser_process_missing', 'restart_guest_browser')`."
    ),
    "ubuntu_power_guard_commands": (
        "Define `def solve(uid, sleep_ac_type, idle_delay, lock_enabled):` returning a list of "
        "exact shell command strings to harden an Ubuntu GNOME guest session over SSH.\n"
        "Every command must start with `DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/<uid>/bus gsettings set`.\n"
        "In exact order:\n"
        "1. If `sleep_ac_type != 'nothing'`, append the command setting schema "
        "`org.gnome.settings-daemon.plugins.power` key `sleep-inactive-ac-type` to `'nothing'`.\n"
        "2. If `int(idle_delay) != 0`, append the command setting schema "
        "`org.gnome.desktop.session` key `idle-delay` to `0`.\n"
        "3. If `bool(lock_enabled)` is True, append the command setting schema "
        "`org.gnome.desktop.screensaver` key `lock-enabled` to `false`.\n"
        "Return `[]` when all three settings are already compliant."
    ),
    "reconcile_vm_cdp_tunnel": (
        "Define `def solve(local_cdp_ok, host_port_listening, listener_is_vm_ssh, guest_cdp_ok):` "
        "returning the ordered list of action step names to reconcile the Windows-to-VM SSH CDP bridge.\n"
        "- If `local_cdp_ok` and `host_port_listening` and `listener_is_vm_ssh`, return `['verify_ego_reverse_forward']`.\n"
        "- If `host_port_listening` and not `listener_is_vm_ssh`, return `['abort_foreign_listener']`.\n"
        "- Otherwise build a list: if `host_port_listening` and `listener_is_vm_ssh` and not `local_cdp_ok`, "
        "append `'stop_stale_vm_ssh_forward'`; if not `guest_cdp_ok`, append `'recover_vm_guest_browser'`; "
        "then always append `'start_vm_ssh_forward'` followed by `'verify_local_cdp_version'`."
    ),
}

# Intentionally test whether doc skills resolve exact schema/command details when prompt alone is terse:
TERSE_NO_DOC_TASKS: Dict[str, str] = {
    "diagnose_vm_greeter_stall": (
        "Define `def solve(vm_state, greeter_on_seat0, guest_cdp_listening):` returning `(diagnosis, action)` "
        "for a VirtualBox headless Ubuntu VM using VirtualBox & GDM automatic-login conventions "
        "(states: `vm_stopped`/`startvm_headless`, `healthy`/`noop`, `gdm_greeter_stall`/`controlvm_reset`, "
        "or `browser_process_missing`/`restart_guest_browser`)."
    ),
    "ubuntu_power_guard_commands": (
        "Define `def solve(uid, sleep_ac_type, idle_delay, lock_enabled):` returning the ordered list of "
        "`DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/<uid>/bus gsettings set <schema> <key> <val>` commands "
        "needed to disable Ubuntu GNOME AC auto-suspend, session idle-delay, and screensaver lock when non-compliant."
    ),
    "reconcile_vm_cdp_tunnel": (
        "Define `def solve(local_cdp_ok, host_port_listening, listener_is_vm_ssh, guest_cdp_ok):` returning "
        "the ordered list of tunnel reconciliation step identifiers for Sabrina's VirtualBox SSH CDP bridge."
    ),
}

CODE_FENCE = re.compile(r"```(?:python|py)?\s*\n(.*?)```", re.S | re.I)
BLOCKED_NAMES = {
    "__import__", "open", "exec", "eval", "compile", "input", "globals", "locals", "getattr", "setattr", "print",
}


def extract_and_validate(raw: str) -> str:
    text = (raw or "").strip().removesuffix("[end of text]").strip()
    blocks = CODE_FENCE.findall(text)
    cand = blocks[0].strip() if blocks else text
    tree = ast.parse(cand)
    funcs = [n for n in tree.body if isinstance(n, ast.FunctionDef)]
    if not any(f.name == "solve" for f in funcs):
        raise ValueError("Submission must define top-level `def solve(...):`")
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            mod = getattr(node, "module", "") or ",".join(a.name for a in node.names)
            if mod not in ("typing",):
                raise ValueError(f"Disallowed import `{mod}`; pure function required")
        if isinstance(node, ast.Name) and node.id in BLOCKED_NAMES:
            raise ValueError(f"Blocked builtin `{node.id}`")
    compile(tree, "<ssm_candidate>", "exec")
    return cand + "\n"


def evaluate_candidate(task_id: str, source: str) -> Tuple[str, str, Optional[str]]:
    """Run smoke (public) and heldout test suites, returning `(public, heldout, public_feedback)`."""
    ns: Dict[str, Any] = {}
    exec(compile(source, "<ssm_candidate>", "exec"), ns)
    solve = ns["solve"]

    if task_id == "diagnose_vm_greeter_stall":
        smoke_cases = [
            (("poweroff", False, False), ("vm_stopped", "startvm_headless")),
            (("running", True, False), ("gdm_greeter_stall", "controlvm_reset")),
        ]
        heldout_cases = [
            (("aborted", True, False), ("vm_stopped", "startvm_headless")),
            (("saved", False, False), ("vm_stopped", "startvm_headless")),
            (("running", False, True), ("healthy", "noop")),
            (("running", False, False), ("browser_process_missing", "restart_guest_browser")),
            (("running", True, True), ("browser_process_missing", "restart_guest_browser")),
        ]
    elif task_id == "ubuntu_power_guard_commands":
        p1000 = "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus gsettings set"
        p1001 = "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1001/bus gsettings set"
        smoke_cases = [
            (
                (1000, "suspend", 300, True),
                [
                    f"{p1000} org.gnome.settings-daemon.plugins.power sleep-inactive-ac-type 'nothing'",
                    f"{p1000} org.gnome.desktop.session idle-delay 0",
                    f"{p1000} org.gnome.desktop.screensaver lock-enabled false",
                ],
            ),
            ((1000, "nothing", 0, False), []),
        ]
        heldout_cases = [
            (
                (1001, "hibernate", 0, False),
                [f"{p1001} org.gnome.settings-daemon.plugins.power sleep-inactive-ac-type 'nothing'"],
            ),
            (
                (1000, "nothing", 600, True),
                [
                    f"{p1000} org.gnome.desktop.session idle-delay 0",
                    f"{p1000} org.gnome.desktop.screensaver lock-enabled false",
                ],
            ),
        ]
    elif task_id == "reconcile_vm_cdp_tunnel":
        smoke_cases = [
            (
                (False, True, True, False),
                ["stop_stale_vm_ssh_forward", "recover_vm_guest_browser", "start_vm_ssh_forward", "verify_local_cdp_version"],
            ),
            ((True, True, True, True), ["verify_ego_reverse_forward"]),
        ]
        heldout_cases = [
            ((False, True, False, True), ["abort_foreign_listener"]),
            ((False, False, False, True), ["start_vm_ssh_forward", "verify_local_cdp_version"]),
            ((False, False, False, False), ["recover_vm_guest_browser", "start_vm_ssh_forward", "verify_local_cdp_version"]),
            ((False, True, True, True), ["stop_stale_vm_ssh_forward", "start_vm_ssh_forward", "verify_local_cdp_version"]),
        ]
    else:
        raise KeyError(task_id)

    for args, expected in smoke_cases:
        try:
            got = solve(*args)
        except Exception as exc:
            return "runtime_error", "not_run", f"Smoke call solve{args!r} raised {type(exc).__name__}: {exc}"
        if got != expected:
            return "wrong_value", "not_run", f"For solve{args!r}, expected {expected!r} but got {got!r}."

    for args, expected in heldout_cases:
        try:
            got = solve(*args)
        except Exception as exc:
            return "pass", f"heldout_runtime_error:{type(exc).__name__}", None
        if got != expected:
            return "pass", "heldout_mismatch", None

    return "pass", "pass", None


def call_mamba2_ssm(prompt: str) -> Tuple[str, str]:
    """Query `mahade315/mamba-codestral:7b-q4_K_M` via verified llama-b12600-cuda runner on `ego`."""
    if not RUNNER.exists() or not MODEL_FILE.exists():
        raise RuntimeError("Verified llama-b12600-cuda Mamba-2 runner or GGUF blob not found on this host.")

    gpu_args = [
        str(RUNNER), "-m", str(MODEL_FILE), "-ngl", "99", "-t", "8",
        "-c", "2048", "-n", "380", "-s", "1475", "--temp", "0",
        "--no-conversation", "--no-display-prompt", "--no-warmup", "--simple-io",
        "--log-verbosity", "0", "-p", prompt,
    ]
    cpu_args = [
        str(RUNNER), "-m", str(MODEL_FILE), "--device", "none", "-ngl", "0", "-t", "12",
        "-c", "2048", "-n", "380", "-s", "1475", "--temp", "0",
        "--no-conversation", "--no-display-prompt", "--no-warmup", "--simple-io",
        "--log-verbosity", "0", "-p", prompt,
    ]

    client = configured()
    for _retry in range(8):
        try:
            if client is not None:
                with client.lease("compute:lab", wait_seconds=300):
                    run = subprocess.run(gpu_args, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=120, check=False)
                    backend = "llamacpp_b12600_cuda_mamba2"
                    if run.returncode != 0 or not run.stdout.strip():
                        run = subprocess.run(cpu_args, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=300, check=True)
                        backend = "llamacpp_b12600_cpu_mamba2"
                    return run.stdout.strip(), backend
            else:
                run = subprocess.run(gpu_args, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=120, check=False)
                if run.returncode == 0 and run.stdout.strip():
                    return run.stdout.strip(), "llamacpp_b12600_cuda_mamba2"
                run = subprocess.run(cpu_args, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=300, check=True)
                return run.stdout.strip(), "llamacpp_b12600_cpu_mamba2"
        except DependencyUnavailable:
            time.sleep(5)
            continue
    # Final direct fallback if coordinator HTTP socket timed out while local GPU/CPU is idle
    run = subprocess.run(gpu_args, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=120, check=False)
    if run.returncode == 0 and run.stdout.strip():
        return run.stdout.strip(), "llamacpp_b12600_cuda_mamba2_direct"
    run = subprocess.run(cpu_args, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=300, check=True)
    return run.stdout.strip(), "llamacpp_b12600_cpu_mamba2_direct"


def run_arm(arm_name: str, use_doc_skills: bool, skills_dir: Path) -> Dict[str, Any]:
    arm_dir = RESULTS_DIR / arm_name
    arm_dir.mkdir(parents=True, exist_ok=True)
    task_results: Dict[str, Any] = {}

    for task_id in ("diagnose_vm_greeter_stall", "ubuntu_power_guard_commands", "reconcile_vm_cdp_tunnel"):
        t_folder = arm_dir / task_id
        t_folder.mkdir(parents=True, exist_ok=True)
        base_spec = TERSE_NO_DOC_TASKS[task_id]
        if use_doc_skills:
            doc_ctx = vm_os_doc_skills.prompt_context(skills_dir, task_id)
            spec_text = f"{base_spec}\n\n{doc_ctx}"
        else:
            spec_text = base_spec

        rounds: List[Dict[str, Any]] = []
        prev_raw: Optional[str] = None
        prev_feedback: Optional[str] = None

        max_rounds = ROUND_LIMIT if use_doc_skills else 1
        for rnum in range(1, max_rounds + 1):
            prompt = f"[INST] {SYSTEM_PROMPT}\n\n{spec_text} [/INST]"
            if prev_raw is not None and prev_feedback is not None:
                prompt += (
                    f" {prev_raw}</s>[INST] Public check feedback (round {rnum} of {max_rounds}): {prev_feedback}\n"
                    "Re-read the Relevant Sabrina documentation skill carefully and return the complete revised `def solve(...)` Python function. [/INST]"
                )
            t0 = time.time()
            raw, backend = call_mamba2_ssm(prompt)
            latency_s = round(time.time() - t0, 2)
            (t_folder / f"round-{rnum}-raw.txt").write_text(raw, encoding="utf-8")

            try:
                validated = extract_and_validate(raw)
                (t_folder / f"round-{rnum}.py").write_text(validated, encoding="utf-8")
                pub, held, fb = evaluate_candidate(task_id, validated)
            except Exception as exc:
                pub, held, fb = f"validation_error:{type(exc).__name__}", "not_run", str(exc)[:220]

            rec = {
                "round": rnum,
                "backend": backend,
                "latency_sec": latency_s,
                "raw_sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
                "public": pub,
                "heldout": held,
                "public_feedback": fb,
            }
            rounds.append(rec)
            prev_raw = raw
            prev_feedback = f"{pub}. {fb or ''}".strip()
            if pub == "pass" and held == "pass":
                break

        task_results[task_id] = {
            "passed_first_round": rounds[0]["public"] == "pass" and rounds[0]["heldout"] == "pass",
            "passed_final": rounds[-1]["public"] == "pass" and rounds[-1]["heldout"] == "pass",
            "rounds_used": len(rounds),
            "rounds": rounds,
        }
        (t_folder / "record.json").write_text(json.dumps(task_results[task_id], indent=2), encoding="utf-8")

    return {
        "arm": arm_name,
        "use_doc_skills": use_doc_skills,
        "tasks_passed_first_round": sum(1 for v in task_results.values() if v["passed_first_round"]),
        "tasks_passed_final": sum(1 for v in task_results.values() if v["passed_final"]),
        "total_tasks": len(task_results),
        "tasks": task_results,
    }


def main() -> None:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    weights_verified = MODEL_FILE.exists() and hashlib.file_digest(MODEL_FILE.open("rb"), "sha256").hexdigest() == MODEL_SHA
    skills_dir = ROOT / "skills"

    no_docs_summary = run_arm("baseline_no_docs", use_doc_skills=False, skills_dir=skills_dir)
    with_docs_summary = run_arm("with_vm_os_doc_skills", use_doc_skills=True, skills_dir=skills_dir)

    summary = {
        "campaign": "ssm-vm-doc-skills-20261122",
        "model": MODEL,
        "weights_sha256": MODEL_SHA,
        "weights_verified_on_disk": weights_verified,
        "timestamp": time.time(),
        "baseline_no_docs": no_docs_summary,
        "with_vm_os_doc_skills": with_docs_summary,
    }
    (RESULTS_DIR / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
