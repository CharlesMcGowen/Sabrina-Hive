"""Evaluate the Python source used by a versioned recovery context worker."""
from __future__ import annotations

import ast
import json
import operator
import sys


CUT = "\n[Context shortened]\n"
MAX_SOURCE = 4096
MAX_CONTEXT = 65536
_PARAMS = ("context", "limit", "fault")
_OPS = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.FloorDiv: operator.floordiv,
    ast.Eq: operator.eq, ast.NotEq: operator.ne, ast.Lt: operator.lt,
    ast.LtE: operator.le, ast.Gt: operator.gt, ast.GtE: operator.ge,
}
_FUNCTIONS = {"len": len, "min": min, "max": max}
_NODES = (
    ast.If, ast.Assign, ast.Name, ast.Return, ast.Load, ast.Store, ast.Constant,
    ast.BinOp, ast.UnaryOp, ast.USub, ast.Compare, ast.Subscript, ast.Slice, ast.Call,
    ast.Add, ast.Sub, ast.FloorDiv, ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE,
)


def parse_worker(source: str) -> list[ast.stmt]:
    if len(source.encode("utf-8")) > MAX_SOURCE:
        raise ValueError("Worker source is too large")
    tree = ast.parse(source)
    definitions = tree.body[1:] if ast.get_docstring(tree) is not None else tree.body
    if len(definitions) != 1 or not isinstance(definitions[0], ast.FunctionDef):
        raise ValueError("Provide one compact(context, limit, fault) function")
    func = definitions[0]
    args = func.args
    if (func.name != "compact" or tuple(arg.arg for arg in args.args) != _PARAMS
        or any(arg.annotation for arg in args.args) or func.decorator_list or func.returns
        or func.type_params or args.defaults or args.posonlyargs or args.kwonlyargs or args.vararg or args.kwarg):
        raise ValueError("Invalid recovery worker signature")
    statements = func.body[1:] if ast.get_docstring(func) is not None else func.body
    nodes = [node for statement in statements for node in ast.walk(statement)]
    if len(nodes) > 220:
        raise ValueError("Recovery worker has too many operations")
    for node in nodes:
        if not isinstance(node, _NODES):
            raise ValueError(f"Unsupported recovery worker operation {type(node).__name__}")
        if isinstance(node, ast.Assign):
            if len(node.targets) != 1 or not isinstance(node.targets[0], ast.Name):
                raise ValueError("Use a single local variable assignment")
            if node.targets[0].id in {*_PARAMS, "CUT", *_FUNCTIONS}:
                raise ValueError("Worker inputs are read-only")
        if isinstance(node, ast.Constant):
            if (type(node.value) is str and node.value in {"timeout", "context_overflow", "other", ""}):
                continue
            if type(node.value) is not int or not 0 <= node.value <= MAX_CONTEXT:
                raise ValueError("Use integers and the documented fault names")
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in _FUNCTIONS or node.keywords:
                raise ValueError("Calls are limited to len, min, and max")
        if isinstance(node, ast.Compare) and len(node.ops) != 1:
            raise ValueError("Use a single comparison at a time")
        if isinstance(node, ast.Subscript) and not isinstance(node.slice, ast.Slice):
            raise ValueError("Only slices are supported")
        if isinstance(node, ast.Slice) and node.step is not None:
            raise ValueError("Slice steps are unsupported")
    return statements


def evaluate_worker(statements: list[ast.stmt], context: str, limit: int, fault: str) -> str:
    if not isinstance(context, str) or len(context) > MAX_CONTEXT:
        raise ValueError("Invalid recovery context")
    if type(limit) is not int or not 1000 <= limit <= 12000 or fault not in {"timeout", "context_overflow", "other"}:
        raise ValueError("Invalid recovery worker inputs")
    values = {"context": context, "limit": limit, "fault": fault, "CUT": CUT}

    def expression(node: ast.expr):
        match node:
            case ast.Name(id=name):
                return values[name]
            case ast.Constant(value=value):
                return value
            case ast.UnaryOp(op=ast.USub(), operand=operand):
                return -expression(operand)
            case ast.BinOp(left=left, op=op, right=right):
                value = _OPS[type(op)](expression(left), expression(right))
                if isinstance(value, str) and len(value) > MAX_CONTEXT:
                    raise ValueError("Worker text exceeds the size limit")
                return value
            case ast.Compare(left=left, ops=[op], comparators=[right]):
                return _OPS[type(op)](expression(left), expression(right))
            case ast.Subscript(value=value, slice=ast.Slice(lower=lower, upper=upper)):
                start = expression(lower) if lower is not None else None
                stop = expression(upper) if upper is not None else None
                return expression(value)[start:stop]
            case ast.Call(func=ast.Name(id=name), args=args):
                return _FUNCTIONS[name](*(expression(arg) for arg in args))
            case _:
                raise ValueError("Unsupported recovery worker expression")

    def block(body: list[ast.stmt]) -> str | None:
        for statement in body:
            match statement:
                case ast.Assign(targets=[ast.Name(id=name)], value=value):
                    values[name] = expression(value)
                case ast.If(test=test, body=yes, orelse=no):
                    result = block(yes if expression(test) else no)
                    if result is not None:
                        return result
                case ast.Return(value=value):
                    result = expression(value)
                    if not isinstance(result, str):
                        raise ValueError("Recovery worker must return text")
                    return result
        return None

    result = block(statements)
    if result is None or len(result) > limit:
        raise ValueError("Recovery worker exceeded its context limit or omitted a return")
    return result


if __name__ == "__main__":
    try:
        request = json.loads(sys.stdin.buffer.read(1_048_577))
        instructions = parse_worker(request["source"])
        cases = request["cases"]
        if not 1 <= len(cases) <= 12:
            raise ValueError("Invalid validation batch size")
        results = [evaluate_worker(instructions, **case) for case in cases]
        print(json.dumps({"results": results}))
    except (ValueError, SyntaxError, KeyError, TypeError, ArithmeticError, RecursionError) as error:
        print(json.dumps({"error": str(error)[:220]}))
        raise SystemExit(1)
