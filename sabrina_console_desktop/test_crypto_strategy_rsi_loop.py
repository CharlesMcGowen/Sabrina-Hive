"""Unit tests for SwarmSearchHydrator arXiv RSS and CryptoStrategyRsiEngine."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tools.swarm_search_hydrator import SwarmSearchHydrator, SearchResult
from tools.crypto_strategy_rsi_loop import CryptoStrategyRsiEngine, RsiGenerationResult


class TestCryptoStrategyRsiLoop(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_search_hydrator_includes_strategy_and_arxiv(self) -> None:
        """Verify that searching for crypto strategies returns blueprints, local proofs, and arXiv papers."""
        hydrator = SwarmSearchHydrator(workspace_root=str(self.tmp_path))
        res = hydrator.search_and_hydrate("crypto trading strategies order book imbalance")

        self.assertGreater(res["total_hits"], 0)
        source_types = {r["source_type"] for r in res["results"]}
        self.assertIn("strategy", source_types)
        self.assertIn("arxiv", source_types)

        # Check prompt formatting
        prompt = res["hydration_prompt"]
        self.assertIn("Hydrated Knowledge", prompt)
        self.assertIn("Barrier Edge Theorem", prompt)

    def test_validator_retry_loop_captures_and_repairs_errors(self) -> None:
        """Test that CryptoStrategyRsiEngine compiler loop captures exact validation errors and retries."""
        call_count = 0

        def mocking_llm_caller(role: str, model: str, sys_p: str, usr_p: str) -> dict[str, str]:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # First attempt: Invalid drawdown > 0 and missing usd_vol
                bad_signal = [
                    {
                        "name": "bad_signal",
                        "rationale": "Faulty units test",
                        "all": [{"feature": "drawdown_from_high", "n": 30, "op": "<", "value": 0.95}],
                        "barriers": {"up_pct": 5, "down_pct": 1, "max_hold_bars": 100},
                        "cooldown_bars": 24,
                    }
                ]
                return {"text": json.dumps(bad_signal)}
            else:
                # Second attempt: Repaired correctly after receiving feedback error
                self.assertIn("VALIDATION ERROR ON ATTEMPT 1", usr_p)
                self.assertIn("drawdown_from_high", usr_p)
                good_signal = [
                    {
                        "name": "repaired_signal",
                        "rationale": "Fixed units after compiler feedback",
                        "all": [
                            {"feature": "drawdown_from_high", "n": 30, "op": "<=", "value": -0.05},
                            {"feature": "usd_vol", "n": 96, "op": ">=", "value": 50000},
                        ],
                        "barriers": {"up_pct": 5, "down_pct": 1, "max_hold_bars": 100},
                        "cooldown_bars": 24,
                    }
                ]
                return {"text": json.dumps(good_signal)}

        engine = CryptoStrategyRsiEngine(
            workspace_root=self.tmp_path,
            llm_caller=mocking_llm_caller,
            max_generations=2,
            max_validation_retries=2,
        )

        ok, signals, retries, err = engine.generate_and_validate(generation=1, previous_disproven=[])
        self.assertTrue(ok)
        self.assertEqual(retries, 1)  # 1 retry performed
        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0]["name"], "repaired_signal")

    def test_stopping_rule_triggered_at_max_generations(self) -> None:
        """Verify that engine halts with stopping rule when budget is exhausted."""
        engine = CryptoStrategyRsiEngine(
            workspace_root=self.tmp_path,
            max_generations=2,
        )

        # Run cycle 1
        res1 = engine.run_cycle(generation=1)
        self.assertFalse(res1.stopping_rule_triggered)
        self.assertGreater(len(res1.disproven), 0)

        # Run cycle 2 (reaches max_generations)
        res2 = engine.run_cycle(generation=2)
        self.assertTrue(res2.stopping_rule_triggered)
        self.assertIn("STOPPING RULE REACHED", res2.verdict_summary)

        # Verify disproven ledger persisted
        disproven = engine.load_disproven_history()
        self.assertGreater(len(disproven), 0)


if __name__ == "__main__":
    unittest.main()
