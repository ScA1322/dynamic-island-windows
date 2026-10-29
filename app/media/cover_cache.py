"""On-disk album art cache.

* SMTC covers arrive as bytes and are stored by content hash, so the same cover is written once.
* Web API covers are stored by URL hash, so an image is downloaded only once.
"""
from __future__ import annotations

import hashlib
import logging
import os
import threading
from pathlib import Path

log = logging.getLogger(__name__)

MAX_COVER_BYTES = 8 * 1024 * 1024
_SIGNATURES = {
    b"\x89PNG": ".png",
    b"\xff\xd8\xff": ".jpg",
    b"GIF8": ".gif",
    b"BM": ".bmp",
    b"RIFF": ".webp",
}


def image_extension(data: bytes) -> str | None:
    for signature, ext in _SIGNATURES.items():
        if data.startswith(signature):
            return ext
    return None


class CoverCache:
    def __init__(self, directory: Path, max_files: int = 300) -> None:
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.max_files = max_files
        self._lock = threading.Lock()
        self._writes = 0

    def store_bytes(self, data: bytes) -> Path | None:
        if not data or len(data) > MAX_COVER_BYTES:
            return None
        ext = image_extension(data)
        if ext is None:
            log.debug("Ignoring cover with unknown image format")
            return None
        path = self.directory / f"c_{hashlib.sha1(data).hexdigest()}{ext}"
        with self._lock:
            if not path.exists():
                self._write_atomic(path, data)
            else:
                _touch(path)
        return path

    def path_for_url(self, url: str) -> Path | None:
        """Cached file for a URL, or None if it has not been downloaded yet."""
        stem = "u_" + hashlib.sha1(url.encode("utf-8")).hexdigest()
        for ext in set(_SIGNATURES.values()):
            path = self.directory / f"{stem}{ext}"
            if path.exists():
                _touch(path)
                return path
        return None

    def store_url(self, url: str, data: bytes) -> Path | None:
        if not data or len(data) > MAX_COVER_BYTES:
            return None
        ext = image_extension(data)
        if ext is None:
            return None
        path = self.directory / f"u_{hashlib.sha1(url.encode('utf-8')).hexdigest()}{ext}"
        with self._lock:
            self._write_atomic(path, data)
        return path

    def _write_atomic(self, path: Path, data: bytes) -> None:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, path)
        self._writes += 1
        if self._writes % 25 == 0:
            self.prune()

    def prune(self) -> None:
        """Delete the least recently used files beyond max_files."""
        try:
            files = sorted(
                (p for p in self.directory.iterdir() if p.is_file()),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
        except OSError:
            return
        for old in files[self.max_files:]:
            try:
                old.unlink()
            except OSError:
                pass


def _touch(path: Path) -> None:
    try:
        os.utime(path, None)
    except OSError:
        pass
