"""Content-addressed document storage and PDF text extraction.

Uploaded PDFs are stored once, keyed by SHA-256. Workflow state holds only the
hash, never the bytes: checkpoints stay small, identical uploads de-duplicate,
and the hash doubles as an integrity check when a document is re-read.
"""

from __future__ import annotations

import hashlib
import io
import logging
import os
import re
import tempfile
from pathlib import Path

import pdfplumber

logger = logging.getLogger(__name__)

PDF_MAGIC = b"%PDF-"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class DocumentError(Exception):
    pass


class DocumentStore:
    def __init__(self, root: Path) -> None:
        self._root = root
        self._root.mkdir(parents=True, exist_ok=True)

    def _path(self, sha256: str) -> Path:
        if not _SHA256.fullmatch(sha256):  # never let a caller-controlled string become a path
            raise DocumentError("invalid document reference")
        return self._root / sha256[:2] / f"{sha256}.pdf"

    def put(self, data: bytes) -> str:
        if not data.startswith(PDF_MAGIC):
            raise DocumentError("not a PDF")
        digest = hashlib.sha256(data).hexdigest()
        path = self._path(digest)
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            # Atomic write: a crash mid-write never leaves a truncated document behind.
            fd, tmp = tempfile.mkstemp(dir=path.parent)
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
            Path(tmp).replace(path)
            logger.info("Stored document", extra={"sha256": digest, "bytes": len(data)})
        return digest

    def get(self, sha256: str) -> bytes:
        path = self._path(sha256)
        if not path.is_file():
            raise DocumentError("document not found")
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != sha256:
            raise DocumentError("document integrity check failed")
        return data


def pdf_text(data: bytes) -> str:
    """The PDF's text layer. Used to ground LLM extractions, not to parse."""
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        return "\n".join(page.extract_text() or "" for page in pdf.pages)
