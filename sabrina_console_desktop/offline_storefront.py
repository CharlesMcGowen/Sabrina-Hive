"""Offline checkout policy shared by workspace writes and mission verification."""

import re
from pathlib import Path

INSTRUCTION = (
    "Offline checkout is mandatory. Research hosted services for a comparison document only. "
    "The storefront must use local HTML, CSS and JavaScript with an in-memory checkout simulation. "
    "Use no remote assets, hosted checkout SDKs, network calls, live orders, or credentials. "
    "Keep product unit prices in integer cents, group cart quantities, support plus/minus/remove, "
    "and display a local confirmation with the correct subtotal."
)
ASSET_EXTENSIONS = {".htm", ".html", ".css", ".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".svg", ".json", ".webmanifest"}
_REMOTE = re.compile(
    r"(?:https?|wss?):\s*//|['\"(=]\s*//[\w-]+\.[\w.-]+"
    r"|\b(?:fetch|WebSocket|EventSource|XMLHttpRequest|sendBeacon|importScripts)\s*\("
    r"|<\s*(?:iframe|object|embed)\b",
    re.I,
)


def required(goal: str) -> bool:
    return bool(
        re.search(r"\bcheckout\b", goal, re.I)
        and re.search(r"\boffline\b|\bsimulated checkout\b|\bsimulate checkout\b", goal, re.I)
    )


def asset_issue(path: str, content: str) -> str:
    if Path(path).suffix.lower() not in ASSET_EXTENSIONS:
        return ""
    content = re.sub(r'xmlns(?::\w+)?\s*=\s*["\']http://www\.w3\.org/[^"\']+["\']', "", content, flags=re.I)
    match = _REMOTE.search(content)
    return f"{path}: offline checkout disallows external resources or network APIs ({match.group()})." if match else ""


def audit(root: Path, written_files: list[str]) -> list[str]:
    issues = []
    if not any(Path(path).suffix.lower() in {".html", ".htm"} and (root / path).is_file() for path in written_files):
        issues.append("Offline checkout needs a local HTML deliverable written by this mission.")
    for path in sorted(set(written_files)):
        if Path(path).suffix.lower() not in ASSET_EXTENSIONS:
            continue
        candidate = (root / path).resolve()
        if not candidate.is_relative_to(root.resolve()):
            issues.append(f"{path}: outside the offline workspace.")
            continue
        try:
            issue = asset_issue(path, candidate.read_text(encoding="utf-8"))
        except (OSError, UnicodeError) as exc:
            issue = f"{path}: cannot check offline policy ({exc})."
        if issue:
            issues.append(issue)
    return issues
