"""Tests for app.utils.http — retry, backoff, FetchResult, UA rotation, Link header."""

from __future__ import annotations

import pytest

from app.utils.http import (
    FetchResult,
    _rotate_user_agent,
    _USER_AGENTS,
    parse_link_header,
    _compute_backoff,
)


# ---------------------------------------------------------------------------
# FetchResult
# ---------------------------------------------------------------------------

class TestFetchResult:
    def test_default_values(self):
        r = FetchResult()
        assert r.text == ""
        assert r.status_code == 0
        assert r.headers == {}
        assert r.response_time_ms == 0.0

    def test_content_type_json(self):
        r = FetchResult(headers={"content-type": "application/json; charset=utf-8"})
        assert r.content_type == "application/json"
        assert r.is_json is True
        assert r.is_html is False

    def test_content_type_html(self):
        r = FetchResult(headers={"content-type": "text/html; charset=utf-8"})
        assert r.content_type == "text/html"
        assert r.is_html is True
        assert r.is_json is False

    def test_etag(self):
        r = FetchResult(headers={"etag": '"abc123"'})
        assert r.etag == '"abc123"'

    def test_last_modified(self):
        r = FetchResult(headers={"last-modified": "Wed, 21 Oct 2025 07:28:00 GMT"})
        assert r.last_modified == "Wed, 21 Oct 2025 07:28:00 GMT"

    def test_retry_after_seconds(self):
        r = FetchResult(headers={"retry-after": "120"})
        assert r.retry_after == 120.0

    def test_retry_after_missing(self):
        r = FetchResult(headers={})
        assert r.retry_after is None

    def test_retry_after_invalid(self):
        r = FetchResult(headers={"retry-after": "not-a-number"})
        assert r.retry_after is None

    def test_rate_limit_remaining(self):
        r = FetchResult(headers={"x-ratelimit-remaining": "42"})
        assert r.rate_limit_remaining == 42

    def test_rate_limit_remaining_alt_header(self):
        r = FetchResult(headers={"x-rate-limit-remaining": "7"})
        assert r.rate_limit_remaining == 7

    def test_rate_limit_remaining_missing(self):
        r = FetchResult(headers={})
        assert r.rate_limit_remaining is None

    def test_rate_limit_remaining_invalid(self):
        r = FetchResult(headers={"x-ratelimit-remaining": "nope"})
        assert r.rate_limit_remaining is None


# ---------------------------------------------------------------------------
# User-Agent rotation
# ---------------------------------------------------------------------------

class TestUserAgentRotation:
    def test_returns_string(self):
        ua = _rotate_user_agent()
        assert isinstance(ua, str)
        assert len(ua) > 20

    def test_from_pool(self):
        ua = _rotate_user_agent()
        assert ua in _USER_AGENTS

    def test_pool_has_variety(self):
        assert len(_USER_AGENTS) >= 5

    def test_rotation_varies(self):
        """Over many calls we should see more than one UA."""
        seen = {_rotate_user_agent() for _ in range(50)}
        assert len(seen) > 1


# ---------------------------------------------------------------------------
# Link header parsing (RFC 5988)
# ---------------------------------------------------------------------------

class TestParseLinkHeader:
    def test_single_next(self):
        header = '<https://api.example.com/items?page=2>; rel="next"'
        result = parse_link_header(header)
        assert result == {"next": "https://api.example.com/items?page=2"}

    def test_multiple_rels(self):
        header = (
            '<https://api.example.com/items?page=2>; rel="next", '
            '<https://api.example.com/items?page=5>; rel="last"'
        )
        result = parse_link_header(header)
        assert result["next"] == "https://api.example.com/items?page=2"
        assert result["last"] == "https://api.example.com/items?page=5"

    def test_empty_string(self):
        assert parse_link_header("") == {}

    def test_malformed_ignored(self):
        header = "this is not a valid link header"
        assert parse_link_header(header) == {}

    def test_mixed_valid_invalid(self):
        header = (
            'garbage, '
            '<https://example.com/page/2>; rel="next"'
        )
        result = parse_link_header(header)
        assert result == {"next": "https://example.com/page/2"}


# ---------------------------------------------------------------------------
# Backoff computation
# ---------------------------------------------------------------------------

class TestComputeBackoff:
    def test_first_attempt(self):
        delay = _compute_backoff(0)
        # backoff_factor=1.0, 2^0=1, so base=1.0, jitter up to 0.25
        assert 1.0 <= delay <= 1.3

    def test_second_attempt(self):
        delay = _compute_backoff(1)
        # base=2.0, jitter up to 0.5
        assert 2.0 <= delay <= 2.6

    def test_third_attempt(self):
        delay = _compute_backoff(2)
        # base=4.0, jitter up to 1.0
        assert 4.0 <= delay <= 5.1

    def test_retry_after_overrides(self):
        delay = _compute_backoff(0, retry_after=10.0)
        assert delay >= 10.0

    def test_capped_at_max(self):
        # attempt=10 → base=1024, should be capped
        delay = _compute_backoff(10)
        assert delay <= 30.0  # max_request_delay_ms / 1000
