# Sabrina Console 🐝

> **Swarm agentic framework for any task!**

Sabrina Console is a desktop-first, extensible multi-agent orchestration framework. It coordinates autonomous agent swarms, live execution monitoring, dynamic plugin tool-buses, and hot-swappable native harness profiles.

---

## ✨ Features

- **🐝 Stigmergic Multi-Agent Swarm Collective**:
  - Dynamic agent proliferation via entropy splitting ($\theta = 0.55$) and confidence gating (`min_logprob >= -0.25`).
  - Shared SQLite blackboard bus (`dnd_rsi_blackboard.sqlite3`) for asynchronous peer coordination, cross-agent handoffs, and multi-parent fan-in convergence.
  - Multi-model council support spanning `Gemini`, `OpenAI`, `Anthropic`, and local `Ollama` models.
  - Real-time token usage (`🪙 tok`) and blended API cost (`💵 $`) tracking.

- **🔍 Human-In-The-Loop Autonomous Search Hydration**:
  - `SwarmSearchHydrator`: Grounding engine that searches local workspace docs, production blueprints (PostgreSQL connection pools, JWT/Argon2id auth, Docker compose), and search engines to hydrate unfamiliar tasks.
  - Glowing `🔍 SEARCH` pill indicators in the live telemetry feed.

- **📈 Kraken Institutional & Quant Trading Suite**:
  - **Quant Tree Turnover Engine**: Log-odds gradient tree ensemble $\ln(p_{\text{Up}}/p_{\text{Down}})$ paired with capital turnover velocity scoring $\text{Score}_s = (\$1 \cdot p_s - C_s)/(C_s \cdot \tau_s)$ and half-Kelly sizing.
  - **Avellaneda-Stoikov Market Maker**: Reservation price skew $r(s, q, \tau)$ capturing parity spread and maker rebates while avoiding adverse selection.
  - **High-Frequency Tick Gates & 2s Book Recorder**: Microprice, order flow imbalance (OFI), and causal price windows.

- **🔮 QuantOrb Solana Holder Scanner & CPMM Dump Simulator**:
  - Separates liquidity pool reserves from insider/dev wallets.
  - Constant Product ($x \cdot y = k$) price collapse simulations for top-1 and coordinated top-5 syndicate dumps.
  - Authority and mutability audit tripwires (`mint_authority`, `freeze_authority`, mutable metadata).
  - Concentric SVG Orb visualizer and execution safety vetoes integrated into the Desktop Console.

- **💻 Dual PowerShell & Linux (WSL2) CLI Execution**:
  - Seamless execution across Windows PowerShell 5.1/7 and Linux Ubuntu 24.04 (WSL2 / SSH).
  - Granular execution authorization policies protecting core source trees.

- **📊 Advanced Swarm Activity Monitor**:
  - Live model heartbeats, step-by-step progress tracking, and execution state management (`running`, `completed`, `failed`).
  - Cross-application handoff cards bridging tasks across `Codex`, `Antigravity`, `Claude`, and native workers.
  - Real-time telemetry blackboard, decision tracing, and verification evidence logging.

- **🧩 Dynamic Plugin Bus**:
  - Add custom tools in minutes by placing a single Python file in `tools/custom/<name>_plugin.py`.
  - Declare `TOOL_TAB` to instantly mount custom navigation tabs rendering HTML5 dashboards, SVGs, canvas charts, and interactive action buttons.
  - Granular coordinator approval workflows (`approve_writes`, `sandbox_auto`, `full_harness`).

- **🎨 Themes & Template Customization**:
  - Customizable UI themes (`robots`, `christmas`, `workshop`, `classic`) and motion styles.
  - Parameterized prompt and message templates (`automation_engine.py`) for automated loops and multi-agent QA gates.
  - Configurable model routes and role bindings (`custom_models.json`).

- **🛡️ Pluggable Workspace Harnesses**:
  - Native harness updates with staged proposals, behavioral probes, and rollback capability.
  - Support for MCP (Model Context Protocol) tool buses and external CLI adapters.

---

## 🚀 Quickstart

### 1. Prerequisites
- Windows 10/11
- Python 3.10+
- An API key for your chosen model provider (e.g., Google Gemini, Anthropic, OpenAI)

### 2. Setup
Clone the repository and configure your environment:
```powershell
# Copy the sample environment configuration
Copy-Item .env.example .env

# Edit .env to set your model provider and API key
notepad .env
```

### 3. Launching the Console
Launch the operator desktop console:
```powershell
.\Launch_Sabrina_Console.bat
```
Or launch via Python:
```powershell
python sabrina_console_desktop/launch_console.py
```

---

## 🧩 Adding a Custom Plugin

Creating a plugin is as simple as defining a module-level contract:

1. Copy `sabrina_console_desktop/plugin_template/sample_plugin.py.template` to `tools/custom/my_tool_plugin.py`.
2. Define the tool metadata:
```python
TOOL_NAME = "my_tool"
TOOL_PROMPT = "Operations: status | run. Fields: query (string). Access: read."
PARALLEL_SAFE = True
TOOL_ACCESS = "read"

# Optional: Add an interactive UI tab in the desktop console
TOOL_TAB = {
    "title": "My Tool",
    "icon": "⚡",
    "operation": "status",
    "display": "dashboard",  # "dashboard", "html", "json", or "text"
    "refresh_seconds": 10
}

def execute(action, loop=None):
    if action.get("operation") == "status":
        return {"ok": True, "status": "operational", "html": "<div><h3>Tool Ready</h3></div>"}
    return {"ok": False, "error": "unsupported_operation"}
```
3. Run `/plugin list` in the console chat to verify discovery.

---

## 📜 License

This project is licensed under the **Business Source License 1.1 (BSL 1.1)**. 
- **Free Use**: You are free to use, modify, and self-host Sabrina Console for personal, research, and non-commercial internal evaluation.
- **Commercial Restrictions**: Commercial hosting or providing Sabrina Console as a managed paid service requires a commercial license.
- **Open Source Transition**: Converts automatically to the open-source **Apache License 2.0** on **November 26, 2028**.

See [LICENSE](file:///LICENSE) for complete terms.
