"""Content-addressed raw artifact store.

Every response body -- HTTP or email -- is written here before parsing.

Sharding by sha256 prefix is deliberate: it caps directory width, deduplicates
identical re-fetches for free, and keeps paths well under Windows MAX_PATH
(a claims-agent docket URL slugged into a filename would blow past 260 chars).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import zstandard

_COMPRESS_OVER = 2048  # bytes; below this, compression is not worth it


@dataclass(slots=True)
class StoredArtifact:
    sha256: str
    path: Path
    byte_size: int
    compressed: bool
    ext: str


class ArtifactStore:
    def __init__(self, root: Path, manifest_root: Path) -> None:
        self.root = root
        self.manifest_root = manifest_root
        self.root.mkdir(parents=True, exist_ok=True)
        self.manifest_root.mkdir(parents=True, exist_ok=True)
        self._cctx = zstandard.ZstdCompressor(level=10)
        self._dctx = zstandard.ZstdDecompressor()

    def _shard(self, sha: str, ext: str, compressed: bool) -> Path:
        d = self.root / sha[0:2] / sha[2:4]
        d.mkdir(parents=True, exist_ok=True)
        suffix = f".{ext}.zst" if compressed else f".{ext}"
        return d / f"{sha}{suffix}"

    def put(
        self,
        *,
        sha256: str,
        body: bytes,
        source_id: str,
        url: str,
        content_type: str | None = None,
    ) -> StoredArtifact:
        ext = _ext_for(content_type, url)
        compressed = len(body) >= _COMPRESS_OVER
        path = self._shard(sha256, ext, compressed)

        if not path.exists():
            payload = self._cctx.compress(body) if compressed else body
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_bytes(payload)
            tmp.replace(path)

        self._append_manifest(
            source_id,
            {
                "sha256": sha256,
                "url": url,
                "content_type": content_type,
                "byte_size": len(body),
                "path": str(path),
                "compressed": compressed,
            },
        )
        return StoredArtifact(sha256, path, len(body), compressed, ext)

    def get(self, sha256: str) -> bytes | None:
        d = self.root / sha256[0:2] / sha256[2:4]
        if not d.is_dir():
            return None
        for p in d.glob(f"{sha256}.*"):
            raw = p.read_bytes()
            if p.suffix == ".zst":
                return self._dctx.decompress(raw)
            return raw
        return None

    def _append_manifest(self, source_id: str, record: dict) -> None:
        """Human-greppable JSONL, and a rebuild path if the DB is ever lost."""
        d = self.manifest_root / source_id
        d.mkdir(parents=True, exist_ok=True)
        f = d / f"{date.today().isoformat()}.jsonl"
        with f.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, sort_keys=True) + "\n")


def _ext_for(content_type: str | None, url: str) -> str:
    ct = (content_type or "").split(";")[0].strip().lower()
    mapping = {
        "application/rss+xml": "xml",
        "application/atom+xml": "xml",
        "application/xml": "xml",
        "text/xml": "xml",
        "application/json": "json",
        "text/html": "html",
        "text/plain": "txt",
        "application/pdf": "pdf",
        "message/rfc822": "eml",
    }
    if ct in mapping:
        return mapping[ct]
    lowered = url.lower()
    for ext in ("pdf", "json", "xml", "html", "txt"):
        if lowered.endswith("." + ext):
            return ext
    return "bin"
