"""Safe, non-executing ingestion for customer SOW and rules-of-engagement files."""
from __future__ import annotations

import hashlib
import io
import json
import re
import sqlite3
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from defusedxml import ElementTree
from pypdf import PdfReader

from tools.messaging_store import ROOT


ALLOWED_EXTENSIONS = {".pdf", ".docx", ".txt", ".md"}
MAX_DOCUMENT_BYTES = 10 * 1024 * 1024
MAX_EXTRACTED_CHARACTERS = 200_000
MAX_PDF_PAGES = 200
MAX_DOCX_FILES = 2_000
MAX_DOCX_UNCOMPRESSED_BYTES = 50 * 1024 * 1024
DEFAULT_OUTPUT_ROOT = ROOT / "artifacts/customer_engagement/inbound"
DEFAULT_INDEX_PATH = ROOT / "data/customer_documents.sqlite3"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def safe_filename(value: str) -> str:
    name = Path(str(value or "document").replace("\\", "/")).name
    name = re.sub(r"[^A-Za-z0-9._ -]+", "_", name).strip(" .")
    return (name or "document")[:180]


def _clean_text(value: str) -> str:
    value = value.replace("\x00", " ").replace("\r\n", "\n").replace("\r", "\n")
    value = re.sub(r"[ \t]+", " ", value)
    value = re.sub(r"\n{4,}", "\n\n\n", value)
    return value.strip()[:MAX_EXTRACTED_CHARACTERS]


def _extract_pdf(data: bytes) -> tuple[str, dict[str, Any]]:
    if not data.startswith(b"%PDF-"):
        raise ValueError("PDF signature does not match the filename")
    reader = PdfReader(io.BytesIO(data), strict=True)
    if reader.is_encrypted:
        raise ValueError("Encrypted PDFs require operator review")
    if len(reader.pages) > MAX_PDF_PAGES:
        raise ValueError(f"PDF exceeds the {MAX_PDF_PAGES}-page limit")
    pages = []
    for page in reader.pages:
        pages.append(page.extract_text() or "")
    text = _clean_text("\n\n".join(pages))
    if not text:
        raise ValueError("PDF contains no extractable text")
    return text, {"format": "pdf", "page_count": len(reader.pages)}


def _extract_docx(data: bytes) -> tuple[str, dict[str, Any]]:
    if not data.startswith(b"PK"):
        raise ValueError("DOCX signature does not match the filename")
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        members = archive.infolist()
        if len(members) > MAX_DOCX_FILES:
            raise ValueError("DOCX contains too many archive members")
        if sum(member.file_size for member in members) > MAX_DOCX_UNCOMPRESSED_BYTES:
            raise ValueError("DOCX expands beyond the safe uncompressed limit")
        names = {member.filename for member in members}
        if "[Content_Types].xml" not in names or "word/document.xml" not in names:
            raise ValueError("Archive is not a valid DOCX document")
        if any(name.lower().endswith("vbaproject.bin") for name in names):
            raise ValueError("Macro-enabled Office content requires operator review")
        xml_names = [
            name for name in names
            if name == "word/document.xml"
            or re.fullmatch(r"word/(?:header|footer)\d+\.xml", name)
            or name in {"word/footnotes.xml", "word/endnotes.xml"}
        ]
        text_parts = []
        for name in sorted(xml_names):
            xml = archive.read(name)
            root = ElementTree.fromstring(xml)
            for node in root.iter():
                if node.tag.endswith("}t") and node.text:
                    text_parts.append(node.text)
                elif node.tag.endswith("}p"):
                    text_parts.append("\n")
    text = _clean_text(" ".join(text_parts))
    if not text:
        raise ValueError("DOCX contains no extractable text")
    return text, {"format": "docx", "xml_parts_read": len(xml_names)}


def _extract_plain_text(data: bytes, extension: str) -> tuple[str, dict[str, Any]]:
    if b"\x00" in data[:4096]:
        raise ValueError("Text document contains binary data")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise ValueError("Text document must be UTF-8") from error
    text = _clean_text(text)
    if not text:
        raise ValueError("Text document is empty")
    return text, {"format": extension.lstrip(".")}


def extract_customer_document_bytes(data: bytes, filename: str) -> tuple[str, dict[str, Any]]:
    name = safe_filename(filename)
    extension = Path(name).suffix.lower()
    if extension not in ALLOWED_EXTENSIONS:
        raise ValueError("Only PDF, DOCX, TXT, and Markdown customer documents are accepted")
    if not data:
        raise ValueError("Customer document is empty")
    if len(data) > MAX_DOCUMENT_BYTES:
        raise ValueError(f"Customer document exceeds {MAX_DOCUMENT_BYTES // (1024 * 1024)} MiB")
    if extension == ".pdf":
        return _extract_pdf(data)
    if extension == ".docx":
        return _extract_docx(data)
    return _extract_plain_text(data, extension)


def ingest_customer_document(
    source: str | Path,
    filename: str,
    *,
    output_root: str | Path | None = None,
) -> dict[str, Any]:
    source = Path(source)
    data = source.read_bytes()
    name = safe_filename(filename)
    text, metadata = extract_customer_document_bytes(data, name)
    sha256 = hashlib.sha256(data).hexdigest()
    destination = Path(output_root or DEFAULT_OUTPUT_ROOT) / sha256
    destination.mkdir(parents=True, exist_ok=True)
    original = destination / name
    if not original.exists():
        temporary = destination / (name + ".tmp")
        temporary.write_bytes(data)
        temporary.replace(original)
    extracted = destination / "extracted.txt"
    temporary_text = destination / "extracted.txt.tmp"
    temporary_text.write_text(text, encoding="utf-8")
    temporary_text.replace(extracted)
    manifest = {
        "version": 1,
        "status": "ingested",
        "filename": name,
        "extension": Path(name).suffix.lower(),
        "sha256": sha256,
        "size_bytes": len(data),
        "stored_path": str(original.resolve()),
        "extracted_path": str(extracted.resolve()),
        "extracted_characters": len(text),
        "extracted_text": text,
        "metadata": metadata,
        "ingested_at": utc_now(),
        "execution_permitted": False,
    }
    manifest_path = destination / "manifest.json"
    temporary_manifest = destination / "manifest.json.tmp"
    temporary_manifest.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary_manifest.replace(manifest_path)
    return manifest


class CustomerDocumentIndex:
    """Caches Gmail attachment receipts so unchanged threads are not redownloaded."""

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path or DEFAULT_INDEX_PATH)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS documents(
              message_id TEXT NOT NULL,
              attachment_index INTEGER NOT NULL,
              filename TEXT NOT NULL,
              status TEXT NOT NULL,
              manifest TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              PRIMARY KEY(message_id,attachment_index,filename)
            )"""
        )
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    def get(self, message_id: str, attachment_index: int, filename: str) -> dict[str, Any] | None:
        row = self.db.execute(
            "SELECT manifest FROM documents WHERE message_id=? AND attachment_index=? AND filename=?",
            (message_id, int(attachment_index), safe_filename(filename)),
        ).fetchone()
        if row is None:
            return None
        value = json.loads(row["manifest"])
        if value.get("status") == "ingested" and not Path(value.get("extracted_path", "")).is_file():
            return None
        return value

    def save(self, message_id: str, attachment_index: int, filename: str, manifest: dict[str, Any]) -> None:
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO documents VALUES(?,?,?,?,?,?)",
                (
                    message_id,
                    int(attachment_index),
                    safe_filename(filename),
                    manifest.get("status", "rejected"),
                    json.dumps(manifest, ensure_ascii=False),
                    utc_now(),
                ),
            )


def rejected_document(filename: str, error: Exception | str) -> dict[str, Any]:
    return {
        "version": 1,
        "status": "rejected",
        "filename": safe_filename(filename),
        "reason": str(error),
        "ingested_at": utc_now(),
        "execution_permitted": False,
    }
