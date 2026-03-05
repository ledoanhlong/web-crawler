"""Page-level cache for incremental crawling.

Stores fetched HTML keyed by URL with ETag / Last-Modified headers so
subsequent crawls can use conditional requests (``If-None-Match``,
``If-Modified-Since``) to skip unchanged pages.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

from app.config import settings
from app.utils.logging import get_logger

log = get_logger(__name__)

_CACHE_DIR = Path("output/page_cache")


def _url_key(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()[:20]


class PageCache:
    """File-backed page cache for incremental crawling."""

    def __init__(self, cache_dir: Path | None = None) -> None:
        self._dir = cache_dir or _CACHE_DIR

    def _meta_path(self, key: str) -> Path:
        return self._dir / f"{key}.meta.json"

    def _html_path(self, key: str) -> Path:
        return self._dir / f"{key}.html"

    def lookup(self, url: str) -> dict | None:
        """Look up cached metadata for *url*.

        Returns ``{"etag": ..., "last_modified": ..., "cached_at": ...}``
        or ``None`` if not cached or expired.
        """
        if not settings.enable_page_cache:
            return None

        key = _url_key(url)
        meta_path = self._meta_path(key)
        if not meta_path.exists():
            return None

        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

        age_h = (time.time() - meta.get("cached_at", 0)) / 3600
        if age_h > settings.page_cache_max_age_hours:
            log.debug("Page cache expired for %s (%.1f h old)", url, age_h)
            return None

        return meta

    def get_html(self, url: str) -> str | None:
        """Return cached HTML for *url*, or ``None``."""
        key = _url_key(url)
        html_path = self._html_path(key)
        if html_path.exists():
            return html_path.read_text(encoding="utf-8")
        return None

    def store(
        self,
        url: str,
        html: str,
        *,
        etag: str | None = None,
        last_modified: str | None = None,
    ) -> None:
        """Store HTML and metadata for *url*."""
        if not settings.enable_page_cache:
            return
        self._dir.mkdir(parents=True, exist_ok=True)
        key = _url_key(url)
        meta = {
            "url": url,
            "etag": etag,
            "last_modified": last_modified,
            "cached_at": time.time(),
            "size": len(html),
        }
        self._meta_path(key).write_text(
            json.dumps(meta, ensure_ascii=False),
            encoding="utf-8",
        )
        self._html_path(key).write_text(html, encoding="utf-8")
        log.debug("Cached page %s (%d chars)", url, len(html))

    def conditional_headers(self, url: str) -> dict[str, str]:
        """Return conditional request headers for *url*.

        If the page was previously cached with an ETag or Last-Modified,
        returns the appropriate ``If-None-Match`` / ``If-Modified-Since``
        headers so the server can respond with 304.
        """
        meta = self.lookup(url)
        if not meta:
            return {}
        headers: dict[str, str] = {}
        if meta.get("etag"):
            headers["If-None-Match"] = meta["etag"]
        if meta.get("last_modified"):
            headers["If-Modified-Since"] = meta["last_modified"]
        return headers

    def clear(self) -> int:
        """Remove all cached pages.  Returns the number of entries removed."""
        if not self._dir.exists():
            return 0
        count = 0
        for path in self._dir.glob("*"):
            path.unlink()
            count += 1
        log.info("Cleared %d page cache file(s)", count)
        return count // 2  # meta + html pairs


# Module-level singleton
page_cache = PageCache()
