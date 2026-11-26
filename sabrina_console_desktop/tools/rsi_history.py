"""Dream-RSI phase ❶ + ❷ glue: record REAL improvement attempts as discovery trees, and load them
back as the replay-simulator pool.

Each improvement attempt Sabrina makes is a node in a discovery tree: the target she worked, a short
diff summary, the action, and its realized score s_v (from capability_bench / tests). A completed
session is one serialized tree in data/rsi_history/<ts>.json. `load_pool()` reads them back as
tools.dream_rsi.tree.Tree objects, which tools/dream_rsi/replay.py then scores candidate policies
against off-policy (no re-running the agent) -- the whole point of Dream-RSI.

Reuses tools/dream_rsi/tree.py (Node/Tree + serialize/deserialize); no new tree logic.
"""

import datetime as _dt
import json
import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
HISTORY_DIR = REPO_ROOT / "data" / "rsi_history"

# Import the finished, verified tree module (tools/dream_rsi/tree.py).
import sys
sys.path.insert(0, str(REPO_ROOT))
from tools.dream_rsi.tree import Node, Tree  # noqa: E402


class SessionTree:
    """Thin recorder around dream_rsi.tree for one improvement session (one discovery tree)."""

    def __init__(self, root_label="session-root", root_score=0.0):
        self.root = Node(workspace_snapshot={"label": root_label, "kind": "root"}, score=root_score)
        self.tree = Tree(self.root)

    def add_attempt(self, parent, target, action, diff_summary, score):
        """Record one improvement attempt as a child node. `score` is s_v (capability/test outcome).
        Returns the new node so later attempts can chain off it."""
        node = Node(parent=parent or self.root,
                    workspace_snapshot={"target": target, "action": action,
                                        "diff": (diff_summary or "")[:500], "kind": "attempt"},
                    score=float(score))
        (parent or self.root).add_child(node)
        return node

    def save(self, label="", history_dir=None):
        """Serialize this session's tree. `history_dir` (default data/rsi_history) lets a distinct pool
        (e.g. task-solving trees in data/rsi_history_solve) be kept separate from improvement-session
        trees so their dreamed policies don't muddle each other."""
        d = Path(history_dir) if history_dir else HISTORY_DIR
        d.mkdir(parents=True, exist_ok=True)
        ts = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        name = f"{ts}_{label}.json" if label else f"{ts}.json"
        path = d / name
        path.write_text(json.dumps(self.tree.serialize(), indent=2), encoding="utf-8")
        return path


def load_pool(history_dir=None):
    """Load every recorded discovery tree as the Dream-RSI replay-simulator pool (list of Tree).
    `history_dir` selects which pool to load (default data/rsi_history)."""
    pool = []
    d = Path(history_dir) if history_dir else HISTORY_DIR
    if not d.exists():
        return pool
    for f in sorted(d.glob("*.json")):
        try:
            pool.append(Tree.deserialize(json.loads(f.read_text(encoding="utf-8"))))
        except Exception:
            pass
    return pool


def pool_stats(history_dir=None):
    pool = load_pool(history_dir)
    total_nodes = sum(len(t.leaves()) for t in pool) if pool else 0
    best = max((max((n.score for n in t.leaves()), default=0.0) for t in pool), default=0.0) if pool else 0.0
    return {"trees": len(pool), "leaf_nodes": total_nodes, "best_score": best}


if __name__ == "__main__":
    # Self-demo: build a tiny session tree, save, reload, show stats (proves phase ❶+❷ round-trip).
    st = SessionTree(root_score=0.40)
    a = st.add_attempt(None, "tests/unit/test_foo.py", "add_tests", "added 3 asserts", 0.45)
    st.add_attempt(a, "tools/foo.py", "bugfix", "fixed off-by-one", 0.55)
    p = st.save("demo")
    print("saved:", p)
    print("pool stats after reload:", pool_stats())
