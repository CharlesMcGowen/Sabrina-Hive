#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Sabrina VM & Guest OS Documentation-to-Skill Builder (`tools/vm_os_doc_skills.py`)
==================================================================================
Extends Sabrina's SSM PyTorch doc-skills pattern (`pytorch_doc_skills.py`) and
`SabrinaToolSkillForge.ingest_docs_and_reason` to VirtualBox (`VBoxManage`),
Ubuntu Guest OS (`loginctl`, `systemd.service`, `GDM3`, `gsettings`), OpenSSH
port forwarding, and Chrome DevTools Protocol (`CDP`).

1. Fetches official zero-auth reference manuals over HTTPS with SHA-256 receipts.
2. Screens every document through `CyberComplianceSentinel` and `inspect_untrusted_content_safety`.
3. Caches vendor docs in `docs/vendor/vm-browser-stack/` + `skill-verification.json`,
   indexes them in `InternetResourceIndexer`, and records a structured Reasoning Note
   in `HierarchicalCognitiveBlackboard` + FTS5 RAG (`knowledge_base.db`).
4. Runs executable recipe self-checks (`exec(...)`) and writes verified `SKILL.md`
   playbooks for Sabrina and her Mamba-2 SSM (`mahade315/mamba-codestral:7b-q4_K_M`).
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
from html import unescape
import json
from pathlib import Path
import re
import sys
from tempfile import NamedTemporaryFile
import time
from typing import Any, Dict, List, Optional, Tuple
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

VENDOR_DOC_DIR = ROOT / "docs" / "vendor" / "vm-browser-stack"
DEFAULT_SKILLS_DIR = ROOT / "skills"
DEFAULT_RECEIPT_PATH = VENDOR_DOC_DIR / "skill-verification.json"
PAGE_LIMIT = 1_500_000

OFFICIAL_DOC_SOURCES: Dict[str, Dict[str, Any]] = {
    "vboxmanage_overview": {
        "title": "Oracle VirtualBox VBoxManage Command-Line Reference Overview",
        "url": "https://www.virtualbox.org/manual/topics/vboxmanage.html",
        "tags": ["virtualbox", "vboxmanage", "vm", "hypervisor", "cli-docs"],
        "expected_terms": ["VBoxManage", "list", "showvminfo", "startvm", "controlvm"],
    },
    "vboxmanage_ch08": {
        "title": "Oracle VirtualBox User Manual Chapter 8: VBoxManage Complete Commands",
        "url": "https://www.virtualbox.org/manual/ch08.html",
        "tags": ["virtualbox", "vboxmanage", "headless", "reset", "screenshotpng", "natpf1"],
        "expected_terms": ["VBoxManage", "headless", "controlvm", "screenshotpng"],
    },
    "systemd_loginctl": {
        "title": "freedesktop.org loginctl(1) Session, Seat & Linger Control Manual",
        "url": "https://www.freedesktop.org/software/systemd/man/latest/loginctl.html",
        "tags": ["ubuntu", "linux", "systemd", "loginctl", "seat0", "sessions"],
        "expected_terms": ["loginctl", "list-sessions", "show-session", "enable-linger"],
    },
    "systemd_service": {
        "title": "freedesktop.org systemd.service(5) Service Unit Configuration Manual",
        "url": "https://www.freedesktop.org/software/systemd/man/latest/systemd.service.html",
        "tags": ["ubuntu", "linux", "systemd", "service", "restart", "daemon"],
        "expected_terms": ["ExecStart=", "Restart=", "Type="],
    },
    "gnome_gdm_autologin": {
        "title": "GNOME System Administration Guide: GDM Automatic Login Configuration",
        "url": "https://help.gnome.org/admin/system-admin-guide/stable/login-automatic.html.en",
        "tags": ["ubuntu", "gnome", "gdm3", "autologin", "custom.conf", "display-manager"],
        "expected_terms": ["AutomaticLoginEnable", "AutomaticLogin"],
    },
    "glib_gsettings": {
        "title": "Debian/Ubuntu gsettings(1) GSettings Configuration Tool Manual",
        "url": "https://manpages.debian.org/bookworm/libglib2.0-bin/gsettings.1.en.html",
        "tags": ["ubuntu", "gnome", "gsettings", "power-management", "screensaver"],
        "expected_terms": ["gsettings", "SCHEMA", "KEY", "set"],
    },
    "openbsd_ssh": {
        "title": "OpenSSH ssh(1) Manual: Local (-L) & Remote (-R) Port Forwarding",
        "url": "https://man.openbsd.org/ssh.1",
        "tags": ["ssh", "openssh", "port-forwarding", "tunnel", "ExitOnForwardFailure"],
        "expected_terms": ["ssh", "bind_address", "port", "ExitOnForwardFailure"],
    },
    "openbsd_ssh_config": {
        "title": "OpenSSH ssh_config(5) Manual: BatchMode & ServerAliveInterval Keepalives",
        "url": "https://man.openbsd.org/ssh_config.5",
        "tags": ["ssh", "ssh_config", "BatchMode", "ServerAliveInterval", "keepalive"],
        "expected_terms": ["BatchMode", "ServerAliveInterval", "ExitOnForwardFailure"],
    },
    "chrome_devtools_protocol": {
        "title": "Chrome DevTools Protocol (CDP) HTTP Endpoints & Remote Debugging Spec",
        "url": "https://chromedevtools.github.io/devtools-protocol/",
        "tags": ["chrome", "cdp", "remote-debugging-port", "browser-automation"],
        "expected_terms": ["DevTools", "Protocol", "json"],
    },
}


@dataclass(frozen=True)
class DocSkillRecipe:
    task_id: str
    name: str
    description: str
    steps: Tuple[str, ...]
    example: str
    doc_keys: Tuple[str, ...]


RECIPES: Dict[str, DocSkillRecipe] = {
    "diagnose_vm_greeter_stall": DocSkillRecipe(
        task_id="diagnose_vm_greeter_stall",
        name="virtualbox-headless-vm-ops",
        description="Diagnose VirtualBox headless VMs stuck at GDM login/greeter screens and select safe VBoxManage lifecycle actions.",
        steps=(
            "Inspect VM runtime state with `VBoxManage showvminfo <vm> --machinereadable` (`VMState=\"running\"` vs `\"poweroff\"` / `\"aborted\"` / `\"saved\"`) and optionally capture visual state via `VBoxManage controlvm <vm> screenshotpng <path>`.",
            "When `VMState != \"running\"`, start headless via `VBoxManage startvm <vm> --type headless` (`startvm_headless`).",
            "Per GNOME GDM `AutomaticLoginEnable=true` docs and `loginctl(1)`, automatic login executes ONLY on initial boot; if the user Wayland/X11 session closes (`State=closing`) and `gdm-greeter` owns `seat0`/`tty1` while Chrome CDP `:9222` is down, execute `VBoxManage controlvm <vm> reset` (`controlvm_reset`) so fresh boot triggers `AutomaticLogin` cleanly.",
            "When `VMState == \"running\"`, no `gdm-greeter` lock owns `seat0`, and guest `:9222` is already listening, return `noop` (`healthy`) and never reset a live browser session.",
        ),
        example="""def classify_vm(vm_state, greeter_on_seat0, guest_cdp_listening):
    if vm_state != 'running':
        return ('vm_stopped', 'startvm_headless')
    if guest_cdp_listening and not greeter_on_seat0:
        return ('healthy', 'noop')
    if greeter_on_seat0 and not guest_cdp_listening:
        return ('gdm_greeter_stall', 'controlvm_reset')
    return ('browser_process_missing', 'restart_guest_browser')

assert classify_vm('poweroff', False, False) == ('vm_stopped', 'startvm_headless')
assert classify_vm('running', True, False) == ('gdm_greeter_stall', 'controlvm_reset')
assert classify_vm('running', False, True) == ('healthy', 'noop')
assert classify_vm('running', False, False) == ('browser_process_missing', 'restart_guest_browser')""",
        doc_keys=("vboxmanage_overview", "vboxmanage_ch08", "gnome_gdm_autologin", "systemd_loginctl"),
    ),
    "ubuntu_power_guard_commands": DocSkillRecipe(
        task_id="ubuntu_power_guard_commands",
        name="ubuntu-gdm-session-and-power-ops",
        description="Prevent Ubuntu GNOME guest VMs from auto-suspending, blanking, or locking out headless Chrome sessions using gsettings and systemd user units.",
        steps=(
            "Over SSH (`sabrina@127.0.0.1 -p 2222`), address the user D-Bus bus explicitly via `DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$UID/bus` before invoking `gsettings(1)`.",
            "Check `org.gnome.settings-daemon.plugins.power sleep-inactive-ac-type`; if not `'nothing'`, emit `gsettings set org.gnome.settings-daemon.plugins.power sleep-inactive-ac-type 'nothing'` so GNOME never suspends the VM after idle timeout.",
            "Ensure `org.gnome.desktop.session idle-delay` is `uint32 0` (`gsettings set org.gnome.desktop.session idle-delay 0`) and `org.gnome.desktop.screensaver lock-enabled` is `false` (`gsettings set org.gnome.desktop.screensaver lock-enabled false`).",
            "Keep Chrome managed under a user `systemd.service(5)` unit (`Restart=always`, `RestartSec=5`) or autostart launcher bound to `127.0.0.1:9222` so crashes self-heal without dropping the session.",
        ),
        example="""def build_gsettings_plan(uid, sleep_ac_type, idle_delay, lock_enabled):
    prefix = f'DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/{int(uid)}/bus gsettings set'
    cmds = []
    if sleep_ac_type != 'nothing':
        cmds.append(f"{prefix} org.gnome.settings-daemon.plugins.power sleep-inactive-ac-type 'nothing'")
    if int(idle_delay) != 0:
        cmds.append(f'{prefix} org.gnome.desktop.session idle-delay 0')
    if bool(lock_enabled):
        cmds.append(f'{prefix} org.gnome.desktop.screensaver lock-enabled false')
    return cmds

plan = build_gsettings_plan(1000, 'suspend', 300, True)
assert len(plan) == 3
assert "sleep-inactive-ac-type 'nothing'" in plan[0]
assert build_gsettings_plan(1000, 'nothing', 0, False) == []""",
        doc_keys=("glib_gsettings", "systemd_loginctl", "systemd_service", "gnome_gdm_autologin"),
    ),
    "reconcile_vm_cdp_tunnel": DocSkillRecipe(
        task_id="reconcile_vm_cdp_tunnel",
        name="vm-ssh-cdp-browser-bridge",
        description="Maintain and reconcile two-hop OpenSSH port forwards between the Ubuntu browser VM (2222 -> 9222), Windows host (9222), and ego lab server (19224).",
        steps=(
            "Verify end-to-end CDP readiness via HTTP GET `http://127.0.0.1:9222/json/version` (`webSocketDebuggerUrl` non-empty) AND verify the owning Windows `:9222` listener is `ssh.exe` forwarding `-L 127.0.0.1:9222:127.0.0.1:9222` to `sabrina@127.0.0.1` (`-p 2222`).",
            "If host `:9222` has an existing `sabrina@127.0.0.1` `ssh.exe` listener (`host_forward_listening=True`) while `/json/version` fails (`local_cdp_ok=False`), the forward is pointing at a dead guest port or hung VM session—stop that stale VM `ssh.exe` tunnel BEFORE reconnecting (never throw 'Port 9222 is occupied' on Sabrina's own stale VM tunnel!).",
            "Refuse to kill any `:9222` listener that is NOT Sabrina's `sabrina@127.0.0.1` SSH tunnel (`foreign_port_owner=True` -> `abort_foreign_listener`).",
            "Once guest Chrome `:9222` is up, launch `ssh -4 -N -T -p 2222 -o BatchMode=yes -o ExitOnForwardFailure=yes -L 127.0.0.1:9222:127.0.0.1:9222 sabrina@127.0.0.1` and verify `/json/version` before forwarding `-R 127.0.0.1:19224:127.0.0.1:9222` to `ego`.",
        ),
        example="""def plan_tunnel_reconcile(local_cdp_ok, host_port_listening, listener_is_vm_ssh, guest_cdp_ok):
    if local_cdp_ok and host_port_listening and listener_is_vm_ssh:
        return ['verify_ego_reverse_forward']
    if host_port_listening and not listener_is_vm_ssh:
        return ['abort_foreign_listener']
    steps = []
    if host_port_listening and listener_is_vm_ssh and not local_cdp_ok:
        steps.append('stop_stale_vm_ssh_forward')
    if not guest_cdp_ok:
        steps.append('recover_vm_guest_browser')
    steps.append('start_vm_ssh_forward')
    steps.append('verify_local_cdp_version')
    return steps

assert plan_tunnel_reconcile(False, True, True, False) == [
    'stop_stale_vm_ssh_forward', 'recover_vm_guest_browser', 'start_vm_ssh_forward', 'verify_local_cdp_version'
]
assert plan_tunnel_reconcile(False, True, False, True) == ['abort_foreign_listener']
assert plan_tunnel_reconcile(True, True, True, True) == ['verify_ego_reverse_forward']""",
        doc_keys=("openbsd_ssh", "openbsd_ssh_config", "chrome_devtools_protocol", "vboxmanage_overview"),
    ),
}


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as tmp:
        tmp.write(content)
        tmp_path = Path(tmp.name)
    tmp_path.replace(path)


def _strip_html_to_text(raw_html: str) -> str:
    cleaned = re.sub(r"(?is)<(script|style)\b.*?>.*?</\1>", " ", raw_html)
    cleaned = re.sub(r"(?s)<[^>]+>", " ", cleaned)
    cleaned = unescape(cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


def fetch_and_verify_doc(key: str, spec: Dict[str, Any], cache_dir: Path) -> Dict[str, Any]:
    """Fetch official doc over HTTPS (or reuse verified cache), screen safety, and extract summary."""
    from tools.cyber_compliance_sentinel import CyberComplianceSentinel
    from tools.resource_adapter import inspect_untrusted_content_safety

    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_html = cache_dir / f"{key}.html"
    cache_txt = cache_dir / f"{key}.md"

    raw_bytes: bytes
    t0 = time.time()
    req = urllib.request.Request(
        spec["url"],
        headers={"User-Agent": "Sabrina-VM-OS-Doc-Skills/1.0 (+zero-auth official manual reader)"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw_bytes = resp.read(PAGE_LIMIT + 1)
            http_status = int(resp.status or 200)
            content_type = resp.headers.get_content_type()
    except Exception:
        if not cache_html.exists():
            raise
        raw_bytes = cache_html.read_bytes()
        http_status = 200
        content_type = "text/html"
    latency_ms = round((time.time() - t0) * 1000.0, 1)

    if len(raw_bytes) > PAGE_LIMIT:
        raise ValueError(f"Doc {key} exceeded PAGE_LIMIT ({len(raw_bytes)} bytes)")

    decoded = raw_bytes.decode("utf-8", errors="replace")
    plain = _strip_html_to_text(decoded)

    sentinel = CyberComplianceSentinel()
    ok_out, out_msg = sentinel.sanitize_outbound_query(spec["url"])
    if not ok_out:
        raise ValueError(f"CyberComplianceSentinel blocked outbound URL {key}: {out_msg}")

    from tools.internet_resource_indexer import UNTRUSTED_DOC_PATTERNS
    for bad_pat in UNTRUSTED_DOC_PATTERNS:
        if bad_pat.search(plain[:50000]):
            raise ValueError(f"Untrusted doc pattern matched in {key}: {bad_pat.pattern}")

    safe_ok, safe_err = inspect_untrusted_content_safety(plain[:50000])
    if not safe_ok:
        raise ValueError(f"Untrusted content check failed on {key}: {safe_err}")

    missing = [term for term in spec["expected_terms"] if term.lower() not in plain.lower()]
    if missing:
        raise ValueError(f"Doc {key} missing expected reference terms: {missing}")

    page_sha256 = hashlib.sha256(raw_bytes).hexdigest()
    cache_html.write_bytes(raw_bytes)
    summary_md = (
        f"# {spec['title']}\n\n"
        f"- **Source URL**: {spec['url']}\n"
        f"- **SHA-256**: `{page_sha256}`\n"
        f"- **Verified Terms**: {', '.join(spec['expected_terms'])}\n\n"
        f"## Extracted Reference Text\n\n{plain[:8000]}\n"
    )
    _atomic_write(cache_txt, summary_md)

    return {
        "key": key,
        "title": spec["title"],
        "url": spec["url"],
        "tags": spec["tags"],
        "http_status": http_status,
        "content_type": content_type,
        "latency_ms": latency_ms,
        "bytes": len(raw_bytes),
        "page_sha256": page_sha256,
        "markdown_path": str(cache_txt),
        "excerpt": plain[:600],
    }


def skill_body(recipe: DocSkillRecipe) -> str:
    lines = [
        f"# Skill: {recipe.name}",
        "",
        "## When to use it",
        "",
        recipe.description,
        "",
        "## Procedure",
        "",
        *[f"- {step}" for step in recipe.steps],
        "",
        "## Runnable check",
        "",
        "```python",
        recipe.example,
        "```",
    ]
    return "\n".join(lines)


def render_skill(recipe: DocSkillRecipe, references: Dict[str, Dict[str, Any]]) -> str:
    source_lines = [
        f"- `{references[dk]['title']}` (`sha256:{references[dk]['page_sha256'][:16]}`) [{dk}]({references[dk]['url']})"
        for dk in recipe.doc_keys
    ]
    frontmatter = "\n".join([
        "---",
        f"name: {recipe.name}",
        f'description: "{recipe.description}"',
        'version: "1.0.0"',
        "requires_tools:",
        "  - vbox_vm_operator",
        "---",
        "",
    ])
    return frontmatter + skill_body(recipe) + "\n\n## Sources\n\n" + "\n".join(source_lines) + "\n"


def prompt_context(skills_dir: Path, task_id: str) -> str:
    """Return verified doc-grounded SKILL.md body for SSM / LLM synthesis prompts."""
    recipe = RECIPES[task_id]
    path = Path(skills_dir) / recipe.name / "SKILL.md"
    text = path.read_text(encoding="utf-8")
    body = text.split("---\n", 2)[-1].strip().partition("\n\n## Sources\n")[0]
    if body != skill_body(recipe):
        raise ValueError(f"Skill body differs from its tested recipe at {path}")
    return "Relevant Sabrina documentation skill. Adapt the procedure strictly to the task contract.\n\n" + body


def build_vm_os_doc_skills(
    skills_dir: Path = DEFAULT_SKILLS_DIR,
    receipt_path: Path = DEFAULT_RECEIPT_PATH,
    cache_dir: Path = VENDOR_DOC_DIR,
    index_in_catalog: bool = True,
) -> Dict[str, Any]:
    """Fetch & verify all official VM/OS docs, run executable checks, emit SKILL.md files, and index into RAG."""
    references: Dict[str, Dict[str, Any]] = {}
    for key, spec in OFFICIAL_DOC_SOURCES.items():
        references[key] = fetch_and_verify_doc(key, spec, cache_dir=cache_dir)

    written_skills: Dict[str, str] = {}
    for recipe in RECIPES.values():
        # Execute self-check before admitting skill
        exec(compile(recipe.example, recipe.name, "exec"), {})
        skill_text = render_skill(recipe, references)
        skill_path = Path(skills_dir) / recipe.name / "SKILL.md"
        _atomic_write(skill_path, skill_text)
        written_skills[recipe.name] = hashlib.sha256(skill_text.encode("utf-8")).hexdigest()

    if index_in_catalog:
        try:
            from tools.internet_resource_indexer import DEFAULT_DB_PATH, InternetResourceIndexer
            target_db = (
                ROOT / "data" / "sabrina_assistant_fresh.sqlite3"
                if (ROOT / "data" / "sabrina_assistant_fresh.sqlite3").exists()
                else DEFAULT_DB_PATH
            )
            indexer = InternetResourceIndexer(db_path=target_db)
            for key, ref in references.items():
                indexer._upsert_resource(
                    item={
                        "resource_kind": "doc_skill_repo",
                        "title": ref["title"],
                        "url": ref["url"],
                        "endpoint_or_raw_url": ref["url"],
                        "domain_tags": ref["tags"],
                        "description": ref["excerpt"][:280],
                        "http_status": ref["http_status"],
                        "latency_ms": ref["latency_ms"],
                        "content_type": ref["content_type"],
                        "schema_summary": {"doc_key": key, "bytes": ref["bytes"]},
                        "usage_snippet": ref["excerpt"][:360],
                        "quality_score": 95.0,
                        "content_sha256": ref["page_sha256"],
                    },
                    arm_id="arm_vm_os_docs",
                    query="virtualbox vboxmanage ubuntu systemd gdm3 ssh cdp docs",
                )
        except Exception:
            pass

        try:
            from tools.sabrina_tool_skill_forge import SabrinaToolSkillForge
            forge = SabrinaToolSkillForge()
            forge.ingest_docs_and_reason(
                problem_slug="sabrina-browser-vm-ops",
                problem_statement=(
                    "Enable Sabrina to operate her headless VirtualBox Ubuntu 26.04 browser VM ('Sabrina Browser'), "
                    "diagnose and recover from GDM3 greeter/login stalls after Wayland session closure or idle suspend, "
                    "apply GNOME gsettings power/lock guards, and reconcile 2-hop SSH CDP port forwards (2222 -> 9222 -> 19224)."
                ),
                doc_paths=[ref["markdown_path"] for ref in references.values()],
                rag_queries=["VirtualBox VBoxManage headless", "Ubuntu GDM3 AutomaticLogin gsettings suspend", "SSH CDP 9222"],
                extracted_invariants=[
                    "GDM3 AutomaticLoginEnable=true only logs in once on initial boot; if the Wayland session closes, gdm-greeter owns seat0/tty1 and requires `VBoxManage controlvm <vm> reset` to re-trigger automatic login.",
                    "Ubuntu GNOME defaults `org.gnome.settings-daemon.plugins.power sleep-inactive-ac-type` to `'suspend'`; over SSH, set it to `'nothing'` via `DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus gsettings set ...`.",
                    "If host port 9222 is held by Sabrina's own `ssh.exe -p 2222 -L 127.0.0.1:9222:127.0.0.1:9222 sabrina@127.0.0.1` while `/json/version` fails, terminate that stale forward before reconnecting.",
                ],
                design_reasoning=(
                    "Grounded in official VirtualBox 7.2 Ch.8, systemd loginctl(1), GNOME GDM Admin Guide, gsettings(1), "
                    "and OpenSSH ssh(1) manuals."
                ),
                test_hypotheses=[
                    "Mamba-2 SSM (`mahade315/mamba-codestral:7b-q4_K_M`) passes smoke + heldout VM/OS synthesis checks when equipped with these doc skills.",
                    "`vbox_vm_operator` plugin + `Sabrina Bridge Watcher` autonomously recover 'Sabrina Browser' past the GDM3 password screen into live CDP :9222.",
                ],
                author_model="mahade315/mamba-codestral:7b-q4_K_M",
            )
        except Exception:
            pass

    receipt = {
        "generated_at": time.time(),
        "sources": {
            k: {
                "title": v["title"],
                "url": v["url"],
                "bytes": v["bytes"],
                "page_sha256": v["page_sha256"],
            }
            for k, v in references.items()
        },
        "verified_skill_sha256": written_skills,
    }
    _atomic_write(receipt_path, json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    return receipt


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skills-dir", type=Path, default=DEFAULT_SKILLS_DIR)
    parser.add_argument("--receipt", type=Path, default=DEFAULT_RECEIPT_PATH)
    parser.add_argument("--no-index", action="store_true")
    args = parser.parse_args()
    result = build_vm_os_doc_skills(
        skills_dir=args.skills_dir,
        receipt_path=args.receipt,
        index_in_catalog=not args.no_index,
    )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
