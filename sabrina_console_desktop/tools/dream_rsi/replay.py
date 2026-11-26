"""Dream-RSI: Offline Replay Evaluation + Replay Objective (paper algorithms #6, #7).

Replays a batch-selection policy over a FROZEN historical discovery tree, revealing recorded
children one at a time (no new execution), and scores it with the paper's objective:

    V = max(s_v) - b1 * N + b2 * ( N / max(1, k) )

where max(s_v) is the best score reached in the revealed subtree, N is the number of revealed
generations (cost), and k is the number of rounds used (parallelism efficiency).
"""

from tree import Node, Tree


class BatchSelectionPolicy:
    """Baseline decision policy: pick up to W feasible nodes to expand this round."""

    def __init__(self, worker_count):
        self.worker_count = worker_count

    def select_batch(self, feasible_set):
        return feasible_set[: self.worker_count]


class OfflineReplayEvaluation:
    def __init__(self, policy, historical_trees, max_rounds):
        self.policy = policy
        self.historical_trees = historical_trees
        self.max_rounds = max_rounds

    def evaluate(self):
        if not self.historical_trees:
            return 0.0
        return sum(self._evaluate_tree(t)["v"] for t in self.historical_trees) / len(self.historical_trees)

    def evaluate_detailed(self):
        """Like evaluate() but also returns the efficiency components the paper's objective trades off:
        mean best-score reached, mean reveals (execution cost N), mean rounds (parallelism depth k).
        This is what makes a Dream-RSI efficiency win VISIBLE -- a better policy reaches the same
        max_score with fewer reveals -- rather than hidden inside the scalar V."""
        if not self.historical_trees:
            return {"v": 0.0, "max_score": 0.0, "reveals": 0.0, "rounds": 0.0, "trees": 0}
        rows = [self._evaluate_tree(t) for t in self.historical_trees]
        n = len(rows)
        return {"v": sum(r["v"] for r in rows) / n,
                "max_score": sum(r["max_score"] for r in rows) / n,
                "reveals": sum(r["reveals"] for r in rows) / n,
                "rounds": sum(r["rounds"] for r in rows) / n,
                "trees": n}

    def _evaluate_tree(self, full_tree):
        # The observed subtree is SEPARATE from full_tree: it starts as just the root and reveals
        # recorded children one at a time. We track our own frontier (observed nodes that still have
        # an unrevealed recorded child) rather than relying on Tree.feasible_set(), because a node
        # must stay expandable until ALL its recorded children are revealed -- otherwise siblings
        # revealed in later rounds would be missed.
        observed_root = Node(score=full_tree.root.score)
        link = {id(observed_root): full_tree.root}     # observed node -> corresponding full node
        revealed_full = {id(full_tree.root)}           # full nodes already mirrored into observed
        observed_nodes = [observed_root]
        max_score = observed_root.score
        revealed_count = 0
        rounds_used = 0

        for _ in range(self.max_rounds):
            feasible = [n for n in observed_nodes
                        if any(id(fc) not in revealed_full for fc in link[id(n)].children)]
            batch = self.policy.select_batch(feasible)
            if not batch:
                break  # nothing left to reveal
            for obs_node in batch:
                full_node = link[id(obs_node)]
                for full_child in full_node.children:     # reveal one unrevealed recorded child
                    if id(full_child) not in revealed_full:
                        new_obs = Node(parent=obs_node, score=full_child.score)
                        obs_node.add_child(new_obs)
                        link[id(new_obs)] = full_child
                        revealed_full.add(id(full_child))
                        observed_nodes.append(new_obs)
                        revealed_count += 1
                        max_score = max(max_score, full_child.score)
                        break
            rounds_used += 1

        return {"v": self._replay_objective(max_score, revealed_count, rounds_used),
                "max_score": max_score, "reveals": revealed_count, "rounds": rounds_used}

    def _replay_objective(self, max_score, revealed_count, rounds_used, beta1=0.1, beta2=0.1):
        quality = max_score
        cost_penalty = beta1 * revealed_count
        parallelism_bonus = beta2 * (revealed_count / max(1, rounds_used))
        return quality - cost_penalty + parallelism_bonus


if __name__ == "__main__":
    # Tiny synthetic frozen tree: root(10) -> {c1(15) -> g1(25), c2(20)}
    root = Node(score=10)
    c1 = Node(parent=root, score=15)
    c2 = Node(parent=root, score=20)
    g1 = Node(parent=c1, score=25)
    root.add_child(c1)
    root.add_child(c2)
    c1.add_child(g1)
    full = Tree(root)

    policy = BatchSelectionPolicy(worker_count=2)
    ev = OfflineReplayEvaluation(policy, historical_trees=[full], max_rounds=5)
    score = ev.evaluate()
    print("Replay Score:", round(score, 3))
    print("(best reachable score is 25; policy reveals children to find it while paying cost)")
