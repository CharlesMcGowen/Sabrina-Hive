"""
Scoped Windows PowerShell 5.1 Tools for Sabrina
Implements three read-only tools: powershell_info, powershell_help, powershell_item.
Communicates with PowerShell via subprocess using -EncodedCommand and JSON stdin.
Strictly validates paths and arguments according to security requirements.
"""

import base64
import json
import os
from pathlib import Path
import subprocess

# Fixed immutable PowerShell script that handles JSON input from stdin,
# executes the requested read-only operation, and outputs JSON on stdout.
PS_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$env:PSModulePath = [IO.Path]::Combine($PSHOME, 'Modules')
[Console]::InputEncoding = [System.Text.Encoding]::UTF8
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$inputJson = [Console]::In.ReadToEnd()
if ([string]::IsNullOrWhiteSpace($inputJson)) {
    $request = @{}
} else {
    $request = $inputJson | ConvertFrom-Json
}

$action = $request.action

try {
    switch ($action) {
        'info' {
            $ver = $PSVersionTable.PSVersion.ToString()
            $edition = if ($PSVersionTable.PSEdition) { $PSVersionTable.PSEdition } else { 'Desktop' }
            
            $cmdlets = @()
            foreach ($c in @('Get-Command', 'Get-Help', 'Get-Item')) {
                $found = Get-Command -Name $c -CommandType Cmdlet -ErrorAction SilentlyContinue
                if ($found) {
                    $cmdlets += $c
                }
            }
            
            $output = @{
                version = $ver
                edition = $edition
                cmdlets = $cmdlets
            }
            $output | ConvertTo-Json -Compress
        }
        'help' {
            $cmdletName = $request.cmdlet
            if (-not $cmdletName -or @('Get-Help', 'Get-Command', 'Get-Item') -notcontains $cmdletName) {
                throw "Invalid or unauthorized cmdlet name requested: $cmdletName"
            }
            
            $h = Get-Help -Name $cmdletName -ErrorAction Stop
            $synopsisVal = ''
            if ($h.Synopsis) {
                $synopsisVal = [string]$h.Synopsis.Trim()
            } elseif ($h.Description) {
                $desc = @($h.Description)
                if ($desc.Count -gt 0 -and $desc[0].Text) {
                    $synopsisVal = [string]$desc[0].Text.Trim()
                }
            }
            
            $cmdInfo = Get-Command -Name $cmdletName -CommandType Cmdlet -ErrorAction Stop
            $hasLiteralPath = $false
            if ($cmdInfo.Parameters -and $cmdInfo.Parameters.ContainsKey('LiteralPath')) {
                $hasLiteralPath = $true
            }
            
            $output = @{
                name = [string]$h.Name
                synopsis = $synopsisVal
                literal_path = [bool]$hasLiteralPath
            }
            $output | ConvertTo-Json -Compress
        }
        'item' {
            $targetPath = $request.path
            if (-not $targetPath) {
                throw "Path is required for powershell_item"
            }
            
            $item = Get-Item -LiteralPath $targetPath -Force -ErrorAction Stop
            
            $isDir = $item.PSIsContainer
            $length = $null
            if (-not $isDir) {
                if ($item.Length -ne $null) {
                    $length = [int64]$item.Length
                }
            }
            
            $output = @{
                name = [string]$item.Name
                is_directory = [bool]$isDir
                length = $length
            }
            $output | ConvertTo-Json -Compress
        }
        default {
            throw "Unknown action: $action"
        }
    }
} catch {
    $errObj = @{
        error = $_.Exception.Message
    }
    $errObj | ConvertTo-Json -Compress
    exit 1
}
"""


def _run_powershell(workspace_root: str, powershell_exe: str, action: str, payload: dict) -> dict:
    request_data = {"action": action}
    request_data.update(payload)
    try:
        input_json = json.dumps(request_data)
    except Exception:
        raise RuntimeError("Failed to serialize request payload")

    encoded_bytes = PS_SCRIPT.encode("utf-16le")
    encoded_command = base64.b64encode(encoded_bytes).decode("ascii")

    cmd = [
        powershell_exe,
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-EncodedCommand",
        encoded_command,
    ]

    try:
        res = subprocess.run(
            cmd,
            input=input_json,
            text=True,
            encoding="utf-8",
            capture_output=True,
            cwd=workspace_root,
            timeout=10,
            shell=False,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError("Operation timed out")
    except Exception:
        raise RuntimeError("Failed to execute process")

    if res.returncode != 0:
        raise RuntimeError("Operation failed")

    stdout = res.stdout.strip()
    if not stdout:
        return {}

    try:
        lines = [line.strip() for line in stdout.splitlines() if line.strip()]
        for line in reversed(lines):
            if line.startswith("{") and line.endswith("}"):
                return json.loads(line)
        return json.loads(stdout)
    except Exception:
        raise RuntimeError("Invalid response")


def _validate_and_resolve_path(workspace_root: str, path_arg: str) -> str:
    if not isinstance(path_arg, str) or not path_arg.strip():
        raise ValueError("Path must be a non-empty string.")

    if (
        os.path.isabs(path_arg)
        or ":" in path_arg
        or path_arg.startswith("\\\\")
        or path_arg.startswith("/")
        or "*" in path_arg
        or "?" in path_arg
        or path_arg.endswith(".")
        or path_arg.endswith(" ")
    ):
        raise ValueError("Invalid path format")

    normalized = path_arg.replace("\\", "/")
    parts = normalized.split("/")

    for part in parts:
        if not part:
            raise ValueError("Empty path parts are forbidden")
        if any(ord(c) < 32 or ord(c) == 127 for c in part):
            raise ValueError("Control characters are forbidden")
        if part.endswith(".") or part.endswith(" "):
            raise ValueError("Parts ending in space or dot are forbidden")
        if part == ".":
            raise ValueError("Dot components are forbidden")
        if part == "..":
            raise ValueError("Parent directory traversal is forbidden")
        if part.startswith("."):
            raise ValueError("Dot-prefixed paths are forbidden")

        lower_part = part.lower()
        if lower_part in ("provider_vault.json", "connectors.dpapi.json"):
            raise ValueError("Access to sensitive files is forbidden")
        if lower_part.endswith((".db", ".sqlite", ".sqlite3", ".dpapi.json", ".env")):
            raise ValueError("Access to sensitive suffixes is forbidden")

    root_path = Path(workspace_root).resolve()
    raw_current = root_path
    for part in parts:
        raw_current = raw_current / part
        try:
            st = raw_current.lstat()
            st_attr = getattr(st, "st_file_attributes", 0)
            if st_attr & 0x400:
                raise ValueError("Reparse points are forbidden")
        except OSError:
            raise ValueError("Path does not exist")
        if raw_current.is_symlink():
            raise ValueError("Symlinks are forbidden")

    resolved_path = (root_path / path_arg).resolve()

    try:
        resolved_path.relative_to(root_path)
    except ValueError:
        raise ValueError("Path escapes workspace root")

    if resolved_path.is_symlink():
        raise ValueError("Symlinks are forbidden")

    try:
        st = resolved_path.lstat()
        st_attr = getattr(st, "st_file_attributes", 0)
        if st_attr & 0x400:
            raise ValueError("Reparse points are forbidden")
    except OSError:
        pass

    return str(resolved_path)


def register_windows_tools(registry, workspace_root: str, powershell_exe: str):
    def handle_powershell_info(args: dict) -> dict:
        if not isinstance(args, dict) or len(args) > 0:
            raise ValueError("powershell_info takes no arguments.")
        return _run_powershell(workspace_root, powershell_exe, "info", {})

    def handle_powershell_help(args: dict) -> dict:
        if not isinstance(args, dict) or len(args) != 1:
            raise ValueError("powershell_help requires exactly one key 'cmdlet'.")
        cmdlet = args.get("cmdlet")
        if not isinstance(cmdlet, str):
            raise ValueError("Argument 'cmdlet' must be a string.")
        if cmdlet not in ("Get-Help", "Get-Command", "Get-Item"):
            raise ValueError(
                f"Unauthorized cmdlet for help: {cmdlet}. Allowed: Get-Help, Get-Command, Get-Item."
            )
        return _run_powershell(workspace_root, powershell_exe, "help", {"cmdlet": cmdlet})

    def handle_powershell_item(args: dict) -> dict:
        if not isinstance(args, dict) or len(args) != 1:
            raise ValueError("powershell_item requires exactly one key 'path'.")
        path_arg = args.get("path")
        if not isinstance(path_arg, str):
            raise ValueError("Argument 'path' must be a string.")

        validated_path = _validate_and_resolve_path(workspace_root, path_arg)
        return _run_powershell(workspace_root, powershell_exe, "item", {"path": validated_path})

    registry.register(
        "powershell_info",
        handle_powershell_info,
        description=(
            "Report Windows PowerShell version, edition, and check built-in cmdlets "
            "(Get-Command, Get-Help, Get-Item)."
        ),
    )

    registry.register(
        "powershell_help",
        handle_powershell_help,
        description=(
            "Get help metadata and synopsis for authorized cmdlets (Get-Help, Get-Command, Get-Item)."
        ),
    )

    registry.register(
        "powershell_item",
        handle_powershell_item,
        description=(
            "Get file or directory metadata within the authorized workspace using Get-Item -LiteralPath."
        ),
    )