"""Capture version-matched agent-desktop guides and MCP schemas for Sabrina.

The installed binary is the authority for its own command semantics.  This script
does not scrape arbitrary web pages and never invokes a desktop action.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DESTINATION = ROOT / "docs" / "vendor" / "agent-desktop"
TOOL_ALLOWLIST = {
    "desktop_status",
    "desktop_list_apps",
    "desktop_list_windows",
    "desktop_snapshot",
    "desktop_find",
    "desktop_get",
    "desktop_click",
    "desktop_set_value",
    "desktop_wait",
}
GUIDES = {
    "desktop.md": ("desktop", None),
    "windows.md": ("platform", None),
    "commands-observation.md": ("desktop", "commands-observation"),
    "commands-interaction.md": ("desktop", "commands-interaction"),
    "commands-system.md": ("desktop", "commands-system"),
    "workflows.md": ("desktop", "workflows"),
}


def agent_desktop_binary() -> str:
    override = os.environ.get("AGENT_DESKTOP_BIN")
    if override:
        path = Path(override)
        if path.is_file():
            return str(path)
        raise FileNotFoundError(f"AGENT_DESKTOP_BIN does not exist: {path}")
    if os.name == "nt":
        launcher = shutil.which("agent-desktop")
        if launcher:
            arch = "arm64" if platform.machine().lower() in {"arm64", "aarch64"} else "x64"
            native = Path(launcher).parent / "node_modules" / "agent-desktop" / "bin" / f"agent-desktop-win32-{arch}.exe"
            if native.is_file():
                return str(native)
    binary = shutil.which("agent-desktop")
    if binary and Path(binary).suffix.lower() not in {".cmd", ".ps1"}:
        return binary
    raise FileNotFoundError("agent-desktop native executable was not found")


def _json_command(*args: str) -> dict:
    completed = subprocess.run(
        [agent_desktop_binary(), *args],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return json.loads(completed.stdout)


def _skill(name: str, reference: str | None) -> str:
    args = ["skills", "get", name]
    if reference:
        args.append(reference)
    envelope = _json_command(*args)
    if not envelope.get("ok"):
        raise RuntimeError(envelope)
    return envelope["data"]["content"].replace("\r\n", "\n")


async def _tool_schemas() -> list[dict]:
    server = StdioServerParameters(command=agent_desktop_binary(), args=["mcp"])
    async with stdio_client(server) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            await session.initialize()
            response = await session.list_tools()
            tools = [
                tool.model_dump(mode="json")
                for tool in response.tools
            ]
    names = {tool["name"] for tool in tools}
    missing = TOOL_ALLOWLIST - names
    if missing:
        raise RuntimeError(f"agent-desktop did not expose required MCP tools: {sorted(missing)}")
    return sorted(tools, key=lambda tool: tool["name"])


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def sync(destination: Path) -> dict:
    destination.mkdir(parents=True, exist_ok=True)
    version = _json_command("version")
    status = _json_command("status")
    written: list[Path] = []
    for filename, (skill, reference) in GUIDES.items():
        path = destination / filename
        path.write_text(_skill(skill, reference), encoding="utf-8", newline="\n")
        written.append(path)
    schemas = destination / "mcp-tools.json"
    schemas.write_text(
        json.dumps(asyncio.run(_tool_schemas()), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    written.append(schemas)
    source = destination / "SOURCE.md"
    source.write_text(
        "# Source\n\n"
        "Captured from the installed `agent-desktop` binary using its `skills get` "
        "and MCP `tools/list` interfaces. Upstream: "
        "https://github.com/vercel-labs/agent-desktop (Apache-2.0).\n",
        encoding="utf-8",
        newline="\n",
    )
    written.append(source)
    manifest = {
        "source": "https://github.com/vercel-labs/agent-desktop",
        "binary_sha256": _sha256(Path(agent_desktop_binary())),
        "version": version.get("data", {}).get("version"),
        "protocol_version": version.get("version"),
        "platform": status.get("data", {}).get("platform"),
        "supported_surfaces": status.get("data", {}).get("supported_surfaces", []),
        "files": {path.name: _sha256(path) for path in written},
    }
    manifest_path = destination / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", type=Path, default=DEFAULT_DESTINATION)
    args = parser.parse_args()
    print(json.dumps(sync(args.destination), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
