"""Closed-Loop Recursive Self-Improvement (RSI) Engine for Crypto Trading Strategies.

Implements an automated, self-repairing research and discovery loop:
  1. Hydrates generation prompts with verified proofs (BarrierEdge.lean),
     microstructure blueprints, and arXiv research feeds (q-fin.TR, q-fin.ST).
  2. Generates candidate signal hypotheses from the Swarm Council.
  3. Validates output through a compiler feedback loop (validate_signals_json_text):
     rejects formatting/unit errors and forces immediate self-repair.
  4. Scores validated candidates against empirical multi-month and holdout data.
  5. Enforces mathematical stopping rules: archives disproven feature sets into
     negative memory and halts after max_generations if no candidate clears the bar.
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(REPO_ROOT / "sabrina_console_desktop") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "sabrina_console_desktop"))

from tools.gainers_signal_scorer import Market, validate_signals_json_text, score, load_market
from tools.swarm_search_hydrator import SwarmSearchHydrator
from tools.signal_promotion_gate import SignalGate

logger = logging.getLogger(__name__)

# Author label for the built-in template signals used when no model caller is configured.
TEMPLATE_AUTHOR = "template_no_model"


@dataclass
class RsiGenerationResult:
    generation: int
    signals_attempted: int
    validation_retries: int
    valid_signals: List[Dict[str, Any]]
    scores: List[Dict[str, Any]]
    promoted: List[Dict[str, Any]]
    disproven: List[Dict[str, Any]]
    stopping_rule_triggered: bool = False
    verdict_summary: str = ""


class CryptoStrategyRsiEngine:
    """Orchestrates closed-loop recursive self-improvement for crypto trading signals."""

    def __init__(
        self,
        workspace_root: Optional[Path] = None,
        market: Optional[Market] = None,
        llm_caller: Optional[Callable[[str, str, str, str], Dict[str, Any]]] = None,
        max_generations: int = 4,
        max_validation_retries: int = 3,
        cost_round_trip: float = 0.013,
        model_id: str = "gemini-2.5-flash",
    ):
        self.model_id = model_id
        # Who actually wrote the last accepted proposal: "provider/model", or the template marker.
        self.last_author = TEMPLATE_AUTHOR
        self.workspace_root = Path(workspace_root or REPO_ROOT).resolve()
        self.market = market
        self.llm_caller = llm_caller
        self.max_generations = max_generations
        self.max_validation_retries = max_validation_retries
        self.cost = cost_round_trip

        self.hydrator = SwarmSearchHydrator(str(self.workspace_root))
        self.artifacts_dir = self.workspace_root / "sabrina_console_desktop" / "artifacts" / "gainers_paper"
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)

        self.disproven_file = self.artifacts_dir / "disproven_signals.jsonl"
        self.promoted_file = self.artifacts_dir / "promoted_signals.json"
        self.history_file = self.artifacts_dir / "rsi_run_history.jsonl"
        # Every proposal is hashed into the ledger before it is scored, and only the gate's
        # own scoring can promote one. See tools/signal_promotion_gate.py.
        self.gate = SignalGate(root=self.artifacts_dir / "ledger", market=market, cost=cost_round_trip)

    def load_disproven_history(self) -> List[Dict[str, Any]]:
        if not self.disproven_file.is_file():
            return []
        items = []
        for line in self.disproven_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    items.append(json.loads(line))
                except Exception:
                    continue
        return items

    def record_disproven(self, record: Dict[str, Any]) -> None:
        with self.disproven_file.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")

    def record_promoted(self, records: List[Dict[str, Any]]) -> None:
        existing = []
        if self.promoted_file.is_file():
            try:
                existing = json.loads(self.promoted_file.read_text(encoding="utf-8"))
            except Exception:
                existing = []
        existing.extend(records)
        self.promoted_file.write_text(json.dumps(existing, indent=2), encoding="utf-8")

    def record_generation(self, res: RsiGenerationResult) -> None:
        with self.history_file.open("a", encoding="utf-8") as f:
            record = {
                "ts": time.time(),
                "generation": res.generation,
                "validation_retries": res.validation_retries,
                "valid_signals_count": len(res.valid_signals),
                "promoted_count": len(res.promoted),
                "disproven_count": len(res.disproven),
                "verdict_summary": res.verdict_summary,
                "stopping_rule_triggered": res.stopping_rule_triggered,
            }
            f.write(json.dumps(record) + "\n")

    def build_prompts(self, generation: int, previous_disproven: List[Dict[str, Any]]) -> Tuple[str, str]:
        """Constructs generation system and user prompts hydrated with research and negative memory."""
        # Query search hydrator for strategy blueprints and arXiv research
        hydrated = self.hydrator.search_and_hydrate(
            "crypto trading strategies order book imbalance barrier edge",
            agent_role="planner_architect",
            agent_name="Council-Planner",
        )

        system = (
            "You are the Quantitative Architecture Council of the Sabrina Swarm.\n"
            "Your objective: Formulate 5 novel, mathematically rigorous entry signal candidates for Kraken USD spot pairs.\n"
            "Format: Return ONLY a valid JSON array of signal objects. No conversational text, no markdown wrapping.\n"
            "\n"
            "EXACT SCHEMA REQUIRED:\n"
            "[\n"
            "  {\n"
            '    "name": "strategy_name",\n'
            '    "rationale": "One-sentence hypothesis on why this selects positive drift.",\n'
            '    "all": [\n'
            '      {"feature": "drawdown_from_high", "n": 240, "op": "<=", "value": -0.05},\n'
            '      {"feature": "range_pos", "n": 60, "op": ">=", "value": 0.80},\n'
            '      {"feature": "usd_vol", "n": 96, "op": ">=", "value": 50000}\n'
            "    ],\n"
            '    "barriers": {"up_pct": 12, "down_pct": 6, "max_hold_bars": 384},\n'
            '    "cooldown_bars": 96\n'
            "  }\n"
            "]\n"
            "(The example shows the format only. Its thresholds are not a recommendation.)\n"
            "\n"
            "MEASURED FACTS (15-minute Kraken USD bars, July-September 2026 and 19-26 November 2026):\n"
            "- Round-trip cost is about 1.3% (0.4% taker fee each side plus spread).\n"
            "- Break-even hit rate = down_pct/(up_pct+down_pct) + 1.3/(up_pct+down_pct). With a 5% target and\n"
            "  1% stop that is 38%, a 22-point lift over random entry; no signal has come close. Wider barriers\n"
            "  need far less lift (12%/6%: 40.6% needed vs 33.3% random; 30%/10%: 28.3% vs 25%).\n"
            "- Coins entering the 24h top-ten gainers list had already risen a median 13.7% in 24h and then fell\n"
            "  on average (-2% over the next 24h). Entering after a coin has already risen has lost every test.\n"
            "- Volume ratios did not lead price moves at 15-minute resolution.\n"
            "\n"
            "STRICT RULES:\n"
            "- 'drawdown_from_high' MUST BE NON-POSITIVE (e.g. <= -0.05 for 5% pullback, NEVER positive).\n"
            "- 'usd_vol' is total dollar volume summed over lookback n. MUST include at least one usd_vol >= 50000 liquidity floor per signal.\n"
            "- 'up_pct' and 'down_pct' are percentages (e.g. 5 and 1, NOT 0.05 or 0.01).\n"
            "- Available features: ret, vol_ratio, usd_vol, range_pos, drawdown_from_high, volatility, btc_ret, breadth.\n"
            "- 'range_pos' and 'breadth' are shares from 0 to 1. 'ret', 'btc_ret' and 'drawdown_from_high' are fractions.\n"
            "- up_pct 0.5-200, down_pct 0.5-50, max_hold_bars 1-672 (672 bars = 7 days), lookback n 1-400 bars.\n"
            "- Do not resubmit a signal listed under Negative Lessons, or a small variation of one.\n"
        )

        user_parts = [
            f"# Generation {generation} Strategy Proposal Request\n",
            hydrated.get("hydration_prompt", ""),
        ]

        if previous_disproven:
            user_parts.append("\n# Negative Lessons (Previously Disproven Feature Combinations - DO NOT REPEAT):\n")
            for item in previous_disproven[-8:]:
                user_parts.append(f"- Signal '{item.get('name')}' failed: {item.get('reason', 'negative net return')}\n")

        user_parts.append(
            "\nTask: Generate 5 new distinct signals adhering to the Lean barrier-edge theorem. "
            "Output the JSON array only."
        )

        return system, "\n".join(user_parts)

    def generate_and_validate(
        self, generation: int, previous_disproven: List[Dict[str, Any]]
    ) -> Tuple[bool, List[Dict[str, Any]], int, str]:
        """Runs the generation and compiler validation repair loop."""
        system_prompt, user_prompt = self.build_prompts(generation, previous_disproven)

        # Default fallback candidate generator if no live LLM caller is passed
        def default_fallback_caller(sys_p: str, usr_p: str) -> str:
            # High-quality fallback signal template
            sample_signals = [
                {
                    "name": f"rsi_gen{generation}_rebound_expansion",
                    "rationale": f"Gen {generation} drawdown recovery with volume surge and tight barrier ratio.",
                    "all": [
                        {"feature": "drawdown_from_high", "n": 240, "op": "<=", "value": -0.06},
                        {"feature": "range_pos", "n": 60, "op": ">=", "value": 0.85},
                        {"feature": "vol_ratio", "n": 30, "op": ">=", "value": 2.2},
                        {"feature": "usd_vol", "n": 96, "op": ">=", "value": 60000},
                    ],
                    "barriers": {"up_pct": 5, "down_pct": 1, "max_hold_bars": 400},
                    "cooldown_bars": 24,
                },
                {
                    "name": f"rsi_gen{generation}_breadth_divergence",
                    "rationale": f"Gen {generation} market breadth accumulation with positive btc correlation.",
                    "all": [
                        {"feature": "breadth", "n": 60, "op": ">=", "value": 0.75},
                        {"feature": "btc_ret", "n": 60, "op": ">=", "value": 0.015},
                        {"feature": "range_pos", "n": 30, "op": ">=", "value": 0.70},
                        {"feature": "usd_vol", "n": 96, "op": ">=", "value": 50000},
                    ],
                    "barriers": {"up_pct": 4, "down_pct": 2, "max_hold_bars": 300},
                    "cooldown_bars": 12,
                },
            ]
            return json.dumps(sample_signals)

        current_prompt = user_prompt
        retries = 0

        while retries <= self.max_validation_retries:
            if self.llm_caller:
                resp = self.llm_caller("planner_architect", self.model_id, system_prompt, current_prompt)
                raw_text = str(resp.get("text", "") or resp.get("reply", "") or resp.get("response", ""))
                if resp.get("ok") is False or not raw_text.strip():
                    # A failed call is not a validation error: stop instead of "repairing" nothing.
                    return False, [], retries, f"Model call failed: {resp.get('error') or 'empty reply'}"
                self.last_author = f"{resp.get('provider') or 'unknown'}/{resp.get('model') or self.model_id}"
                if resp.get("used_fallback"):
                    self.last_author += f" (fallback from {resp.get('requested_model') or self.model_id})"
            else:
                raw_text = default_fallback_caller(system_prompt, current_prompt)
                self.last_author = TEMPLATE_AUTHOR

            ok, parsed, err_msg = validate_signals_json_text(raw_text)
            if ok and parsed:
                return True, parsed, retries, ""

            retries += 1
            if retries > self.max_validation_retries:
                return False, [], retries, f"Exceeded max retries. Last error: {err_msg}"

            # Feedback repair prompt fed into the loop
            current_prompt = (
                f"{user_prompt}\n\n"
                f"=== VALIDATION ERROR ON ATTEMPT {retries} ===\n"
                f"{err_msg}\n"
                f"Fix the error immediately. Ensure all units are correct and return ONLY the valid JSON array."
            )

        return False, [], retries, "Unknown failure"

    def run_cycle(self, generation: int = 1) -> RsiGenerationResult:
        """Executes a single generation cycle: Ingest -> Propose -> Validate -> Score -> Evaluate."""
        disproven_history = self.load_disproven_history()

        ok, signals, retries, err = self.generate_and_validate(generation, disproven_history)
        if not ok:
            return RsiGenerationResult(
                generation=generation,
                signals_attempted=0,
                validation_retries=retries,
                valid_signals=[],
                scores=[],
                promoted=[],
                disproven=[],
                stopping_rule_triggered=False,
                verdict_summary=f"Validation failed: {err}",
            )

        # Score signals if market data is loaded
        if self.market is None:
            try:
                self.market = load_market()
            except Exception as e:
                logger.warning("Could not load full market: %s. Signals will be registered but not scored.", e)
        self.gate._market = self.market

        scored_results = []
        promoted = []
        disproven = []
        unscored = 0
        author = f"rsi_gen{generation}:{self.last_author}"

        for sig in signals:
            registered = self.gate.register(sig, author)
            if not registered["ok"]:
                # e.g. the same name re-proposed with different thresholds after its score is known
                res = {"name": sig.get("name"), "verdict": "not_registered", "error": registered["error"]}
            else:
                evaluated = self.gate.evaluate(registered["artifact_id"], author)
                res = evaluated["score"]
                if res is None:
                    # Nothing was measured. Never invent a score, and never file it as disproven.
                    unscored += 1
                    scored_results.append({"name": sig.get("name"), "verdict": "unscored", "error": evaluated["error"]})
                    continue
                res = {**res, "signal_id": registered["artifact_id"], "candidate_finalized": evaluated["ok"]}

            scored_results.append(res)

            verdict = res.get("verdict")
            if res.get("candidate_finalized"):
                promoted.append(res)
            else:
                disproven_item = {
                    "name": sig.get("name"),
                    "generation": generation,
                    "reason": f"Verdict: {verdict}, mean_net: {res.get('overall', {}).get('mean_net_pct')}%, required: {res.get('required_hit_rate_pct')}%",
                    "ts": time.time(),
                }
                disproven.append(disproven_item)
                self.record_disproven(disproven_item)

        if promoted:
            self.record_promoted(promoted)

        # Stopping rule: halt if generation >= max_generations and no promoted candidates
        # An unscored generation proves nothing, so it cannot trigger the stopping rule.
        stopping_rule = bool(generation >= self.max_generations and not promoted and not unscored)
        verdict = (
            f"Gen {generation}: {len(promoted)} promoted, {len(disproven)} disproven"
            + (f", {unscored} unscored (no market data)." if unscored else ".")
            if not stopping_rule
            else f"STOPPING RULE REACHED at Gen {generation}. No pre-registered candidate cleared holdout bar. Falsification confirmed."
        )

        gen_res = RsiGenerationResult(
            generation=generation,
            signals_attempted=len(signals),
            validation_retries=retries,
            valid_signals=signals,
            scores=scored_results,
            promoted=promoted,
            disproven=disproven,
            stopping_rule_triggered=stopping_rule,
            verdict_summary=verdict,
        )

        self.record_generation(gen_res)
        return gen_res

    def run_rsi_loop(self) -> List[RsiGenerationResult]:
        """Executes full multi-generation RSI closed loop until promotion or stopping rule triggers."""
        history = []
        for g in range(1, self.max_generations + 1):
            res = self.run_cycle(generation=g)
            history.append(res)
            if res.promoted or res.stopping_rule_triggered:
                break
        return history


def format_cycle_report(res: RsiGenerationResult, author: str) -> str:
    """Plain-text summary of one generation for the console chat. Measured numbers only."""
    lines = [
        f"Signal research, generation {res.generation}. Proposed by: {author}.",
        f"{res.verdict_summary} Format retries: {res.validation_retries}.",
    ]
    for s in res.scores:
        overall = s.get("overall") or {}
        if s.get("verdict") in ("unscored", "not_registered"):
            lines.append(f"- {s.get('name')}: {s.get('verdict')} ({s.get('error')})")
        else:
            lines.append(
                f"- {s.get('name')}: {s.get('verdict')}; {overall.get('trades')} trades; "
                f"hit rate {overall.get('hit_rate_pct')}% vs required {s.get('required_hit_rate_pct')}%; "
                f"mean net {overall.get('mean_net_pct')}% per trade"
                + ("; FINALIZED AS CANDIDATE" if s.get("candidate_finalized") else "")
            )
    if author == TEMPLATE_AUTHOR:
        lines.append("No model was used: these are the built-in template signals.")
    lines.append("Research only. A candidate is a signal for the forward paper run, not an order. execution_authorized=false")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Sabrina Crypto Strategy RSI Loop.")
    parser.add_argument("--generations", type=int, default=1, help="Max generations to run")
    parser.add_argument("--dry-run", action="store_true", help="Run with dry-run scoring")
    args = parser.parse_args()

    engine = CryptoStrategyRsiEngine(max_generations=args.generations)
    print(f"[*] Starting Crypto Strategy RSI Loop (max_generations={args.generations})...")
    res = engine.run_cycle(generation=1)
    print(f"[*] Cycle completed: {res.verdict_summary}")
    print(f"[*] Valid signals: {len(res.valid_signals)}, Retries: {res.validation_retries}")
    print(json.dumps([s["name"] for s in res.valid_signals], indent=2))


if __name__ == "__main__":
    main()
