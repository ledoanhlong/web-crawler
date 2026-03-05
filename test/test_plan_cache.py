"""Tests for app.services.plan_cache — PlanCache."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from app.services.plan_cache import PlanCache, _domain_key


@pytest.fixture()
def cache(tmp_path: Path) -> PlanCache:
    return PlanCache(cache_dir=tmp_path)


class TestDomainKey:
    def test_same_domain_same_key(self):
        assert _domain_key("https://example.com/dir") == _domain_key("https://example.com/dir")

    def test_different_domains(self):
        assert _domain_key("https://a.com/x") != _domain_key("https://b.com/x")

    def test_trailing_slash_ignored(self):
        assert _domain_key("https://example.com/dir/") == _domain_key("https://example.com/dir")


class TestPlanCache:
    def test_put_and_get(self, cache: PlanCache):
        plan = {"url": "https://example.com", "selectors": {"name": "h2"}}
        cache.put("https://example.com", plan)
        result = cache.get("https://example.com")
        assert result == plan

    def test_miss(self, cache: PlanCache):
        assert cache.get("https://never-cached.com") is None

    def test_expired(self, cache: PlanCache, tmp_path: Path):
        plan = {"url": "https://old.com"}
        cache.put("https://old.com", plan)
        # Manually backdate the cache file
        key = _domain_key("https://old.com")
        path = tmp_path / f"{key}.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["_cached_at"] = time.time() - 200 * 3600  # 200 hours ago
        path.write_text(json.dumps(data), encoding="utf-8")

        assert cache.get("https://old.com", max_age_hours=168) is None

    def test_invalidate(self, cache: PlanCache):
        cache.put("https://remove-me.com", {"x": 1})
        assert cache.get("https://remove-me.com") is not None
        assert cache.invalidate("https://remove-me.com") is True
        assert cache.get("https://remove-me.com") is None

    def test_invalidate_nonexistent(self, cache: PlanCache):
        assert cache.invalidate("https://nope.com") is False

    def test_list_entries(self, cache: PlanCache):
        cache.put("https://a.com", {"a": 1})
        cache.put("https://b.com", {"b": 2})
        entries = cache.list_entries()
        assert len(entries) == 2
        urls = [e["url"] for e in entries]
        assert "https://a.com" in urls

    def test_clear(self, cache: PlanCache):
        cache.put("https://x.com", {"x": 1})
        cache.put("https://y.com", {"y": 2})
        count = cache.clear()
        assert count == 2
        assert cache.list_entries() == []

    def test_corrupt_file_ignored(self, cache: PlanCache, tmp_path: Path):
        # Create a corrupt cache file
        key = _domain_key("https://corrupt.com")
        (tmp_path / f"{key}.json").write_text("not json!!!", encoding="utf-8")
        assert cache.get("https://corrupt.com") is None
