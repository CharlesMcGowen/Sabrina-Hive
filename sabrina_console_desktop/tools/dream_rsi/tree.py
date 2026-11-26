class Node:
    def __init__(self, parent=None, workspace_snapshot=None, score=0):
        self.parent = parent
        self.workspace_snapshot = workspace_snapshot
        self.score = score
        self.children = []

    def add_child(self, child):
        self.children.append(child)

    def leaves(self):
        if not self.children:
            return [self]
        return [leaf for child in self.children for leaf in child.leaves()]

    def feasible_set(self):
        return [self] + [leaf for leaf in self.leaves() if not leaf.children]

    def serialize(self):
        # Serialize the subtree DOWNWARD only. `parent` is a back-reference and must NOT be
        # serialized, or a child would serialize its parent, which re-serializes that child,
        # recursing forever. Parent links are rebuilt on deserialize.
        return {
            'workspace_snapshot': self.workspace_snapshot,
            'score': self.score,
            'children': [child.serialize() for child in self.children]
        }

    @classmethod
    def deserialize(cls, data, parent=None):
        node = cls(
            parent=parent,
            workspace_snapshot=data['workspace_snapshot'],
            score=data['score']
        )
        node.children = [cls.deserialize(child_data, parent=node) for child_data in data['children']]
        return node

class Tree:
    def __init__(self, root=None):
        self.root = root if root else Node()

    def add_child(self, parent, child):
        parent.add_child(child)

    def leaves(self):
        return self.root.leaves()

    def feasible_set(self):
        return self.root.feasible_set()

    def child_revelation(self, node, full_tree, observed_subtree):
        if node == self.root:
            for child in full_tree.root.children:
                if child not in observed_subtree.root.children:
                    return child
        else:
            for child in node.children:
                if child not in observed_subtree.children:
                    return child
        return None

    def serialize(self):
        return self.root.serialize()

    @classmethod
    def deserialize(cls, data):
        return cls(root=Node.deserialize(data))

if __name__ == '__main__':
    # Example usage
    root = Node()
    child1 = Node(parent=root, score=10)
    child2 = Node(parent=root, score=20)
    root.add_child(child1)
    root.add_child(child2)

    tree = Tree(root)
    print('Tree structure:', tree.serialize())
    print('Leaves:', [leaf.score for leaf in tree.leaves()])
    print('Feasible set:', [node.score for node in tree.feasible_set()])
    # Reveal against a FRESH (empty) observed subtree so the first unrevealed child shows up.
    observed = Tree(Node())
    rev = tree.child_revelation(root, tree, observed)
    print('Child revelation:', rev.score if rev else 'None')