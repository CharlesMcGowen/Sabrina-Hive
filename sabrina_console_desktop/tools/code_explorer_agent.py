"""
tools/code_explorer_agent.py
=============================
A real tool-using agent loop for exploring/patching a codebase, built after
tonight's finding that "analysis" scripts which just paste code chunks into a
single prompt and relay the text back are NOT the same thing as an agent
looking at the codebase itself. This gives the model actual tools --
read_file, list_dir, grep, propose_patch -- scoped to one root directory, and
lets the model decide what to look at next across multiple turns, instead of
a human (or a hardcoded script) pre-selecting every chunk it sees.

Guardrails, on purpose:
- Read-only tools execute immediately and their results go back to the model.
- propose_patch does NOT write to disk. It uses code_repair_helper.py to
  validate the patch (ast.parse gate, splice_function) against a COPY of the
  file's text, and returns the resulting file text as a diff-preview string.
  A human (or a separate validator step) must call apply_proposed_patch()
  explicitly to actually write it. The model can never touch disk directly.
- All tool paths are resolved and checked to stay inside an operator-authorized
  ROOT_DIR -- no path traversal outside the scoped repo.
"""

import ast
import os
import re
import sys
import subprocess
import json
from pathlib import Path
from typing import Optional

import requests

# Live-crashed tonight: reading a real source file can surface a character
# (e.g. a smart quote or emoji in a comment/docstring) that Windows' default
# console codepage (cp1252) can't print, which raised UnicodeEncodeError and
# killed the whole run mid-loop. Reconfigure stdout to UTF-8 with replacement
# so a display-only print never takes down a multi-turn agent run.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from tools.code_repair_helper import splice_function
from tools.repository_authorization import RepositoryAuthorization

# Live-observed tonight: the public RunPod HTTPS proxy (Cloudflare-fronted) reliably
# drops requests once the harness's accumulated tool-result context grows past a few
# turns (died at the same turn twice in a row with an empty response body). Every
# direct on-pod curl all night was rock solid, so tunnel straight to the pod's Ollama
# port over SSH instead of going through the public proxy at all:
#   ssh -f -N -L 11500:127.0.0.1:11434 root@<pod-ip> -p <ssh-port>
OLLAMA_URL = "http://127.0.0.1:11500/api/generate"
MODEL = "qwen2.5-coder:32b"


def _resolve_scoped(root: Path, rel_path: str) -> Optional[Path]:
    """Resolves rel_path under root, refusing anything that escapes it."""
    try:
        candidate = (root / rel_path).resolve()
        root_resolved = root.resolve()
        if root_resolved not in candidate.parents and candidate != root_resolved:
            return None
        return candidate
    except Exception:
        return None


class CodeExplorerAgent:
    def __init__(self, root_dir: str, authorization: dict, max_turns: int = 12):
        """Create a code-review agent for one explicitly authorized root only."""
        self.authorization = RepositoryAuthorization.from_mapping(authorization)
        self.root = Path(root_dir).resolve()
        allowed, reason = self.authorization.check_root(self.root)
        if not allowed:
            raise ValueError(f"refusing repository analysis: {reason}")
        self.max_turns = max_turns
        self.history = []  # list of {"role": ..., "content": ...}
        self.pending_patches = {}  # patch_id -> {file, function, new_source, preview}

    # ---- Tools -------------------------------------------------------

    def tool_list_dir(self, rel_path: str = ".") -> str:
        p = _resolve_scoped(self.root, rel_path)
        if p is None or not p.exists():
            return f"ERROR: path not found or outside scope: {rel_path}"
        if not p.is_dir():
            return f"ERROR: not a directory: {rel_path}"
        entries = sorted(os.listdir(p))
        return "\n".join(entries[:200])

    def tool_read_file(self, rel_path: str, start_line: int = 1, end_line: int = 200) -> str:
        p = _resolve_scoped(self.root, rel_path)
        if p is None or not p.exists() or not p.is_file():
            return f"ERROR: file not found or outside scope: {rel_path}"
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        start = max(1, start_line)
        end = min(len(lines), end_line)
        numbered = [f"{i+1}: {lines[i]}" for i in range(start - 1, end)]
        return "\n".join(numbered) if numbered else "(empty range)"

    def tool_grep(self, pattern: str, rel_path: str = ".") -> str:
        p = _resolve_scoped(self.root, rel_path)
        if p is None or not p.exists():
            return f"ERROR: path not found or outside scope: {rel_path}"
        try:
            regex = re.compile(pattern)
        except re.error as e:
            return f"ERROR: invalid regex: {e}"
        matches = []
        search_root = p if p.is_dir() else p.parent
        for py_file in search_root.rglob("*.py"):
            try:
                text = py_file.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            for i, line in enumerate(text.splitlines(), start=1):
                if regex.search(line):
                    rel = py_file.relative_to(self.root)
                    matches.append(f"{rel}:{i}: {line.strip()[:200]}")
                    if len(matches) >= 100:
                        break
            if len(matches) >= 100:
                break
        return "\n".join(matches) if matches else "(no matches)"

    def tool_lint_file(self, rel_path: str) -> str:
        """Runs pyflakes on a real file so the model can verify its own understanding/work,
        not just mine. Read-only -- never touches the staged-patch text, only what's on disk."""
        p = _resolve_scoped(self.root, rel_path)
        if p is None or not p.exists() or not p.is_file():
            return f"ERROR: file not found or outside scope: {rel_path}"
        try:
            result = subprocess.run(
                [sys.executable, "-m", "pyflakes", str(p)],
                capture_output=True, text=True, timeout=20,
            )
        except Exception as e:
            return f"ERROR running pyflakes: {e}"
        out = (result.stdout + result.stderr).strip()
        return out if out else "(no issues found)"

    def tool_propose_line_patch(self, rel_path: str, old_line_text: str, new_line_text: Optional[str] = None) -> str:
        """
        For edits propose_patch can't handle: module-level imports, constants,
        any single-line statement outside a function/method body. Requires an
        EXACT, unique match for old_line_text so there's no ambiguity about
        which line is being changed. Same ast.parse safety gate as
        propose_patch -- never stages a result that fails to parse.

        new_line_text=None (or omitted) DELETES the line entirely, shifting
        later lines up -- the common case for removing one name out of a
        multi-line import. Pass an empty string explicitly if you genuinely
        want a blank line left in place instead.
        """
        p = _resolve_scoped(self.root, rel_path)
        if p is None or not p.exists() or not p.is_file():
            return f"ERROR: file not found or outside scope: {rel_path}"
        original = p.read_text(encoding="utf-8", errors="replace")
        lines = original.splitlines(keepends=True)
        matches = [i for i, l in enumerate(lines) if l.rstrip("\r\n") == old_line_text.rstrip("\r\n")]
        if len(matches) == 0:
            return f"ERROR: old_line_text not found verbatim in {rel_path}. Read the exact current line first."
        if len(matches) > 1:
            return (f"ERROR: old_line_text matches {len(matches)} lines in {rel_path} "
                    f"(lines {[m+1 for m in matches]}) -- not unique enough to safely replace.")
        idx = matches[0]
        if new_line_text is None:
            new_lines = lines[:idx] + lines[idx + 1:]
        else:
            line_ending = "\n" if lines[idx].endswith("\n") else ""
            new_lines = lines[:idx] + [new_line_text.rstrip("\r\n") + line_ending] + lines[idx + 1:]
        updated = "".join(new_lines)
        try:
            ast.parse(updated)
        except SyntaxError as e:
            return f"ERROR: replacement would break the file's syntax: {e}. No changes made."
        patch_id = f"patch_{len(self.pending_patches) + 1}"
        self.pending_patches[patch_id] = {
            "file": str(p), "function": f"(line {idx + 1})", "class": None,
            "new_source": new_line_text, "updated_full_text": updated,
        }
        return (f"Patch staged as {patch_id} (NOT written to disk). Line {idx + 1} changed, "
                f"file still parses cleanly. A human must call apply_proposed_patch('{patch_id}') "
                f"to actually write it.")

    def tool_propose_patch(self, rel_path: str, function_name: str, new_function_source: str,
                            class_name: Optional[str] = None) -> str:
        p = _resolve_scoped(self.root, rel_path)
        if p is None or not p.exists() or not p.is_file():
            return f"ERROR: file not found or outside scope: {rel_path}"
        original = p.read_text(encoding="utf-8", errors="replace")
        updated = splice_function(original, function_name, new_function_source, class_name)
        if updated is None:
            return (f"ERROR: could not splice '{function_name}' -- either it wasn't found in "
                    f"{rel_path}, or the resulting file failed ast.parse(). No changes made.")
        patch_id = f"patch_{len(self.pending_patches) + 1}"
        self.pending_patches[patch_id] = {
            "file": str(p), "function": function_name, "class": class_name,
            "new_source": new_function_source, "updated_full_text": updated,
        }
        return (f"Patch staged as {patch_id} (NOT written to disk). It parses cleanly. "
                f"A human must call apply_proposed_patch('{patch_id}') to actually write it.")

    TOOLS = [
        {"type": "function", "function": {"name": "list_dir", "description": "List files/dirs at a path relative to the scoped repo root.",
            "parameters": {"type": "object", "properties": {"rel_path": {"type": "string"}}, "required": []}}},
        {"type": "function", "function": {"name": "read_file", "description": "Read a range of lines from a file (line numbers included in output).",
            "parameters": {"type": "object", "properties": {
                "rel_path": {"type": "string"}, "start_line": {"type": "integer"}, "end_line": {"type": "integer"}},
                "required": ["rel_path"]}}},
        {"type": "function", "function": {"name": "grep", "description": "Search for a regex pattern across .py files under a path.",
            "parameters": {"type": "object", "properties": {"pattern": {"type": "string"}, "rel_path": {"type": "string"}},
                "required": ["pattern"]}}},
        {"type": "function", "function": {"name": "propose_patch", "description": "Stage a replacement for one function/method. Does NOT write to disk -- only stages it for human approval.",
            "parameters": {"type": "object", "properties": {
                "rel_path": {"type": "string"}, "function_name": {"type": "string"},
                "new_function_source": {"type": "string"}, "class_name": {"type": "string"}},
                "required": ["rel_path", "function_name", "new_function_source"]}}},
        {"type": "function", "function": {"name": "lint_file", "description": "Run pyflakes on a real file on disk and return its findings (unused imports, undefined names, etc.).",
            "parameters": {"type": "object", "properties": {"rel_path": {"type": "string"}}, "required": ["rel_path"]}}},
        {"type": "function", "function": {"name": "propose_line_patch", "description": "Stage an exact single-line change for edits propose_patch can't handle (imports, constants, any line outside a function body). old_line_text must match exactly one line verbatim. Omit new_line_text to delete the line entirely (the common case for removing one name from a multi-line import); pass an empty string only if you want a blank line left behind instead.",
            "parameters": {"type": "object", "properties": {
                "rel_path": {"type": "string"}, "old_line_text": {"type": "string"}, "new_line_text": {"type": "string"}},
                "required": ["rel_path", "old_line_text"]}}},
    ]

    def _dispatch(self, name: str, args: dict) -> str:
        if name == "list_dir":
            return self.tool_list_dir(args.get("rel_path", "."))
        if name == "read_file":
            return self.tool_read_file(args["rel_path"], args.get("start_line", 1), args.get("end_line", 200))
        if name == "grep":
            return self.tool_grep(args["pattern"], args.get("rel_path", "."))
        if name == "propose_patch":
            return self.tool_propose_patch(args["rel_path"], args["function_name"],
                                            args["new_function_source"], args.get("class_name"))
        if name == "lint_file":
            return self.tool_lint_file(args["rel_path"])
        if name == "propose_line_patch":
            return self.tool_propose_line_patch(args["rel_path"], args["old_line_text"], args.get("new_line_text"))
        return f"ERROR: unknown tool {name}"

    def apply_proposed_patch(self, patch_id: str, deploy_to_lab: bool = False) -> str:
        """
        The ONLY path that writes to disk. Called by a human, not the model.
        deploy_to_lab=True additionally pushes the same file to both known
        lab-host copies (Sabrina-windows-sync, sabrina_win_arena) right after
        writing it locally, closing the loop from "harness proposed a fix" to
        "fix is live on the lab host" without a separate manual sync step.
        """
        patch = self.pending_patches.get(patch_id)
        if not patch:
            return f"ERROR: no such staged patch: {patch_id}"
        local_path = Path(patch["file"])
        local_path.write_text(patch["updated_full_text"], encoding="utf-8")
        result = f"Applied {patch_id} to {patch['file']}."

        if deploy_to_lab:
            from tools.sync_to_lab import push_single_file
            rel = local_path.relative_to(self.root)
            rel_posix = str(rel).replace("\\", "/")
            for dest_root in ("/home/ego/bot/Sabrina-windows-sync", "/home/ego/bot/sabrina_win_arena"):
                remote_path = f"{dest_root}/{rel_posix}"
                try:
                    push_single_file(local_path, remote_path)
                    result += f"\nDeployed to {remote_path}."
                except Exception as e:
                    result += f"\nFAILED deploying to {remote_path}: {e}"
        return result

    # ---- Main loop -----------------------------------------------------

    @staticmethod
    def _parse_tool_calls_from_content(content: str) -> list:
        """
        Ollama's native tool_calls field only populates when the model wraps its
        call in <tool_call>...</tool_call> exactly as its template instructs --
        live-confirmed tonight that qwen2.5-coder:32b sometimes emits the bare
        JSON object with no wrapper tags, and sometimes emits SEVERAL separate
        JSON objects on separate lines in one response (invalid as a single
        json.loads() call even though each line is individually valid JSON).
        Rather than trust the model's exact formatting, extract every
        standalone {...} object out of the raw text with a bracket-matching
        scan and keep the ones shaped like a tool call. Only the FIRST valid
        call is used per turn (one tool call per turn keeps this a real
        observe-then-decide loop instead of blind batching).
        """
        wrapped = re.findall(r"<tool_call>\s*(.*?)\s*</tool_call>", content, re.DOTALL)
        search_text = "\n".join(wrapped) if wrapped else content

        candidates = []
        depth = 0
        start = None
        for i, ch in enumerate(search_text):
            if ch == "{":
                if depth == 0:
                    start = i
                depth += 1
            elif ch == "}":
                if depth > 0:
                    depth -= 1
                    if depth == 0 and start is not None:
                        candidates.append(search_text[start:i + 1])
                        start = None

        calls = []
        for cand in candidates:
            try:
                obj = json.loads(cand)
            except Exception:
                continue
            if isinstance(obj, dict) and "name" in obj:
                calls.append({"function": {"name": obj["name"], "arguments": obj.get("arguments", {})}})
        return calls[:1]

    def run(self, objective: str) -> str:
        system = (
            "You are exploring a real codebase to answer a question or find/fix a bug. "
            "You have real tools: list_dir, read_file, grep, propose_patch. Use them to actually "
            "look at files instead of guessing. propose_patch stages a change for human approval; "
            "it does not modify anything on disk itself. When you have enough information, give a "
            "final concrete answer citing real file paths, line numbers, and function/class names "
            "you actually saw via your tools -- do not speculate beyond what you observed.\n\n"
            "IMPORTANT: call exactly ONE tool per turn, then wait for its real result before "
            "deciding your next step. Do not output several tool calls at once -- you have not "
            "seen the result of the first one yet, so later ones would be guesses, not decisions.\n\n"
            "When you already know the NAME of a function, class, or string you're looking for, "
            "use grep for it first instead of reading a large file sequentially from line 1 -- "
            "grep gives you the exact line number to jump to with read_file, which is much faster "
            "and uses far fewer turns than paging through hundreds of lines you don't need.\n\n"
            "To see what methods a class actually has, do NOT guess method names one at a time with "
            "grep -- that wastes turns on names that don't exist. Instead read_file a generous range "
            "starting at the class's line number (e.g. 80-120 lines) to see its real methods directly, "
            "or grep for the pattern 'def ' scoped to that file to list every method in one call."
        )
        messages = [{"role": "system", "content": system}, {"role": "user", "content": objective}]

        for turn in range(1, self.max_turns + 1):
            payload = {
                "model": MODEL, "messages": messages, "tools": self.TOOLS, "stream": False,
                "options": {"temperature": 0.1, "num_ctx": 16384},
            }
            # Live-observed tonight: the RunPod HTTPS proxy occasionally returns an empty
            # body (Cloudflare-fronted, prone to idle-timeout drops on long-running requests
            # under load), which raised an unhandled JSONDecodeError and killed a multi-turn
            # run outright -- losing several turns' worth of real progress. One retry on a
            # transient network/decode failure is cheap insurance against throwing away work.
            resp = None
            for attempt in range(2):
                try:
                    r = requests.post(OLLAMA_URL.replace("/api/generate", "/api/chat"), json=payload,
                                       timeout=420, headers={"User-Agent": "curl/8.4.0"})
                    resp = r.json()
                    break
                except (requests.exceptions.RequestException, ValueError) as e:
                    print(f"    [!] Request failed (attempt {attempt + 1}/2): {e}")
                    if attempt == 0:
                        continue
                    return f"(agent stopped: repeated request failures on turn {turn}: {e})"
            msg = resp.get("message", {})
            content = msg.get("content", "") or ""
            tool_calls = msg.get("tool_calls") or self._parse_tool_calls_from_content(content)

            print(f"\n[Turn {turn}] Model: {content[:300]}")

            if not tool_calls:
                return content or "(no final answer produced)"

            messages.append({"role": "assistant", "content": content})
            for tc in tool_calls:
                fn = tc.get("function", {})
                name = fn.get("name")
                args = fn.get("arguments", {})
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except Exception:
                        args = {}
                print(f"    -> tool: {name}({args})")
                result = self._dispatch(name, args)
                print(f"    <- result ({len(result)} chars): {result[:200]}")
                messages.append({"role": "tool", "content": result})

        return "(max turns reached without a final answer)"


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 4:
        raise SystemExit("usage: code_explorer_agent.py AUTHORIZATION.json ROOT OBJECTIVE")
    authorization = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    root, objective = sys.argv[2], sys.argv[3]
    agent = CodeExplorerAgent(root, authorization)
    answer = agent.run(objective)
    print("\n\n===== FINAL ANSWER =====")
    print(answer)
    if agent.pending_patches:
        print(f"\n{len(agent.pending_patches)} patch(es) staged (NOT applied): {list(agent.pending_patches.keys())}")
