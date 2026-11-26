---
name: github-console-publisher
description: Packages, sanitizes, and prepares the Sabrina Operator Console repository for GitHub publication. Audits safe paths, strips sensitive secrets/vaults/SQLite caches, generates sanitized .env.example, verifies execution_authorized=False invariant, and produces release artifacts.
---

# GitHub Console Publisher Skill

This skill provides an automated, fail-safe procedure for packaging and publishing the **Sabrina Operator Console** to GitHub while strictly preserving privacy, security invariants, and path isolation.

## Security & Privacy Invariants

1. **Fail-Closed Vault Exclusion**:
   - Never commit `provider_vault.json` (DPAPI-encrypted keys or plain keys).
   - Never commit local SQLite databases (`*.sqlite3`, `availability-backups/`).
   - Never commit `.env` or personal workspace config files.
2. **Safety Boundary**:
   - Repository release must enforce `execution_authorized: false` by default.
   - All tools run in sandboxed read-only mode unless explicitly elevated with user authorization.
3. **Reproducibility**:
   - Include clean `.env.example` with blank placeholders.
   - Include `pyproject.toml` or `requirements.txt` with locked dependencies.

---

## Directory Layout for Public Release

When publishing Sabrina Console to GitHub, ensure the following core modules are staged:

```
Sabrina/
├── .gitignore                          # Excludes vaults, venvs, and local SQLite databases
├── .env.example                        # Template for optional API keys
├── README.md                           # Architecture overview, installation, and usage
├── sabrina_desktop_ui.html             # Synced PyWebView desktop shell UI
├── sabrina_console_desktop/            # Console desktop application package
│   ├── sabrina_desktop_ui.py           # Native pywebview desktop bridge
│   ├── live_swarm_runner.py            # Live multi-agent collective runner & blackboard
│   ├── sabrina_desktop_ui.html         # Desktop shell HTML, CSS, SVG DAG & Blackboard Chat
│   ├── provider_vault.py               # Key vault management
│   ├── role_bindings.py                # Swarm role and model bindings
│   └── tests/                          # Automated desktop bridge test suite
├── swarm_command/                      # SwarmCommand routing, CAS blackboard, handoffs
│   ├── blackboard.py                   # SessionBlackboard CAS state store
│   ├── router.py                       # Complexity-based router & handoff dispatcher
│   └── role_binding.py                 # Role binding resolution & model provider inference
├── swarm_console/                      # Console registries, harnesses, and workspace agent
└── scripts/
    └── package_for_github.py           # Pre-flight audit & packaging verification script
```

---

## Pre-Publication Audit Checklist

Before pushing to any GitHub remote:

1. **Run Pre-flight Audit Script**:
   ```bash
   python scripts/package_for_github.py --check
   ```
2. **Execute Full Test Suite**:
   ```bash
   python -m unittest sabrina_console_desktop.test_console_bridge_wiring sabrina_console_desktop.test_live_swarm_runner swarm_command.test_swarm_command swarm_console.test_swarm_console
   ```
3. **Verify Synced HTML Files**:
   Ensure `sabrina_console_desktop/sabrina_desktop_ui.html` and root `sabrina_desktop_ui.html` are identical:
   ```powershell
   Compare-Object (Get-Content sabrina_console_desktop\sabrina_desktop_ui.html) (Get-Content sabrina_desktop_ui.html)
   ```
4. **Initialize and Push**:
   ```bash
   git init
   git add .
   git commit -m "feat: initial public release of Sabrina Operator Console"
   git branch -M main
   git remote add origin https://github.com/<your-username>/sabrina-operator-console.git
   git push -u origin main
   ```
