# External Harnesses (Templates)

This directory contains the core package structure for `harnesses.external`:

- `external/__init__.py` — PEP 562 virtual portal dispatcher backed by SQLite (`resolve_virtual_portal_spec`).
- `external/collective_pool.py` — `KindCollectivePool` for tiered routing, cooldowns, semantic caching, and local model discovery.
- `external/cognitive_blackboard.py` — `HierarchicalCognitiveBlackboard` for hypotheses, solution patterns, and competence tracking.
- `external/swarm_system_bridges.py` — Integration bridge helpers and virtual portal lookup.
- `external/portal_template.py.example` — Template for adding a local file-based portal entry point.

## Storing Private External Models Locally (Git-Ignored)

To keep external models, endpoints, and Hugging Face spaces out of GitHub:

1. **Declarative JSON (`external_models.json`)**: Copy `external_models.example.json` at the repo root to `external_models.json` (git-ignored) and set `"enabled": true` on your entries. `KindCollectivePool` loads them automatically at startup.
2. **Local Python modules (`external/local/*.py` or `external/<slug>.py`)**: Copy `external/portal_template.py.example` to `external/local/<slug>.py` (or `external/<slug>.py`). Both are git-ignored so they will never be committed.
3. **SQLite / DPAPI Vault**: Registry-backed portals in `sabrina_assistant_fresh.sqlite3` and API keys in `provider_vault.json` are also git-ignored.
