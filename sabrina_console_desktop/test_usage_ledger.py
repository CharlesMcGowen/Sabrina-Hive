import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from tools import usage_ledger as ul

NOW = datetime(2026, 11, 26, tzinfo=timezone.utc)


def measured(model, provider, role_in, role_out):
    return {"ok": True, "model": model, "provider": provider, "tokens_used": role_in + role_out,
            "token_usage": {"input": role_in, "output": role_out}}


class UsageLedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "usage.jsonl"
        ul.set_run_label("")

    def tearDown(self):
        self.tmp.cleanup()
        ul.set_run_label("")

    def test_cost_uses_per_million_prices(self):
        # Opus 5.5 is $4 in / $20 out per million tokens.
        self.assertAlmostEqual(ul.cost_usd("claude-opus-5-5", 1_000_000, 500_000), 14.0)
        self.assertAlmostEqual(ul.cost_usd("anthropic/claude-haiku-5-5", 1_000_000, 1_000_000), 0.6)
        self.assertIsNone(ul.cost_usd("some-unlisted-model", 10, 10))

    def test_measured_result_gets_cost(self):
        rec = ul.build_record("coordinator", measured("claude-opus-5-5", "anthropic", 1000, 200), now=NOW)
        self.assertEqual(rec["basis"], "measured")
        self.assertEqual(rec["input_tokens"], 1000)
        self.assertEqual(rec["output_tokens"], 200)
        self.assertAlmostEqual(rec["cost_usd"], 0.008, places=6)

    def test_estimate_has_no_cost_and_is_marked(self):
        # Local/portal calls report only a character-count estimate.
        res = {"ok": True, "model": "llama3", "provider": "ollama", "tokens_used": 480, "token_usage": {}}
        rec = ul.build_record("worker", res, now=NOW)
        self.assertEqual(rec["basis"], "estimated")
        self.assertIsNone(rec["cost_usd"])
        self.assertEqual(rec["input_tokens"], 0)
        self.assertEqual(rec["tokens_total_reported"], 480)

    def test_unknown_price_keeps_tokens_but_no_cost(self):
        rec = ul.build_record("worker", measured("mystery-model", "x", 10, 5), now=NOW)
        self.assertEqual(rec["basis"], "measured")
        self.assertIsNone(rec["cost_usd"])

    def test_record_appends_and_summarize_totals(self):
        ul.set_run_label("baseline")
        ul.record("coordinator", measured("claude-opus-5-5", "anthropic", 1000, 200), path=self.path)
        ul.record("worker", measured("claude-haiku-5-5", "anthropic", 4000, 1000), path=self.path)
        ul.record("worker", {"ok": True, "model": "llama3", "provider": "ollama", "tokens_used": 50},
                  path=self.path)
        ul.set_run_label("candidate")
        ul.record("worker", measured("claude-haiku-5-5", "anthropic", 100, 100), path=self.path)

        lines = [json.loads(x) for x in self.path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(lines), 4)
        self.assertEqual(lines[0]["run"], "baseline")

        base = ul.summarize(self.path, run="baseline")
        self.assertEqual(base["calls"], 3)
        self.assertEqual(base["measured_calls"], 2)
        self.assertEqual(base["estimated_calls"], 1)
        self.assertEqual(base["cost_unknown_calls"], 1)
        self.assertEqual(base["by_role"]["worker"]["calls"], 2)
        self.assertAlmostEqual(base["cost_usd"], 0.008 + 0.0009, places=6)

        everything = ul.summarize(self.path)
        self.assertEqual(everything["calls"], 4)

    def test_record_never_raises_into_the_caller(self):
        blocker = Path(self.tmp.name) / "not_a_dir"
        blocker.write_text("file", encoding="utf-8")
        self.assertEqual(ul.record("worker", measured("claude-haiku-5-5", "a", 1, 1),
                                   path=blocker / "usage.jsonl"), {})


if __name__ == "__main__":
    unittest.main()
