from tree import Tree, Node


class BatchSelectionPolicy:
    def __init__(self, worker_count):
        self.worker_count = worker_count

    def select_batch(self, tree: Tree):
        feasible_set = tree.leaves()
        if len(feasible_set) == 0:
            return []
        # Simple baseline policy: select the top N nodes by score
        feasible_set.sort(key=lambda node: node.score, reverse=True)
        return feasible_set[:self.worker_count]


if __name__ == "__main__":
    # Create a simple synthetic tree for demonstration
    root = Node(parent=None, score=10)
    child1 = Node(parent=root, score=8)
    child2 = Node(parent=root, score=7)
    child3 = Node(parent=root, score=6)
    root.add_child(child1)
    root.add_child(child2)
    root.add_child(child3)

    tree = Tree(root)

    # Create a policy with a worker count of 2
    policy = BatchSelectionPolicy(worker_count=2)

    # Select a batch from the tree
    batch = policy.select_batch(tree)

    # Print the selected batch
    print([node.score for node in batch])