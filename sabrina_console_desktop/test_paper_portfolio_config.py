"""Tests for the editable paper portfolio file (data/control/paper_portfolios.json)."""
import copy
import json
import tempfile
import unittest
from pathlib import Path

from tools import kraken_paper_1000_forward as lab

SEED_SNAPSHOT = copy.deepcopy(lab.PORTFOLIO_SPECS)  # Taken at import, before any reload.


class PortfolioConfigTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.path = self.dir / "paper_portfolios.json"
        self._specs_before = copy.deepcopy(lab.PORTFOLIO_SPECS)
        lab._PORTFOLIO_CONFIG_SEEN.update(stamp=None, error=None)

    def tearDown(self):
        lab.PORTFOLIO_SPECS.clear()
        lab.PORTFOLIO_SPECS.update(copy.deepcopy(self._specs_before))
        self._tmp.cleanup()

    def write(self, portfolios, version=1):
        self.path.write_text(json.dumps({"version": version, "portfolios": portfolios}), encoding="utf-8")

    def state(self, **pf_fields):
        """A minimal state dict with one portfolio per name given as name={...}."""
        return {"portfolios": {name: dict(fields) for name, fields in pf_fields.items()}}

    def seed_portfolios(self):
        return copy.deepcopy(SEED_SNAPSHOT)

    # --- seed file ---------------------------------------------------------
    def test_seed_file_matches_current_specs(self):
        seed = json.loads(lab.PORTFOLIO_CONFIG_PATH.read_text(encoding="utf-8"))
        active, retire = lab.parse_portfolio_config(seed)
        self.assertEqual(retire, [])
        self.assertEqual(active, SEED_SNAPSHOT)

    # --- rejection keeps the previous specs ----------------------------------
    def test_bad_json_is_rejected_and_specs_kept(self):
        self.path.write_text("{not json", encoding="utf-8")
        report = lab.reload_portfolio_specs({"portfolios": {}}, self.path)
        self.assertEqual(report["status"], "rejected")
        self.assertEqual(lab.PORTFOLIO_SPECS, self._specs_before)

    def test_wrong_version_is_rejected(self):
        self.write(self.seed_portfolios(), version=2)
        self.assertEqual(lab.reload_portfolio_specs({}, self.path)["status"], "rejected")
        self.assertEqual(lab.PORTFOLIO_SPECS, self._specs_before)

    def test_missing_required_key_is_rejected(self):
        portfolios = self.seed_portfolios()
        del portfolios["live_mirror_40bps"]["tp_bps"]
        self.write(portfolios)
        report = lab.reload_portfolio_specs({}, self.path)
        self.assertEqual(report["status"], "rejected")
        self.assertIn("tp_bps", report["error"])
        self.assertEqual(lab.PORTFOLIO_SPECS, self._specs_before)

    def test_non_numeric_value_is_rejected(self):
        portfolios = self.seed_portfolios()
        portfolios["live_mirror_40bps"]["sl_bps"] = "-125"
        self.write(portfolios)
        self.assertEqual(lab.reload_portfolio_specs({}, self.path)["status"], "rejected")

    def test_pause_status_is_rejected_until_supported(self):
        portfolios = self.seed_portfolios()
        portfolios["live_mirror_40bps"]["status"] = "paused"
        self.write(portfolios)
        report = lab.reload_portfolio_specs({}, self.path)
        self.assertEqual(report["status"], "rejected")
        self.assertIn("pause", report["error"])

    def test_missing_file_reports_missing(self):
        self.assertEqual(lab.reload_portfolio_specs({}, self.dir / "nope.json")["status"], "missing")

    # --- apply rules -----------------------------------------------------------
    def test_unchanged_file_is_skipped(self):
        self.write(self.seed_portfolios())
        self.assertEqual(lab.reload_portfolio_specs({}, self.path)["status"], "applied")
        self.assertEqual(lab.reload_portfolio_specs({}, self.path)["status"], "unchanged")

    def test_parameter_edit_updates_in_place(self):
        identity = lab.PORTFOLIO_SPECS  # Call sites keep the same dict object.
        portfolios = self.seed_portfolios()
        portfolios["live_mirror_40bps"]["tp_bps"] = 1500.0
        self.write(portfolios)
        report = lab.reload_portfolio_specs({}, self.path)
        self.assertEqual(report["updated"], ["live_mirror_40bps"])
        self.assertIs(lab.PORTFOLIO_SPECS, identity)
        self.assertEqual(lab.PORTFOLIO_SPECS["live_mirror_40bps"]["tp_bps"], 1500.0)

    def test_added_portfolio_appears_and_gets_reserve_slots_when_policy_frozen(self):
        portfolios = self.seed_portfolios()
        new = copy.deepcopy(portfolios["live_mirror_40bps"])
        new["description"] = "test added lab"
        portfolios["test_added_40bps"] = new
        self.write(portfolios)
        state = {"portfolios": {}, "cash_reserve_policy": {
            "version": 1, "basis_slots": 4,
            "slots_by_portfolio": {"live_mirror_40bps": 1, "majors_fee_be_40bps": 1}}}
        report = lab.reload_portfolio_specs(state, self.path)
        self.assertEqual(report["added"], ["test_added_40bps"])
        self.assertIn("test_added_40bps", lab.PORTFOLIO_SPECS)
        self.assertEqual(state["cash_reserve_policy"]["slots_by_portfolio"]["test_added_40bps"], 1)

    def test_added_tokenized_portfolio_gets_no_crypto_reserve(self):
        portfolios = self.seed_portfolios()
        new = copy.deepcopy(portfolios["stocks_xstock_0bps"])
        new["description"] = "test stocks lab"
        portfolios["test_stocks_0bps"] = new
        self.write(portfolios)
        state = {"portfolios": {}, "cash_reserve_policy": {
            "version": 1, "basis_slots": 4, "slots_by_portfolio": {"live_mirror_40bps": 1}}}
        lab.reload_portfolio_specs(state, self.path)
        self.assertNotIn("test_stocks_0bps", state["cash_reserve_policy"]["slots_by_portfolio"])

    def test_retire_when_flat_removes_spec_and_keeps_state(self):
        portfolios = self.seed_portfolios()
        portfolios["majors_fee_be_40bps"]["status"] = "retired"
        self.write(portfolios)
        state = self.state(majors_fee_be_40bps={"open_positions": {}, "active_position": None,
                                               "pending_limit": None, "closed_trades": [1, 2]})
        report = lab.reload_portfolio_specs(state, self.path)
        self.assertEqual(report["retired"], ["majors_fee_be_40bps"])
        self.assertNotIn("majors_fee_be_40bps", lab.PORTFOLIO_SPECS)
        self.assertIn("majors_fee_be_40bps", state["portfolios"])  # History stays.

    def test_retire_with_open_position_is_blocked_and_keeps_trading(self):
        portfolios = self.seed_portfolios()
        portfolios["majors_fee_be_40bps"]["status"] = "retired"
        self.write(portfolios)
        state = self.state(majors_fee_be_40bps={"open_positions": {"BTC/USD": {}}, "active_position": None,
                                               "pending_limit": None})
        report = lab.reload_portfolio_specs(state, self.path)
        self.assertEqual(report["retire_blocked_open_positions"], ["majors_fee_be_40bps"])
        self.assertIn("majors_fee_be_40bps", lab.PORTFOLIO_SPECS)

    def test_absent_portfolio_is_kept_not_deleted(self):
        portfolios = self.seed_portfolios()
        del portfolios["highconv_pb25_40bps"]
        self.write(portfolios)
        lab.reload_portfolio_specs({}, self.path)
        self.assertIn("highconv_pb25_40bps", lab.PORTFOLIO_SPECS)


if __name__ == "__main__":
    unittest.main()
