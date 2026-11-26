import copy
from pathlib import Path
import random
import tempfile
import unittest

from .common import load
from .goals import expr, features, freeze, specimen, validate


def value(term, env):
    if type(term) is int:
        return term
    if isinstance(term, str):
        return env[term]
    op, left, right = term
    if op == "seq":
        return env["seq"]
    a, b = value(left, env), value(right, env)
    return {"+": lambda: a+b, "-": lambda: a-b, "*": lambda: a*b, "^": lambda: a**b}[op]()


class ContractTests(unittest.TestCase):
    def test_freeze_and_split(self):
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory)
            initial = freeze(run)
            self.assertEqual(initial, freeze(run))
            dev, holdout = [load(run / f"{s}.json") for s in ("development", "holdout")]
            self.assertEqual(len(dev["problems"]), 256)
            self.assertEqual(len(holdout["problems"]), 256)
            self.assertFalse(set(x["id"] for x in dev["problems"]) & set(x["id"] for x in holdout["problems"]))

    def test_recursions_against_iteration(self):
        for seed in range(50):
            goal = specimen(random.Random(seed), 3, bool(seed % 2))
            for n in (0, 1, 2, 11, 77):
                x, actual = -5, -5
                for k in range(n):
                    actual += sum(c*k**i for i,c in enumerate(goal["recurrence"]))
                env = dict(n=n, x=x, seq=actual)
                self.assertEqual(value(goal["lhs"], env), value(goal["rhs"], env))

    def test_injection_and_bounds(self):
        goal = specimen(random.Random(1), 0, False)
        self.assertEqual(len(features(goal)), 12)
        for term in ("x; axiom False", ["#", "x", 0], ["^", "x", 99], float("nan")):
            bad = copy.deepcopy(goal)
            bad["rhs"] = term
            with self.assertRaises(ValueError):
                validate(bad)

    def test_contradictory_assignments(self):
        goal = specimen(random.Random(1), 0, False)
        goal["hypotheses"] = [["=", "x", 0], ["=", "x", 1]]
        with self.assertRaisesRegex(ValueError, "Contradictory"):
            validate(goal)

    def test_finished_rejection_releases_lease(self):
        from contextlib import contextmanager
        from .common import compute
        class Client:
            released = False
            @contextmanager
            def lease(self, *args, **kwargs):
                yield
                self.released = True
        client = Client()
        with self.assertRaisesRegex(ValueError, "rejected"):
            with compute(client):
                raise ValueError("rejected")
        self.assertTrue(client.released)

    def test_second_dispatcher_cannot_enter(self):
        from .locking import run_lock
        import subprocess
        import sys
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "worker.lock"
            with run_lock(path):
                script = ("from " + __package__ + ".locking import run_lock\n"
                          "from pathlib import Path\nimport sys\n"
                          "with run_lock(Path(sys.argv[1])): print('entered')")
                child = subprocess.run([sys.executable, "-B", "-c", script, str(path)], capture_output=True)
                self.assertNotEqual(child.returncode, 0)


if __name__ == "__main__":
    unittest.main()
