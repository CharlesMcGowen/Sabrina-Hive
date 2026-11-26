"""Bounded neural action selection, with the same budget for every comparator."""
import random
import torch

from .goals import ACTIONS, features
from .lean import proof
from .common import digest


def solve(goal, policy, checker, mode="policy", budget=64):
    if budget <= 0:
        return dict(solved=False, attempts=0, actions=[], trace=[], reason="budget_exhausted")
    outcomes = checker.actions(goal)
    history = torch.zeros(6)
    feature_values = features(goal)
    tried, trace = set(), []
    uniform = random.Random(int(digest(goal)[:12], 16))
    fixed = list(range(6))
    uniform.shuffle(fixed)
    while len(tried) < 6 and len(trace) < budget:
        if mode in ("policy", "reset"):
            ranking, following = policy.rank(feature_values, history if mode == "policy" else torch.zeros(6))
        elif mode == "zero":
            ranking, following = list(range(6)), history
        elif mode == "uniform":
            ranking, following = fixed, history
        else:
            ranking, following = list(range(6)), history
        chosen = next(i for i in ranking if i not in tried)
        tried.add(chosen)
        ok = outcomes[ACTIONS[chosen]]["ok"]
        trace.append([ACTIONS[chosen], ok])
        if ok:
            return dict(solved=True, attempts=len(trace), actions=[ACTIONS[chosen]], trace=trace)
        history = following.clone()
        history[chosen] += 1
    # Expand the best four branches, with the same finite budget for all policies.
    priority = ranking[:4]
    frontier = [(i,) for i in priority]
    for depth in range(2, 13):
        expanded = [prefix + (i,) for prefix in frontier for i in priority]
        expanded.sort(key=lambda path: sum(ranking.index(i) for i in path))
        following_frontier = []
        for path in expanded:
            if len(trace) >= budget:
                return dict(solved=False, attempts=len(trace), actions=[], trace=trace, reason="budget_exhausted")
            # all_goals permits an earlier action to close some or all subgoals.
            body = "\n".join("all_goals (\n" + proof(goal, ACTIONS[i]) + "\n)" for i in path)
            verdict = checker.check(goal, body)
            trace.append([[ACTIONS[i] for i in path], verdict["ok"]])
            if verdict["ok"]:
                return dict(solved=True, attempts=len(trace), actions=[ACTIONS[i] for i in path], trace=trace)
            if len(following_frontier) < 4:
                following_frontier.append(path)
        frontier = following_frontier
    return dict(solved=False, attempts=len(trace), actions=[], trace=trace, reason="depth_exhausted")
