"""Dream-RSI: Policy Improvement Loop (paper algorithm #8).

Iterates over candidate policy settings (here, worker_count), runs an Online Rollout for each against
the frozen history, scores it with the replay objective V = max_score - cost * revealed_count, and
keeps the best -- a minimal demonstration of improving the decision policy over iterations. A wider
policy reaches the best score faster but reveals (pays for) more nodes; V trades those off, so the
loop can discover the most efficient setting.
"""

from tree import Node, Tree
from policy import BatchSelectionPolicy
from rollout import online_rollout


def improve_policy(frozen_tree, candidate_worker_counts, max_rounds, cost=0.1):
    results = []
    best = None
    for w in candidate_worker_counts:
        policy = BatchSelectionPolicy(worker_count=w)
        _observed, _trajectory, revealed_count, max_score = online_rollout(frozen_tree, policy, max_rounds)
        V = max_score - cost * revealed_count
        entry = {"worker_count": w, "V": V, "max_score": max_score, "revealed": revealed_count}
        results.append(entry)
        if best is None or V > best["V"]:
            best = entry
    return results, best


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

    results, best = improve_policy(frozen, candidate_worker_counts=[1, 2, 3], max_rounds=5)
    for r in results:
        print(f"worker_count={r['worker_count']}: V={round(r['V'], 3)} "
              f"(max_score={r['max_score']}, revealed={r['revealed']})")
    print(f"Best policy: worker_count={best['worker_count']} with V={round(best['V'], 3)}")
