"""Stdlib unittest suite for swarm_console."""

from __future__ import annotations

import io
import json
import os
import tempfile
import threading
import unittest
from email.message import Message
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from swarm_console.client import SwarmClient, seed_demo_swarm
from swarm_console.gemini_copilot import (
    DEFAULT_GEMINI_MODEL,
    GeminiCopilotError,
    analyze_swarm_state,
)
from swarm_console.protocol import (
    LockConflictError,
    PathIsolationError,
    SwarmStore,
    guard_safe_path,
)
from swarm_console.registry import (
    ModelSkillRegistry,
    normalize_model_record,
    normalize_models_dev_catalog,
    normalize_ollama_catalog,
    normalize_openrouter_catalog,
    scan_skills,
)
from swarm_console.server import SwarmHTTPServer, SwarmRequestHandler, create_server
from swarm_console.vault import SUPPORTED_PROVIDERS, ProviderKeyVault, mask_key


class TestPathIsolation(unittest.TestCase):
    def test_blocks_all_five_excluded_path_classes(self) -> None:
        blocked_paths = [
            "outputs/kraken-holdout",
            "outputs\\kraken-holdout\\run1",
            "research/holdout/.swarm",
            "runs/2026-11-24/.swarm",
            "runs/2026-11-30/state",
            "runs/20261124/board",
            "runs/20261130/board",
            "artifacts/2d8acaed/swarm",
            "studies/14-day/run",
            "studies/14day/run",
            "studies/active 14-day study/.swarm",
            "data/sampled-book/.swarm",
        ]
        for candidate in blocked_paths:
            with self.subTest(candidate=candidate):
                with self.assertRaises(PathIsolationError):
                    guard_safe_path(candidate)

    def test_allows_safe_workspace_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            safe_path = Path(tmp) / "research_swarm" / ".swarm"
            resolved = guard_safe_path(safe_path)
            self.assertEqual(resolved, safe_path.resolve())


class TestProtocolAndClient(unittest.TestCase):
    def test_atomic_state_updates_and_default_safety(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            swarm_dir = Path(tmp) / ".swarm"
            client = SwarmClient(swarm_dir)
            state = client.init_swarm(
                title="Unit Test Swarm",
                goal="Verify atomic persistence.",
                phase="executing",
            )
            self.assertFalse(state["safety"]["execution_authorized"])

            client.update_goal("Updated goal", phase="verifying")
            client.upsert_thread(
                thread_id="t1",
                title="Thread 1",
                outcome="Clean pass",
                status="active",
                workers=["worker-a"],
            )
            client.register_worker(
                name="worker-a",
                role="builder",
                model="test-model",
                locks=["pkg/a.py"],
                state="working",
                thread_id="t1",
            )
            msg1 = client.post_message("coordinator", "@worker-a start")
            msg2 = client.post_message("worker-a", "@coordinator [done] [PASS] finished")

            self.assertEqual(msg1["seq"], 1)
            self.assertEqual(msg2["seq"], 2)
            self.assertIn("worker-a", msg1["mentions"])
            self.assertEqual(msg2["tags"], ["done", "PASS"])

            # Verify no leftover .tmp files
            tmp_files = list(swarm_dir.glob("*.tmp"))
            self.assertEqual(tmp_files, [])

            # Verify fresh store read matches
            reloaded = SwarmStore(swarm_dir).get_state()
            self.assertEqual(reloaded["plan"]["goal"], "Updated goal")
            self.assertEqual(reloaded["plan"]["phase"], "verifying")
            self.assertEqual(len(reloaded["board"]), 2)
            self.assertFalse(reloaded["safety"]["execution_authorized"])

            # Verify A2A workflow link upsert, reload, and delete
            link1 = client.upsert_a2a_link(
                source="coordinator",
                target="worker-a",
                comm_type="pass_off",
                message_template="Hand off task slice",
            )
            link2 = client.upsert_a2a_link(
                source="worker-a",
                target="coordinator",
                comm_type="loop",
                condition="on_needs_work",
                max_loops=3,
            )
            self.assertEqual(len(client.list_a2a_links()), 2)
            state_with_links = SwarmStore(swarm_dir).get_state()
            self.assertEqual(len(state_with_links["a2a_links"]), 2)
            self.assertTrue(client.delete_a2a_link(link1["id"]))
            self.assertEqual([x["id"] for x in client.list_a2a_links()], [link2["id"]])

    def test_disjoint_lock_conflict_rejection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            client = SwarmClient(Path(tmp) / ".swarm", auto_init=True)
            client.register_worker(
                name="worker-a",
                locks=["swarm_console/protocol.py"],
                state="working",
            )

            # Exact overlap
            with self.assertRaises(LockConflictError):
                client.register_worker(
                    name="worker-b",
                    locks=["swarm_console/protocol.py"],
                    state="waiting",
                )

            # Glob overlap
            with self.assertRaises(LockConflictError):
                client.register_worker(
                    name="worker-b",
                    locks=["swarm_console/**"],
                    state="waiting",
                )

            # Disjoint lock succeeds
            client.register_worker(
                name="worker-b",
                locks=["swarm_console/ui.html"],
                state="waiting",
            )

            # Marking worker-a done releases its lock for another worker
            client.update_worker_state("worker-a", state="done")
            client.register_worker(
                name="worker-c",
                locks=["swarm_console/protocol.py"],
                state="working",
            )


class TestHTTPServerRoundTrips(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.swarm_dir = seed_demo_swarm(Path(self._tmp.name) / ".swarm")
        self.server = create_server(self.swarm_dir, host="127.0.0.1", port=0)
        self.port = self.server.server_address[1]
        self.base_url = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2.0)
        self._tmp.cleanup()

    def _get_json(self, path: str) -> tuple[int, dict]:
        with urlopen(self.base_url + path, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))

    def _post_json(self, path: str, payload: dict) -> tuple[int, dict]:
        data = json.dumps(payload).encode("utf-8")
        req = Request(
            self.base_url + path,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_ui_and_api_state_and_board_round_trip(self) -> None:
        try:
            with urlopen(self.base_url + "/", timeout=5) as resp:
                self.assertEqual(resp.status, 200)
                html = resp.read().decode("utf-8")
                self.assertIn("execution_authorized", html)
        except URLError as exc:
            if isinstance(exc.reason, PermissionError) and getattr(exc.reason, "winerror", None) == 10013:
                self.skipTest("Loopback TCP sockets blocked in sandbox (WinError 10013)")
            raise

        status, state = self._get_json("/api/state")
        self.assertEqual(status, 200)
        self.assertTrue(state["ok"])
        self.assertFalse(state["safety"]["execution_authorized"])
        initial_head = state["head_seq"]
        self.assertGreaterEqual(initial_head, 1)

        post_status, posted = self._post_json(
            "/api/board",
            {"agent": "test-runner", "text": "@server-agent [PASS] round-trip ok"},
        )
        self.assertEqual(post_status, 201)
        self.assertEqual(posted["message"]["seq"], initial_head + 1)

        board_status, board_resp = self._get_json(f"/api/board?since={initial_head}")
        self.assertEqual(board_status, 200)
        self.assertEqual(len(board_resp["messages"]), 1)
        self.assertEqual(board_resp["messages"][0]["agent"], "test-runner")

        # Worker lock conflict via HTTP POST /api/worker
        conflict_status, conflict_body = self._post_json(
            "/api/worker",
            {
                "name": "intruder-agent",
                "state": "working",
                "locks": ["swarm_console/server.py"],
            },
        )
        self.assertEqual(conflict_status, 409)
        self.assertEqual(conflict_body["error"], "lock_conflict")


class _FakeInteraction:
    def __init__(self, interaction_id: str, output_text: str) -> None:
        self.id = interaction_id
        self.output_text = output_text


class _FakeInteractionsResource:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        payload = {
            "health": "healthy",
            "summary": "All active workers are progressing without lock conflicts.",
            "blockers": [],
            "recommended_actions": ["Monitor verifier-agent completion."],
            "suggested_board_message": {
                "agent": "coordinator",
                "text": "@verifier-agent please post final [PASS] check.",
            },
        }
        return _FakeInteraction(f"int-{len(self.calls)}", json.dumps(payload))


class _FakeGenAIClient:
    def __init__(self) -> None:
        self.interactions = _FakeInteractionsResource()


class TestGeminiCopilot(unittest.TestCase):
    def test_analyze_swarm_state_uses_interactions_api_and_chaining(self) -> None:
        fake_client = _FakeGenAIClient()
        with tempfile.TemporaryDirectory() as tmp:
            swarm_dir = seed_demo_swarm(Path(tmp) / ".swarm")
            state = SwarmStore(swarm_dir).get_state()

        first = analyze_swarm_state(state, client=fake_client)
        self.assertEqual(first["model"], DEFAULT_GEMINI_MODEL)
        self.assertEqual(first["interaction_id"], "int-1")
        self.assertEqual(first["analysis"]["health"], "healthy")
        self.assertEqual(
            fake_client.interactions.calls[0]["response_format"]["mime_type"],
            "application/json",
        )

        second = analyze_swarm_state(
            state,
            question="Who should I ping next?",
            previous_interaction_id=first["interaction_id"],
            client=fake_client,
        )
        self.assertEqual(second["interaction_id"], "int-2")
        self.assertEqual(
            fake_client.interactions.calls[1]["previous_interaction_id"],
            "int-1",
        )

    def test_rejects_unsupported_model(self) -> None:
        fake_client = _FakeGenAIClient()
        with self.assertRaises(GeminiCopilotError) as ctx:
            analyze_swarm_state({}, model="gemini-2.5-pro", client=fake_client)
        self.assertEqual(ctx.exception.code, "invalid_model")

    def test_http_gemini_endpoints_with_custom_client_factory(self) -> None:
        fake_client = _FakeGenAIClient()
        with tempfile.TemporaryDirectory() as tmp:
            swarm_dir = seed_demo_swarm(Path(tmp) / ".swarm")
            server = create_server(
                swarm_dir,
                host="127.0.0.1",
                port=0,
                gemini_client_factory=lambda: fake_client,
            )
            port = server.server_address[1]
            base_url = f"http://127.0.0.1:{port}"
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with urlopen(base_url + "/api/gemini/status", timeout=5) as resp:
                    status_body = json.loads(resp.read().decode("utf-8"))
                self.assertTrue(status_body["ok"])
                self.assertTrue(status_body["available"])
                self.assertEqual(status_body["default_model"], DEFAULT_GEMINI_MODEL)

                req = Request(
                    base_url + "/api/gemini/analyze",
                    data=json.dumps({"question": "Summarize blockers"}).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urlopen(req, timeout=5) as resp:
                    analyze_body = json.loads(resp.read().decode("utf-8"))
                self.assertTrue(analyze_body["ok"])
                self.assertEqual(analyze_body["interaction_id"], "int-1")
                self.assertEqual(analyze_body["analysis"]["health"], "healthy")
            except URLError as exc:
                if isinstance(exc.reason, PermissionError) and getattr(exc.reason, "winerror", None) == 10013:
                    self.skipTest("Loopback TCP sockets blocked in sandbox (WinError 10013)")
                raise
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2.0)


class TestModelSkillRegistry(unittest.TestCase):
    def test_normalizers_and_malformed_inputs(self) -> None:
        # Malformed payloads return empty lists without raising
        self.assertEqual(normalize_openrouter_catalog(None), [])
        self.assertEqual(normalize_openrouter_catalog({"data": ["bad", {}]}), [])
        self.assertEqual(normalize_models_dev_catalog("not-a-dict"), [])
        self.assertEqual(normalize_ollama_catalog({"models": [None, {}]}), [])
        self.assertIsNone(normalize_model_record({"id": ""}))

        # OpenRouter normalization (pricing per-token -> per-1M, modalities)
        or_items = normalize_openrouter_catalog(
            {
                "data": [
                    {
                        "id": "anthropic/claude-sonnet-4",
                        "name": "Claude Sonnet 4",
                        "context_length": 200000,
                        "architecture": {
                            "input_modalities": ["text", "image"],
                            "output_modalities": ["text"],
                        },
                        "pricing": {"prompt": "0.000003", "completion": "0.000015"},
                    }
                ]
            }
        )
        self.assertEqual(len(or_items), 1)
        self.assertEqual(or_items[0]["id"], "openrouter/anthropic/claude-sonnet-4")
        self.assertEqual(or_items[0]["context_window"], 200000)
        self.assertEqual(or_items[0]["pricing"], {"input_per_1m": 3.0, "output_per_1m": 15.0})
        self.assertIn("image", or_items[0]["capabilities"])

        # models.dev normalization (deprecated status + reasoning)
        md_items = normalize_models_dev_catalog(
            {
                "openai": {
                    "models": {
                        "o3-mini": {
                            "id": "o3-mini",
                            "name": "o3-mini",
                            "reasoning": True,
                            "limit": {"context": 200000, "output": 100000},
                            "cost": {"input": 1.1, "output": 4.4},
                        },
                        "gpt-3.5-turbo-0301": {
                            "id": "gpt-3.5-turbo-0301",
                            "status": "deprecated",
                        },
                    }
                }
            }
        )
        self.assertEqual(len(md_items), 2)
        o3 = next(m for m in md_items if m["id"] == "openai/o3-mini")
        self.assertIn("reasoning", o3["capabilities"])
        self.assertFalse(o3["deprecated"])
        dep = next(m for m in md_items if m["id"] == "openai/gpt-3.5-turbo-0301")
        self.assertTrue(dep["deprecated"])

        # Ollama normalization
        ol_items = normalize_ollama_catalog({"models": [{"name": "qwen2.5-coder:32b"}]})
        self.assertEqual(len(ol_items), 1)
        self.assertEqual(ol_items[0]["provider"], "ollama")

    def test_850_plus_models_search_filtering_and_pagination(self) -> None:
        fake_openrouter = {
            "data": [
                {
                    "id": f"vendor-{i % 10}/model-{i}",
                    "name": f"Model {i} {'Vision' if i % 5 == 0 else 'Chat'}",
                    "context_length": 128000,
                    "architecture": {
                        "input_modalities": ["text", "image"] if i % 5 == 0 else ["text"],
                        "output_modalities": ["text"],
                    },
                    "deprecated": (i % 20 == 0),
                }
                for i in range(850)
            ]
        }

        def fake_fetch(url: str):
            if "openrouter" in url:
                return fake_openrouter
            raise OSError("offline")

        registry = ModelSkillRegistry(fetch_json=fake_fetch, skill_roots=[])
        page1 = registry.search_models(limit=50, offset=0)
        self.assertGreaterEqual(page1["catalog_total"], 850)
        self.assertEqual(len(page1["items"]), 50)
        self.assertTrue(all(not m["deprecated"] for m in page1["items"]))

        dep_page = registry.search_models(include_deprecated=True, limit=50, offset=0)
        self.assertGreater(dep_page["total"], page1["total"])

        filtered = registry.search_models(query="model-42", capability="chat", include_deprecated=True)
        self.assertTrue(any("model-42" in m["id"] for m in filtered["items"]))

    def test_scan_skills_handles_valid_and_malformed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "skills"
            valid_dir = root / "alpha-skill"
            valid_dir.mkdir(parents=True)
            (valid_dir / "SKILL.md").write_text(
                "---\nname: Alpha Skill\ndescription: >-\n  Multi-line\n  description here.\n---\n# Body\n",
                encoding="utf-8",
            )
            no_fm_dir = root / "plain-skill"
            no_fm_dir.mkdir(parents=True)
            (no_fm_dir / "SKILL.md").write_text("# Just markdown\n", encoding="utf-8")
            empty_dir = root / "not-a-skill"
            empty_dir.mkdir(parents=True)

            skills = scan_skills([root])
            self.assertEqual(len(skills), 2)
            alpha = next(s for s in skills if s["id"] == "alpha-skill")
            self.assertEqual(alpha["name"], "Alpha Skill")
            self.assertEqual(alpha["description"], "Multi-line description here.")

            reg = ModelSkillRegistry(enable_remote=False, skill_roots=[root])
            res = reg.search_skills(query="multi-line")
            self.assertEqual(res["total"], 1)
            self.assertEqual(res["items"][0]["id"], "alpha-skill")


class _InMemoryHandlerHarness:
    """Invokes SwarmRequestHandler.do_GET/do_POST in-memory without a TCP socket."""

    def __init__(self, server: SwarmHTTPServer) -> None:
        self.server = server

    def request(self, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
        raw_body = json.dumps(body).encode("utf-8") if body is not None else b""
        rfile = io.BytesIO(raw_body)
        wfile = io.BytesIO()

        class _DummySocket:
            def makefile(self, mode: str, *args, **kwargs):
                return rfile if "r" in mode else wfile

            def sendall(self, data: bytes) -> None:
                wfile.write(data)

        handler = object.__new__(SwarmRequestHandler)
        handler.server = self.server
        handler.client_address = ("127.0.0.1", 0)
        handler.request = _DummySocket()
        handler.rfile = rfile
        handler.wfile = wfile
        handler.requestline = f"{method} {path} HTTP/1.1"
        handler.request_version = "HTTP/1.1"
        handler.command = method
        handler.path = path
        headers = Message()
        if body is not None:
            headers["Content-Type"] = "application/json"
            headers["Content-Length"] = str(len(raw_body))
        handler.headers = headers
        handler.close_connection = True

        if method == "GET":
            handler.do_GET()
        elif method == "POST":
            handler.do_POST()
        else:
            raise ValueError(f"Unsupported method: {method}")

        raw_resp = wfile.getvalue()
        head_bytes, _, resp_body = raw_resp.partition(b"\r\n\r\n")
        status_line = head_bytes.decode("utf-8", errors="replace").splitlines()[0]
        status_code = int(status_line.split()[1])
        return status_code, json.loads(resp_body.decode("utf-8"))


class TestProviderKeyVault(unittest.TestCase):
    def test_dpapi_round_trip_masking_and_delete(self) -> None:
        secret = "AIzaSyTestSecretKey9999xQ2"
        self.assertEqual(mask_key(secret), "AIza...9xQ2")
        self.assertEqual(mask_key("short"), "••••••••")

        with tempfile.TemporaryDirectory() as tmp:
            vault_path = Path(tmp) / "vault.json"
            vault = ProviderKeyVault(vault_path=vault_path)
            status = vault.set_key("gemini", secret)
            self.assertTrue(status["configured"])
            self.assertTrue(status["unlocked"])
            self.assertEqual(status["source"], "vault")
            self.assertEqual(status["masked_key"], "AIza...9xQ2")

            raw_bytes = vault_path.read_bytes()
            self.assertNotIn(secret.encode("utf-8"), raw_bytes)
            disk_json = json.loads(raw_bytes.decode("utf-8"))
            self.assertEqual(disk_json["keys"]["gemini"]["scheme"], "dpapi")

            # Re-open from disk and decrypt
            reloaded = ProviderKeyVault(vault_path=vault_path)
            self.assertEqual(reloaded.get_key("gemini", include_env=False), secret)
            self.assertTrue(reloaded.is_provider_unlocked("gemini"))
            self.assertTrue(reloaded.is_provider_unlocked("gemma"))
            self.assertTrue(reloaded.is_provider_unlocked("openrouter:gemini"))
            self.assertTrue(reloaded.is_provider_unlocked("ollama"))

            # Delete key
            after_del = reloaded.delete_key("gemini")
            self.assertIsNone(reloaded.get_key("gemini", include_env=False))
            gem_row = next(p for p in after_del["providers"] if p["provider"] == "gemini")
            self.assertFalse(gem_row["configured"])
            self.assertEqual(gem_row["source"], "none")

    def test_env_fallback_and_provider_list(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault = ProviderKeyVault(vault_path=Path(tmp) / "vault.json")
            with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-env-1234567890abcd"}, clear=False):
                self.assertEqual(vault.get_key("openai", include_env=True), "sk-env-1234567890abcd")
                self.assertIsNone(vault.get_key("openai", include_env=False))
                listing = vault.list_providers()
                self.assertEqual(listing["encryption"], "dpapi")
                self.assertEqual(len(listing["providers"]), len(SUPPORTED_PROVIDERS))
                openai_row = next(p for p in listing["providers"] if p["provider"] == "openai")
                self.assertEqual(openai_row["source"], "env")
                self.assertTrue(openai_row["unlocked"])
                self.assertEqual(openai_row["masked_key"], "sk-e...abcd")

    def test_live_probing_and_error_scrubbing(self) -> None:
        seen_requests: list[tuple[str, dict[str, str]]] = []

        def fake_fetch(url: str, headers: dict[str, str] | None = None, timeout: float = 6.0):
            hdrs = headers or {}
            seen_requests.append((url, hdrs))
            if "generativelanguage.googleapis.com" in url:
                return {
                    "models": [
                        {
                            "name": "models/gemini-4-carbon",
                            "displayName": "Gemini 4 Carbon",
                            "inputTokenLimit": 1000000,
                            "outputTokenLimit": 65536,
                        }
                    ]
                }
            if "api.openai.com" in url:
                if hdrs.get("Authorization") == "Bearer sk-bad-secret-token":
                    raise HTTPError(url, 401, "Unauthorized for key sk-bad-secret-token", Message(), None)
                return {"data": [{"id": "gpt-4.1", "name": "GPT-4.1"}]}
            if "api.anthropic.com" in url:
                return {"data": [{"id": "claude-sonnet-4", "display_name": "Claude Sonnet 4"}]}
            if "openrouter.ai" in url:
                return {"data": [{"id": "anthropic/claude-sonnet-4", "name": "Claude Sonnet 4"}]}
            if "11434/api/tags" in url:
                return {"models": [{"name": "qwen2.5:14b"}]}
            raise OSError("unexpected url")

        with tempfile.TemporaryDirectory() as tmp:
            vault = ProviderKeyVault(vault_path=Path(tmp) / "vault.json", fetch_json=fake_fetch)
            vault.set_key("gemini", "AIza-gem-test-key")
            vault.set_key("anthropic", "sk-ant-test-key")
            vault.set_key("openrouter", "sk-or-test-key")

            all_res = vault.probe_all_unlocked()
            self.assertTrue(all_res["ok"])
            self.assertTrue(all_res["results"]["gemini"]["valid"])
            self.assertTrue(all_res["results"]["anthropic"]["valid"])
            self.assertTrue(all_res["results"]["openrouter"]["valid"])
            self.assertTrue(all_res["results"]["ollama"]["valid"])

            # Verify headers sent
            gem_hdrs = next(h for u, h in seen_requests if "generativelanguage" in u)
            self.assertEqual(gem_hdrs["x-goog-api-key"], "AIza-gem-test-key")
            ant_hdrs = next(h for u, h in seen_requests if "anthropic" in u)
            self.assertEqual(ant_hdrs["x-api-key"], "sk-ant-test-key")
            self.assertEqual(ant_hdrs["anthropic-version"], "2023-06-01")

            # Verify 401 error scrubbing
            fail_res = vault.probe_provider("openai", api_key="sk-bad-secret-token")
            self.assertFalse(fail_res["ok"])
            self.assertFalse(fail_res["valid"])
            self.assertIsNotNone(fail_res["error"])
            self.assertNotIn("sk-bad-secret-token", fail_res["error"])

    def test_registry_and_server_vault_routes_in_memory(self) -> None:
        def fake_fetch(url: str, headers: dict[str, str] | None = None, timeout: float = 6.0):
            if "api.openai.com" in url:
                return {"data": [{"id": "gpt-4.1-mini", "name": "GPT-4.1 Mini"}]}
            if "11434/api/tags" in url:
                return {"models": [{"name": "llama3.3:70b"}]}
            return {"data": []}

        with tempfile.TemporaryDirectory() as tmp:
            swarm_dir = seed_demo_swarm(Path(tmp) / ".swarm")
            vault = ProviderKeyVault(vault_path=swarm_dir / "vault.json", fetch_json=fake_fetch)
            registry = ModelSkillRegistry(enable_remote=False, skill_roots=[], vault=vault)
            server = create_server(swarm_dir, port=0, registry=registry, vault=vault)
            try:
                harness = _InMemoryHandlerHarness(server)

                code, status_body = harness.request("GET", "/api/vault/status")
                self.assertEqual(code, 200)
                self.assertTrue(status_body["ok"])
                self.assertEqual(status_body["encryption"], "dpapi")

                code, set_body = harness.request(
                    "POST",
                    "/api/vault/key",
                    {"provider": "openai", "api_key": "sk-openai-1234567890", "probe": True},
                )
                self.assertEqual(code, 200)
                self.assertTrue(set_body["ok"])
                self.assertTrue(set_body["probe"]["valid"])
                self.assertIn("openai", set_body["unlocked_providers"])

                code, models_body = harness.request("GET", "/api/registry/models?unlocked_only=1")
                self.assertEqual(code, 200)
                self.assertTrue(models_body["ok"])
                self.assertIn("openai", models_body["unlocked_providers"])
                self.assertGreater(models_body["unlocked_total"], 0)
                self.assertTrue(all(m["unlocked"] is True for m in models_body["items"]))
                self.assertTrue(any(m["id"] == "openai/gpt-4.1-mini" for m in models_body["items"]))

                code, probe_all_body = harness.request("POST", "/api/vault/probe-all", {})
                self.assertEqual(code, 200)
                self.assertTrue(probe_all_body["ok"])

                code, del_body = harness.request("POST", "/api/vault/delete", {"provider": "openai"})
                self.assertEqual(code, 200)
                self.assertTrue(del_body["ok"])
                self.assertNotIn("openai", del_body["unlocked_providers"])

                # Custom model add / tier filter / remove routes
                code, add_body = harness.request(
                    "POST",
                    "/api/registry/models/add",
                    {
                        "id": "openrouter/deepseek/deepseek-r1",
                        "name": "DeepSeek R1",
                        "tier": "external",
                        "provider": "openrouter",
                        "role": "External Reasoner",
                    },
                )
                self.assertEqual(code, 201)
                self.assertTrue(add_body["ok"])
                self.assertEqual(add_body["model"]["tier"], "external")
                self.assertTrue(add_body["model"]["custom"])

                code, ext_body = harness.request("GET", "/api/registry/models?tier=external")
                self.assertEqual(code, 200)
                self.assertTrue(any(m["id"] == "openrouter/deepseek/deepseek-r1" for m in ext_body["items"]))

                code, rem_body = harness.request(
                    "POST",
                    "/api/registry/models/remove",
                    {"id": "openrouter/deepseek/deepseek-r1"},
                )
                self.assertEqual(code, 200)
                self.assertTrue(rem_body["removed"])
            finally:
                server.server_close()


if __name__ == "__main__":
    unittest.main()
