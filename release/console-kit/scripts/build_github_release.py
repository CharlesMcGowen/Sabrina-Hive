"""Build the local Windows beta ZIP from an explicitly selected frozen runtime."""

import argparse
from pathlib import Path
from tempfile import TemporaryDirectory
from zipfile import ZIP_DEFLATED, ZipFile

KIT_ROOT = Path(__file__).resolve().parents[1]
KIT_FILES = ("quickstart.md", "release_report.md", ".env.example", "run_console.ps1", "install_beta.ps1")
BLOCKED_PARTS = {".git", ".venv", "__pycache__", "data"}


def build_release(runtime_dir: Path, destination: Path) -> Path:
    runtime_dir = runtime_dir.resolve(strict=True)
    executable = runtime_dir / "sabrina-console.exe"
    internal = runtime_dir / "_internal"
    if not executable.is_file() or not internal.is_dir():
        raise ValueError("Pass the PyInstaller onedir output with sabrina-console.exe and _internal.")
    if executable.read_bytes()[:2] != b"MZ":
        raise ValueError("sabrina-console.exe is missing the Windows executable header.")

    sources = {name: KIT_ROOT / name for name in KIT_FILES}
    sources[executable.name] = executable
    for path in sorted(internal.rglob("*")):
        relative = path.relative_to(runtime_dir)
        lowered = relative.as_posix().lower()
        if path.is_symlink() or any(part.lower() in BLOCKED_PARTS for part in relative.parts):
            raise ValueError(f"Unexpected state or link in the frozen runtime: {relative}")
        if path.is_file():
            suffix = path.suffix.lower()
            if ("kraken" in lowered or suffix in {".log", ".sqlite3"}
                or (suffix == ".zip" and lowered != "_internal/base_library.zip")
                or path.name.lower().startswith(".env")):
                raise ValueError(f"Unexpected runtime file: {relative}")
            sources[relative.as_posix()] = path

    for name, source in sources.items():
        root = runtime_dir if name == executable.name or name.startswith("_internal/") else KIT_ROOT
        if source.is_symlink() or not source.resolve().is_relative_to(root) or not source.is_file():
            raise ValueError(f"Invalid beta input: {name}")
    for line in sources[".env.example"].read_text(encoding="utf-8-sig").splitlines():
        key, delimiter, value = line.partition("=")
        if delimiter and not key.lstrip().startswith("#") and any(
            indicator in key.upper() for indicator in ("KEY", "TOKEN", "SECRET", "PASSWORD")
        ) and value.strip().strip("\"'"):
            raise ValueError("The sample environment contains a populated credential field.")

    destination.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=".sabrina-beta-", dir=destination.parent) as scratch:
        temporary = Path(scratch) / destination.name
        with ZipFile(temporary, "w", ZIP_DEFLATED) as archive:
            for name, source in sources.items():
                archive.write(source, name)
        with ZipFile(temporary) as archive:
            if archive.namelist() != list(sources) or archive.testzip() is not None:
                raise ValueError("Generated archive failed its member-list or integrity check.")
        temporary.replace(destination)
    return destination


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=KIT_ROOT / "windows_beta.zip")
    args = parser.parse_args()
    print(f"Created and verified {build_release(args.runtime_dir, args.output)}")
