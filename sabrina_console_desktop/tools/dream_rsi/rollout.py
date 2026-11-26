"""Dream-RSI: Online Rollout (paper algorithm #4).

Expands a discovery tree round by round under a decision policy, using the History-as-Simulator to
serve recorded children (no real execution), and records the rollout trajectory -- the pre-order
sequence of revealed scores.
"""

from tree import Node, Tree
from policy import BatchSelectionPolicy
from simulator import Simulator


def _preorder_scores(node):
    scores = [node.score]
    for child in node.children:
        scores.extend(_preorder_scores(child))
    return scores


def online_rollout(frozen_tree, policy, max_rounds):
    """Roll a policy out over the frozen history. Returns
    (observed_tree, trajectory, revealed_count, max_score), where trajectory is the pre-order list of
    revealed scores (root first)."""
    sim = Simulator(frozen_tree)
    observed, revealed_count, max_score = sim.simulate(policy, max_rounds)
    trajectory = _preorder_scores(observed.root)
    return observed, trajectory, revealed_count, max_score


if __name__ == "__main__":
    # Frozen tree: root(10) -> c1(20) -> g1(35); root -> c2(30) -> g2(40)
    root = Node(score=10)
    c1 = Node(parent=root, score=20)
    c2 = Node(parent=root, score=30)
    g1 = Node(parent=c1, score=35)
    g2 = Node(parent=c2, score=40)
    root.add_child(c1)
    root.add_child(c2)
    c1.add_child(g1)
    c2.add_child(g2)
    frozen = Tree(root)

    policy = BatchSelectionPolicy(worker_count=2)
    observed, trajectory, revealed_count, max_score = online_rollout(frozen, policy, max_rounds=5)
    print("Rollout trajectory (scores):", trajectory)
    print("Revealed count:", revealed_count)
    print("Max score reached:", max_score)
