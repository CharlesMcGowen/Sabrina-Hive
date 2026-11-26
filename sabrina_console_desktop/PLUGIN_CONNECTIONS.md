# Plugin app connections

An installed plugin can declare a connection with literal metadata. Sabrina discovers it without importing the plugin and displays it under **App Connections**:

```python
TOOL_CONNECTION = {
    "title": "Example service",
    "description": "Enter the service endpoint and credentials.",
    "fields": [
        {"key": "endpoint", "label": "Webhook URL", "kind": "url", "required": True},
        {"key": "api_key", "label": "API key", "secret": True, "required": True},
    ],
}
```

Each field has a unique lowercase identifier, a label, optional `kind` (`text` or `url`), `placeholder`, `default`, and boolean `required` and `secret` flags. Secret fields must set `secret: True` and cannot have defaults. Use secret fields for all credentials, including webhook URLs that themselves contain a token. Plugin authors should never include credentials in actions, prompts, responses, exceptions, command-line arguments, or logs.

Trusted plugin code can read its own settings via `ConnectorRegistry().get_plugin_config(TOOL_NAME)`. The vault is encrypted with Windows DPAPI for the current Windows user; the settings page returns only masked secret status, and an empty secret input preserves the saved value. This API is a convenience namespace, not a sandbox: plugins run as the desktop user and must be trusted. Coordinator permission to run an action remains separate from saving a connection.

For the bundled Kraken plugin, create a **dedicated read-only** Kraken API key and secret and enter both under **App Connections → Kraken read-only API**. Do not reuse the keys used by an active trading service. The plugin passes them to Ego through SSH standard input only when a private read is requested. Its remote checks must also succeed before account reads work; the account identity is not independently verified. Saving credentials does not enable trading or grant permission to workers.
