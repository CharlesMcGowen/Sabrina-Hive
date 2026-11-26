"""Typed expressions and frozen fixtures. No input is executable Lean source."""
import random
import re
from typing import Literal, TypedDict

from .common import digest, load, save

ACTIONS = ("rfl", "simp", "rewrite", "ring", "omega", "induct")
FEATURES = ("bias", "has_hypotheses", "has_recurrence", "equality", "inequality",
            "degree0", "degree1", "degree2", "degree3", "has_nat", "has_power", "has_add")
SCHEMA = "math-proof-goal-v1"


class GoalSpec(TypedDict):
    schema: Literal["math-proof-goal-v1"]
    variables: dict[str, Literal["Nat", "Int"]]
    hypotheses: list[list]
    relation: Literal["=", "<=", "<"]
    lhs: int | str | list
    rhs: int | str | list
    recurrence: list[int] | None


def expr(value, variables, depth=0):
    if depth > 12:
        raise ValueError("Expression nesting exceeds 12")
    if type(value) is int and abs(value) <= 1000000:
        return f"({value} : Int)", 0, set()
    if isinstance(value, str) and value in variables:
        return ("(n : Int)" if value == "n" else value), 1, set()
    if not isinstance(value, list) or len(value) != 3:
        raise ValueError("Expected a typed expression")
    op, left, right = value
    if op == "seq" and left == "n" and right == "x" and variables == {"n": "Nat", "x": "Int"}:
        return "(seq n x)", 1, {"seq"}
    l, dl, ol = expr(left, variables, depth + 1)
    if op == "^":
        if type(right) is not int or not 0 <= right <= 3:
            raise ValueError("Exponent must be between zero and three")
        if dl * right > 3:
            raise ValueError("Polynomial degree exceeds three")
        return f"({l} ^ {right})", dl * right, ol | {"^"}
    r, dr, ore = expr(right, variables, depth + 1)
    if op not in ("+", "-", "*"):
        raise ValueError("Unknown expression operation")
    degree = dl + dr if op == "*" else max(dl, dr)
    if degree > 3:
        raise ValueError("Polynomial degree exceeds three")
    return f"({l} {op} {r})", degree, ol | ore | {op}


def validate(goal):
    if not isinstance(goal, dict) or set(goal) != {"schema", "variables", "hypotheses", "relation", "lhs", "rhs", "recurrence"}:
        raise ValueError("Expected the exact GoalSpec v1 fields")
    variables = goal["variables"]
    if goal["schema"] != SCHEMA or not isinstance(variables, dict) or not 1 <= len(variables) <= 6:
        raise ValueError("Invalid goal schema or variable count")
    if any(not re.fullmatch("[a-z]", key) or key in ("h", "i") or value not in ("Nat", "Int")
           or (value == "Nat" and key != "n") or (key == "n" and value != "Nat")
           for key, value in variables.items()):
        raise ValueError("Use single-letter integer variables and optional natural n")
    hypotheses = goal["hypotheses"]
    if not isinstance(hypotheses, list) or len(hypotheses) > 4:
        raise ValueError("At most four assumptions")
    assignments = {}
    for hypothesis in hypotheses:
        if not isinstance(hypothesis, list) or len(hypothesis) != 3:
            raise ValueError("Expected a relation and two expressions for each assumption")
        relation, left, right = hypothesis
        if relation == "=" and isinstance(left, str) and type(right) is int:
            if left in assignments and assignments[left] != right:
                raise ValueError("Contradictory constant assignments")
            assignments[left] = right
    facts = []
    for relation, left, right in [*hypotheses, [goal["relation"], goal["lhs"], goal["rhs"]]]:
        if relation not in ("=", "<=", "<"):
            raise ValueError("Unknown relation")
        facts.extend((expr(left, variables), expr(right, variables)))
    rec = goal["recurrence"]
    if rec is not None:
        if variables != {"n": "Nat", "x": "Int"} or not isinstance(rec, list) or not 1 <= len(rec) <= 3:
            raise ValueError("Recurrences use n, x and up to three integer coefficients")
        if any(type(c) is not int or abs(c) > 100 for c in rec):
            raise ValueError("Invalid recurrence coefficient")
    elif any("seq" in ops for _, _, ops in facts):
        raise ValueError("Missing recurrence definition")
    return facts


def features(goal):
    facts = validate(goal)
    degree = max(d for _, d, _ in facts)
    ops = set().union(*(o for _, _, o in facts))
    return [1, int(bool(goal["hypotheses"])), int(goal["recurrence"] is not None),
            int(goal["relation"] == "="), int(goal["relation"] != "="),
            *(int(degree == i) for i in range(4)), int("Nat" in goal["variables"].values()),
            int("^" in ops), int("+" in ops)]


def add(a, b): return ["+", a, b]
def sub(a, b): return ["-", a, b]
def mul(a, b): return ["*", a, b]
def power(a, n): return ["^", a, n]


def specimen(rng, family, holdout):
    c, d = rng.randint(2, 95), rng.randint(-95, -2)
    variables, assumptions, recurrence = {"x": "Int", "y": "Int"}, [], None
    relation = "="
    if family == 0:
        if holdout:
            left = power(add("x", c), 3)
            right = add(add(power("x", 3), mul(3*c, power("x", 2))), add(mul(3*c*c, "x"), c**3))
        else:
            left = mul(add("x", c), add("y", d))
            right = add(add(mul("x", "y"), mul(d, "x")), add(mul(c, "y"), c*d))
    elif family == 1:
        assumptions = [["=", "y", add("x", c)]]
        left, right = power("y", 2), add(add(power("x", 2), mul(2*c, "x")), c*c)
        if holdout:
            left, right = mul(d, left), mul(d, right)
    elif family == 2:
        variables["z"] = "Int"
        assumptions = [["<=", "x", "y"], ["<=", "y", "z"]]
        left, right = add("x", c), add("z", c)
        if holdout:
            left, right = add(mul(c, "x"), d), add(mul(c, "z"), d)
        relation = "<="
    else:
        variables = {"n": "Nat", "x": "Int"}
        degree = rng.randrange(3)
        recurrence = [rng.randint(1, 9) for _ in range(degree + 1)]
        if degree == 0:
            left, right = ["seq", "n", "x"], add("x", mul(recurrence[0], "n"))
        elif degree == 1:
            b, a = recurrence
            left = mul(2, ["seq", "n", "x"])
            right = add(add(mul(2, "x"), mul(a, mul("n", sub("n", 1)))), mul(2*b, "n"))
        else:
            c0, b, a = recurrence
            left = mul(6, ["seq", "n", "x"])
            right = add(add(mul(6, "x"), mul(a, mul(mul("n", sub("n", 1)), sub(mul(2, "n"), 1)))),
                        add(mul(3*b, mul("n", sub("n", 1))), mul(6*c0, "n")))
        if holdout:
            left, right = add(left, "x"), add("x", right)
    goal = dict(schema=SCHEMA, variables=variables, hypotheses=assumptions, relation=relation,
                lhs=left, rhs=right, recurrence=recurrence)
    validate(goal)
    return goal


def freeze(run):
    if (run / "fixtures.json").exists():
        return load(run / "fixtures.json")
    # Freeze both corpora before sending author briefs.
    manifest = {}
    seen = set()
    for split, seed in (("development", 2026112201), ("holdout", 2026112202)):
        rng = random.Random(seed)
        items = []
        for panel in range(8):
            for i in range(32):
                while True:
                    goal = specimen(rng, i % 4, split == "holdout")
                    key = digest(goal)
                    if key not in seen:
                        break
                seen.add(key)
                items.append(dict(id=key[:20], panel=panel, goal=goal))
        negatives = []
        for item in items[:16]:
            import copy
            goal = copy.deepcopy(item["goal"])
            if goal["relation"] == "=":
                goal["rhs"] = add(goal["rhs"], 1)
            else:
                goal["hypotheses"] = []
                goal["relation"] = "<"
                goal["lhs"], goal["rhs"] = "x", "x"
            negatives.append(dict(id=digest(goal)[:20], goal=goal))
        data = dict(problems=items, negatives=negatives)
        save(run / f"{split}.json", data)
        manifest[split] = digest(data)
    save(run / "fixtures.json", manifest)
    return manifest
