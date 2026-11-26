"""Check fixed GoalSpecs, never a recruit-supplied theorem statement."""
import os
from pathlib import Path
import re
import subprocess
import uuid

from .common import digest, load, save
from .goals import ACTIONS, expr, validate

PROJECT = Path("/home/ego/eth-audit-20261119/audit-lake")
MATHLIB_REV = "d13f23b723b8a846827a245b89c10fc7d3f11612"
HEADER = """import Mathlib.Tactic.Ring
import Mathlib.Tactic.LinearCombination
import Mathlib.Tactic.Linarith
"""


def proof(goal, action):
    hypotheses = ", ".join(f"h{i}" for i in range(len(goal["hypotheses"])))
    simplification = "simp" + (f" only [{hypotheses}]" if hypotheses else "")
    scripts = {"rfl": "rfl", "simp": "simp_all", "rewrite": simplification + "\nring",
               "ring": "ring", "omega": "omega",
               "induct": """induction n with
| zero => simp [seq] <;> ring
| succ n ih =>
  simp only [seq, Int.natCast_succ]
  linear_combination ih"""}
    return scripts[action]


def theorem(goal, body, name):
    validate(goal)
    variables = goal["variables"]
    decl = " ".join(f"({key} : {value})" for key, value in variables.items())
    for i, (op, left, right) in enumerate(goal["hypotheses"]):
        decl += f" (h{i} : {expr(left, variables)[0]} {op} {expr(right, variables)[0]})"
    statement = f"{expr(goal['lhs'], variables)[0]} {goal['relation']} {expr(goal['rhs'], variables)[0]}"
    definition = ""
    if goal["recurrence"] is not None:
        coeff = goal["recurrence"]
        term = " + ".join(f"({c} : Int) * ((k : Int) ^ {i})" for i, c in enumerate(coeff))
        definition = f"def seq : Nat → Int → Int\n| 0, x => x\n| k + 1, x => seq k x + ({term})\n"
    return f"{definition}theorem {name} {decl} : {statement} := by\n" + "\n".join("  " + s for s in body.splitlines()) + "\n"


class Checker:
    def __init__(self, cache):
        self.cache = Path(cache)
        self.cache.mkdir(parents=True, exist_ok=True)
        revision = subprocess.check_output(["git", "-C", str(PROJECT / ".lake/packages/mathlib"),
                                            "rev-parse", "HEAD"], text=True).strip()
        if revision != MATHLIB_REV:
            raise ValueError("Pinned mathlib revision changed")
        chain = (PROJECT / "lean-toolchain").read_text().strip()
        if chain != "leanprover/lean4:v4.34.1":
            raise ValueError("Pinned Lean version changed")
        self.toolchain = Path("/home/ego/.elan/toolchains/leanprover--lean4---v4.34.1")
        self.libs = sorted((PROJECT / ".lake/packages").glob("*/.lake/build/lib/lean"))

    def run(self, source):
        key = digest(dict(source=source, revision=MATHLIB_REV, lean="4.34.1"))
        path = self.cache / f"{key}.json"
        if path.exists():
            return load(path)
        name = "math-proof-" + uuid.uuid4().hex
        cmd = ["docker", "run", "--rm", "-i", "--name", name, "--pull", "never", "--network", "none",
               "--read-only", "--cap-drop", "ALL", "--user", f"{os.getuid()}:{os.getgid()}",
               "--security-opt", "no-new-privileges", "--pids-limit", "48", "--memory", "5g",
               "--memory-swap", "5g", "--cpus", "1", "--log-driver", "none",
               "-v", f"{self.toolchain}:/lean:ro"]
        search = []
        for lib in self.libs:
            target = "/deps/" + lib.parents[3].name
            cmd.extend(["-v", f"{lib}:{target}:ro"])
            search.append(target)
        cmd.extend(["-e", "LEAN_PATH=" + ":".join(search), "python:3.11-slim-bookworm",
                    "timeout", "150", "/lean/bin/lean", "-j1", "-DmaxHeartbeats=250000", "--stdin"])
        try:
            result = subprocess.run(cmd, input=source.encode(), capture_output=True, timeout=170)
        except subprocess.TimeoutExpired:
            subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=15)
            return dict(ok=False, error="checker_timeout", source_hash=key)
        diagnostic = (result.stdout + result.stderr).decode(errors="replace")[:65536]
        axioms = re.findall(r"depends on axioms:\s*\[([^]]*)\]", diagnostic)
        allowed = {"propext", "Classical.choice", "Quot.sound"}
        sound = (bool(axioms) or "does not depend on any axioms" in diagnostic) and all(set(x.strip() for x in row.split(",") if x.strip()) <= allowed for row in axioms)
        verdict = dict(ok=result.returncode == 0 and sound, exit_code=result.returncode,
                       diagnostic=diagnostic, source_hash=key)
        # Timeouts and host failures may be retried. Proof rejections are durable.
        if result.returncode in (0, 1):
            save(path, verdict)
        return verdict

    def check(self, goal, body):
        source = HEADER + theorem(goal, body, "result") + "\n#print axioms result\n"
        return self.run(source)

    def actions(self, goal):
        """Batch six isolated declarations. Each has its own kernel axiom check."""
        key = digest(dict(goal=goal, header=HEADER, actions=[proof(goal, a) for a in ACTIONS],
                          revision=MATHLIB_REV))
        path = self.cache / f"actions-{key}.json"
        if path.exists():
            return load(path)
        source = HEADER
        for index, action in enumerate(ACTIONS):
            source += f"\nnamespace C{index}\n" + theorem(goal, proof(goal, action), "result")
            source += f"end C{index}\n#print axioms C{index}.result\n"
        outcome = self.run(source)
        if outcome.get("exit_code") not in (0, 1):
            raise RuntimeError("Lean batch did not complete: " + str(outcome))
        result = {}
        for index, action in enumerate(ACTIONS):
            match = re.search(rf"'C{index}\.result' depends on axioms:\s*\[([^]]*)\]", outcome["diagnostic"])
            no_axioms = f"'C{index}.result' does not depend on any axioms" in outcome["diagnostic"]
            deps = set(x.strip() for x in match[1].split(",") if x.strip()) if match else (set() if no_axioms else {"missing"})
            result[action] = dict(ok=deps <= {"propext", "Classical.choice", "Quot.sound"},
                                  axioms=sorted(deps))
        save(path, result)
        return result
