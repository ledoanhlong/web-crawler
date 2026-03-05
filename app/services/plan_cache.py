"""Plan cache — saves and reuses ScrapingPlans by domain.

When a user successfully scrapes a website, the plan (selectors, pagination
strategy, etc.) is cached to disk so the next crawl of the same domain skips
the LLM planning step and starts scraping immediately.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from urllib.parse import urlparse

from app.utils.logging import get_logger

log = get_logger(__name__)

_CACHE_DIR = Path("output/plan_cache")


def _domain_key(url: str) -> str:
    """Return a cache key derived from the URL's domain + path prefix."""
    p = urlparse(url)
    raw = f"{p.netloc}{p.path.rstrip('/')}".lower()
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _ensure_cache_dir() -> Path:
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return _CACHE_DIR


class PlanCache:
    """Simple file-backed cache for ScrapingPlans keyed by domain."""

    def __init__(self, cache_dir: Path | None = None) -> None:
        self._dir = cache_dir or _CACHE_DIR

    def _path(self, key: str) -> Path:
        return self._dir / f"{key}.json"

    def get(self, url: str, *, max_age_hours: int = 168) -> dict | None:
        """Look up a cached plan for *url*.  Returns ``None`` on miss.

        Parameters
        ----------
        max_age_hours : int
            Maximum age in hours before the cached plan is considered stale.
            Default is 168 (one week).
        """
        key = _domain_key(url)
        path = self._path(key)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            log.warning("Corrupt cache file %s — ignoring", path)
            return None

        cached_at = data.get("_cached_at", 0)
        age_hours = (time.time() - cached_at) / 3600
        if age_hours > max_age_hours:
            log.info("Cached plan for %s expired (%.1f h old)", url, age_hours)
            return None

        log.info("Cache hit for %s (%.1f h old, key=%s)", url, age_hours, key)
        return data.get("plan")

    def put(self, url: str, plan: dict) -> None:
        """Store a plan in the cache."""
        self._dir.mkdir(parents=True, exist_ok=True)
        key = _domain_key(url)
        data = {"url": url, "plan": plan, "_cached_at": time.time()}
        self._path(key).write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        log.info("Cached plan for %s (key=%s)", url, key)

    def invalidate(self, url: str) -> bool:
        """Remove the cached plan for *url*.  Returns ``True`` if it existed."""
        key = _domain_key(url)
        path = self._path(key)
        if path.exists():
            path.unlink()
            log.info("Invalidated cache for %s", url)
            return True
        return False

    def list_entries(self) -> list[dict]:
        """List all cached entries with metadata."""
        entries: list[dict] = []
        if not self._dir.exists():
            return entries
        for path in sorted(self._dir.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                age_h = (time.time() - data.get("_cached_at", 0)) / 3600
                entries.append({
                    "key": path.stem,
                    "url": data.get("url", "?"),
                    "age_hours": round(age_h, 1),
                })
            except Exception:
                continue
        return entries

    def clear(self) -> int:
        """Remove all cached plans.  Returns the number of entries removed."""
        if not self._dir.exists():
            return 0
        count = 0
        for path in self._dir.glob("*.json"):
            path.unlink()
            count += 1
        log.info("Cleared %d cached plan(s)", count)
        return count


# Module-level singleton
plan_cache = PlanCache()
