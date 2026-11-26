"""Dream-RSI: History-as-Simulator (paper algorithm #5).

Replays a FROZEN recorded discovery tree as a cheap oracle: instead of doing real work, a decision
policy is run against this simulator, which reveals the recorded children (and their recorded scores)
one at a time from history -- no new execution. The observed subtree is built from FRESH Node objects
that mirror the frozen tree; it must never share the frozen tree's node objects, or every child would
look already-revealed and nothing would be served.
"""

from tree import Node, Tree


class Simulator:
    def __init__(self, frozen_tree: Tree):
        self.frozen_tree = frozen_tree

    def simulate(self, policy, max_rounds):
        """Run `policy` against the frozen history for up to max_rounds. Each round the policy picks a
        batch of observed leaf nodes to expand (policy.select_batch takes the observed Tree); for each
        selected node we reveal ALL of its still-unrevealed recorded children from history. Revealing
        every child of an expanded node (rather than one) is what lets a wider policy -- a larger
        worker_count -- actually explore more per round, which the Policy Improvement Loop depends on.
        Returns (observed_tree, revealed_count, max_score).
        """
        observed_root = Node(score=self.frozen_tree.root.score)
        observed = Tree(observed_root)
        link = {id(observed_root): self.frozen_tree.root}   # observed node -> frozen node
        revealed = {id(self.frozen_tree.root)}              # frozen nodes already mirrored
        max_score = observed_root.score
        revealed_count = 0

        for _ in range(max_rounds):
            batch = policy.select_batch(observed)
            if not batch:
                break
            progressed = False
            for obs_node in batch:
                frozen = link.get(id(obs_node))
                if frozen is None:
                    continue
                for frozen_child in frozen.children:       # reveal ALL unrevealed recorded children
                    if id(frozen_child) not in revealed:
                        new_obs = Node(parent=obs_node, score=frozen_child.score)
                        obs_node.add_child(new_obs)
                        link[id(new_obs)] = frozen_child
                        revealed.add(id(frozen_child))
                        revealed_count += 1
                        max_score = max(max_score, frozen_child.score)
                        progressed = True
            if not progressed:
                break   # policy selected only fully-revealed nodes -> nothing left to serve

        return observed, revealed_count, max_score


if __name__ == "__main__":
    from policy import BatchSelectionPolicy

    # Frozen recorded tree: root(10) -> c1(20) -> g1(35); root -> c2(30)
    root = Node(score=10)
    c1 = Node(parent=root, score=20)
    c2 = Node(parent=root, score=30)
    g1 = Node(parent=c1, score=35)
    root.add_child(c1)
    root.add_child(c2)
    c1.add_child(g1)
    frozen = Tree(root)

    policy = BatchSelectionPolicy(worker_count=2)
    sim = Simulator(frozen)
    observed, revealed_count, max_score = sim.simulate(policy, max_rounds=5)

    print("Revealed count:", revealed_count)
    print("Max score reached:", max_score)
    print("Observed leaves:", [n.score for n in observed.leaves()])
