"""DPAPI-encrypted connector registry & action executor for Sabrina Console Automations."""
from __future__ import annotations

import json
import os
import subprocess
import threading
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from swarm_console.vault import ProviderKeyVault, dpapi_decrypt, dpapi_encrypt

try:
    from sabrina_console_desktop.sabrina_config import FORGEJO_URL as _FORGEJO_DEFAULT_URL
except ImportError:
    try:
        from sabrina_config import FORGEJO_URL as _FORGEJO_DEFAULT_URL  # type: ignore[no-redef]
    except ImportError:
        _FORGEJO_DEFAULT_URL = "http://127.0.0.1:3003"

DEFAULT_CONNECTOR_VAULT_PATH = Path(__file__).resolve().parent / "data" / "connectors.dpapi.json"


@dataclass(frozen=True)
class ConnectorField:
    key: str
    label: str
    secret: bool = False
    required: bool = True
    placeholder: str = ""
    default: str = ""
    options: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ConnectorAction:
    name: str
    label: str
    description: str
    kind: str = "read"  # "read" | "write"
    approval_required: bool = False
    default_params: tuple[tuple[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["default_params"] = dict(self.default_params)
        return d


@dataclass(frozen=True)
class ConnectorSpec:
    id: str
    name: str
    category: str  # "ai" | "finance" | "retail" | "mcp" | "opensource"
    description: str
    fields: tuple[ConnectorField, ...]
    actions: tuple[ConnectorAction, ...]
    uses_provider_vault: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "category": self.category,
            "description": self.description,
            "uses_provider_vault": self.uses_provider_vault,
            "fields": [f.to_dict() for f in self.fields],
            "actions": [a.to_dict() for a in self.actions],
        }


CONNECTOR_SPECS: tuple[ConnectorSpec, ...] = (
    ConnectorSpec(
        id="gemini",
        name="Google Gemini",
        category="ai",
        description="Use Google Gemini (via your DPAPI API Key Vault) for reasoning, classification, structured JSON extraction, and goal evaluation.",
        uses_provider_vault="gemini",
        fields=(
            ConnectorField("default_model", "Default Model", secret=False, required=False, default="gemini-3.5-flash-lite", placeholder="gemini-3.5-flash-lite"),
        ),
        actions=(
            ConnectorAction("generate", "Generate / Summarize", "Run a prompt with previous step context.", kind="read", approval_required=False),
            ConnectorAction("classify_json", "Classify / Extract JSON", "Extract structured JSON fields from step output.", kind="read", approval_required=False),
            ConnectorAction("evaluate_goal", "Evaluate Loop Goal", "Return goal_met=true/false for goal-until-done loops.", kind="read", approval_required=False),
        ),
    ),
    ConnectorSpec(
        id="plaid",
        name="Plaid (Banking & Finance)",
        category="finance",
        description="Connect bank accounts via Plaid Link or Sandbox to monitor balances, sync transactions, and flag anomalies.",
        fields=(
            ConnectorField("environment", "Environment", secret=False, required=True, default="sandbox", options=("sandbox", "development", "production")),
            ConnectorField("client_id", "Client ID", secret=False, required=True, placeholder="Plaid client_id"),
            ConnectorField("secret", "Secret", secret=True, required=True, placeholder="Plaid secret"),
            ConnectorField("access_token", "Linked Access Token", secret=True, required=False, placeholder="access-sandbox-... (auto-filled by Plaid Link)"),
        ),
        actions=(
            ConnectorAction("accounts.balance", "Get Account Balances", "Fetch real-time balances for linked bank accounts.", kind="read", approval_required=False),
            ConnectorAction("transactions.sync", "Sync Transactions", "Fetch recent bank transactions.", kind="read", approval_required=False, default_params=(("count", 25),)),
            ConnectorAction("recurring.list", "List Recurring Streams", "Inspect recurring inflow/outflow subscriptions.", kind="read", approval_required=False),
        ),
    ),
    ConnectorSpec(
        id="shopify",
        name="Shopify (Retail Store)",
        category="retail",
        description="Monitor orders, products, and inventory levels on a Shopify storefront.",
        fields=(
            ConnectorField("shop_domain", "Shop Domain", secret=False, required=True, placeholder="mystore.myshopify.com"),
            ConnectorField("access_token", "Admin API Token", secret=True, required=True, placeholder="shpat_..."),
        ),
        actions=(
            ConnectorAction("orders.list", "List Recent Orders", "Fetch open or recent orders.", kind="read", approval_required=False, default_params=(("limit", 20), ("status", "any"))),
            ConnectorAction("products.list", "List Products", "Fetch storefront product catalog.", kind="read", approval_required=False, default_params=(("limit", 20),)),
            ConnectorAction("order.note", "Update Order Note", "Append an internal note to a Shopify order.", kind="write", approval_required=True),
        ),
    ),
    ConnectorSpec(
        id="woocommerce",
        name="WooCommerce (Open-Source Retail)",
        category="retail",
        description="Open-source WordPress/WooCommerce store integration via REST API.",
        fields=(
            ConnectorField("store_url", "Store URL", secret=False, required=True, placeholder="https://shop.example.com"),
            ConnectorField("consumer_key", "Consumer Key", secret=True, required=True, placeholder="ck_..."),
            ConnectorField("consumer_secret", "Consumer Secret", secret=True, required=True, placeholder="cs_..."),
        ),
        actions=(
            ConnectorAction("orders.list", "List Orders", "Fetch recent WooCommerce orders.", kind="read", approval_required=False, default_params=(("per_page", 20),)),
            ConnectorAction("products.list", "List Products", "Fetch WooCommerce products.", kind="read", approval_required=False, default_params=(("per_page", 20),)),
            ConnectorAction("order.update", "Update Order Status", "Modify an order status (requires approval).", kind="write", approval_required=True),
        ),
    ),
    ConnectorSpec(
        id="stripe",
        name="Stripe (Payments & Retail)",
        category="retail",
        description="Track charges, payouts, and customer payments from Stripe.",
        fields=(
            ConnectorField("secret_key", "Secret Key", secret=True, required=True, placeholder="sk_test_... or sk_live_..."),
        ),
        actions=(
            ConnectorAction("payments.list", "List Payment Intents", "Fetch recent Stripe payment intents.", kind="read", approval_required=False, default_params=(("limit", 20),)),
            ConnectorAction("payouts.list", "List Payouts", "Fetch recent Stripe payouts.", kind="read", approval_required=False, default_params=(("limit", 10),)),
            ConnectorAction("refund.create", "Create Refund", "Issue a refund (approval gated).", kind="write", approval_required=True),
        ),
    ),
    ConnectorSpec(
        id="mcp",
        name="MCP Server (Universal Bus)",
        category="mcp",
        description="Connect any open-source Model Context Protocol (MCP) server via stdio or HTTP/SSE.",
        fields=(
            ConnectorField("server_name", "Server Name", secret=False, required=True, placeholder="e.g. github-mcp, postgres-mcp"),
            ConnectorField("transport", "Transport", secret=False, required=True, default="http", options=("http", "stdio")),
            ConnectorField("endpoint_or_cmd", "HTTP URL or Command", secret=False, required=True, placeholder="http://127.0.0.1:8765/mcp or npx -y @modelcontextprotocol/server-filesystem"),
            ConnectorField("auth_token", "Bearer Token / Env Secret", secret=True, required=False, placeholder="Optional auth token"),
        ),
        actions=(
            ConnectorAction("tools.list", "List MCP Tools", "Discover tools exposed by the MCP server.", kind="read", approval_required=False),
            ConnectorAction("tools.call_read", "Call MCP Tool (Read)", "Invoke a read-only MCP tool.", kind="read", approval_required=False),
            ConnectorAction("tools.call_write", "Call MCP Tool (Write)", "Invoke a mutating MCP tool (approval gated).", kind="write", approval_required=True),
        ),
    ),
    ConnectorSpec(
        id="webhook",
        name="REST / Webhook (Open-Source)",
        category="opensource",
        description="Call any local or external REST endpoint, n8n webhook, or open-source service.",
        fields=(
            ConnectorField("base_url", "Base / Webhook URL", secret=False, required=True, placeholder="http://127.0.0.1:5678/webhook/..."),
            ConnectorField("auth_header", "Authorization Header", secret=True, required=False, placeholder="Bearer ..."),
        ),
        actions=(
            ConnectorAction("http.get", "HTTP GET", "Fetch JSON/text from the configured URL.", kind="read", approval_required=False),
            ConnectorAction("http.post", "HTTP POST", "Send JSON payload to the webhook (approval gated).", kind="write", approval_required=True),
        ),
    ),
    ConnectorSpec(
        id="searxng",
        name="SearXNG (Open-Source Search)",
        category="opensource",
        description="Privacy-respecting metasearch using your local SearXNG instance.",
        fields=(
            ConnectorField("base_url", "SearXNG URL", secret=False, required=True, default="http://127.0.0.1:8888", placeholder="http://127.0.0.1:8888"),
        ),
        actions=(
            ConnectorAction("web.search", "Search Web", "Run a JSON search query against SearXNG.", kind="read", approval_required=False),
            ConnectorAction("docs.search", "Search Docs & Science", "Run a query targeting documentation, arXiv, and scientific literature.", kind="read", approval_required=False),
        ),
    ),
    ConnectorSpec(
        id="crawl4ai",
        name="Crawl4AI (Open-Source Scraper)",
        category="opensource",
        description="Extract clean markdown and structured data from web pages via local Crawl4AI.",
        fields=(
            ConnectorField("base_url", "Crawl4AI URL", secret=False, required=True, default="http://127.0.0.1:11235", placeholder="http://127.0.0.1:11235"),
            ConnectorField("api_token", "API Token", secret=True, required=False, placeholder="Optional Crawl4AI token"),
        ),
        actions=(
            ConnectorAction("page.crawl", "Crawl URL", "Fetch and extract page content.", kind="read", approval_required=False),
        ),
    ),
    ConnectorSpec(
        id="home_assistant",
        name="Home Assistant (Open-Source)",
        category="opensource",
        description="Automate smart-home sensors and services via Home Assistant REST API.",
        fields=(
            ConnectorField("base_url", "Home Assistant URL", secret=False, required=True, placeholder="http://homeassistant.local:8123"),
            ConnectorField("access_token", "Long-Lived Token", secret=True, required=True, placeholder="eyJ..."),
        ),
        actions=(
            ConnectorAction("states.get", "Get Entity States", "Read sensor/device states.", kind="read", approval_required=False),
            ConnectorAction("service.call", "Call Service", "Trigger a Home Assistant service (approval gated).", kind="write", approval_required=True),
        ),
    ),
    ConnectorSpec(
        id="rss",
        name="RSS / Atom Feed",
        category="opensource",
        description="Poll any open-source RSS or Atom feed for new entries.",
        fields=(
            ConnectorField("feed_url", "Feed URL", secret=False, required=True, placeholder="https://example.com/feed.xml"),
        ),
        actions=(
            ConnectorAction("feed.poll", "Poll Feed Entries", "Fetch latest items from the RSS/Atom feed.", kind="read", approval_required=False, default_params=(("limit", 10),)),
        ),
    ),
    ConnectorSpec(
        id="forgejo",
        name="Forgejo (Git, PyPI & Artifact Registry)",
        category="opensource",
        description="Self-hosted Forgejo instance for Git sync, PyPI wheels, OCI images, and RSI artifacts.",
        fields=(
            ConnectorField("base_url", "Forgejo URL", secret=False, required=True, default=_FORGEJO_DEFAULT_URL, placeholder="http://192.168.1.133:3003"),
            ConnectorField("owner", "Owner / Org", secret=False, required=True, default="ego", placeholder="ego"),
            ConnectorField("api_token", "API Access Token", secret=True, required=True, placeholder="Forgejo Personal Access Token"),
            ConnectorField("repo_name", "Default Repository", secret=False, required=False, default="sabrina", placeholder="sabrina"),
        ),
        actions=(
            ConnectorAction("version.get", "Check Forgejo Version", "Probe Forgejo /api/v1/version endpoint.", kind="read", approval_required=False),
            ConnectorAction("packages.list", "List Packages", "List hosted PyPI and Generic packages.", kind="read", approval_required=False),
            ConnectorAction("repos.list", "List Repositories", "List repositories for the configured user.", kind="read", approval_required=False),
            ConnectorAction("branches.list", "List Branches", "List branches in the Forgejo repository.", kind="read", approval_required=False),
            ConnectorAction("pulls.list", "List Pull Requests", "List pull requests in the Forgejo repository.", kind="read", approval_required=False),
            ConnectorAction("pulls.diff", "Get Pull Request Diff", "Fetch unified diff for a pull request.", kind="read", approval_required=False),
            ConnectorAction("comments.list", "List Comments", "List comments on a Forgejo PR or issue.", kind="read", approval_required=False),
            ConnectorAction("branches.create", "Create Branch", "Create an isolated patch branch in Forgejo.", kind="write", approval_required=True),
            ConnectorAction("files.patch", "Commit File Patch", "Commit code changes to a feature branch in Forgejo.", kind="write", approval_required=True),
            ConnectorAction("pulls.create", "Create Pull Request", "Open a pull request from a patch branch.", kind="write", approval_required=True),
            ConnectorAction("comments.create", "Post Code Comment", "Post a comment on a Forgejo PR or issue.", kind="write", approval_required=True),
            ConnectorAction("reviews.create", "Submit PR Code Review", "Submit line-level PR review comments in Forgejo.", kind="write", approval_required=True),
            ConnectorAction("pypi.sync_wheelhouse", "Sync Local Wheelhouse", "Push local *.whl artifacts to Forgejo PyPI registry.", kind="write", approval_required=True),
            ConnectorAction("generic.publish_rsi", "Publish RSI Artifacts", "Publish Evo-Bench scorecards and mission reports.", kind="write", approval_required=True),
        ),
    ),
)

CONNECTOR_MAP: dict[str, ConnectorSpec] = {spec.id: spec for spec in CONNECTOR_SPECS}


def _http_request_json(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    payload: dict[str, Any] | None = None,
    timeout: float = 12.0,
) -> dict[str, Any]:
    req_headers = {"Accept": "application/json", **(headers or {})}
    data_bytes = None
    if payload is not None:
        data_bytes = json.dumps(payload).encode("utf-8")
        req_headers.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data_bytes, headers=req_headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            try:
                parsed = json.loads(raw) if raw.strip() else {}
            except json.JSONDecodeError:
                parsed = {"text": raw}
            return {"ok": True, "status": resp.status, "data": parsed}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        return {"ok": False, "status": exc.code, "error": body or str(exc)}
    except Exception as exc:
        return {"ok": False, "status": 0, "error": str(exc)}


class ConnectorRegistry:
    """Manages DPAPI-encrypted connector credentials, probes, and action execution."""

    def __init__(
        self,
        vault_path: Path | None = None,
        provider_vault: ProviderKeyVault | None = None,
    ) -> None:
        self.vault_path = Path(vault_path) if vault_path else DEFAULT_CONNECTOR_VAULT_PATH
        self.vault_path.parent.mkdir(parents=True, exist_ok=True)
        self.provider_vault = provider_vault or ProviderKeyVault()
        self._lock = threading.RLock()

    def _read_store(self) -> dict[str, dict[str, Any]]:
        if not self.vault_path.exists():
            return {}
        try:
            raw = json.loads(self.vault_path.read_text(encoding="utf-8"))
            return raw if isinstance(raw, dict) else {}
        except Exception:
            return {}

    def _write_store(self, data: dict[str, dict[str, Any]]) -> None:
        self.vault_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.vault_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(self.vault_path)

    def _encrypt(self, value: str) -> str:
        if os.name == "nt":
            return dpapi_encrypt(value)
        return "plain:" + value

    def _decrypt(self, value: str) -> str:
        if value.startswith("plain:"):
            return value[6:]
        return dpapi_decrypt(value)

    def _plugin_spec(self, name: str) -> dict[str, Any]:
        from tools.plugin_console import discover
        plugins, _ = discover()
        plugin = plugins.get(name)
        if not plugin or not plugin.get("connection"):
            raise ValueError("Unknown plugin connection")
        return plugin["connection"]

    def get_plugin_config(self, name: str) -> dict[str, str]:
        """Return credentials to trusted plugin code; never return this to the UI."""
        spec = self._plugin_spec(name)
        with self._lock:
            entry = self._read_store().get("plugin:" + name, {})
        result = {}
        for field in spec["fields"]:
            value = entry.get(field["key"], "")
            if value and field["secret"]:
                if os.name != "nt" or str(value).startswith("plain:"):
                    raise ValueError("Plugin secrets require Windows DPAPI")
                value = self._decrypt(str(value))
            result[field["key"]] = str(value or field.get("default", ""))
        return result

    def list_plugin_connections(self) -> list[dict[str, Any]]:
        from tools.plugin_console import discover
        plugins, _ = discover()
        items = []
        for name, plugin in sorted(plugins.items()):
            spec = plugin.get("connection")
            if not spec:
                continue
            config = self.get_plugin_config(name)
            masked = {f["key"]: ("••••••••" if config[f["key"]] else "")
                      if f["secret"] else config[f["key"]] for f in spec["fields"]}
            items.append({"id": name, **spec, "masked_config": masked,
                          "configured": all(config[f["key"]] for f in spec["fields"] if f["required"])})
        return items

    def save_plugin_config(self, name: str, values: dict[str, Any]) -> None:
        spec = self._plugin_spec(name)
        if not isinstance(values, dict) or set(values) - {f["key"] for f in spec["fields"]}:
            raise ValueError("Invalid connection fields")
        if os.name != "nt":
            raise ValueError("Plugin connections require Windows DPAPI")
        with self._lock:
            store = self._read_store()
            entry = dict(store.get("plugin:" + name, {}))
            for field in spec["fields"]:
                key = field["key"]
                if key not in values:
                    continue
                value = values[key]
                if not isinstance(value, str) or len(value) > 8192:
                    raise ValueError("Invalid connection value")
                value = value.strip()
                if field["secret"]:
                    if value and value != "••••••••":
                        entry[key] = self._encrypt(value)
                elif value:
                    entry[key] = value
                else:
                    entry.pop(key, None)
            entry["updated_at"] = datetime.now(timezone.utc).isoformat()
            store["plugin:" + name] = entry
            self._write_store(store)

    def get_config(self, connector_id: str) -> dict[str, str]:
        spec = CONNECTOR_MAP.get(connector_id)
        if not spec:
            raise ValueError(f"Unknown connector: {connector_id}")
        with self._lock:
            store = self._read_store()
            entry = store.get(connector_id, {})
        result: dict[str, str] = {}
        for field in spec.fields:
            raw_val = entry.get(field.key, "")
            if raw_val and field.secret:
                try:
                    result[field.key] = self._decrypt(str(raw_val))
                except Exception:
                    result[field.key] = ""
            elif raw_val:
                result[field.key] = str(raw_val)
            else:
                result[field.key] = field.default
        return result

    def save_config(self, connector_id: str, values: dict[str, Any]) -> dict[str, Any]:
        spec = CONNECTOR_MAP.get(connector_id)
        if not spec:
            raise ValueError(f"Unknown connector: {connector_id}")
        with self._lock:
            store = self._read_store()
            existing = store.get(connector_id, {})
            updated: dict[str, Any] = dict(existing)
            for field in spec.fields:
                if field.key not in values:
                    continue
                val = str(values[field.key] or "").strip()
                if not val:
                    if not field.secret or values.get(f"__clear_{field.key}"):
                        updated.pop(field.key, None)
                    continue
                if field.secret:
                    if val == "••••••••":
                        continue
                    updated[field.key] = self._encrypt(val)
                else:
                    updated[field.key] = val
            updated["updated_at"] = datetime.now(timezone.utc).isoformat()
            store[connector_id] = updated
            self._write_store(store)
        return {"ok": True, "connector_id": connector_id}

    def delete_config(self, connector_id: str) -> dict[str, Any]:
        with self._lock:
            store = self._read_store()
            existed = connector_id in store
            store.pop(connector_id, None)
            self._write_store(store)
        return {"ok": True, "connector_id": connector_id, "deleted": existed}

    def is_configured(self, connector_id: str) -> bool:
        spec = CONNECTOR_MAP.get(connector_id)
        if not spec:
            return False
        if spec.uses_provider_vault:
            return bool(self.provider_vault.get_key(spec.uses_provider_vault))
        cfg = self.get_config(connector_id)
        for field in spec.fields:
            if field.required and not cfg.get(field.key, "").strip():
                return False
        return True

    def list_connectors(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for spec in CONNECTOR_SPECS:
            cfg = self.get_config(spec.id)
            masked: dict[str, str] = {}
            for field in spec.fields:
                val = cfg.get(field.key, "")
                if field.secret:
                    masked[field.key] = "••••••••" if val else ""
                else:
                    masked[field.key] = val
            d = spec.to_dict()
            d["configured"] = self.is_configured(spec.id)
            d["masked_config"] = masked
            items.append(d)
        return items

    def get_action_spec(self, connector_id: str, action_name: str) -> ConnectorAction | None:
        spec = CONNECTOR_MAP.get(connector_id)
        if not spec:
            return None
        for act in spec.actions:
            if act.name == action_name:
                return act
        return None

    def _plaid_base_url(self, env: str) -> str:
        mapping = {
            "sandbox": "https://sandbox.plaid.com",
            "development": "https://development.plaid.com",
            "production": "https://production.plaid.com",
        }
        return mapping.get((env or "sandbox").lower(), "https://sandbox.plaid.com")

    def plaid_create_link_token(self, user_id: str = "sabrina-operator") -> dict[str, Any]:
        cfg = self.get_config("plaid")
        if not cfg.get("client_id") or not cfg.get("secret"):
            return {"ok": False, "error": "Plaid client_id and secret must be configured first."}
        base = self._plaid_base_url(cfg.get("environment", "sandbox"))
        return _http_request_json(
            f"{base}/link/token/create",
            method="POST",
            payload={
                "client_id": cfg["client_id"],
                "secret": cfg["secret"],
                "user": {"client_user_id": user_id},
                "client_name": "Rogue Colony",
                "products": ["transactions"],
                "country_codes": ["US"],
                "language": "en",
            },
        )

    def plaid_exchange_public_token(self, public_token: str) -> dict[str, Any]:
        cfg = self.get_config("plaid")
        if not cfg.get("client_id") or not cfg.get("secret"):
            return {"ok": False, "error": "Plaid client_id and secret must be configured first."}
        base = self._plaid_base_url(cfg.get("environment", "sandbox"))
        res = _http_request_json(
            f"{base}/item/public_token/exchange",
            method="POST",
            payload={
                "client_id": cfg["client_id"],
                "secret": cfg["secret"],
                "public_token": public_token,
            },
        )
        if res.get("ok") and isinstance(res.get("data"), dict) and res["data"].get("access_token"):
            self.save_config("plaid", {"access_token": res["data"]["access_token"]})
            return {"ok": True, "linked": True, "item_id": res["data"].get("item_id", "")}
        return res

    def plaid_sandbox_quick_connect(self, institution_id: str = "ins_109508") -> dict[str, Any]:
        """Create a Plaid Sandbox public_token and immediately exchange it for an access_token."""
        cfg = self.get_config("plaid")
        if cfg.get("environment", "sandbox") != "sandbox":
            return {"ok": False, "error": "Quick Connect is only available in Plaid sandbox environment."}
        if not cfg.get("client_id") or not cfg.get("secret"):
            return {"ok": False, "error": "Enter Plaid client_id and secret first."}
        base = self._plaid_base_url("sandbox")
        pub = _http_request_json(
            f"{base}/sandbox/public_token/create",
            method="POST",
            payload={
                "client_id": cfg["client_id"],
                "secret": cfg["secret"],
                "institution_id": institution_id,
                "initial_products": ["transactions"],
            },
        )
        if not pub.get("ok"):
            return pub
        public_token = (pub.get("data") or {}).get("public_token")
        if not public_token:
            return {"ok": False, "error": "No public_token returned by Plaid Sandbox."}
        return self.plaid_exchange_public_token(public_token)

    def test_connector(self, connector_id: str) -> dict[str, Any]:
        if not self.is_configured(connector_id):
            return {"ok": False, "connector_id": connector_id, "error": "Connector is not configured."}
        cfg = self.get_config(connector_id)
        if connector_id == "gemini":
            res = self.provider_vault.probe_provider("gemini")
            return {"ok": bool(res.get("ok")), "connector_id": "gemini", "detail": res}
        if connector_id == "plaid":
            if not cfg.get("access_token"):
                return {"ok": True, "connector_id": "plaid", "message": "Credentials saved. Run Sandbox Quick-Connect or Plaid Link to link a bank account."}
            return self.execute_action("plaid", "accounts.balance", {})
        if connector_id == "shopify":
            return self.execute_action("shopify", "orders.list", {"limit": 1})
        if connector_id == "woocommerce":
            return self.execute_action("woocommerce", "orders.list", {"per_page": 1})
        if connector_id == "stripe":
            return self.execute_action("stripe", "payments.list", {"limit": 1})
        if connector_id == "mcp":
            return self.execute_action("mcp", "tools.list", {})
        if connector_id == "webhook":
            return self.execute_action("webhook", "http.get", {})
        if connector_id == "searxng":
            return self.execute_action("searxng", "web.search", {"query": "sabrina"})
        if connector_id == "crawl4ai":
            return _http_request_json(f"{cfg['base_url'].rstrip('/')}/health")
        if connector_id == "home_assistant":
            return self.execute_action("home_assistant", "states.get", {})
        if connector_id == "rss":
            return self.execute_action("rss", "feed.poll", {"limit": 3})
        if connector_id == "forgejo":
            return self.execute_action("forgejo", "version.get", {})
        return {"ok": False, "error": f"No probe implemented for {connector_id}"}

    def execute_action(
        self,
        connector_id: str,
        action_name: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        params = dict(params or {})
        cfg = self.get_config(connector_id)

        if connector_id == "plaid":
            base = self._plaid_base_url(cfg.get("environment", "sandbox"))
            token = cfg.get("access_token", "")
            if not token:
                return {"ok": False, "error": "Plaid access_token is missing. Link an account first."}
            body = {"client_id": cfg["client_id"], "secret": cfg["secret"], "access_token": token}
            if action_name == "accounts.balance":
                return _http_request_json(f"{base}/accounts/balance/get", method="POST", payload=body)
            if action_name == "transactions.sync":
                body["count"] = int(params.get("count", 25))
                return _http_request_json(f"{base}/transactions/sync", method="POST", payload=body)
            if action_name == "recurring.list":
                return _http_request_json(f"{base}/transactions/recurring/get", method="POST", payload=body)

        elif connector_id == "shopify":
            domain = cfg["shop_domain"].replace("https://", "").replace("http://", "").strip("/")
            headers = {"X-Shopify-Access-Token": cfg["access_token"]}
            if action_name == "orders.list":
                limit = int(params.get("limit", 20))
                status = urllib.parse.quote(str(params.get("status", "any")))
                return _http_request_json(f"https://{domain}/admin/api/2025-01/orders.json?limit={limit}&status={status}", headers=headers)
            if action_name == "products.list":
                limit = int(params.get("limit", 20))
                return _http_request_json(f"https://{domain}/admin/api/2025-01/products.json?limit={limit}", headers=headers)
            if action_name == "order.note":
                order_id = params.get("order_id")
                note = params.get("note", "")
                return _http_request_json(
                    f"https://{domain}/admin/api/2025-01/orders/{order_id}.json",
                    method="PUT",
                    headers=headers,
                    payload={"order": {"id": order_id, "note": note}},
                )

        elif connector_id == "woocommerce":
            base = cfg["store_url"].rstrip("/")
            ck = urllib.parse.quote(cfg["consumer_key"])
            cs = urllib.parse.quote(cfg["consumer_secret"])
            if action_name == "orders.list":
                per_page = int(params.get("per_page", 20))
                return _http_request_json(f"{base}/wp-json/wc/v3/orders?per_page={per_page}&consumer_key={ck}&consumer_secret={cs}")
            if action_name == "products.list":
                per_page = int(params.get("per_page", 20))
                return _http_request_json(f"{base}/wp-json/wc/v3/products?per_page={per_page}&consumer_key={ck}&consumer_secret={cs}")
            if action_name == "order.update":
                order_id = params.get("order_id")
                status = params.get("status", "processing")
                return _http_request_json(
                    f"{base}/wp-json/wc/v3/orders/{order_id}?consumer_key={ck}&consumer_secret={cs}",
                    method="PUT",
                    payload={"status": status},
                )

        elif connector_id == "stripe":
            headers = {"Authorization": f"Bearer {cfg['secret_key']}"}
            if action_name == "payments.list":
                limit = int(params.get("limit", 20))
                return _http_request_json(f"https://api.stripe.com/v1/payment_intents?limit={limit}", headers=headers)
            if action_name == "payouts.list":
                limit = int(params.get("limit", 10))
                return _http_request_json(f"https://api.stripe.com/v1/payouts?limit={limit}", headers=headers)
            if action_name == "refund.create":
                pi = urllib.parse.quote(str(params.get("payment_intent", "")))
                return _http_request_json(f"https://api.stripe.com/v1/refunds?payment_intent={pi}", method="POST", headers=headers)

        elif connector_id == "mcp":
            transport = cfg.get("transport", "http")
            target = cfg.get("endpoint_or_cmd", "")
            headers = {}
            if cfg.get("auth_token"):
                headers["Authorization"] = f"Bearer {cfg['auth_token']}"
            if transport == "http":
                if action_name == "tools.list":
                    return _http_request_json(target, method="POST", headers=headers, payload={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
                return _http_request_json(
                    target,
                    method="POST",
                    headers=headers,
                    payload={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": params.get("tool_name", ""), "arguments": params.get("arguments", {})}},
                )
            # stdio MCP single-shot JSON-RPC call
            rpc_req = (
                {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
                if action_name == "tools.list"
                else {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": params.get("tool_name", ""), "arguments": params.get("arguments", {})}}
            )
            try:
                proc = subprocess.run(
                    target,
                    input=json.dumps(rpc_req) + "\n",
                    text=True,
                    capture_output=True,
                    shell=True,
                    timeout=15,
                )
                lines = [ln.strip() for ln in proc.stdout.splitlines() if ln.strip()]
                parsed = json.loads(lines[-1]) if lines else {"raw": proc.stdout, "stderr": proc.stderr}
                return {"ok": proc.returncode == 0, "data": parsed}
            except Exception as exc:
                return {"ok": False, "error": str(exc)}

        elif connector_id == "webhook":
            url = str(params.get("url") or cfg["base_url"])
            headers = {"Authorization": cfg["auth_header"]} if cfg.get("auth_header") else {}
            if action_name == "http.get":
                return _http_request_json(url, method="GET", headers=headers)
            if action_name == "http.post":
                return _http_request_json(url, method="POST", headers=headers, payload=params.get("payload", {}))

        elif connector_id == "searxng":
            base = cfg.get("base_url", "http://127.0.0.1:8888").rstrip("/")
            q = urllib.parse.quote(str(params.get("query", "")))
            categories = params.get("categories")
            if not categories and action_name == "docs.search":
                categories = "science,it"
            cat_param = f"&categories={categories}" if categories else ""
            return _http_request_json(f"{base}/search?q={q}&format=json{cat_param}")

        elif connector_id == "crawl4ai":
            base = cfg.get("base_url", "http://127.0.0.1:11235").rstrip("/")
            headers = {"Authorization": f"Bearer {cfg['api_token']}"} if cfg.get("api_token") else {}
            return _http_request_json(f"{base}/crawl", method="POST", headers=headers, payload={"urls": [params.get("url", "")]})

        elif connector_id == "home_assistant":
            base = cfg["base_url"].rstrip("/")
            headers = {"Authorization": f"Bearer {cfg['access_token']}"}
            if action_name == "states.get":
                entity = params.get("entity_id", "")
                suffix = f"/api/states/{entity}" if entity else "/api/states"
                return _http_request_json(f"{base}{suffix}", headers=headers)
            if action_name == "service.call":
                domain = params.get("domain", "homeassistant")
                service = params.get("service", "turn_on")
                return _http_request_json(f"{base}/api/services/{domain}/{service}", method="POST", headers=headers, payload=params.get("data", {}))

        elif connector_id == "rss":
            url = str(params.get("feed_url") or cfg.get("feed_url", ""))
            limit = int(params.get("limit", 10))
            try:
                with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "SabrinaConsole/1.0"}), timeout=12.0) as resp:
                    xml_text = resp.read().decode("utf-8", errors="replace")
                root = ET.fromstring(xml_text)
                entries: list[dict[str, str]] = []
                for item in root.findall(".//item")[:limit]:
                    entries.append({
                        "title": (item.findtext("title") or "").strip(),
                        "link": (item.findtext("link") or "").strip(),
                        "pubDate": (item.findtext("pubDate") or "").strip(),
                    })
                if not entries:
                    ns = {"atom": "http://www.w3.org/2005/Atom"}
                    for entry in root.findall(".//atom:entry", ns)[:limit]:
                        link_el = entry.find("atom:link", ns)
                        entries.append({
                            "title": (entry.findtext("atom:title", default="", namespaces=ns) or "").strip(),
                            "link": (link_el.attrib.get("href", "") if link_el is not None else "").strip(),
                            "updated": (entry.findtext("atom:updated", default="", namespaces=ns) or "").strip(),
                        })
                return {"ok": True, "data": {"items": entries, "count": len(entries)}}
            except Exception as exc:
                return {"ok": False, "error": str(exc)}

        elif connector_id == "forgejo":
            try:
                from sabrina_console_desktop.forgejo_client import ForgejoClient
            except ImportError:
                from forgejo_client import ForgejoClient  # type: ignore[no-redef]
            client = ForgejoClient(
                base_url=cfg.get("base_url", _FORGEJO_DEFAULT_URL),
                owner=cfg.get("owner", "ego"),
                token=cfg.get("api_token", ""),
            )
            repo_name = str(params.get("repo_name") or cfg.get("repo_name") or "sabrina")
            if action_name == "version.get":
                return client.get_version()
            if action_name == "packages.list":
                return client.list_packages(package_type=params.get("package_type"), limit=int(params.get("limit", 50)))
            if action_name == "repos.list":
                return client.list_repos(limit=int(params.get("limit", 50)))
            if action_name == "branches.list":
                return client.list_branches(repo_name=repo_name, limit=int(params.get("limit", 50)))
            if action_name == "pulls.list":
                return client.list_pull_requests(state=str(params.get("state", "open")), repo_name=repo_name, limit=int(params.get("limit", 25)))
            if action_name == "pulls.diff":
                return client.get_pull_request_diff(int(params["index"]), repo_name=repo_name)
            if action_name == "comments.list":
                return client.list_comments(int(params["index"]), repo_name=repo_name)
            if action_name == "branches.create":
                return client.create_branch(str(params["new_branch"]), old_branch=str(params.get("old_branch", "main")), repo_name=repo_name)
            if action_name == "files.patch":
                return client.apply_file_patches(
                    str(params["branch"]),
                    dict(params.get("files") or {}),
                    str(params.get("commit_message", "chore: swarm patch")),
                    base_branch=str(params.get("base_branch", "main")),
                    repo_name=repo_name,
                )
            if action_name == "pulls.create":
                return client.create_pull_request(
                    str(params["title"]),
                    str(params.get("body", "")),
                    str(params["head"]),
                    base=str(params.get("base", "main")),
                    repo_name=repo_name,
                )
            if action_name == "comments.create":
                return client.create_comment(int(params["index"]), str(params["body"]), repo_name=repo_name)
            if action_name == "reviews.create":
                return client.create_pull_review(
                    int(params["index"]),
                    body=str(params.get("body", "")),
                    event=str(params.get("event", "COMMENT")),
                    comments=params.get("comments"),
                    commit_id=str(params.get("commit_id", "")),
                    repo_name=repo_name,
                )
            if action_name == "pypi.sync_wheelhouse":
                wh = params.get("wheelhouse_dir") or str(Path(__file__).resolve().parent.parent / "artifacts" / "wheels")
                return client.sync_wheelhouse(wh)
            if action_name == "generic.publish_rsi":
                art = params.get("artifacts_dir") or str(Path(__file__).resolve().parent / "artifacts")
                return client.publish_rsi_artifacts(art, version=params.get("version"))

        return {"ok": False, "error": f"Unsupported action {connector_id}.{action_name}"}
