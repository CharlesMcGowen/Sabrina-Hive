# Changing Sabrina’s harness

Use this workflow when the user asks to change how Sabrina’s Workspace Agent runs. A harness is the set of instructions, tools, and execution rules around the model. The workspace harness selector controls Workspace Agent chat. Native harness revisions apply to Sabrina Native; other adapters use their registered implementations. Swarm mission orchestration lives in `sabrina_console_desktop/live_swarm_runner.py`.

## Choose a workspace harness

Open the Workspace panel in Sabrina Console, attach a project, and choose a harness. The selection is saved for the next launch. Send a message in **Workspace Agent** chat to use the selected adapter.

| Selector | What a workspace chat turn does |
| --- | --- |
| Sabrina Native Harness | Runs the workspace tool loop with the active native profile and current workspace permissions. |
| MCP Tool-Bus Harness | Runs workspace diagnostics. Programmatic turns can also pass calls to registered MCP tools. |
| External CLI Harness Adapter | Runs diagnostics with the stock configuration. An operator-configured CLI requires the existing workspace execution policy and local runtime opt-in. |
| MLX / Ego Portal | Calls the configured portal and can report local diagnostics when the portal is unavailable. |

## Propose, probe, and compose a native revision

1. **Propose.** Identify the expected behavior and record the active revision. Stage a small change to the additional workspace instructions, tool-loop limit, or JSON handoff setting. Include the behavior you expect to improve in the reason. For instruction changes, keep the model route constant while gathering evidence.
2. **Probe.** Run the paired fixture against the base and candidate. Check that both complete a fixture read, block a write in read-only mode, load their own instructions, and honor their respective turn limits. The fixture uses scripted model responses in temporary workspaces. Use representative tasks in disposable workspaces when the change is intended to improve model behavior, and keep a short record of the task, revision, and observed result. The `show` command retrieves stored fixture evidence for any revision ID.
3. **Compose.** If several ideas were tried, stage a new revision with the evidence-supported changes and probe the combined revision on its own. If the base changed, stage the proposal against the current revision and rerun the probe.
4. **Activate or roll back.** In the Workspace panel, open **Native harness updates**, refresh, select the revision, and review the preview. **Activate revision** becomes available after a passing probe on the current base. **Roll back** restores the previous revision and its settings. The next native conversation uses the active profile. An existing conversation with open file approvals keeps its profile until those approvals are resolved.

### Commands for a future AI

Run these commands from the Sabrina source directory. The existing Windows launcher uses this Python environment. The commands emit JSON. Use `--state` before the subcommand to point to an isolated database when experimenting.

```powershell
$python = '.\freqtrade_pivot\.venv\Scripts\python.exe'
$state = & $python -m swarm_console.harness_updates status | ConvertFrom-Json
$revision = & $python -m swarm_console.harness_updates propose `
    --base $state.active.revision `
    --instructions .\candidate-instructions.md `
    --reason 'Inspect the affected files before proposing any edit.' | ConvertFrom-Json
if ($revision.ok) {
    & $python -m swarm_console.harness_updates probe $revision.revision
    & $python -m swarm_console.harness_updates show $revision.revision
}
```

Create `candidate-instructions.md` with the **complete additional instructions** before running the example. Omit `--instructions` to retain the current instructions when using `--max-turns`. Return the candidate ID, anticipated effect, and probe findings to the operator so they can review it in the Console.

Enable **Finalize requested JSON handoffs** in the Console or pass `--json-handoff` when staging a revision. A direct request such as “Return one JSON object” then reserves one call within the turn limit. Sabrina sends the request and observed tool results to a final model call with JSON output and validates the resulting object before returning it.

For a model-backed comparison, load each revision with `HarnessUpdates.get(revision)` and construct a `NativeHarnessProfile` from its fields after removing `probe`. Use `NativeHarnessProfile.create_agent` with a fresh `WorkspaceSandbox` for each revision. Give each agent identical project inputs, model, thinking level, tool policy, and task. Compare task outcomes and tool calls before bringing the candidate to the Console.

## Import a Harness² candidate

Run Google Research’s Harness² in an evaluation environment with a representative disposable task. Its upstream benchmark setup targets Linux with Python 3.12. Review the selected candidate’s source tree and rollout evidence before importing its instructions. The importer reads `systemprompt.md` for OpenCode or `AGENTS.md` for Codex. Sabrina appends the imported text to its built-in workspace instructions. Other Harness² components require a source-level adapter change. [Harness² setup and requirements](https://github.com/google-research/harness2/blob/6868817518876de8fd170c6a8354daca273b6191/docs/SETUP.md)

```powershell
$state = & $python -m swarm_console.harness_updates status | ConvertFrom-Json
$revision = & $python -m swarm_console.harness_updates propose `
    --base $state.active.revision `
    --harness2-tree '.\path-to-a-harness2-candidate' `
    --substrate opencode `
    --reason 'Record which Harness² task and behavior support this prompt.' | ConvertFrom-Json
if ($revision.ok) {
    & $python -m swarm_console.harness_updates probe $revision.revision
}
```

If the prompt exceeds the native editor’s limit, extract and review the instructions relevant to Sabrina, then stage them with `--instructions`. Reuse the usual review and activation steps for the resulting revision.

## Add or change an adapter in code

- Start with `swarm_console/harness_collective.py`. Implement `BaseHarness.step` using the existing session policy, and emit `HarnessEvent` items that report errors and a final summary. Register the implementation in `HarnessRegistry` with a stable ID and accurate capability labels. The desktop selector reads the registry at runtime.
- Use `WorkspaceSandbox` tool policy and preserve the existing workspace approval flow. Keep data-only native revisions in `swarm_console/harness_updates.py`. Use source review and adapter tests for changes to tools, commands, permission handling, memory, or plugins.
- Exercise the real entry point `SabrinaDesktopBridge.send_chat(..., "workspace_agent", ...)` with the new adapter selected. Verify that a native file approval survives switching away and back. The harness update regression tests live in `sabrina_console_desktop/test_harness_updates.py`.
- Persisted source-launcher state is in `sabrina_console_desktop/data/control/harness_updates.sqlite3`. The packaged Windows runtime uses `%LOCALAPPDATA%\SabrinaConsole\control\harness_updates.sqlite3`. Use the Console to change the active pointer so fixture evidence and base-revision checks are enforced.
