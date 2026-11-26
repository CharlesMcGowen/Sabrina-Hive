/-
  BarrierEdge.lean

  The piece missing from MoonExpectancy.lean.

  MoonExpectancy proves: IF the hit rate is at least h, THEN a (target, stop) pair
  has positive expectancy. It treats the hit rate as independent of the barriers.
  It is not. For a price with no drift, the chance of reaching +up before -down is
  down / (up + down). A far target with a tight stop is hit exactly rarely enough
  that the gross expectancy is zero, so the net result is minus the costs, for every
  choice of barriers.

  What this file proves (integers, Lean 4 core only):
    1. net_decomposition: net = hits * (up + down) - total * (down + cost).
    2. fair_rate_loses_the_cost: at the no-drift hit rate, the result is exactly
       minus the total cost, whatever up and down are.
    3. net_positive_iff: profit requires hits * (up + down) > total * (down + cost),
       i.e. the hit rate must exceed the no-drift rate by more than cost / (up + down).

  What it does not prove: that any signal achieves that excess. That is measured.
  The no-drift hit rate down / (up + down) is a standard result (gambler's ruin,
  optional stopping) and is used here as a stated premise, not derived.

  NOT YET COMPILED: Lean is not installed on the machine this was written on.
-/

namespace Sabrina.BarrierEdge

/-- Net result, in bps summed over `total` equal trades: `hits` reach `+up`, the rest
    reach `-down`, and every trade pays `cost` (round-trip fees, spread, slippage). -/
def netNum (hits total : Nat) (up down cost : Int) : Int :=
  (hits : Int) * up - ((total - hits : Nat) : Int) * down - (total : Int) * cost

theorem net_decomposition (hits total : Nat) (h : hits ≤ total) (up down cost : Int) :
    netNum hits total up down cost =
      (hits : Int) * (up + down) - (total : Int) * (down + cost) := by
  unfold netNum
  have hcast : ((total - hits : Nat) : Int) = (total : Int) - (hits : Int) :=
    Int.ofNat_sub h
  rw [hcast, Int.sub_mul, Int.mul_add, Int.mul_add]
  omega

/-- Barrier neutrality. If the hit rate is the no-drift rate, `hits / total =
    down / (up + down)`, the net result is minus the total cost for ANY barriers.
    Moving the target or the stop cannot create an edge. -/
theorem fair_rate_loses_the_cost (hits total : Nat) (h : hits ≤ total) (up down cost : Int)
    (hfair : (hits : Int) * (up + down) = (total : Int) * down) :
    netNum hits total up down cost = -((total : Int) * cost) := by
  rw [net_decomposition hits total h, hfair, Int.mul_add]
  omega

/-- The edge condition: profit iff the hit count beats the no-drift count by more than
    the cost share. -/
theorem net_positive_iff (hits total : Nat) (h : hits ≤ total) (up down cost : Int) :
    netNum hits total up down cost > 0 ↔
      (hits : Int) * (up + down) > (total : Int) * (down + cost) := by
  rw [net_decomposition hits total h]
  omega

/-- The 2x target with a 3.2% stop and 32 bps maker round trip, per 10,000 trades.
    The no-drift hit count is 10000 * 320 / 10320 = 310.08. Profit needs 342. -/
theorem moon_maker_cutoff (hits : Nat) (h : hits ≤ 10000) :
    netNum hits 10000 10000 320 32 > 0 ↔ 342 ≤ hits := by
  rw [net_positive_iff hits 10000 h]
  omega

/-- At the no-drift hit count (310 of 10,000) the same structure loses. -/
theorem moon_maker_loses_at_no_drift_rate :
    netNum 310 10000 10000 320 32 < 0 := by
  decide

end Sabrina.BarrierEdge
