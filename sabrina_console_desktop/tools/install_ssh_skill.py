"""Hydrate and cryptographically approve the `ssh-multitude-ops` skill for Sabrina,
aligned with https://huggingface.co/docs/hub/agents-skills and `huggingface/skills`.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.sabrina_skill_hydration import SabrinaSkillHydration

SKILL_MD = """---
name: ssh-multitude-ops
version: 1.0.0
description: "Comprehensive OpenSSH troubleshooting, port-forwarding/tunnel recovery, key and permission remediation, non-interactive remote command execution, SCP/rsync synchronization, systemd user-service management, and Hugging Face Hub SSH/CLI (`hf`) remote operations across Windows (OpenSSH client) and Linux (`ego@ego.local`). Use whenever encountering SSH connection drops, broken port forwards (8087/8090/11434/19224/19225), `Permission denied (publickey)`, host-key mismatches, zombie tunnels, remote syntax/headless deployment errors, or Hugging Face SSH git/Hub tasks."
compatibility: ">=2.1.0"
keywords: SSH, OpenSSH, tunnel, port-forwarding, scp, rsync, systemd, ego.local, huggingface, hf-cli, troubleshooting
requires_tools:
  - discover_lab
  - remote_run_shell
  - remote_read_file
  - completed
---

# SSH Multitude Operations & Remote Infrastructure Playbook (v1.0.0)

Built to the [Hugging Face Agents Skills (`SKILL.md`) specification](https://huggingface.co/docs/hub/agents-skills) and grounded in Sabrina's Windows <-> Linux (`ego@ego.local`) hybrid runtime.

---

## 1. Core SSH & Tunneling Architecture in Sabrina's Lab

Sabrina spans two primary hosts connected over OpenSSH:
- **Windows Operator Host**: Runs `sabrina_operator_console.py`, Antigravity IDE (`Antigravity.exe`), `Codex.exe`, `Claude.exe`, and `sabrina_social.bridge_watcher` (`127.0.0.1:19225`).
- **Linux Lab Node (`ego@ego.local` / `192.168.1.133`)**: Runs Ollama (`11434`), Lab Coordinator (`8090`), Sabrina Social API (`127.0.0.1:8087`), Content Research API (`8088`), and Social Scheduler.

### Standard Port-Forward Matrix
| Local Port (Windows) | Direction | Remote Target (`ego.local`) | Service Purpose |
| :--- | :--- | :--- | :--- |
| `127.0.0.1:8087` | `-L` (Local -> Remote) | `127.0.0.1:8087` | Sabrina Swarm & Social Web Dashboard (`sabrina-social-api.service`) |
| `127.0.0.1:8090` | `-L` / Direct LAN | `192.168.1.133:8090` | Shared Lab Coordinator (`sabrina-lab-coordinator.service`) |
| `127.0.0.1:11434` | `-L` / Direct LAN | `192.168.1.133:11434` | Ollama Model Server |
| `127.0.0.1:19224` | `-R` (Remote -> Local) | `127.0.0.1:19224` | Reverse tunnel for LinkedIn Chrome CDP browser access |

---

## 2. Diagnostic & Remediation Playbooks (The Multitude of SSH Problems)

### Problem A: Port-Forward / Tunnel Drops (`Unable to connect to remote server` on `8087` or `19224`)
**Symptoms**: Web dashboard (`127.0.0.1:8087`) is unreachable or sockets sit in `TimeWait` after sleep/network blip.
**Diagnosis & Fix**:
1. Inspect local listener and owning process on Windows:
   ```powershell
   Get-NetTCPConnection -LocalPort 8087 -State Listen -ErrorAction SilentlyContinue
   ```
2. Launch a resilient, non-interactive background OpenSSH tunnel with keep-alives and `ExitOnForwardFailure=yes`:
   ```powershell
   Start-Process -FilePath "C:\\WINDOWS\\System32\\OpenSSH\\ssh.exe" `
     -ArgumentList "-N -T -o BatchMode=yes -o ConnectTimeout=5 -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -L 127.0.0.1:8087:127.0.0.1:8087 ego@ego.local" `
     -WindowStyle Hidden
   ```
3. Verify the remote service is actually listening on `ego.local`:
   ```bash
   ssh -o BatchMode=yes ego@ego.local "ss -tlnp | grep 8087"
   ```

### Problem B: Authentication Failures (`Permission denied (publickey,password)`)
**Symptoms**: SSH or SCP rejects non-interactive login or hangs on password prompt.
**Diagnosis & Fix**:
1. Always pass `-o BatchMode=yes -o ConnectTimeout=5` in automated scripts so SSH fails fast rather than hanging a background worker.
2. Run verbose handshake trace to isolate key rejection:
   ```bash
   ssh -vvv -o BatchMode=yes ego@ego.local "echo SSH_OK"
   ```
3. Verify Linux server file modes (OpenSSH `StrictModes` rejects keys if group/world writable):
   ```bash
   chmod 700 ~/.ssh && chmod 600 ~/.ssh/authorized_keys
   ```
4. Verify Windows client key permissions (`~/.ssh/id_ed25519` or `id_rsa`) and agent status (`Get-Service ssh-agent`).

### Problem C: Host Key Mismatch / DHCP IP Drift (`REMOTE HOST IDENTIFICATION HAS CHANGED`)
**Symptoms**: `ego.local` mDNS resolves to a new IP or key rotation triggers `Host key verification failed`.
**Diagnosis & Fix**:
1. Verify IP resolution via mDNS / ARP:
   ```powershell
   Resolve-DnsName ego.local
   ```
2. Remove the stale host key entry cleanly before reconnecting:
   ```bash
   ssh-keygen -R ego.local
   ```

### Problem D: Quoting, Escaping & Hanging Remote Commands
**Symptoms**: PowerShell mangles double quotes `"` or `$variables` before sending them over SSH, causing remote Python/Bash `SyntaxError: unterminated string literal`, or remote background processes hold `stdout`/`stderr` open and hang the SSH session.
**Diagnosis & Fix**:
1. **Quote Isolation**: Use single-quoted Python expressions or deploy a `.py` file via `scp` rather than nesting escaped quotes across PowerShell -> SSH -> Bash -> Python.
2. **Non-Blocking Daemons**: When starting a remote service over SSH, manage it via `systemctl --user restart <service>` or redirect all three streams (`>/dev/null 2>&1 </dev/null &`).

### Problem E: Headless Environment & Sync Mismatches (`ModuleNotFoundError: No module named tkinter`)
**Symptoms**: Syncing shared Python modules from Windows (`c:\\Users\\bcmcg\\Desktop\\bot\\Sabrina`) to headless Linux (`/home/ego/bot/Sabrina-windows-sync`) causes `sabrina-social-api.service` to crash on GUI imports (`tkinter`) or signature drift.
**Diagnosis & Fix**:
1. Guard all GUI imports (`tkinter`) with `try ... except ImportError:` so headless Linux services can import shared data/control functions cleanly.
2. **Mandatory Pre-Restart Check**: Always run remote syntax and import verification over SSH before restarting `systemd` user services:
   ```bash
   ssh -o BatchMode=yes ego@ego.local "cd /home/ego/bot/Sabrina-windows-sync && /usr/bin/python3 -m py_compile sabrina_social/api.py && /usr/bin/python3 -c 'import sabrina_social.api'"
   ```
3. Restart and verify active status:
   ```bash
   ssh -o BatchMode=yes ego@ego.local "systemctl --user restart sabrina-social-api.service && systemctl --user is-active sabrina-social-api.service"
   ```

### Problem F: Hugging Face Hub SSH & `hf` CLI Remote Operations
**Symptoms**: Cloning/pushing datasets or models over SSH (`git@hf.co`) fails on `ego.local`, or remote training/download jobs stall.
**Diagnosis & Fix**:
1. Verify Hugging Face SSH authentication from the target node:
   ```bash
   ssh -T git@hf.co
   ```
2. Use the modern Hugging Face CLI (`hf`, replacing `huggingface-cli`) per `huggingface/skills`:
   - Check auth: `hf auth whoami`
   - Download models/datasets cleanly: `hf download <repo_id>`
   - Install/sync Agent Skills: `hf skills add <skill-name> --global`

---

## 3. Safety & Verification Invariants
- **Never** modify `/etc/ssh/sshd_config` without running `sudo sshd -t` first.
- **Never** expose private keys (`id_rsa`, `id_ed25519`) in logs, prompts, or tool outputs.
- **Always** validate Python syntax locally (`python -m py_compile`) and remotely (`python3 -m py_compile`) before restarting remote services.
"""


def main() -> None:
    sha = hashlib.sha256(SKILL_MD.encode("utf-8")).hexdigest()
    hyd = SabrinaSkillHydration()
    prop_id = hyd.create_proposal(
        skill_name="ssh-multitude-ops",
        skill_content=SKILL_MD,
        trigger_reason="operator_request_huggingface_agents_skills",
        query_used="https://huggingface.co/docs/hub/agents-skills + OpenSSH multitude troubleshooting",
        target_path="skills/ssh-multitude-ops/SKILL.md",
        search_sources=json.dumps(
            [
                {
                    "title": "Hugging Face Hub - Agent Skills Specification",
                    "url": "https://huggingface.co/docs/hub/agents-skills",
                },
                {
                    "title": "huggingface/skills Official Repository",
                    "url": "https://github.com/huggingface/skills",
                },
            ]
        ),
    )
    res = hyd.approve_proposal(
        proposal_id=prop_id,
        expected_version="1.0.0",
        expected_sha256=sha,
        reviewer="operator",
        feedback="Approved & hydrated SSH multitude skill per Hugging Face Agents Skills spec",
    )

    # Also mirror to workspace .agents/skills and .gemini/skills so Antigravity & Codex discover it natively
    repo_root = ROOT.parent
    for base in (repo_root / ".agents" / "skills", repo_root / ".gemini" / "skills"):
        dest = base / "ssh-multitude-ops" / "SKILL.md"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(SKILL_MD, encoding="utf-8")

    print(json.dumps(res, indent=2))


if __name__ == "__main__":
    main()
