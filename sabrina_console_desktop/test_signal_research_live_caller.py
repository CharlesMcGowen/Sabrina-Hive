"""The signal research loop with a model caller, and the console's /signals entry point."""
from __future__ import annotations

import json
import tempfile
import types
import unittest
from pathlib import Path

from tools.crypto_strategy_rsi_loop import TEMPLATE_AUTHOR, CryptoStrategyRsiEngine, format_cycle_report
from test_signal_promotion_gate import SIGNAL, market


class LiveCallerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def test_model_reply_is_registered_under_the_model_that_wrote_it(self) -> None:
        seen: list[str] = []

        def caller(role, model_id, system_prompt, user_prompt):
            seen.append(model_id)
            return {"ok": True, "text": json.dumps([SIGNAL]), "provider": "ollama", "model": "gemma3:12b",
                    "used_fallback": True, "requested_model": "gemini-4-neon"}

        engine = CryptoStrategyRsiEngine(workspace_root=self.root, market=market(winning=False),
                                         llm_caller=caller, model_id="gemini-4-neon")
        res = engine.run_cycle(generation=1)
        self.assertEqual(seen, ["gemini-4-neon"])
        self.assertEqual(engine.last_author, "ollama/gemma3:12b (fallback from gemini-4-neon)")
        self.assertEqual((len(res.promoted), len(res.disproven)), (0, 1))
        ledger_lines = (engine.artifacts_dir / "ledger" / "speculative.jsonl").read_text(encoding="utf-8")
        self.assertIn("rsi_gen1:ollama/gemma3:12b (fallback from gemini-4-neon)", ledger_lines)
        report = format_cycle_report(res, engine.last_author)
        self.assertIn("Proposed by: ollama/gemma3:12b (fallback from gemini-4-neon)", report)
        self.assertIn("up2: fails", report)
        self.assertIn("execution_authorized=false", report)

    def test_measured_pass_is_reported_as_candidate(self) -> None:
        engine = CryptoStrategyRsiEngine(
            workspace_root=self.root, market=market(winning=True),
            llm_caller=lambda *a: {"ok": True, "text": json.dumps([SIGNAL]), "provider": "gemini", "model": "m"})
        res = engine.run_cycle(generation=1)
        self.assertEqual(len(res.promoted), 1)
        self.assertIn("FINALIZED AS CANDIDATE", format_cycle_report(res, engine.last_author))

    def test_failed_model_call_stops_without_retrying_or_inventing_signals(self) -> None:
        calls = 0

        def caller(role, model_id, system_prompt, user_prompt):
            nonlocal calls
            calls += 1
            return {"ok": False, "error": "provider unavailable"}

        engine = CryptoStrategyRsiEngine(workspace_root=self.root, market=market(winning=False), llm_caller=caller)
        res = engine.run_cycle(generation=1)
        self.assertEqual(calls, 1)
        self.assertEqual(res.valid_signals, [])
        self.assertIn("Model call failed: provider unavailable", res.verdict_summary)
        self.assertFalse((engine.artifacts_dir / "ledger" / "speculative.jsonl").exists())

    def test_template_signals_are_labelled_as_not_from_a_model(self) -> None:
        engine = CryptoStrategyRsiEngine(workspace_root=self.root, market=market(winning=False))
        res = engine.run_cycle(generation=1)
        self.assertEqual(engine.last_author, TEMPLATE_AUTHOR)
        self.assertIn("No model was used", format_cycle_report(res, engine.last_author))


class ConsoleEntryPointTests(unittest.TestCase):
    def bridge(self, caller):
        from sabrina_desktop_ui import SabrinaDesktopBridge

        fake = types.SimpleNamespace(
            swarm_runner=types.SimpleNamespace(_resolve_model_for_role=lambda role: "gemini-4-neon"),
            _call_swarm_llm=caller,
        )
        return lambda root: SabrinaDesktopBridge.run_signal_research(fake, str(root))

    def test_model_failure_is_reported_and_stopping_rule_blocks_new_runs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = self.bridge(lambda *a, **k: {"ok": False, "error": "provider unavailable"})
            res = run(tmp)
            self.assertTrue(res["ok"])
            self.assertFalse(res["execution_authorized"])
            self.assertIn("Model call failed: provider unavailable", res["reply"])

            history = Path(tmp) / "sabrina_console_desktop" / "artifacts" / "gainers_paper" / "rsi_run_history.jsonl"
            history.write_text(json.dumps({"generation": 4, "stopping_rule_triggered": True,
                                           "verdict_summary": "STOPPING RULE REACHED at Gen 4."}) + "\n", encoding="utf-8")

            def must_not_call(*a, **k):
                raise AssertionError("model called after the stopping rule")

            res = self.bridge(must_not_call)(tmp)
            self.assertTrue(res["stopped"])
            self.assertIn("stopping rule was reached in generation 4", res["reply"])


if __name__ == "__main__":
    unittest.main()
