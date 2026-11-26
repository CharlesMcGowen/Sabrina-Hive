"""Unit tests for parallel Scheduler dispatch, execution_kind, and resource mutual exclusion."""
from __future__ import annotations

import sys
import tempfile
import threading
import time
import unittest
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sabrina_social.scheduler import JobStore, Scheduler  # noqa: E402


@dataclass(frozen=True)
class _FakeCapWithKind:
    name: str
    resources: tuple[str, ...] = ()
    execution_kind: str = "code"


@dataclass(frozen=True)
class _FakeCapWithoutKind:
    name: str
    resources: tuple[str, ...] = ()


class _FakeRegistry:
    def __init__(self, caps: dict[str, dict]):
        self._caps = caps

    def discover(self, action=None, capability=None, qualification_state=None):
        if capability:
            entry = self._caps.get(capability)
            return [dict(entry)] if entry else []
        return [dict(v) for v in self._caps.values()]


class TestSchedulerParallel(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db_path = Path(self._tmp.name) / "scheduler.sqlite3"
        self.store = JobStore(self.db_path)
        self.registry = _FakeRegistry({
            "code.a": {"name": "code.a", "resources": [], "execution_kind": "code"},
            "code.b": {"name": "code.b", "resources": ["compute:lab"], "execution_kind": "code"},
            "code.c": {"name": "code.c", "resources": ["compute:lab"], "execution_kind": "code"},
            "code.d": {"name": "code.d", "resources": [], "execution_kind": "code"},
            "agent.a": {"name": "agent.a", "resources": [], "execution_kind": "agent"},
            "agent.b": {"name": "agent.b", "resources": [], "execution_kind": "agent"},
            "browser.li1": {"name": "browser.li1", "resources": ["browser:linkedin-primary"], "execution_kind": "browser"},
            "browser.li2": {"name": "browser.li2", "resources": ["browser:linkedin-primary"], "execution_kind": "browser"},
            "browser.x1": {"name": "browser.x1", "resources": ["browser:x-primary"], "execution_kind": "browser"},
            "custom.legacy": {"name": "custom.legacy", "resources": []},
        })

    def tearDown(self):
        self._tmp.cleanup()

    def test_get_execution_kind_and_metadata(self):
        sched = Scheduler(store=self.store, registry=self.registry, adapters={})
        self.assertEqual(sched.get_execution_kind("code.a"), "code")
        self.assertEqual(sched.get_execution_kind("agent.a"), "agent")
        self.assertEqual(sched.get_execution_kind("browser.li1"), "browser")
        self.assertEqual(sched.get_execution_kind("custom.legacy"), "code")
        self.assertEqual(sched.get_execution_kind("unknown.cap"), "code")
        self.assertEqual(sched.get_execution_kind(_FakeCapWithKind("x", execution_kind="agent")), "agent")
        self.assertEqual(sched.get_execution_kind(_FakeCapWithoutKind("y")), "code")
        self.assertEqual(sched.get_execution_kind({"execution_kind": "browser"}), "browser")
        self.assertEqual(sched.get_execution_kind({}), "code")

        job = sched.schedule("agent.a", {"k": 1}, next_run=0)
        self.assertEqual(job["execution_kind"], "agent")
        scheduled_ev = [e for e in job["events"] if e["phase"] == "scheduled"][0]
        self.assertIn('"execution_kind": "agent"', scheduled_ev["detail"])

    def test_dispatch_once_backward_compatible(self):
        sched = Scheduler(
            store=self.store,
            registry=self.registry,
            adapters={"code.a": lambda p: {"ok": True, "v": p["v"]}},
        )
        job = sched.schedule("code.a", {"v": 42}, next_run=0)
        done = sched.dispatch_once()
        self.assertIsNotNone(done)
        self.assertEqual(done["id"], job["id"])
        self.assertEqual(done["status"], "completed")
        self.assertEqual(done["result"], {"ok": True, "v": 42})
        self.assertIsNone(sched.dispatch_once())

    def test_parallel_dispatch_respects_resource_mutual_exclusion(self):
        lock = threading.Lock()
        active_resources: set[str] = set()
        max_concurrent = 0
        current_concurrent = 0
        resource_overlap_detected = False

        def make_adapter(res_tags: list[str], delay: float = 0.08):
            def _run(payload):
                nonlocal max_concurrent, current_concurrent, resource_overlap_detected
                with lock:
                    if active_resources & set(res_tags):
                        resource_overlap_detected = True
                    active_resources.update(res_tags)
                    current_concurrent += 1
                    max_concurrent = max(max_concurrent, current_concurrent)
                time.sleep(delay)
                with lock:
                    current_concurrent -= 1
                    for tag in res_tags:
                        active_resources.discard(tag)
                return {"ok": True}
            return _run

        sched = Scheduler(
            store=self.store,
            registry=self.registry,
            adapters={
                "browser.li1": make_adapter(["browser:linkedin-primary"]),
                "browser.li2": make_adapter(["browser:linkedin-primary"]),
                "browser.x1": make_adapter(["browser:x-primary"]),
                "code.a": make_adapter([]),
            },
            max_code_workers=4,
            max_agent_workers=2,
            max_browser_workers=2,
        )
        j_li1 = sched.schedule("browser.li1", {}, next_run=1)
        j_li2 = sched.schedule("browser.li2", {}, next_run=2)
        j_x1 = sched.schedule("browser.x1", {}, next_run=3)
        j_code = sched.schedule("code.a", {}, next_run=4)

        step1 = sched.dispatch_parallel_step()
        step1_ids = {j["id"] for j in step1}
        # li1, x1, and code.a run in parallel; li2 is deferred because it shares browser:linkedin-primary with li1.
        self.assertEqual(step1_ids, {j_li1["id"], j_x1["id"], j_code["id"]})
        self.assertFalse(resource_overlap_detected)
        self.assertGreaterEqual(max_concurrent, 2)

        step2 = sched.dispatch_parallel_step()
        self.assertEqual([j["id"] for j in step2], [j_li2["id"]])
        self.assertEqual(step2[0]["status"], "completed")

    def test_concurrency_caps_per_execution_kind(self):
        lock = threading.Lock()
        counts = {"code": 0, "agent": 0, "browser": 0}
        max_counts = {"code": 0, "agent": 0, "browser": 0}

        def make_adapter(kind: str):
            def _run(payload):
                with lock:
                    counts[kind] += 1
                    max_counts[kind] = max(max_counts[kind], counts[kind])
                time.sleep(0.06)
                with lock:
                    counts[kind] -= 1
                return {"ok": True}
            return _run

        sched = Scheduler(
            store=self.store,
            registry=self.registry,
            adapters={
                "code.a": make_adapter("code"),
                "code.d": make_adapter("code"),
                "agent.a": make_adapter("agent"),
                "agent.b": make_adapter("agent"),
                "browser.x1": make_adapter("browser"),
            },
            max_code_workers=2,
            max_agent_workers=1,
            max_browser_workers=1,
        )
        sched.schedule("code.a", {}, next_run=1)
        sched.schedule("code.a", {}, next_run=2)
        sched.schedule("code.d", {}, next_run=3)
        sched.schedule("agent.a", {}, next_run=4)
        sched.schedule("agent.b", {}, next_run=5)
        sched.schedule("browser.x1", {}, next_run=6)

        step1 = sched.dispatch_parallel_step()
        # Cap allows 2 code + 1 agent + 1 browser = 4 in first wave.
        self.assertEqual(len(step1), 4)
        self.assertEqual(max_counts["code"], 2)
        self.assertEqual(max_counts["agent"], 1)
        self.assertEqual(max_counts["browser"], 1)

        step2 = sched.dispatch_parallel_step()
        self.assertEqual(len(step2), 2)

    def test_run_forever_parallel_and_clean_stop(self):
        stop_flag = threading.Event()
        completed_count = 0
        lock = threading.Lock()

        def _work(payload):
            nonlocal completed_count
            time.sleep(0.05)
            with lock:
                completed_count += 1
            return {"ok": True}

        sched = Scheduler(
            store=self.store,
            registry=self.registry,
            adapters={
                "code.a": _work,
                "code.b": _work,
                "code.c": _work,
                "agent.a": _work,
            },
            max_code_workers=3,
            max_agent_workers=1,
            max_browser_workers=1,
        )
        jobs = [
            sched.schedule("code.a", {}, next_run=1),
            sched.schedule("code.b", {}, next_run=2),
            sched.schedule("code.c", {}, next_run=3),  # shares compute:lab with code.b
            sched.schedule("agent.a", {}, next_run=4),
        ]

        runner = threading.Thread(
            target=sched.run_forever,
            kwargs={"interval_seconds": 0.1, "stop": stop_flag.is_set},
            daemon=True,
        )
        runner.start()

        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            with lock:
                if completed_count == 4:
                    break
            time.sleep(0.02)
        stop_flag.set()
        runner.join(timeout=3.0)
        self.assertFalse(runner.is_alive())
        self.assertEqual(completed_count, 4)
        for j in jobs:
            self.assertEqual(self.store.get(j["id"])["status"], "completed")

    def test_reload_registry_if_changed(self):
        from tools.capability_registry import CapabilityRegistry
        reg_db = Path(self._tmp.name) / "capabilities.sqlite3"
        sched = Scheduler(store=self.store, registry=CapabilityRegistry(reg_db), adapters={})
        initial_reg = sched.registry
        self.assertIsNotNone(sched.registry_mtime)
        # No change on disk -> False, same registry object
        self.assertFalse(sched.reload_registry_if_changed())
        self.assertIs(sched.registry, initial_reg)

        # Simulated mtime change -> True, rebuilt registry preserving path
        sched.registry_mtime = (sched.registry_mtime or 0.0) - 10.0
        self.assertTrue(sched.reload_registry_if_changed())
        self.assertIsNot(sched.registry, initial_reg)
        self.assertEqual(Path(sched.registry.path), reg_db)

        # Force reload -> True
        prev_reg = sched.registry
        self.assertTrue(sched.reload_registry_if_changed(force=True))
        self.assertIsNot(sched.registry, prev_reg)

    def test_api_services_status_and_reload_endpoints(self):
        from sabrina_social import api as social_api
        status = social_api.get_services_status()
        self.assertTrue(status["ok"])
        self.assertIsInstance(status["pid"], int)
        self.assertIn("uptime_sec", status)
        self.assertIn("registry_mtime", status)
        self.assertEqual(set(status["lane_limits"].keys()), {"code", "agent", "browser"})

        reloaded = social_api.reload_system_registry()
        self.assertTrue(reloaded["ok"])
        self.assertTrue(reloaded["reloaded"])
        self.assertIs(social_api.capability_registry, social_api.scheduler.registry)


if __name__ == "__main__":
    unittest.main()
