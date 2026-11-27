"""Zero-dependency HTTP + SSE server for the Swarm Research Console."""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from .client import SwarmClient, seed_demo_swarm
from .gemini_copilot import (
    DEFAULT_GEMINI_MODEL,
    GeminiCopilotError,
    analyze_swarm_state,
    get_copilot_status,
)
from .protocol import (
    LockConflictError,
    PathIsolationError,
    SwarmProtocolError,
    SwarmStore,
    guard_safe_path,
)
from .gpu_providers import SwarmGpuDeploymentManager
from .registry import ModelSkillRegistry
from .vault import ProviderKeyVault


DEFAULT_UI_PATH = Path(__file__).resolve().parent / "ui.html"
ASSETS_DIR = Path(__file__).resolve().parent / "assets"
ALLOWED_ASSETS = {
    "coordinator.jpg": "image/jpeg",
    "architect.jpg": "image/jpeg",
    "worker.jpg": "image/jpeg",
    "verifier.jpg": "image/jpeg",
}


class SwarmHTTPServer(ThreadingHTTPServer):
    """Threaded HTTP server carrying a `SwarmStore` and `ui.html` path."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        server_address: tuple[str, int],
        RequestHandlerClass: type[BaseHTTPRequestHandler],
        store: SwarmStore,
        ui_html_path: Path = DEFAULT_UI_PATH,
        poll_interval: float = 0.5,
        gemini_client_factory: Callable[[], Any] | None = None,
        registry: ModelSkillRegistry | None = None,
        vault: ProviderKeyVault | None = None,
    ) -> None:
        self.store = store
        self.ui_html_path = ui_html_path
        self.poll_interval = poll_interval
        self.gemini_client_factory = gemini_client_factory
        self.vault = vault if vault is not None else ProviderKeyVault(store.swarm_dir / "vault.json")
        if registry is None:
            self.registry = ModelSkillRegistry(
                vault=self.vault,
                custom_models_path=store.swarm_dir / "custom_models.json",
            )
        else:
            if registry.vault is None:
                registry.vault = self.vault
            if registry.custom_models_path is None:
                registry.custom_models_path = store.swarm_dir / "custom_models.json"
            self.registry = registry
        from swarm_command.blackboard import SessionBlackboard
        from swarm_command.console_bridge import SwarmConsoleBridge
        from swarm_command.router import SwarmCommand

        self.blackboard = SessionBlackboard(
            session_id=f"swarm_console_{store.swarm_dir.name}",
            checkpoint_path=store.swarm_dir / "blackboard.json",
        )
        self.swarm_command = SwarmCommand(
            blackboard=self.blackboard,
            console_bridge=SwarmConsoleBridge(SwarmClient(store.swarm_dir)),
        )
        self.gpu_deployments = SwarmGpuDeploymentManager(
            vault=self.vault,
            deployments_path=store.swarm_dir / "gpu_deployments.json",
            model_registry=self.registry,
        )
        super().__init__(server_address, RequestHandlerClass)


class SwarmRequestHandler(BaseHTTPRequestHandler):
    """Serves the Swarm Console UI, REST endpoints, and live SSE stream."""

    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        """Silence default stderr logging unless debugging is needed."""
        return

    def _send_bytes(
        self,
        status: int,
        content_type: str,
        payload: bytes,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def _send_json(self, status: int, data: dict[str, Any]) -> None:
        body = json.dumps(data).encode("utf-8")
        self._send_bytes(status, "application/json; charset=utf-8", body)

    def _read_json_body(self) -> dict[str, Any]:
        length_header = self.headers.get("Content-Length")
        if not length_header:
            raise ValueError("Missing Content-Length header.")
        try:
            length = int(length_header)
        except ValueError as exc:
            raise ValueError("Invalid Content-Length header.") from exc
        if length <= 0 or length > 1_048_576:
            raise ValueError("Request body size out of bounds.")
        raw = self.rfile.read(length)
        parsed = json.loads(raw.decode("utf-8"))
        if not isinstance(parsed, dict):
            raise ValueError("JSON body must be an object.")
        return parsed

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        route = parsed.path
        store: SwarmStore = self.server.store  # type: ignore[attr-defined]

        if route == "/" or route == "/index.html":
            ui_path: Path = self.server.ui_html_path  # type: ignore[attr-defined]
            if not ui_path.exists():
                self._send_json(
                    HTTPStatus.NOT_FOUND,
                    {"ok": False, "error": "ui_missing", "message": "ui.html not found."},
                )
                return
            self._send_bytes(
                HTTPStatus.OK,
                "text/html; charset=utf-8",
                ui_path.read_bytes(),
            )
            return

        if route == "/api/state":
            state = store.get_state()
            self._send_json(HTTPStatus.OK, {"ok": True, **state})
            return

        if route == "/api/swarm-command/state":
            blackboard: SessionBlackboard = self.server.blackboard  # type: ignore[attr-defined]
            self._send_json(HTTPStatus.OK, {"ok": True, "blackboard": blackboard.snapshot()})
            return

        if route == "/api/gemini/status":
            status = get_copilot_status()
            if getattr(self.server, "gemini_client_factory", None) is not None:
                status["available"] = True
                status["reason"] = "Ready (custom Gemini client)"
            self._send_json(HTTPStatus.OK, {"ok": True, **status})
            return

        if route == "/api/vault/status":
            vault: ProviderKeyVault = self.server.vault  # type: ignore[attr-defined]
            self._send_json(HTTPStatus.OK, {"ok": True, **vault.list_providers()})
            return

        if route == "/api/gpu/providers":
            gpu_mgr: SwarmGpuDeploymentManager = self.server.gpu_deployments  # type: ignore[attr-defined]
            self._send_json(
                HTTPStatus.OK,
                {
                    "ok": True,
                    "providers": gpu_mgr.list_providers(),
                    "deployments": gpu_mgr.list_deployments(),
                },
            )
            return

        if route == "/api/gpu/deployments":
            gpu_mgr = self.server.gpu_deployments  # type: ignore[attr-defined]
            self._send_json(HTTPStatus.OK, {"ok": True, "deployments": gpu_mgr.list_deployments()})
            return

        if route == "/api/registry/models":
            qs = parse_qs(parsed.query)
            query_str = qs.get("q", [""])[0]
            provider = qs.get("provider", [""])[0]
            capability = qs.get("capability", [""])[0]
            tier = qs.get("tier", [""])[0]
            include_dep = qs.get("include_deprecated", ["0"])[0].lower() in ("1", "true", "yes")
            unlocked_only = qs.get("unlocked_only", ["0"])[0].lower() in ("1", "true", "yes")
            refresh = qs.get("refresh", ["0"])[0].lower() in ("1", "true", "yes")
            try:
                limit = int(qs.get("limit", ["50"])[0])
                offset = int(qs.get("offset", ["0"])[0])
            except ValueError:
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"ok": False, "error": "invalid_pagination", "message": "`limit` and `offset` must be integers."},
                )
                return
            registry: ModelSkillRegistry = self.server.registry  # type: ignore[attr-defined]
            result = registry.search_models(
                query=query_str,
                provider=provider,
                capability=capability,
                tier=tier,
                include_deprecated=include_dep,
                unlocked_only=unlocked_only,
                limit=limit,
                offset=offset,
                refresh=refresh,
            )
            self._send_json(HTTPStatus.OK, {"ok": True, **result})
            return

        if route == "/api/registry/skills":
            qs = parse_qs(parsed.query)
            registry = self.server.registry  # type: ignore[attr-defined]
            skill_id = qs.get("id", [""])[0].strip()
            if skill_id:
                detail = registry.get_skill_detail(skill_id)
                if detail is None:
                    self._send_json(
                        HTTPStatus.NOT_FOUND,
                        {"ok": False, "error": "skill_not_found", "message": f"Skill not found: {skill_id}"},
                    )
                    return
                self._send_json(HTTPStatus.OK, {"ok": True, "skill": detail})
                return
            query_str = qs.get("q", [""])[0]
            source_str = qs.get("source", [""])[0]
            refresh = qs.get("refresh", ["0"])[0].lower() in ("1", "true", "yes")
            try:
                limit = int(qs.get("limit", ["50"])[0])
                offset = int(qs.get("offset", ["0"])[0])
            except ValueError:
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"ok": False, "error": "invalid_pagination", "message": "`limit` and `offset` must be integers."},
                )
                return
            result = registry.search_skills(
                query=query_str,
                source=source_str,
                limit=limit,
                offset=offset,
                refresh=refresh,
            )
            self._send_json(HTTPStatus.OK, {"ok": True, **result})
            return

        if route == "/api/a2a-links":
            self._send_json(
                HTTPStatus.OK,
                {"ok": True, "a2a_links": store.list_a2a_links()},
            )
            return

        if route == "/api/board":
            qs = parse_qs(parsed.query)
            try:
                since = int(qs.get("since", ["0"])[0])
            except ValueError:
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"ok": False, "error": "invalid_since", "message": "`since` must be an integer."},
                )
                return
            limit_raw = qs.get("limit", [None])[0]
            limit: int | None = None
            if limit_raw is not None:
                try:
                    limit = int(limit_raw)
                except ValueError:
                    self._send_json(
                        HTTPStatus.BAD_REQUEST,
                        {"ok": False, "error": "invalid_limit", "message": "`limit` must be an integer."},
                    )
                    return

            messages = store.read_board(since=since, limit=limit)
            all_msgs = store.read_board(since=0)
            head_seq = int(all_msgs[-1]["seq"]) if all_msgs else 0
            self._send_json(
                HTTPStatus.OK,
                {"ok": True, "messages": messages, "head_seq": head_seq},
            )
            return

        if route == "/events":
            self._handle_sse(store)
            return

        if route.startswith("/assets/"):
            asset_name = route[len("/assets/"):]
            mime = ALLOWED_ASSETS.get(asset_name)
            asset_path = ASSETS_DIR / asset_name
            if mime and asset_path.is_file():
                self._send_bytes(HTTPStatus.OK, mime, asset_path.read_bytes())
                return
            self._send_json(
                HTTPStatus.NOT_FOUND,
                {"ok": False, "error": "asset_not_found", "message": f"Unknown asset: {asset_name}"},
            )
            return

        self._send_json(
            HTTPStatus.NOT_FOUND,
            {"ok": False, "error": "not_found", "message": f"Unknown route: {route}"},
        )

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        route = parsed.path
        store: SwarmStore = self.server.store  # type: ignore[attr-defined]

        try:
            payload = self._read_json_body()
        except (ValueError, json.JSONDecodeError) as exc:
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"ok": False, "error": "bad_request", "message": str(exc)},
            )
            return

        if route == "/api/swarm-command/turn":
            from swarm_command.models import ComplexityClass
            from swarm_command.router import PreflightError

            swarm_cmd = self.server.swarm_command  # type: ignore[attr-defined]
            request_text = str(payload.get("request", "")).strip()
            subtasks = payload.get("subtasks")
            if subtasks is not None and not isinstance(subtasks, list):
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"ok": False, "error": "bad_request", "message": "`subtasks` must be a list."},
                )
                return
            hint_raw = payload.get("complexity_hint")
            complexity_hint = None
            if hint_raw is not None:
                try:
                    complexity_hint = ComplexityClass(str(hint_raw))
                except ValueError:
                    self._send_json(
                        HTTPStatus.BAD_REQUEST,
                        {"ok": False, "error": "bad_request", "message": f"Invalid complexity_hint: {hint_raw!r}"},
                    )
                    return
            try:
                turn_res = swarm_cmd.run_turn(
                    request_text=request_text,
                    subtasks=[str(s) for s in subtasks] if subtasks is not None else None,
                    complexity_hint=complexity_hint,
                )
            except (PreflightError, ValueError) as exc:
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"ok": False, "error": "swarm_command_error", "message": str(exc)},
                )
                return
            self._send_json(
                HTTPStatus.OK,
                {
                    "ok": True,
                    "mode": turn_res.mode.value,
                    "active_owner": turn_res.active_owner,
                    "response": turn_res.response,
                    "artifacts_written": turn_res.artifacts_written,
                    "verdict": turn_res.verdict,
                    "circuit_breaker_triggered": turn_res.circuit_breaker_triggered,
                    "circuit_breaker_reason": turn_res.circuit_breaker_reason,
                    "envelopes": turn_res.envelopes,
                    "blackboard": swarm_cmd.blackboard.snapshot(),
                },
            )
            return

        if route == "/api/a2a-links":
            source = str(payload.get("from") or payload.get("source") or "").strip()
            target = str(payload.get("to") or payload.get("target") or "").strip()
            comm_type = str(payload.get("comm_type") or "pass_off").strip()
            condition = payload.get("condition")
            max_loops_raw = payload.get("max_loops", 3)
            payload_mode = str(payload.get("payload_mode") or "full_envelope").strip()
            message_template = str(payload.get("message_template") or "").strip()
            link_id = payload.get("id")
            announce = bool(payload.get("announce", False))
            try:
                max_loops = int(max_loops_raw)
            except (TypeError, ValueError):
                max_loops = 3
            try:
                record = store.upsert_a2a_link(
                    source=source,
                    target=target,
                    comm_type=comm_type,
                    condition=str(condition) if condition is not None else None,
                    max_loops=max_loops,
                    payload_mode=payload_mode,
                    message_template=message_template,
                    link_id=str(link_id) if link_id else None,
                )
                board_msg = None
                if announce:
                    note_txt = f" — {record['message_template']}" if record.get("message_template") else ""
                    board_msg = store.append_board(
                        agent=source,
                        text=f"@{target} [A2A:{record['comm_type'].upper()}] ({record['condition']}, max_loops={record['max_loops']}){note_txt}",
                    )
            except SwarmProtocolError as exc:
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"ok": False, "error": "protocol_error", "message": str(exc)},
                )
                return
            self._send_json(
                HTTPStatus.OK,
                {
                    "ok": True,
                    "link": record,
                    "a2a_links": store.list_a2a_links(),
                    "board_message": board_msg,
                },
            )
            return

        if route == "/api/a2a-links/delete":
            link_id = str(payload.get("id") or "").strip()
            deleted = store.delete_a2a_link(link_id)
            self._send_json(
                HTTPStatus.OK,
                {"ok": True, "deleted": deleted, "a2a_links": store.list_a2a_links()},
            )
            return

        if route == "/api/board":
            agent = str(payload.get("agent", "")).strip()
            text = str(payload.get("text", "")).strip()
            tags = payload.get("tags")
            if tags is not None and not isinstance(tags, list):
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"ok": False, "error": "bad_request", "message": "`tags` must be a list."},
                )
                return
            try:
                msg = store.append_board(agent=agent, text=text, tags=tags)
            except SwarmProtocolError as exc:
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"ok": False, "error": "protocol_error", "message": str(exc)},
                )
                return
            self._send_json(HTTPStatus.CREATED, {"ok": True, "message": msg})
            return

        if route == "/api/worker":
            name = str(payload.get("name", "")).strip()
            role = payload.get("role")
            model = payload.get("model")
            state = payload.get("state")
            locks = payload.get("locks")
            thread_id = payload.get("thread_id")
            note = payload.get("note")
            if locks is not None and not isinstance(locks, list):
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"ok": False, "error": "bad_request", "message": "`locks` must be a list."},
                )
                return
            try:
                worker = store.update_worker(
                    name=name,
                    state=state,
                    note=note,
                    locks=locks,
                    role=role,
                    model=model,
                    thread_id=thread_id,
                )
            except LockConflictError as exc:
                self._send_json(
                    HTTPStatus.CONFLICT,
                    {
                        "ok": False,
                        "error": "lock_conflict",
                        "message": str(exc),
                        "conflicts": exc.conflicts,
                    },
                )
                return
            except SwarmProtocolError as exc:
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"ok": False, "error": "protocol_error", "message": str(exc)},
                )
                return
            self._send_json(HTTPStatus.OK, {"ok": True, "worker": worker})
            return

        if route == "/api/gemini/analyze":
            question = payload.get("question")
            if question is not None and not isinstance(question, str):
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"ok": False, "error": "bad_request", "message": "`question` must be a string."},
                )
                return
            model = str(payload.get("model") or DEFAULT_GEMINI_MODEL).strip()
            previous_interaction_id = payload.get("previous_interaction_id")
            if previous_interaction_id is not None and not isinstance(previous_interaction_id, str):
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {
                        "ok": False,
                        "error": "bad_request",
                        "message": "`previous_interaction_id` must be a string.",
                    },
                )
                return
            try:
                result = analyze_swarm_state(
                    store.get_state(),
                    question=question,
                    model=model,
                    previous_interaction_id=previous_interaction_id,
                    client_factory=getattr(self.server, "gemini_client_factory", None),
                )
            except GeminiCopilotError as exc:
                self._send_json(
                    exc.status,
                    {"ok": False, "error": exc.code, "message": str(exc)},
                )
                return
            self._send_json(HTTPStatus.OK, {"ok": True, **result})
            return

        if route == "/api/vault/key":
            vault: ProviderKeyVault = self.server.vault  # type: ignore[attr-defined]
            registry: ModelSkillRegistry = self.server.registry  # type: ignore[attr-defined]
            provider = str(payload.get("provider", "")).strip()
            api_key = str(payload.get("api_key", ""))
            do_probe = bool(payload.get("probe", False))
            config = payload.get("config") if isinstance(payload.get("config"), dict) else None
            try:
                status_dict = vault.set_key(provider, api_key, config=config)
            except ValueError as exc:
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"ok": False, "error": "invalid_vault_request", "message": str(exc)},
                )
                return
            response_data: dict[str, Any] = {
                "ok": True,
                "provider_status": status_dict,
            }
            if do_probe:
                probe_res = vault.probe_provider(provider)
                response_data["probe"] = probe_res
                if probe_res.get("ok"):
                    registry.refresh_models(force=True)
            response_data.update(vault.list_providers())
            self._send_json(HTTPStatus.OK, response_data)
            return

        if route == "/api/vault/config":
            vault = self.server.vault  # type: ignore[attr-defined]
            provider = str(payload.get("provider", "")).strip()
            config = payload.get("config")
            if not isinstance(config, dict):
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"ok": False, "error": "invalid_vault_request", "message": "`config` must be a JSON object."},
                )
                return
            try:
                status_dict = vault.set_provider_config(provider, config)
            except ValueError as exc:
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"ok": False, "error": "invalid_vault_request", "message": str(exc)},
                )
                return
            self._send_json(HTTPStatus.OK, {"ok": True, "provider_status": status_dict, **vault.list_providers()})
            return

        if route == "/api/gpu/deploy":
            gpu_mgr: SwarmGpuDeploymentManager = self.server.gpu_deployments  # type: ignore[attr-defined]
            registry = self.server.registry  # type: ignore[attr-defined]
            result = gpu_mgr.deploy_swarm(payload)
            if result.get("ok"):
                registry.refresh_models(force=True)
            status_code = HTTPStatus.OK if result.get("ok") else HTTPStatus.BAD_REQUEST
            self._send_json(status_code, {**result, "deployments": gpu_mgr.list_deployments()})
            return

        if route == "/api/gpu/status":
            gpu_mgr = self.server.gpu_deployments  # type: ignore[attr-defined]
            dep_id = str(payload.get("deployment_id", "")).strip()
            result = gpu_mgr.check_status(dep_id)
            status_code = HTTPStatus.OK if result.get("ok") else HTTPStatus.BAD_REQUEST
            self._send_json(status_code, {**result, "deployments": gpu_mgr.list_deployments()})
            return

        if route == "/api/gpu/terminate":
            gpu_mgr = self.server.gpu_deployments  # type: ignore[attr-defined]
            dep_id = str(payload.get("deployment_id", "")).strip()
            result = gpu_mgr.terminate_deployment(dep_id)
            status_code = HTTPStatus.OK if result.get("ok") else HTTPStatus.BAD_REQUEST
            self._send_json(status_code, {**result, "deployments": gpu_mgr.list_deployments()})
            return

        if route == "/api/gpu/register-model":
            gpu_mgr = self.server.gpu_deployments  # type: ignore[attr-defined]
            registry = self.server.registry  # type: ignore[attr-defined]
            dep_id = str(payload.get("deployment_id", "")).strip()
            result = gpu_mgr.register_deployment_model(dep_id)
            if result.get("ok"):
                registry.refresh_models(force=True)
            status_code = HTTPStatus.OK if result.get("ok") else HTTPStatus.BAD_REQUEST
            self._send_json(status_code, {**result, "deployments": gpu_mgr.list_deployments()})
            return

        if route == "/api/vault/delete":
            vault = self.server.vault  # type: ignore[attr-defined]
            registry = self.server.registry  # type: ignore[attr-defined]
            provider = str(payload.get("provider", "")).strip()
            try:
                result = vault.delete_key(provider)
            except ValueError as exc:
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"ok": False, "error": "invalid_vault_request", "message": str(exc)},
                )
                return
            registry.refresh_models(force=True)
            self._send_json(HTTPStatus.OK, {"ok": True, **result})
            return

        if route == "/api/vault/probe":
            vault = self.server.vault  # type: ignore[attr-defined]
            registry = self.server.registry  # type: ignore[attr-defined]
            provider = str(payload.get("provider", "")).strip()
            api_key_raw = payload.get("api_key")
            api_key = str(api_key_raw) if api_key_raw is not None else None
            try:
                result = vault.probe_provider(provider, api_key=api_key)
            except ValueError as exc:
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"ok": False, "error": "invalid_vault_request", "message": str(exc)},
                )
                return
            if result.get("ok"):
                registry.refresh_models(force=True)
            self._send_json(HTTPStatus.OK, result)
            return

        if route == "/api/vault/probe-all":
            vault = self.server.vault  # type: ignore[attr-defined]
            registry = self.server.registry  # type: ignore[attr-defined]
            result = vault.probe_all_unlocked()
            registry.refresh_models(force=True)
            self._send_json(HTTPStatus.OK, result)
            return

        if route == "/api/registry/models/add":
            registry = self.server.registry  # type: ignore[attr-defined]
            caps_raw = payload.get("capabilities")
            caps = [str(c) for c in caps_raw] if isinstance(caps_raw, list) else None
            ctx_raw = payload.get("context_window")
            ctx_val = int(ctx_raw) if isinstance(ctx_raw, int) else None
            try:
                record = registry.add_model(
                    model_id=str(payload.get("id") or payload.get("model_id") or ""),
                    name=str(payload.get("name") or ""),
                    provider=str(payload.get("provider") or ""),
                    tier=str(payload.get("tier") or "external"),
                    role=str(payload.get("role") or ""),
                    residency=str(payload.get("residency") or ""),
                    size_gb=str(payload.get("size_gb") or ""),
                    endpoint=str(payload.get("endpoint") or ""),
                    context_window=ctx_val,
                    capabilities=caps,
                )
            except ValueError as exc:
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"ok": False, "error": "invalid_model", "message": str(exc)},
                )
                return
            self._send_json(HTTPStatus.CREATED, {"ok": True, "model": record})
            return

        if route == "/api/registry/models/remove":
            registry = self.server.registry  # type: ignore[attr-defined]
            target_id = str(payload.get("id") or payload.get("model_id") or "").strip()
            if not target_id:
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"ok": False, "error": "invalid_model", "message": "`id` is required."},
                )
                return
            removed = registry.remove_model(target_id)
            self._send_json(HTTPStatus.OK, {"ok": True, "removed": removed, "id": target_id})
            return

        self._send_json(
            HTTPStatus.NOT_FOUND,
            {"ok": False, "error": "not_found", "message": f"Unknown route: {route}"},
        )

    def _handle_sse(self, store: SwarmStore) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        poll_interval: float = getattr(self.server, "poll_interval", 0.5)
        last_fp: tuple[int, int, int, int, int] | None = None
        last_keepalive = time.monotonic()

        try:
            while True:
                fp = store.state_fingerprint()
                if fp != last_fp:
                    state = store.get_state()
                    event_payload = json.dumps(state, separators=(",", ":"))
                    frame = f"event: state\ndata: {event_payload}\n\n".encode("utf-8")
                    self.wfile.write(frame)
                    self.wfile.flush()
                    last_fp = fp
                    last_keepalive = time.monotonic()
                elif time.monotonic() - last_keepalive >= 15.0:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                    last_keepalive = time.monotonic()

                time.sleep(poll_interval)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            return


def create_server(
    swarm_dir: str | Path,
    host: str = "127.0.0.1",
    port: int = 8765,
    ui_html_path: Path = DEFAULT_UI_PATH,
    poll_interval: float = 0.5,
    gemini_client_factory: Callable[[], Any] | None = None,
    registry: ModelSkillRegistry | None = None,
    vault: ProviderKeyVault | None = None,
) -> SwarmHTTPServer:
    """Create a configured `SwarmHTTPServer` bound to `host:port`."""
    safe_dir = guard_safe_path(swarm_dir)
    store = SwarmStore(safe_dir)
    store.init()
    return SwarmHTTPServer(
        (host, port),
        SwarmRequestHandler,
        store=store,
        ui_html_path=ui_html_path,
        poll_interval=poll_interval,
        gemini_client_factory=gemini_client_factory,
        registry=registry,
        vault=vault,
    )


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="swarm_console.server",
        description="Zero-dependency research console for .swarm/ directories.",
    )
    parser.add_argument(
        "--swarm-dir",
        default=None,
        help="Path to .swarm/ directory (default: .swarm, or a temp dir when --demo is set).",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Host interface to bind (default: 127.0.0.1).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8765,
        help="TCP port to listen on (default: 8765).",
    )
    parser.add_argument(
        "--demo",
        action="store_true",
        help="Seed a realistic demo .swarm/ before starting the server.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    try:
        if args.demo:
            target_dir = (
                Path(args.swarm_dir)
                if args.swarm_dir
                else Path(tempfile.mkdtemp(prefix="swarm_console_demo_"))
            )
            safe_dir = seed_demo_swarm(target_dir, reset=True)
        else:
            safe_dir = guard_safe_path(args.swarm_dir or ".swarm")

        server = create_server(safe_dir, host=args.host, port=args.port)
    except PathIsolationError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    bound_host, bound_port = server.server_address[:2]
    print(f"Swarm Console serving {safe_dir} at http://{bound_host}:{bound_port}/")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down Swarm Console.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
