"""Coverage for the interactive backend catalog and fleet isolation guards."""
import os
import socket
import subprocess
import unittest
from unittest.mock import Mock, patch

from sabrina_console_desktop.interactive_tools import create_interactive_session, list_interactive_backends
from sabrina_console_desktop.agent_browser_service.fleet import FleetBrowserSession
from sabrina_console_desktop.agent_browser_service.fleet_worker import FleetWorker
from sabrina_console_desktop.sabrina_desktop_ui import SabrinaDesktopBridge


class InteractiveToolTests(unittest.TestCase):
    def test_catalog_has_unique_ids_and_constructs_each_backend(self):
        backends = list_interactive_backends()
        self.assertEqual(len({row["id"] for row in backends}), len(backends))
        classes = {key: Mock(name=key) for key in ("desktop", "visible", "fleet", "container")}
        create_interactive_session("ego-fleet", "https://example.com/", session_types=classes)
        classes["fleet"].assert_called_once_with("https://example.com/")
        create_interactive_session("visible-edge", "https://example.com/", manual_start=True, session_types=classes)
        classes["visible"].assert_called_once_with("visible-edge", "https://example.com/", manual_start=True)
        create_interactive_session("agent-desktop", desktop_window_id="w-42", headed=True, session_types=classes)
        classes["desktop"].assert_called_once_with("w-42", headed=True, manual_start=False)
        create_interactive_session("agent-browser", "https://example.com/", session_types=classes)
        classes["container"].assert_called_once_with("agent-browser", "https://example.com/")
        with self.assertRaises(ValueError):
            create_interactive_session("unknown", "https://example.com/", session_types=classes)

    def test_only_known_fleet_nodes_are_accepted(self):
        with patch.dict(os.environ, {"SABRINA_FLEET_NODE": "browser_fleet_node_1; echo unsafe"}):
            with self.assertRaises(ValueError):
                FleetBrowserSession("https://example.com/")

    def test_readiness_requires_both_a_running_node_and_a_working_server(self):
        session = FleetBrowserSession("https://example.com/")
        with patch("sabrina_console_desktop.agent_browser_service.fleet.docker_command", return_value=["docker"]), patch(
            "sabrina_console_desktop.agent_browser_service.fleet.subprocess.run",
            side_effect=[Mock(returncode=0, stdout="true\n"), Mock(returncode=1, stdout="")],
        ):
            with self.assertRaisesRegex(RuntimeError, "Playwright server is unavailable"):
                session.check_ready()

    def test_fleet_rejects_private_dns_and_other_navigation_origins(self):
        worker = FleetWorker.__new__(FleetWorker)
        worker.origin = "https://example.com"
        worker._dns = {}
        worker.blocked_navigation = ""
        public = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))]
        private = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]
        with patch("socket.getaddrinfo", return_value=public):
            self.assertTrue(worker._public_url("https://example.com/a"))
            route = Mock()
            route.request.url = "https://another.example/path"
            route.request.is_navigation_request.return_value = True
            worker._route(route)
            route.abort.assert_called_once()
            self.assertIn("outside", worker.blocked_navigation)
        with patch("socket.getaddrinfo", return_value=private):
            self.assertFalse(worker._public_url("https://private.example/"))
        self.assertFalse(worker._public_url("http://example.com/"))

    def test_fleet_cleanup_never_removes_the_shared_container(self):
        session = FleetBrowserSession("https://example.com/")
        process = Mock()
        process.wait.side_effect = [subprocess.TimeoutExpired("ssh", 5), 0]
        session._process = process
        with patch("sabrina_console_desktop.agent_browser_service.fleet.docker_command") as docker:
            session.close()
        docker.assert_not_called()
        process.terminate.assert_called_once()
        self.assertIn("did not exit", session.cleanup_error)

    def test_fleet_refuses_stale_refs_and_does_not_act_on_a_verification(self):
        worker = FleetWorker.__new__(FleetWorker)
        worker.start_url = "https://example.com/"
        worker.origin = "https://example.com"
        worker.latest_snapshot = '- button "Current" [ref=e2]'
        worker.blocked_verification = False
        stale = worker.run({"action": "click", "ref": "e1"})
        self.assertFalse(stale["dispatched"])
        self.assertIn("latest snapshot", stale["error"])
        worker.blocked_verification = True
        result = worker.run({"action": "click", "ref": "e2"})
        self.assertFalse(result["dispatched"])
        self.assertIn("user verification", result["error"])

    def test_configured_searxng_is_used_for_web_grounding(self):
        bridge = SabrinaDesktopBridge.__new__(SabrinaDesktopBridge)
        bridge.connector_registry = Mock()
        bridge.connector_registry.is_configured.return_value = True
        bridge.connector_registry.execute_action.return_value = {"ok": True, "data": {"results": [{"title": "Example"}]}}
        result = bridge.ground_swarm_web("web_search", "example query")
        self.assertTrue(result["ok"])
        self.assertEqual(result["source"], "searxng")
        bridge.connector_registry.execute_action.assert_called_once_with("searxng", "web.search", {"query": "example query"})


if __name__ == "__main__":
    unittest.main()
