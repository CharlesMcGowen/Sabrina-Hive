# `swarm_console`

Zero-dependency Python 3.10+ package for visualizing and managing `.swarm/` agent swarms in Sabrina's management console.

## Features

- **Zero external dependencies**: Uses Python stdlib only (`http.server`, `json`, `pathlib`, `threading`, `unittest`).
- **Fail-closed path isolation**: `guard_safe_path()` blocks any `.swarm/` or workspace path touching `outputs/kraken-holdout`, `holdout`, `2026-11-24..30` / `20261124..30`, `2d8acaed`, `14-day` / `14day` / `active 14-day study`, or `sampled-book`.
- **Safe by default**: `safety.json` initializes with `execution_authorized: false`, displayed in the console header strip.
- **Disjoint file locks**: `LockConflictError` rejects overlapping file or glob locks across active workers.
- **Live SSE console**: `ui.html` streams real-time updates from `/events` with outcome-grouped threads, lock matrix, and `@mention` board filters.
- **Optional Gemini Swarm Copilot**: Uses the `google-genai` Interactions API (`gemini-4-hydrogen` by default) via `/api/gemini/status` and `/api/gemini/analyze` to triage blockers, inspect lock risks, and draft `@mention` board messages with stateful follow-up turns. Set `GEMINI_API_KEY` and install `google-genai>=3.5.0` to enable.

## Quick Start

### 1. Run the built-in `--demo`

```bash
python -m swarm_console.server --demo
```

Open `http://127.0.0.1:8765/` in your browser.

### 2. Point at an existing `.swarm/` directory

```bash
python -m swarm_console.server --swarm-dir path/to/.swarm --host 127.0.0.1 --port 8765
```

### 3. Use `SwarmClient` from Python agents

```python
from swarm_console import SwarmClient

client = SwarmClient("workspace/.swarm", auto_init=True)
client.update_goal("Verify module boundaries", phase="executing")
client.register_worker(
    name="parser-agent",
    role="Parser",
    model="gemini-2.5-pro",
    locks=["src/parser.py"],
    state="working",
)
client.post_message("parser-agent", "@coordinator [done] [PASS] parser.py ready")
```

### 4. Embed into an existing console

- **HTTP / iframe**: Run `create_server(swarm_dir, host="127.0.0.1", port=8765)` in a background thread and embed `/` or consume `/api/state`, `/api/board?since=<seq>`, and `/events` (SSE).
- **Direct store**: Import `SwarmStore` from `swarm_console.protocol` to read/write `plan.json`, `roster.json`, `board.jsonl`, and `safety.json` atomically.

## Running Tests

```bash
python -m unittest discover -s swarm_console -v
```
