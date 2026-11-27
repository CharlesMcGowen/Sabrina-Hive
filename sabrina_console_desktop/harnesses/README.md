# External Harnesses (Templates)

This directory contains the core package structure for `harnesses.external`:

- `external/__init__.py` — PEP 562 virtual portal dispatcher backed by SQLite (`resolve_virtual_portal_spec`).
- `external/collective_pool.py` — `KindCollectivePool` and `CollectiveRouter` for tiered routing, cooldowns, and semantic caching.
- `external/cognitive_blackboard.py` — `HierarchicalCognitiveBlackboard` for hypotheses, solution patterns, and competence tracking.
- `external/swarm_system_bridges.py` — Integration bridge helpers and virtual portal lookup.
- `external/portal_template.py.example` — Template for adding a local file-based portal entry point.

To add a custom portal harness locally, copy `external/portal_template.py.example` to `external/<portal_slug>.py` (do not commit private endpoints or credentials), or register the portal in the local SQLite database.
