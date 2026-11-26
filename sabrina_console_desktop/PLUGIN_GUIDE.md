# Sabrina Console plugins

This guide is for people adding a plugin. For running plugins from the console,
see "Using plugins" at the end.

## Add a plugin in five steps

1. Copy `plugin_template/sample_plugin.py.template` to
   `tools/custom/<name>_plugin.py`. Use a lowercase name with letters, digits,
   and underscores. Delete the instruction docstring when you are done.
2. Set `TOOL_NAME` to the same name, without the `_plugin` suffix. It must be
   unique across all plugins.
3. Write `TOOL_PROMPT` in this form:
   `Operations: status | snapshot. Fields: pair (string). Access: read. Returns: JSON object with ok.`
4. Write `execute(action, loop=None)`. Validate the action first. Return a
   JSON-compatible dict, with `"ok": False` and an `error` code on failure.
5. Add a unit test under `sabrina_console_desktop/test_<name>_plugin.py` that
   calls `execute` directly. Then run it with the project venv.

## Required and optional keys

Every key must be a literal at module level, because the console reads them
without importing the plugin.

| Key | Required | Meaning |
| --- | --- | --- |
| `TOOL_NAME` | yes | Lowercase identifier. Unique. |
| `TOOL_PROMPT` | recommended | The operations, fields, access, and return shape. |
| `PARALLEL_SAFE` | recommended | `True` if concurrent calls are safe. |
| `TOOL_ACCESS` | for tabs | `"read"` or `"write"`. A plugin with a tab must be `"read"`. |
| `TOOL_TAB` | no | Adds a nav entry in the console. See below. |

`TOOL_TAB` keys: `title` (1-40 characters, required), `operation` (a declared
operation, required), `display` (`"json"`, `"text"`, `"html"`, or `"dashboard"`,
required), `icon` (1-4 characters, optional), `refresh_seconds` (`0` for manual
refresh only, or 5-300, optional). Any other key is a discovery error.

When `display` is `"html"` or `"dashboard"`, a read-only plugin may return custom
layout markup in `result["html"]` (HTML5 cards, tables, scoped `<style>`, `<svg>`,
and `<canvas data-plugin-chart="...">`) alongside optional structured data such as
`result["chart_spec"]`. Both `plugin_console.sanitize_plugin_html` and the
Console's `DOMParser` allowlist sanitizer strip `<script>`, `<iframe>`, unsafe URLs,
and inline `on*` handlers while binding declarative `data-plugin-op` /
`data-plugin-args` buttons to validated read-only plugin operations.

## Checklist before you ship

- `execute` rejects any action that is not a JSON object.
- Unknown operations return `{"ok": false, "error": "unsupported_operation_or_fields"}`.
- Read-only plugins do not write files, databases, or remote state.
- Output contains no secrets, keys, or tokens. Credentials are read from their
  own store and never echoed.
- Remote calls have timeouts.
- `TOOL_PROMPT` matches the operations that exist.
- A unit test covers one success case and one rejected action.

## Using plugins

- Send `/plugin list` to see plugins and discovery errors.
- Send `/plugin info <name>` to read one plugin's metadata.
- Send `/plugin run <name> {"operation":"status"}` to run an operation.
- A plugin with a `TOOL_TAB` also has its own nav entry in the console.
  Refresh runs the declared operation again.

Each run executes the plugin's Python as the desktop user, so review the source
before you run it. Discovery reads metadata only and does not run plugin code.
A changed source file is rediscovered before it runs. A duplicate `TOOL_NAME`
makes that plugin unavailable until the duplicate is fixed.

Operator chat commands run plugins when an operator sends them. Missions and
the Workspace Agent use the coordinator request paths described below.

## Workspace Agent plugin requests

In the workspace controls, select `full_harness` for the policy and use
`sandbox_auto` when coordinator-approved plugin requests may run without a
separate operator prompt. Ask the Workspace Agent and include an explicit
capability marker in the message, for example:

    Check BTCUSD's public quote. [plugins:kraken_console.quote]

For several capabilities, separate them with commas inside one marker. The
coordinator checks each requested action against the configured operation,
current source hash, exact arguments and current turn. The decision uses
deterministic coordinator policy. Each grant is single-use.

In `approve_writes` mode, a plugin request enters the existing workspace
approval queue. The operator can approve or reject it, and it remains pending
when switching harnesses. `read_only` and `sandboxed_edit` do not run these
host-executed plugins. Per-operation requirements determine whether network
access is needed. Plugin code executes with the console user's permissions,
so `full_harness` is required for the host-process capability.

## Mission coordinator access

In Launch & Chat, select the permitted plugin operations before launching a
mission. Workers can request an operation in their workstream. The coordinator
applies the operator-selected mission scope and the policy in
`data/control/coordinator_plugin_policy.json`, and records a deterministic
grant or denial. Each grant is bound to the mission, worker, plugin source hash,
action and arguments. It is single-use and expires. Stopping the mission
revokes pending grants. A call already running may finish.

The built-in policy permits Kraken public quotes, Kraken managed status and
selected private account reads, and Polymarket US public market data.
Selecting a network capability permits its plugin to contact the relevant
service even when the separate web-grounding tool is unavailable. Plugin
results are sent to the configured worker model. Select operations only when
you intend to share their results with that provider.

To authorize a user-created plugin, review its code and its dependencies, add a
unique entry under `plugins` in the policy, pin the plugin's current SHA-256
and define each operation's classification, required fields and bounded
`fields` schema. The supported field types are `string` with a regular
expression `pattern`, and `integer` with `min` and `max`. Use
`classification: "public"` for public network data, `"local"` for a
trusted local operation, `"private"` for account reads, or `"financial"`
for an order-capable operation. List optional `requires` values as `network` or
`read`. Policy updates and plugin source changes invalidate in-flight grants.

Plugins execute as the desktop user. The policy pins the plugin source file
but does not sandbox code or pin imported dependencies. A user-configured
local plugin can access resources available to that user. Review its behavior
and provider data sharing before granting access. An explicitly selected private
operation may send account information to the configured worker model provider.
The classification only controls access to the selected action. It does not
validate a strategy, enforce account risk limits, or authorize a live trading
service by itself. The bundled Kraken plugin currently offers no order placement
operation.

Kraken private balance and order reads require a dedicated read-only key on Ego
in `~/.config/sabrina/kraken-plugin-readonly.env`. The plugin verifies its
permissions and refuses either managed trader's key to avoid nonce conflicts.
The selected key's account identity is not independently verified. This key is
separate from the running trader and must be dedicated to the plugin.
