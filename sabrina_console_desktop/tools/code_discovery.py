"""Code discovery -- "what do I already have for X?"

Sabrina's #1 failure mode (RSI_PROCESS.md) is rebuilding broken stubs instead of reusing code she
already has. Her existing search tools are only wired in improvement-mode, and list_available_engines()
only surfaces tools/engines/*_engine.py -- so in a normal engagement she is blind to non-engine code
like smuggle_sweep.py / recon_ant.py / discover_bugcrowd_direct.py. This module lets her ask "http
smuggling" and get back the real files that already implement it, with their entry points, so she reuses
instead of rebuilding.

Pure/AST-only (no import, no exec) and read-only. discover(query) greps the source tree, ranks files by
relevance, and digests the top hits (docstring + top-level defs/classes + CLI/entry note)."""

import ast
import os
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Real source to search; skip the 6.7 GB of vendored/generated/duplicate trees.
SEARCH_ROOTS = ("tools", "ai_systems", "mcp_server", "api", ".")
_DENY_SEGMENTS = {".sandbox_clone_", "security_items", "models", "data", "logs", "node_modules",
                  "__pycache__", ".git", "archives", "scratch", "dev", "Sabrina-dev", "github_variant",
                  ".venv", "datastore", "deploy"}


def _skip(rel: str) -> bool:
    parts = rel.replace(os.sep, "/").split("/")
    return any(p in _DENY_SEGMENTS or p.startswith(".sandbox_clone_") for p in parts)


def _iter_py():
    seen = set()
    for root in SEARCH_ROOTS:
        base = REPO_ROOT / root
        if not base.exists():
            continue
        if root == ".":
            # only root-level .py files (don't re-walk subdirs already covered)
            for f in base.glob("*.py"):
                yield f
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames
                           if d not in _DENY_SEGMENTS and not d.startswith(".sandbox_clone_")]
            for fn in filenames:
                if fn.endswith(".py"):
                    p = Path(dirpath) / fn
                    rp = str(p.resolve())
                    if rp not in seen and not _skip(str(p)):
                        seen.add(rp)
                        yield p


def _digest(path: Path, max_defs: int = 12) -> dict:
    """Docstring first line + top-level classes/functions + a CLI/entry note. AST only."""
    try:
        src = path.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(src)
    except Exception as e:
        return {"doc": f"(unparseable: {e})", "defs": [], "cli": False}
    doc = (ast.get_docstring(tree) or "").strip().splitlines()
    doc = doc[0] if doc else ""
    defs = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef):
            defs.append(f"def {node.name}()")
        elif isinstance(node, ast.ClassDef):
            methods = [n.name for n in node.body if isinstance(n, ast.FunctionDef) and not n.name.startswith("_")][:6]
            defs.append(f"class {node.name}" + (f" [{', '.join(methods)}]" if methods else ""))
    cli = 'if __name__ == "__main__"' in src or "argparse" in src
    return {"doc": doc, "defs": defs[:max_defs], "cli": cli}


def discover(query: str, top_k: int = 6):
    """Rank source files by relevance to `query` and digest the top hits. Returns a list of
    {file, score, hits, digest}. Scoring: filename match is weighted heavily (a file NAMED for the
    thing is usually the thing), plus per-term content-hit counts."""
    terms = [t for t in re.split(r"[^a-zA-Z0-9_]+", query.lower()) if len(t) > 1]
    if not terms:
        return []
    results = []
    for path in _iter_py():
        try:
            text = path.read_text(encoding="utf-8", errors="replace").lower()
        except Exception:
            continue
        name = path.name.lower()
        hits = {t: text.count(t) for t in terms}
        content_score = sum(hits.values())
        if content_score == 0:
            continue
        # A file NAMED for the query is almost always the thing -- weight the filename heavily so it
        # beats content-heavy files that merely mention the terms a lot.
        name_score = sum(60 for t in terms if t in name)
        if all(t in name for t in terms):
            name_score += 120
        results.append({
            "file": str(path.relative_to(REPO_ROOT)).replace(os.sep, "/"),
            "score": name_score + content_score,
            "hits": {t: c for t, c in hits.items() if c},
        })
    results.sort(key=lambda r: r["score"], reverse=True)
    top = results[:top_k]
    for r in top:
        r["digest"] = _digest(REPO_ROOT / r["file"])
    return top


def report(query: str, top_k: int = 6) -> str:
    """Human/agent-readable 'what you already have for <query>' summary."""
    hits = discover(query, top_k)
    if not hits:
        return f"No existing code found for '{query}'. (Nothing to reuse -- safe to build it.)"
    lines = [f"Existing code for '{query}' (reuse these before building):"]
    for r in hits:
        d = r["digest"]
        cli = " [CLI]" if d["cli"] else ""
        lines.append(f"\n  {r['file']}{cli}  (score {r['score']})")
        if d["doc"]:
            lines.append(f"    - {d['doc']}")
        if d["defs"]:
            lines.append(f"    - API: {'; '.join(d['defs'])}")
    return "\n".join(lines)


if __name__ == "__main__":
    import sys
    q = " ".join(sys.argv[1:]) or "http smuggling"
    print(report(q))
