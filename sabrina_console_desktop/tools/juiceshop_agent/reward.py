#!/usr/bin/env python3
"""
tools/juiceshop_agent/reward.py
===============================
Deterministic, ungameable ground-truth reward for the Juice Shop STaR loop.

The reward is Juice Shop's OWN /api/Challenges scoreboard: the running app decides whether
a challenge is solved, not the model. A reward of 1.0 is granted only when the TARGET
challenge's solved flag transitions False->True across an episode (per-challenge set
membership, NOT the global solved-count delta -- the latter can't say WHICH challenge
flipped, and an incidental solve of a different challenge would misattribute).

Three-valued on purpose:
    1.0   target challenge flipped False->True this episode  (real solve)
    0.0   target still unsolved after the episode            (genuine miss)
    None  scoreboard unreachable (target down / WAN blip)    (DROP the episode, do NOT
          train on it -- a dead target must never be scored as a 0, which would teach the
          policy that correct actions "fail")

Design ref: tasks/wru2sb529.output (reward section) + the 2026-09-27 feasibility probe.
"""
import time
from typing import Dict, Optional, Set

import requests

DEFAULT_TARGET = "http://localhost:3000"


def scoreboard(target: str = DEFAULT_TARGET, timeout: int = 10) -> Optional[Dict[str, bool]]:
    """Return {challenge_key: solved_bool} from the live scoreboard.

    Returns None (not {}) on any failure, so the caller can distinguish 'target down'
    from 'a real, empty result'. A non-None dict is always trustworthy live state.
    """
    try:
        r = requests.get(f"{target}/api/Challenges/?size=250", timeout=timeout)
        if r.status_code != 200:
            return None
        rows = r.json().get("data", [])
        out = {row["key"]: bool(row.get("solved")) for row in rows if row.get("key")}
        return out or None
    except Exception:
        return None


def solved_set(target: str = DEFAULT_TARGET) -> Optional[Set[str]]:
    """The set of currently-solved challenge keys, or None if the target is unreachable."""
    board = scoreboard(target)
    if board is None:
        return None
    return {k for k, v in board.items() if v}


def challenge_reward(
    target: str,
    challenge_key: str,
    baseline_solved: Set[str],
    settle_seconds: float = 0.3,
) -> Optional[float]:
    """Reward for the target challenge after an action sequence already ran.

    baseline_solved is the solved-SET snapshotted at episode START (post-reset). Returns:
        1.0  if challenge_key is NOW solved and was NOT in baseline_solved (real flip)
        0.0  if still unsolved
        None if the scoreboard can't be read (drop the episode)

    A challenge already in baseline_solved returns 0.0 (no NEW flip to credit) -- callers
    should skip already-solved challenges rather than probe them, but this stays safe if
    they don't.
    """
    time.sleep(settle_seconds)  # synchronous challenge detection can lag the request slightly
    now = solved_set(target)
    if now is None:
        return None
    if challenge_key in now and challenge_key not in baseline_solved:
        return 1.0
    return 0.0


def newly_solved(
    target: str,
    baseline_solved: Set[str],
    settle_seconds: float = 0.3,
) -> Optional[Set[str]]:
    """Every challenge that flipped False->True since baseline (not just one target).

    Useful for crediting incidental solves during teacher distillation -- a single strong
    action can solve more than the challenge it was aimed at, and those traces are still
    valuable training data. Returns None if the target is unreachable.
    """
    time.sleep(settle_seconds)
    now = solved_set(target)
    if now is None:
        return None
    return now - baseline_solved
