"""Acceptance tests for the promotion ledger (tools/promotion_ledger.py).

Written first as the definition of done for swarm missions m-a1414ff3 and m-a6098d7e,
which targeted outputs/promotion_gate/ and both failed. The passing implementation
now lives in tools/. Set PROMOTION_LEDGER_PATH to test another implementation.
Contract: proofs/promotion_gate_mission_brief.md.
"""
import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path

from tools import gainers_signal_scorer as sc

HERE = Path(__file__).resolve().parent
MODULE_PATH = Path(os.environ.get("PROMOTION_LEDGER_PATH")
                   or HERE / "tools" / "promotion_ledger.py")


def load_module():
    spec = importlib.util.spec_from_file_location("promotion_ledger", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def passes(payload):
    return True, "checked"


def fails(payload):
    return False, "value out of range"


def raises(payload):
    raise RuntimeError("checker exploded")


def signal_checker(payload):
    try:
        sc.validate(payload)
    except ValueError as exc:
        return False, str(exc)
    return True, "scorer accepted the signal"


GOOD_SIGNAL = {"name": "up2", "all": [{"feature": "ret", "n": 1, "op": ">=", "value": 0.02}],
               "barriers": {"up_pct": 10, "down_pct": 5, "max_hold_bars": 5}, "cooldown_bars": 0}
BAD_SIGNAL = {**GOOD_SIGNAL, "barriers": {"up_pct": 10, "down_pct": 0, "max_hold_bars": 5}}


@unittest.skipUnless(MODULE_PATH.is_file(), f"not built yet: {MODULE_PATH}")
class PromotionLedgerAcceptance(unittest.TestCase):
    def setUp(self):
        self.mod = load_module()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.checkers = {"pass": passes, "fail": fails, "raise": raises, "signal": signal_checker}

    def ledger(self):
        return self.mod.PromotionLedger(self.root, self.checkers)

    def lines(self, name):
        path = self.root / name
        return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()] if path.exists() else []

    # --- speculative log -------------------------------------------------
    def test_append_is_content_addressed_and_idempotent(self):
        led = self.ledger()
        a = led.append("pass", {"x": 1, "y": 2}, author="w1", slot="s1")
        b = led.append("pass", {"y": 2, "x": 1}, author="w2", slot="s1")
        c = led.append("pass", {"x": 2}, author="w1", slot="s1")
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)
        self.assertEqual(len(self.lines("speculative.jsonl")), 2)
        self.assertFalse(led.is_finalized(a))

    def test_append_rejects_bad_input(self):
        led = self.ledger()
        with self.assertRaises(ValueError):
            led.append("pass", {"x": 1}, author="w1", slot="s1", parents=["0" * 64])
        with self.assertRaises(ValueError):
            led.append("pass", ["not", "a", "dict"], author="w1", slot="s1")
        with self.assertRaises(ValueError):
            led.append("pass", {"x": object()}, author="w1", slot="s1")
        with self.assertRaises(ValueError):
            led.append("pass", {"x": 1}, author="w1", slot="")
        self.assertEqual(self.lines("speculative.jsonl"), [])

    # --- promotion -------------------------------------------------------
    def test_promote_runs_the_checker_for_the_kind(self):
        led = self.ledger()
        a = led.append("pass", {"x": 1}, author="w1", slot="s1")
        res = led.promote(a)
        self.assertTrue(res["ok"])
        self.assertTrue(led.is_finalized(a))
        (rec,) = led.finalized()
        self.assertEqual((rec["artifact_id"], rec["kind"], rec["slot"], rec["payload"], rec["detail"]),
                         (a, "pass", "s1", {"x": 1}, "checked"))

    def test_failed_check_is_recorded_and_not_finalized(self):
        led = self.ledger()
        a = led.append("fail", {"x": 1}, author="w1", slot="s1")
        res = led.promote(a)
        self.assertFalse(res["ok"])
        self.assertIn("value out of range", res["error"])
        self.assertFalse(led.is_finalized(a))
        self.assertEqual(led.finalized(), [])
        self.assertIn("value out of range", led.rejections(a)[0]["error"])

    def test_checker_exception_and_missing_checker_are_rejections(self):
        led = self.ledger()
        a = led.append("raise", {"x": 1}, author="w1", slot="s1")
        b = led.append("no_such_kind", {"x": 1}, author="w1", slot="s2")
        self.assertIn("checker exploded", led.promote(a)["error"])
        self.assertFalse(led.promote(b)["ok"])
        self.assertFalse(led.promote("f" * 64)["ok"])
        self.assertEqual(led.finalized(), [])
        c = led.append("pass", {"x": 1}, author="w1", slot="s3")
        self.assertTrue(led.promote(c)["ok"])

    def test_one_finalized_artifact_per_slot(self):
        led = self.ledger()
        a = led.append("pass", {"answer": "A"}, author="w1", slot="question-1")
        b = led.append("pass", {"answer": "B"}, author="w2", slot="question-1")
        self.assertTrue(led.promote(a)["ok"])
        res = led.promote(b)
        self.assertFalse(res["ok"])
        self.assertIn("slot", res["error"])
        self.assertIn(a, res["error"])
        self.assertEqual([r["artifact_id"] for r in led.finalized()], [a])

    def test_promote_twice_finalizes_once(self):
        led = self.ledger()
        a = led.append("pass", {"x": 1}, author="w1", slot="s1")
        self.assertTrue(led.promote(a)["ok"])
        self.assertTrue(led.promote(a)["ok"])
        self.assertEqual(len(self.lines("finalized.jsonl")), 1)

    def test_parents_must_be_finalized_first(self):
        led = self.ledger()
        parent = led.append("pass", {"x": 1}, author="w1", slot="s1")
        child = led.append("pass", {"x": 2}, author="w2", slot="s2", parents=[parent])
        res = led.promote(child)
        self.assertFalse(res["ok"])
        self.assertIn(parent, res["error"])
        self.assertTrue(led.promote(parent)["ok"])
        self.assertTrue(led.promote(child)["ok"])
        self.assertEqual(led.finalized()[1]["parents"], [parent])

    def test_author_count_never_substitutes_for_the_check(self):
        led = self.ledger()
        ids = {led.append("fail", {"x": 1}, author=f"w{k}", slot="s1") for k in range(9)}
        self.assertEqual(len(ids), 1)
        self.assertFalse(led.promote(ids.pop())["ok"])

    # --- durability ------------------------------------------------------
    def test_state_survives_reopening(self):
        led = self.ledger()
        a = led.append("pass", {"x": 1}, author="w1", slot="s1")
        b = led.append("fail", {"x": 2}, author="w1", slot="s2")
        led.promote(a)
        led.promote(b)
        again = self.ledger()
        self.assertTrue(again.is_finalized(a))
        self.assertFalse(again.is_finalized(b))
        self.assertEqual(len(again.rejections(b)), 1)
        self.assertFalse(again.promote(again.append("pass", {"x": 9}, author="w3", slot="s1"))["ok"])
        self.assertTrue(again.verify()["ok"])

    def test_verify_detects_an_edited_finalized_record(self):
        led = self.ledger()
        for k in range(3):
            led.promote(led.append("pass", {"x": k}, author="w1", slot=f"s{k}"))
        self.assertTrue(led.verify()["ok"])
        path = self.root / "finalized.jsonl"
        rows = self.lines("finalized.jsonl")
        rows[1]["payload"] = {"x": 999}
        path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
        self.assertFalse(self.ledger().verify()["ok"])

    def test_verify_detects_a_removed_finalized_record(self):
        led = self.ledger()
        for k in range(3):
            led.promote(led.append("pass", {"x": k}, author="w1", slot=f"s{k}"))
        path = self.root / "finalized.jsonl"
        kept = path.read_text(encoding="utf-8").splitlines(keepends=True)
        path.write_text(kept[0] + kept[2], encoding="utf-8")
        self.assertFalse(self.ledger().verify()["ok"])

    # --- the real use ----------------------------------------------------
    def test_scorer_validator_gates_swarm_signals(self):
        led = self.ledger()
        bad = led.append("signal", BAD_SIGNAL, author="council", slot="signal:up2")
        res = led.promote(bad)
        self.assertFalse(res["ok"])
        self.assertTrue(res["error"])
        good = led.append("signal", GOOD_SIGNAL, author="council", slot="signal:up2")
        self.assertTrue(led.promote(good)["ok"])
        self.assertEqual([r["artifact_id"] for r in led.finalized()], [good])


if __name__ == "__main__":
    unittest.main()
