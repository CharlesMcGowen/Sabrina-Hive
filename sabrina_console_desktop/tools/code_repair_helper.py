"""
tools/code_repair_helper.py
============================
Reusable engineering-task infrastructure, built after two real, live findings
tonight while using a coder model to fix Sabrina's own codebase:

1. Asking a model to regenerate an ENTIRE file to apply a small change is
   fragile -- confirmed live: qwen2.5-coder:32b reliably choked on a file
   containing a regex with literal triple-backticks in it, because its own
   response is ALSO wrapped in triple-backtick markdown fences. The nested
   backtick sequence caused a truncated/malformed string literal every time,
   regardless of token budget (raising num_predict from 3000 to 6000 did not
   fix it -- same failure, just later). Asking for ONLY the one function that
   actually needs to change, then splicing it back programmatically, fixed it
   immediately.

2. A linter (pyflakes) cannot repair a raw syntax error -- it requires
   ast.parse() to succeed just to run at all. Deterministic, non-LLM repair
   (_attempt_deterministic_syntax_repair, tools/agent_skills.py) is free,
   instant, and has zero risk of a second generation failure, so it must
   always be tried BEFORE spending another model call on a fix.

Use this module's `patch_function()` for "fix/change this one function in an
existing file" tasks instead of asking a model to regenerate the whole file.
"""

import ast
import re
from typing import Optional, Tuple

from tools.agent_skills import _attempt_deterministic_syntax_repair


def extract_function_source(file_source: str, function_name: str, class_name: Optional[str] = None) -> Optional[Tuple[str, int, int]]:
    """
    Finds a function or method by name (optionally scoped to a class) and
    returns (source_text, start_line, end_line) using its EXACT source
    segment (ast.get_source_segment), 1-indexed inclusive line numbers
    matching file_source.splitlines(). Returns None if not found.
    """
    tree = ast.parse(file_source)
    lines = file_source.splitlines()

    def _search(nodes, want_class):
        for node in nodes:
            if isinstance(node, ast.ClassDef):
                if want_class is None or node.name == want_class:
                    found = _search(node.body, None if want_class == node.name else want_class)
                    if found:
                        return found
                if want_class is not None and node.name != want_class:
                    continue
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == function_name:
                if want_class is None or want_class == "":
                    segment = ast.get_source_segment(file_source, node)
                    if segment is not None:
                        return segment, node.lineno, node.end_lineno
        return None

    result = _search(tree.body, class_name)
    if result:
        return result
    return None


def splice_function(file_source: str, function_name: str, new_function_source: str, class_name: Optional[str] = None) -> Optional[str]:
    """
    Replaces an existing function/method's source lines with new_function_source,
    preserving the original indentation level. Returns the full updated file
    source, or None if the target function couldn't be located or the result
    doesn't parse.
    """
    found = extract_function_source(file_source, function_name, class_name)
    if not found:
        return None
    _, start_line, end_line = found

    lines = file_source.splitlines(keepends=True)
    original_first_line = lines[start_line - 1]
    indent = original_first_line[: len(original_first_line) - len(original_first_line.lstrip())]

    new_lines = new_function_source.strip("\n").splitlines()
    reindented = [indent + l if l.strip() else l for l in new_lines]
    replacement = "\n".join(reindented) + "\n"

    updated = "".join(lines[: start_line - 1]) + replacement + "".join(lines[end_line:])
    try:
        ast.parse(updated)
    except SyntaxError:
        return None
    return updated


def get_repaired_code(code: str) -> Tuple[str, bool, str]:
    """
    The mandatory first step before any re-prompt: try free, instant,
    deterministic repair. Returns (code, was_valid_or_repaired, note).
    If the code already parses, or a deterministic fix makes it parse,
    was_valid_or_repaired is True and no model call was needed.
    """
    try:
        ast.parse(code)
        return code, True, "valid as generated, no repair needed"
    except SyntaxError as e:
        repaired = _attempt_deterministic_syntax_repair(code)
        if repaired:
            return repaired, True, f"deterministically repaired (was: {e})"
        return code, False, f"deterministic repair could not fix: {e}"


def extract_code_block(response_text: str) -> Optional[str]:
    """Pulls the last ```python ... ``` fenced block out of a model response."""
    matches = re.findall(r"```python\s*(.*?)```", response_text, re.DOTALL)
    if matches:
        return matches[-1].strip()
    matches = re.findall(r"```\s*(.*?)```", response_text, re.DOTALL)
    return matches[-1].strip() if matches else None
