"""On-disk HTTP cache supporting conditional requests.

A 304 response is not a failure and is not an empty page -- it is positive
evidence that the resource is byte-identical to what we already have. The
canary layer depends on being able to tell those three apart, so the cache
records enough to say so.
"""

from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(slots=True)
class CacheEntry:
    url: str
    status: int
    etag: str | None
    last_modified: str | None
    content_type: str | None
    sha256: str
    byte_size: int
    fetched_at: str
    body_path: Path

    def read_body(self) -> bytes:
        return self.body_path.read_bytes()


class HttpCache:
    """Keyed on sha1(url), sharded by host to keep directories narrow."""

    def __init__(self, root: Path) -> None:
        self._root = root
        self._lock = threading.Lock()
        self._root.mkdir(parents=True, exist_ok=True)

    def _paths(self, url: str) -> tuple[Path, Path]:
        host = (urlsplit(url).hostname or "unknown").lower()
        digest = hashlib.sha1(url.encode("utf-8")).hexdigest()
        d = self._root / host
        d.mkdir(parents=True, exist_ok=True)
        return d / f"{digest}.meta.json", d / f"{digest}.body"

    def get(self, url: str) -> CacheEntry | None:
        meta_path, body_path = self._paths(url)
        if not meta_path.is_file() or not body_path.is_file():
            return None
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return CacheEntry(
            url=meta.get("url", url),
            status=int(meta.get("status", 200)),
            etag=meta.get("etag"),
            last_modified=meta.get("last_modified"),
            content_type=meta.get("content_type"),
            sha256=meta.get("sha256", ""),
            byte_size=int(meta.get("byte_size", 0)),
            fetched_at=meta.get("fetched_at", ""),
            body_path=body_path,
        )

    def put(
        self,
        url: str,
        *,
        status: int,
        body: bytes,
        etag: str | None,
        last_modified: str | None,
        content_type: str | None,
    ) -> CacheEntry:
        meta_path, body_path = self._paths(url)
        sha = hashlib.sha256(body).hexdigest()
        entry = CacheEntry(
            url=url,
            status=status,
            etag=etag,
            last_modified=last_modified,
            content_type=content_type,
            sha256=sha,
            byte_size=len(body),
            fetched_at=_now_iso(),
            body_path=body_path,
        )
        with self._lock:
            body_path.write_bytes(body)
            meta_path.write_text(
                json.dumps(
                    {
                        "url": url,
                        "status": status,
                        "etag": etag,
                        "last_modified": last_modified,
                        "content_type": content_type,
                        "sha256": sha,
                        "byte_size": len(body),
                        "fetched_at": entry.fetched_at,
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
        return entry

    def touch(self, url: str) -> None:
        """Record that a conditional request confirmed the cached copy (304)."""
        meta_path, _ = self._paths(url)
        if not meta_path.is_file():
            return
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        meta["revalidated_at"] = _now_iso()
        with self._lock:
            meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")

    def conditional_headers(self, url: str) -> dict[str, str]:
        entry = self.get(url)
        if entry is None:
            return {}
        headers: dict[str, str] = {}
        if entry.etag:
            headers["If-None-Match"] = entry.etag
        if entry.last_modified:
            headers["If-Modified-Since"] = entry.last_modified
        return headers
