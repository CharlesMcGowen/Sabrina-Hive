from pathlib import Path
import tempfile
import unittest

import torch

from .policy import ProofPolicy, SHAPES, export


class PolicyTests(unittest.TestCase):
    def test_batch_and_history(self):
        weights = {key: torch.zeros(shape) for key, shape in SHAPES.items()}
        weights["decay"].fill_(0.5)
        weights["up"][0, 12] = 1
        weights["down"][0, 0] = -1
        policy = ProofPolicy(weights)
        goals, state = torch.zeros(3, 12), torch.ones(3, 6)
        scores, following = policy(goals, state)
        self.assertEqual(tuple(scores.shape), (3, 6))
        self.assertTrue(torch.equal(following, state/2))
        self.assertTrue((scores[:, 0] == -1).all())
        empty, _ = policy(goals)
        self.assertTrue((empty == 0).all())

    def test_immutable_reload(self):
        weights = {k: torch.zeros(shape).tolist() for k, shape in SHAPES.items()}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            original = export(weights, path, {"test_control": True})
            loaded = ProofPolicy.load(path / "policy.safetensors")
            self.assertTrue(torch.equal(original(torch.zeros(12))[0], loaded(torch.zeros(12))[0]))
            with (path / "policy.safetensors").open("ab") as stream:
                stream.write(b"x")
            with self.assertRaisesRegex(ValueError, "hash"):
                ProofPolicy.load(path / "policy.safetensors")

    def test_search_bounds(self):
        from .search import solve
        from .goals import ACTIONS, specimen
        import random
        class Checker:
            def actions(self, goal):
                return {action: {"ok": False} for action in ACTIONS}
            def check(self, goal, body):
                return {"ok": False}
        weights = {key: torch.zeros(shape) for key, shape in SHAPES.items()}
        goal = specimen(random.Random(1), 0, False)
        policy, checker = ProofPolicy(weights), Checker()
        result = solve(goal, policy, checker)
        self.assertEqual(result["attempts"], 64)
        self.assertEqual(result["reason"], "budget_exhausted")
        self.assertEqual(solve(goal, policy, checker, budget=0)["attempts"], 0)

    def test_foreign_reservation_is_retained(self):
        from unittest.mock import Mock
        from .worker import recover_own_reservation
        client = Mock()
        client.request.return_value = {"leases": [{"resource": "compute:lab",
                        "state": "uncertain", "owner": "another-project:123"}]}
        recover_own_reservation(client)
        client.request.assert_called_once_with("/v1/status")

    def test_live_previous_executor_is_retained(self):
        import os
        import socket
        from unittest.mock import Mock
        from .worker import recover_own_reservation
        client = Mock()
        client.request.return_value = {"leases": [{"resource": "compute:lab", "state": "uncertain",
                      "owner": "sabrina-math-proof:123",
                      "executor": {"pid": os.getpid(), "host": socket.gethostname()}}]}
        with self.assertRaisesRegex(RuntimeError, "still exists"):
            recover_own_reservation(client)


if __name__ == "__main__":
    unittest.main()
