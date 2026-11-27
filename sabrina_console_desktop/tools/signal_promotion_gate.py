"""Promotion gate for swarm signal proposals. Research only: nothing here places orders.

Two checker-gated steps on one PromotionLedger:

  signal            A proposal is finalized when the scorer's validator accepts it. Its id is
                    a hash of its content, so it is fixed before any result exists. One
                    finalized signal per name: a name cannot be re-registered with different
                    thresholds after its score is known.
  signal_candidate  A finalized signal becomes a candidate only when the gate itself scores
                    it on real market data and the verdict clears the bar. The score is
                    computed inside the checker, so a submitted or simulated score cannot
                    promote anything. With no market data the answer is a rejection.

A candidate is a signal worth a forward paper run. It is not a trading instruction.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from tools import gainers_signal_scorer as sc
from tools.promotion_ledger import PromotionLedger

DEFAULT_ROOT = Path(__file__).resolve().parent.parent / "artifacts" / "gainers_paper" / "ledger"
KIND_SIGNAL = "signal"
KIND_CANDIDATE = "signal_candidate"
CLEARS = "clears_the_bar_on_this_sample"
DEFAULT_DATASET = "kraken_rest_15m_cache"


def _score_summary(score: dict[str, Any]) -> str:
    overall, holdout = score.get("overall") or {}, score.get("holdout_last_40pct") or {}
    return (
        f"verdict={score.get('verdict')}; trades={overall.get('trades')}; "
        f"hit_rate={overall.get('hit_rate_pct')}% vs required {score.get('required_hit_rate_pct')}%; "
        f"mean_net={overall.get('mean_net_pct')}%; holdout_mean_net={holdout.get('mean_net_pct')}%"
    )


class SignalGate:
    def __init__(
        self,
        root: Path | str = DEFAULT_ROOT,
        market: sc.Market | None = None,
        market_loader: Callable[[], sc.Market] | None = None,
        cost: float = sc.ROUND_TRIP_COST,
        dataset: str = DEFAULT_DATASET,
    ):
        self.dataset = dataset  # recorded with every evaluation, so a result names the data it used
        self._market = market
        self._market_loader = market_loader
        self.cost = cost
        self.last_scores: dict[str, dict[str, Any]] = {}
        self.ledger = PromotionLedger(root, {KIND_SIGNAL: self._check_signal, KIND_CANDIDATE: self._check_candidate})

    # --- checkers --------------------------------------------------------
    @staticmethod
    def _check_signal(payload: dict) -> tuple[bool, str]:
        try:
            sc.validate(payload)
        except (ValueError, KeyError, TypeError) as exc:
            return False, str(exc) or type(exc).__name__
        return True, "scorer validator accepted the signal"

    def market(self) -> sc.Market | None:
        if self._market is None and self._market_loader is not None:
            self._market = self._market_loader()
        if self._market is not None and not self._market.pairs:
            return None
        return self._market

    def _check_candidate(self, payload: dict) -> tuple[bool, str]:
        market = self.market()
        if market is None:
            return False, "no market data loaded, so the signal was not scored"
        score = sc.score(payload["signal"], market, self.cost)
        self.last_scores[payload["signal_id"]] = score
        return score.get("verdict") == CLEARS, f"dataset={payload.get('dataset', DEFAULT_DATASET)}; {_score_summary(score)}"

    # --- public API ------------------------------------------------------
    def register(self, signal: Any, author: str) -> dict[str, Any]:
        """Log one proposal and try to finalize it. Returns {"ok", "artifact_id", "error"}."""
        name = signal.get("name") if isinstance(signal, dict) else None
        slot = f"signal:{name}" if isinstance(name, str) and name else "signal:<unnamed>"
        try:
            artifact_id = self.ledger.append(KIND_SIGNAL, signal, author=author, slot=slot)
        except ValueError as exc:
            return {"ok": False, "artifact_id": "", "error": str(exc)}
        return self.ledger.promote(artifact_id)

    def register_text(self, text: str, author: str) -> tuple[bool, list[dict[str, Any]], str]:
        """Parse a model reply holding a JSON array of signals and register each one.

        Returns (all finalized, per-signal results, message). The message lists every
        rejection with its exact error, ready to send back to the author for a retry.
        """
        cleaned = text.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("\n", 1)[-1].rsplit("```", 1)[0]
        try:
            data = json.loads(cleaned)
        except json.JSONDecodeError as exc:
            return False, [], f"JSON syntax error: {exc}"
        if not isinstance(data, list) or not 1 <= len(data) <= 20:
            return False, [], "Expected a JSON array of 1-20 signal objects"
        results = [dict(self.register(item, author), signal=item) for item in data]
        errors = [f"Signal #{n} invalid: {r['error']}" for n, r in enumerate(results, 1) if not r["ok"]]
        return not errors, results, "\n".join(errors)

    def evaluate(self, signal_id: str, author: str) -> dict[str, Any]:
        """Score a finalized signal and promote it to candidate only if it clears the bar.

        Returns {"ok", "artifact_id", "error", "score"}; score is None when nothing was measured.
        """
        signal = next((r["payload"] for r in self.ledger.finalized()
                       if r["artifact_id"] == signal_id and r["kind"] == KIND_SIGNAL), None)
        if signal is None:
            return {"ok": False, "artifact_id": "", "error": "signal is not finalized", "score": None}
        self.last_scores.pop(signal_id, None)
        candidate_id = self.ledger.append(
            KIND_CANDIDATE,
            {"signal_id": signal_id, "signal": signal, "round_trip_cost": self.cost, "dataset": self.dataset},
            author=author, slot=f"candidate:{signal_id}", parents=[signal_id],
        )
        result = self.ledger.promote(candidate_id)
        return {**result, "score": self.last_scores.get(signal_id)}

    def candidates(self) -> list[dict[str, Any]]:
        return [r for r in self.ledger.finalized() if r["kind"] == KIND_CANDIDATE]


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Register a signals file in the ledger and score what is finalized.")
    parser.add_argument("--signals", type=Path, help="JSON array of signals to register and score")
    parser.add_argument("--retest-finalized", action="store_true",
                        help="score every signal already finalized in the ledger, unchanged")
    parser.add_argument("--author", required=True)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--quarter", help="score on a downloaded Kraken archive, e.g. 2026Q3, not the REST cache")
    parser.add_argument("--out", type=Path, help="also write the full scores to this JSON file")
    args = parser.parse_args()
    if bool(args.signals) == bool(args.retest_finalized):
        raise SystemExit("pass exactly one of --signals or --retest-finalized")
    if args.quarter:
        from tools import kraken_history

        data_dir = kraken_history.quarter_dir(args.quarter) / "ohlc15csv"
        gate = SignalGate(args.root, market_loader=lambda: kraken_history.load_market(data_dir),
                          dataset=f"kraken_ohlcvt_{args.quarter}")
    else:
        gate = SignalGate(args.root, market_loader=sc.load_market)
    message = ""
    if args.signals:
        _, results, message = gate.register_text(args.signals.read_text(encoding="utf-8-sig"), args.author)
    else:
        results = [{"ok": True, "artifact_id": r["artifact_id"], "error": "", "signal": r["payload"]}
                   for r in gate.ledger.finalized() if r["kind"] == KIND_SIGNAL]
    rows, scores = [], []
    for reg in results:
        row = {"name": reg["signal"].get("name") if isinstance(reg["signal"], dict) else None,
               "registered": reg["ok"], "signal_id": reg["artifact_id"], "error": reg["error"]}
        if reg["ok"]:
            ev = gate.evaluate(reg["artifact_id"], args.author)
            row.update(candidate=ev["ok"], result=ev["error"] or "finalized as candidate")
            if ev["score"]:
                scores.append({**ev["score"], "signal_id": reg["artifact_id"]})
        rows.append(row)
        print(json.dumps(row), flush=True)
    if args.out:
        market = gate.market()
        args.out.write_text(json.dumps({
            "execution_authorized": False, "dataset": gate.dataset,
            "pairs": len(market.pairs) if market else 0, "scores": scores}, indent=1), encoding="utf-8")
    print(json.dumps({"execution_authorized": False, "ledger": str(args.root), "dataset": gate.dataset,
                      "parse_error": message if not results else "",
                      "signals": rows, "candidates": len(gate.candidates()), "ledger_intact": gate.ledger.verify()["ok"]}, indent=1))


if __name__ == "__main__":
    main()
