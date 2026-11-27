import json
import tempfile
import unittest
from pathlib import Path

from tools import gainers_signal_scorer as sc
from tools.signal_promotion_gate import KIND_CANDIDATE, SignalGate

SIGNAL = {"name": "up2", "all": [{"feature": "ret", "n": 1, "op": ">=", "value": 0.02}],
          "barriers": {"up_pct": 10, "down_pct": 5, "max_hold_bars": 5}, "cooldown_bars": 0}
BAD_UNITS = {**SIGNAL, "name": "bad_units", "barriers": {"up_pct": 0.05, "down_pct": 0.01, "max_hold_bars": 5}}


def bars(closes, start=0):
    return [[900.0 * (start + k), closes[k - 1] if k else c, max(closes[k - 1] if k else c, c),
             min(closes[k - 1] if k else c, c), c, c, 100.0] for k, c in enumerate(closes)]


def market(winning: bool, pairs: int = 40) -> sc.Market:
    """Each pair: a +3% bar (the signal), then a run to +12% (hit) or a drop to -8% (stop).

    Half the pairs start 8 bars later, so the scorer's holdout period has trades too.
    """
    after = [103, 110, 116, 116, 116] if winning else [103, 97, 94, 94, 94]
    return sc.Market({f"P{k}/USD": sc.Pair(bars([100, 100, 103] + after, start=8 * (k % 2)))
                      for k in range(pairs)})


class SignalGateTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def test_register_finalizes_valid_and_rejects_invalid_with_the_exact_error(self):
        gate = SignalGate(self.root)
        good = gate.register(SIGNAL, "council")
        bad = gate.register(BAD_UNITS, "council")
        self.assertTrue(good["ok"])
        self.assertFalse(bad["ok"])
        self.assertIn("up_pct must be 0.5-200", bad["error"])
        self.assertFalse(gate.register("not a signal", "council")["ok"])
        self.assertEqual(len(gate.ledger.finalized()), 1)

    def test_a_name_cannot_be_retuned_after_registration(self):
        gate = SignalGate(self.root)
        first = gate.register(SIGNAL, "council")
        retuned = gate.register({**SIGNAL, "barriers": {"up_pct": 8, "down_pct": 5, "max_hold_bars": 5}}, "council")
        self.assertFalse(retuned["ok"])
        self.assertIn(first["artifact_id"], retuned["error"])
        self.assertTrue(gate.register(SIGNAL, "someone_else")["ok"])  # identical content is the same artifact

    def test_register_text_reports_every_rejection_for_retry(self):
        gate = SignalGate(self.root)
        ok, results, message = gate.register_text("```json\n" + json.dumps([SIGNAL, BAD_UNITS]) + "\n```", "council")
        self.assertFalse(ok)
        self.assertEqual([r["ok"] for r in results], [True, False])
        self.assertIn("Signal #2 invalid", message)
        self.assertEqual(gate.register_text("## Proposed signals\n1. Breakout", "council")[0], False)

    def test_without_market_data_nothing_is_scored_or_promoted(self):
        gate = SignalGate(self.root)
        sid = gate.register(SIGNAL, "council")["artifact_id"]
        res = gate.evaluate(sid, "council")
        self.assertFalse(res["ok"])
        self.assertIsNone(res["score"])
        self.assertIn("no market data", res["error"])
        self.assertEqual(gate.candidates(), [])

    def test_only_a_measured_pass_becomes_a_candidate(self):
        losing = SignalGate(self.root / "losing", market=market(winning=False))
        sid = losing.register(SIGNAL, "council")["artifact_id"]
        res = losing.evaluate(sid, "council")
        self.assertFalse(res["ok"])
        self.assertEqual(res["score"]["verdict"], "fails")
        self.assertIn("verdict=fails", losing.ledger.rejections(res["artifact_id"])[0]["error"])
        self.assertEqual(losing.candidates(), [])

        winning = SignalGate(self.root / "winning", market=market(winning=True))
        sid = winning.register(SIGNAL, "council")["artifact_id"]
        res = winning.evaluate(sid, "council")
        self.assertTrue(res["ok"], res)
        (cand,) = winning.candidates()
        self.assertEqual((cand["kind"], cand["parents"]), (KIND_CANDIDATE, [sid]))
        self.assertIn("verdict=clears_the_bar_on_this_sample", cand["detail"])
        self.assertTrue(winning.ledger.verify()["ok"])

    def test_evaluate_requires_a_finalized_signal(self):
        gate = SignalGate(self.root, market=market(winning=True))
        self.assertFalse(gate.evaluate("f" * 64, "council")["ok"])
        self.assertEqual(gate.candidates(), [])


if __name__ == "__main__":
    unittest.main()
