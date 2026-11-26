"""
tools/code_indexer.py
======================
Walks real source trees (intended use: the lab host's drives, e.g. /home/ego)
and pushes chunked file content into the local SQLite+FTS5 knowledge store
(tools/rag_knowledge.py), tagged source_type="lab_code_index".

Why this exists: prior work (e.g. /home/ego/swarm_react_loop.py) was only
found this session via manual SSH + grep archaeology. Once indexed, both
Sabrina (via search_memory/oracle RAG lookups) and future sessions can find
prior scripts, configs, and logs by keyword instead of re-discovering them
by hand every time.

Chunking is by file, split further only if the file exceeds CHUNK_CHARS --
FTS5 handles large documents fine, but very large files (multi-MB logs) are
split so a single match doesn't return an unusably large blob.
"""

import os
import sqlite3
from pathlib import Path

from tools.rag_knowledge import add_knowledge, clear_by_source_type, DEFAULT_DB_PATH, _ensure_schema

SOURCE_TYPE = "lab_code_index"

DEFAULT_EXTENSIONS = {
    ".py", ".js", ".ts", ".jsx", ".tsx", ".sh", ".yaml", ".yml",
    ".json", ".md", ".txt", ".cfg", ".ini", ".toml", ".sql",
}

DEFAULT_EXCLUDE_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", "env",
    "dist", "build", ".mypy_cache", ".pytest_cache", "site-packages",
}

MAX_FILE_BYTES = 500_000   # skip anything bigger (binaries, huge logs/dumps)
CHUNK_CHARS = 8_000        # split large text files into chunks this size
CHUNK_OVERLAP = 400


def _iter_files(root: Path, extensions, exclude_dirs):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in exclude_dirs and not d.startswith(".")]
        for fname in filenames:
            if Path(fname).suffix.lower() in extensions:
                yield Path(dirpath) / fname


def _chunk_text(text: str, chunk_chars: int = CHUNK_CHARS, overlap: int = CHUNK_OVERLAP):
    if len(text) <= chunk_chars:
        yield text
        return
    start = 0
    while start < len(text):
        end = start + chunk_chars
        yield text[start:end]
        start = end - overlap


def index_paths(paths, extensions=None, exclude_dirs=None, db_path=None,
                 clear_existing=True, max_file_bytes=MAX_FILE_BYTES):
    """
    Index every matching file under each path in `paths` into the local
    RAG store. Returns a summary dict: {"files_indexed", "chunks_written",
    "files_skipped", "errors": [...]}.

    clear_existing=True (default) wipes prior lab_code_index rows first, so
    re-running this doesn't pile up stale duplicates of moved/deleted files.
    """
    extensions = {e.lower() for e in (extensions or DEFAULT_EXTENSIONS)}
    exclude_dirs = set(exclude_dirs or DEFAULT_EXCLUDE_DIRS)

    if clear_existing:
        try:
            clear_by_source_type(SOURCE_TYPE, db_path=db_path)
        except Exception:
            pass

    files_indexed = 0
    chunks_written = 0
    files_skipped = 0
    errors = []

    conn = sqlite3.connect(str(db_path or DEFAULT_DB_PATH))
    _ensure_schema(conn)
    try:
        for raw_path in paths:
            root = Path(raw_path)
            if not root.exists():
                errors.append(f"path does not exist: {raw_path}")
                continue

            targets = [root] if root.is_file() else list(_iter_files(root, extensions, exclude_dirs))

            for fpath in targets:
                try:
                    if fpath.stat().st_size > max_file_bytes:
                        files_skipped += 1
                        continue
                    content = fpath.read_text(encoding="utf-8", errors="ignore")
                    if not content.strip():
                        files_skipped += 1
                        continue
                except Exception as e:
                    errors.append(f"read failed for {fpath}: {e}")
                    continue

                try:
                    chunks = list(_chunk_text(content))
                    for i, chunk in enumerate(chunks):
                        # Each chunk needs a distinct topic -- documents.url is
                        # UNIQUE and is derived from topic, so a multi-chunk file
                        # reusing the same topic for every chunk would collide on
                        # the second insert and silently drop the rest of the file.
                        topic = str(fpath) if len(chunks) == 1 else f"{fpath}#chunk{i}"
                        add_knowledge(topic, chunk, source_type=SOURCE_TYPE, conn=conn)
                        chunks_written += 1
                    files_indexed += 1
                except Exception as e:
                    errors.append(f"index failed for {fpath}: {e}")

        conn.commit()
    finally:
        conn.close()

    return {
        "files_indexed": files_indexed,
        "chunks_written": chunks_written,
        "files_skipped": files_skipped,
        "errors": errors[:20],
    }


if __name__ == "__main__":
    import sys
    import json

    target_paths = sys.argv[1:] or [os.path.expanduser("~")]
    result = index_paths(target_paths)
    print(json.dumps(result, indent=2))
