#!/usr/bin/env python3
"""Tests for tools/coder_agent/. Pure Python, no GPU, no network -- HF downloads and subprocess
spawns are patched out. The `agentic_solver.grade` runner is exercised as-is (its own tests
already cover it upstream)."""
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
_TOOLS = os.path.dirname(_HERE)
for p in (_HERE, _TOOLS):
    if p not in sys.path:
        sys.path.insert(0, p)

import data as CD
import eval as CE
import tool_wrapper as CW


# ── data.py ────────────────────────────────────────────────────────────────────

class TestAsciiFilter(unittest.TestCase):
    def test_ascii_ratio_english_and_code(self):
        r = CD.ascii_ratio("def add(a, b): return a + b  # 100% ASCII")
        self.assertGreaterEqual(r, 0.99)

    def test_ascii_ratio_low_on_cjk(self):
        r = CD.ascii_ratio("请用中文回答这个问题。")
        self.assertLess(r, 0.10)

    def test_is_english_safe_accepts_english(self):
        self.assertTrue(CD.is_english_safe("Please write a Python function that reverses a list."))

    def test_is_english_safe_rejects_cjk(self):
        self.assertFalse(CD.is_english_safe("请写一个Python函数来反转一个列表。"))

    def test_is_english_safe_mixed_below_threshold(self):
        # ~10% non-ASCII should fail the 95% threshold
        text = "abc" * 30 + "日本語テキスト" * 3
        self.assertFalse(CD.is_english_safe(text))


class TestCodealpacaAdapter(unittest.TestCase):
    def test_valid_record_becomes_chatml(self):
        row = {"instruction": "Reverse a string.", "input": "", "output": "def rev(s): return s[::-1]"}
        rec = CD.codealpaca_to_chatml(row)
        self.assertIsNotNone(rec)
        self.assertEqual([m["role"] for m in rec["messages"]], ["system", "user", "assistant"])
        self.assertIn("Reverse a string.", rec["messages"][1]["content"])
        self.assertIn("def rev(s):", rec["messages"][2]["content"])

    def test_input_field_appended_to_user(self):
        row = {"instruction": "Sort this list:", "input": "[3, 1, 2]",
               "output": "sorted([3,1,2])  # -> [1,2,3]"}
        rec = CD.codealpaca_to_chatml(row)
        user = rec["messages"][1]["content"]
        self.assertIn("Sort this list:", user)
        self.assertIn("[3, 1, 2]", user)

    def test_cjk_record_rejected(self):
        row = {"instruction": "反转一个字符串。", "input": "", "output": "def rev(s): return s[::-1]"}
        self.assertIsNone(CD.codealpaca_to_chatml(row))

    def test_empty_output_rejected(self):
        row = {"instruction": "Do something.", "input": "", "output": ""}
        self.assertIsNone(CD.codealpaca_to_chatml(row))


class TestOASSTPairing(unittest.TestCase):
    def test_build_pairs_picks_best_assistant_reply(self):
        rows = [
            {"message_id": "p1", "parent_id": None, "role": "prompter",
             "text": "How do I reverse a list in Python?", "lang": "en"},
            {"message_id": "a1", "parent_id": "p1", "role": "assistant",
             "text": "Use list[::-1].", "lang": "en", "rank": 5},
            {"message_id": "a2", "parent_id": "p1", "role": "assistant",
             "text": "Use reversed(list) or list[::-1].", "lang": "en", "rank": 1},
            {"message_id": "p2", "parent_id": None, "role": "prompter",
             "text": "非英语提示。", "lang": "zh"},  # filtered out by lang
            {"message_id": "a3", "parent_id": "p2", "role": "assistant", "text": "答复。",
             "lang": "zh"},
        ]
        pairs = list(CD._build_oasst_pairs(rows, lang="en"))
        self.assertEqual(len(pairs), 1)
        self.assertIn("reverse a list", pairs[0]["prompter"].lower())
        # rank 1 is BETTER than rank 5 in oasst -- verify we picked a2
        self.assertIn("reversed", pairs[0]["assistant"])

    def test_pair_to_chatml_valid(self):
        rec = CD.oasst_pair_to_chatml({"prompter": "What is a list comprehension?",
                                        "assistant": "A concise way to build a list in Python."})
        self.assertIsNotNone(rec)
        self.assertEqual([m["role"] for m in rec["messages"]], ["system", "user", "assistant"])

    def test_pair_to_chatml_cjk_rejected(self):
        self.assertIsNone(CD.oasst_pair_to_chatml({"prompter": "什么是列表推导式?",
                                                    "assistant": "简洁地创建列表的方法。"}))

    def test_pair_to_chatml_empty_rejected(self):
        self.assertIsNone(CD.oasst_pair_to_chatml({"prompter": "", "assistant": "reply"}))
        self.assertIsNone(CD.oasst_pair_to_chatml({"prompter": "q", "assistant": ""}))


class TestBuildDatasetSkips(unittest.TestCase):
    def test_load_oasst_en_limit_zero_returns_empty_without_download(self):
        # This must be a pure short-circuit -- if it tried to download OASST1 we'd know
        # from a timeout or a datasets ImportError being surfaced.
        self.assertEqual(CD.load_oasst_en(limit=0), [])


class TestWriteJsonl(unittest.TestCase):
    def test_roundtrip(self):
        recs = [
            {"messages": [{"role": "user", "content": "hi"},
                          {"role": "assistant", "content": "hello"}]},
            {"messages": [{"role": "user", "content": "hi2"},
                          {"role": "assistant", "content": "hello2"}]},
        ]
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "out.jsonl")
            CD.write_jsonl(recs, p)
            with open(p, encoding="utf-8") as f:
                back = [json.loads(l) for l in f]
            self.assertEqual(back, recs)


# ── eval.py ────────────────────────────────────────────────────────────────────

_HUMANEVAL_TASK = {
    "task_id": "HumanEval/0",
    "prompt": "def has_close_elements(numbers: list, threshold: float) -> bool:\n    ",
    "entry_point": "has_close_elements",
    "test": (
        "def check(candidate):\n"
        "    assert candidate([1.0, 2.0, 3.0], 0.5) is False\n"
        "    assert candidate([1.0, 2.0, 3.0, 1.05], 0.1) is True\n"
    ),
}

_MBPP_TASK = {
    "task_id": 42,
    "text": "Write a function that returns the sum of two numbers.",
    "test_list": ["assert add(1, 2) == 3", "assert add(-1, 1) == 0"],
}

_CORRECT_HUMANEVAL = (
    "def has_close_elements(numbers, threshold):\n"
    "    for i, a in enumerate(numbers):\n"
    "        for b in numbers[i+1:]:\n"
    "            if abs(a - b) < threshold:\n"
    "                return True\n"
    "    return False\n"
)
_WRONG_HUMANEVAL = "def has_close_elements(numbers, threshold):\n    return True\n"

_CORRECT_MBPP = "def add(a, b):\n    return a + b\n"
_WRONG_MBPP = "def add(a, b):\n    return a - b\n"


class TestEvalHumanEval(unittest.TestCase):
    def test_task_entry_shape(self):
        name, prompt, verify = CE.humaneval_task_to_entry(_HUMANEVAL_TASK)
        self.assertEqual(name, "HumanEval/0")
        self.assertIn("python", prompt.lower())
        self.assertTrue(callable(verify))

    def test_verify_fn_passes_on_correct(self):
        _, _, verify = CE.humaneval_task_to_entry(_HUMANEVAL_TASK)
        ns = {}
        exec(_CORRECT_HUMANEVAL, ns)
        self.assertTrue(verify(ns))

    def test_verify_fn_fails_on_wrong(self):
        _, _, verify = CE.humaneval_task_to_entry(_HUMANEVAL_TASK)
        ns = {}
        exec(_WRONG_HUMANEVAL, ns)
        self.assertFalse(verify(ns))

    def test_verify_fn_fails_when_entry_missing(self):
        _, _, verify = CE.humaneval_task_to_entry(_HUMANEVAL_TASK)
        self.assertFalse(verify({}))


class TestEvalMBPP(unittest.TestCase):
    def test_task_entry_shape(self):
        name, prompt, verify = CE.mbpp_task_to_entry(_MBPP_TASK)
        self.assertTrue(name.startswith("mbpp/"))
        self.assertIn("add(1, 2) == 3", prompt)

    def test_verify_fn_passes(self):
        _, _, verify = CE.mbpp_task_to_entry(_MBPP_TASK)
        ns = {}
        exec(_CORRECT_MBPP, ns)
        self.assertTrue(verify(ns))

    def test_verify_fn_fails_on_wrong(self):
        _, _, verify = CE.mbpp_task_to_entry(_MBPP_TASK)
        ns = {}
        exec(_WRONG_MBPP, ns)
        self.assertFalse(verify(ns))


class TestRunBenchmark(unittest.TestCase):
    def test_solves_when_solver_returns_correct(self):
        tasks = [CE.humaneval_task_to_entry(_HUMANEVAL_TASK),
                 CE.mbpp_task_to_entry(_MBPP_TASK)]
        answers = {tasks[0][0]: _CORRECT_HUMANEVAL, tasks[1][0]: _CORRECT_MBPP}
        # simple solver: look up the correct answer by finding the task in prompt content
        def solver(prompt: str) -> str:
            for name, p, _ in tasks:
                if p == prompt:
                    return f"```python\n{answers[name]}\n```"
            return "```python\n\n```"
        r = CE.run_benchmark(tasks, solver, verbose=False)
        self.assertEqual(r["n"], 2)
        self.assertEqual(r["solves"], 2)
        self.assertEqual(r["pass_at_1"], 1.0)

    def test_zero_solves_when_solver_returns_wrong(self):
        tasks = [CE.humaneval_task_to_entry(_HUMANEVAL_TASK)]
        r = CE.run_benchmark(tasks, lambda p: "```python\ndef has_close_elements(a, b): return True\n```",
                             verbose=False)
        self.assertEqual(r["solves"], 0)

    def test_solver_exception_scored_zero_not_raised(self):
        tasks = [CE.humaneval_task_to_entry(_HUMANEVAL_TASK)]
        def bad(p): raise RuntimeError("model died")
        r = CE.run_benchmark(tasks, bad, verbose=False)
        self.assertEqual(r["solves"], 0)
        self.assertIn("solver raised", r["per_task"][0]["feedback"])


# ── tool_wrapper.py ────────────────────────────────────────────────────────────

class TestToolWrapper(unittest.TestCase):
    def test_new_run_id_shape(self):
        rid = CW._new_run_id("coder-ft-1")
        self.assertRegex(rid, r"^\d{8}_\d{6}_[A-Za-z0-9_\-]+$")

    def test_new_run_id_sanitizes(self):
        rid = CW._new_run_id("nasty/name spaces & chars")
        self.assertNotIn("/", rid)
        self.assertNotIn(" ", rid)
        self.assertNotIn("&", rid)

    def test_pid_alive_false_for_missing(self):
        # a very unlikely PID
        self.assertFalse(CW._pid_alive(2_147_483_640))

    def test_check_unknown_run_id(self):
        r = CW.check_coder_training("no_such_run_id_xyz_" + os.urandom(2).hex())
        self.assertFalse(r["ok"])

    def test_resolve_checkpoint_missing_raises(self):
        with self.assertRaises(FileNotFoundError):
            CW._resolve_checkpoint("definitely-not-a-real-run-id-" + os.urandom(2).hex())

    def test_resolve_checkpoint_finds_local_dir(self):
        with tempfile.TemporaryDirectory() as td:
            # create a fake final/ inside a fake runs_root
            orig_root = CW.RUNS_ROOT
            try:
                CW.RUNS_ROOT = Path(td)
                run = Path(td) / "20260101_120000_test"
                (run / "final").mkdir(parents=True)
                got = CW._resolve_checkpoint("20260101_120000_test")
                self.assertEqual(got.name, "final")
            finally:
                CW.RUNS_ROOT = orig_root

    def test_train_coder_missing_data_returns_error(self):
        r = CW.train_coder(data_path="/nonexistent/definitely_not_a_file.jsonl")
        self.assertFalse(r["ok"])
        self.assertIn("missing", r["error"])


# ── standalone runner (no pytest required) ─────────────────────────────────────

if __name__ == "__main__":
    unittest.main(verbosity=2)
