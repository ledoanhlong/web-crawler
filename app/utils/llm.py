"""Thin wrapper around the Azure OpenAI client with retry logic.

Provides three client tiers:
- **Default** (GPT-5.2): general planning and parsing.
- **Vision** (GPT-4o): screenshot-based page analysis.
- **Claude** (Claude Opus 4.6 via Azure AI Foundry): complex-site extraction fallback.
"""

from __future__ import annotations

import asyncio
import base64
import json
import random
import re
import time
from typing import Any

import httpx
from openai import AsyncAzureOpenAI, APIStatusError, APITimeoutError, APIConnectionError

from app.config import settings
from app.utils.logging import get_logger

log = get_logger(__name__)

_client: AsyncAzureOpenAI | None = None
_vision_client: AsyncAzureOpenAI | None = None
_claude_http_client: httpx.AsyncClient | None = None
_claude_consecutive_errors: int = 0
_claude_disabled_until_ts: float = 0.0

_LLM_MAX_RETRIES = 3
_LLM_BACKOFF_BASE = 2.0
_LLM_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


def get_client() -> AsyncAzureOpenAI:
    global _client
    if _client is None:
        log.info(
            "Connecting to Azure OpenAI: endpoint=%s deployment=%s api_version=%s",
            settings.azure_openai_endpoint,
            settings.azure_openai_deployment,
            settings.azure_openai_api_version,
        )
        _client = AsyncAzureOpenAI(
            azure_endpoint=settings.azure_openai_endpoint,
            api_key=settings.azure_openai_api_key,
            api_version=settings.azure_openai_api_version,
        )
    return _client


def get_vision_client() -> AsyncAzureOpenAI | None:
    """Return the vision model client (GPT-4o), or None if not configured."""
    global _vision_client
    if not settings.azure_vision_endpoint or not settings.azure_vision_api_key:
        return None
    if _vision_client is None:
        log.info(
            "Connecting to Azure Vision model: endpoint=%s deployment=%s",
            settings.azure_vision_endpoint,
            settings.azure_vision_deployment,
        )
        _vision_client = AsyncAzureOpenAI(
            azure_endpoint=settings.azure_vision_endpoint,
            api_key=settings.azure_vision_api_key,
            api_version=settings.azure_vision_api_version,
        )
    return _vision_client


def _claude_is_configured() -> bool:
    return bool(settings.azure_claude_endpoint and settings.azure_claude_api_key)


def _claude_is_temporarily_disabled() -> tuple[bool, int]:
    if _claude_disabled_until_ts <= 0:
        return False, 0
    now = time.time()
    if now >= _claude_disabled_until_ts:
        return False, 0
    return True, int(_claude_disabled_until_ts - now)


def _record_claude_success() -> None:
    global _claude_consecutive_errors
    _claude_consecutive_errors = 0


def _record_claude_failure() -> None:
    global _claude_consecutive_errors, _claude_disabled_until_ts
    _claude_consecutive_errors += 1
    if (
        settings.claude_circuit_breaker_enabled
        and _claude_consecutive_errors >= settings.claude_circuit_breaker_max_errors
    ):
        _claude_disabled_until_ts = time.time() + settings.claude_circuit_breaker_cooldown_s
        log.warning(
            "Claude circuit breaker opened: %d consecutive errors; cooldown=%ss",
            _claude_consecutive_errors,
            settings.claude_circuit_breaker_cooldown_s,
        )


def _build_claude_endpoint() -> str:
    endpoint = settings.azure_claude_endpoint.strip()
    if not endpoint:
        return endpoint
    # User may provide either root endpoint or full anthropic path.
    if endpoint.endswith("/anthropic/v1/messages"):
        return endpoint
    return endpoint.rstrip("/") + "/anthropic/v1/messages"


def _get_claude_http_client() -> httpx.AsyncClient:
    global _claude_http_client
    if _claude_http_client is None:
        _claude_http_client = httpx.AsyncClient(timeout=60.0)
    return _claude_http_client


# ---------------------------------------------------------------------------
# Retry helper
# ---------------------------------------------------------------------------

async def _retry_completion(
    client: AsyncAzureOpenAI,
    model: str,
    kwargs: dict[str, Any],
    *,
    label: str = "LLM",
) -> str:
    """Run a chat completion with retry logic.  Shared by all three tiers."""
    last_exc: Exception | None = None
    for attempt in range(_LLM_MAX_RETRIES):
        try:
            resp = await client.chat.completions.create(**kwargs)
            content = resp.choices[0].message.content or ""
            log.debug("%s response: %d chars", label, len(content))
            return content
        except APIStatusError as exc:
            last_exc = exc
            if exc.status_code not in _LLM_RETRYABLE_STATUS_CODES:
                raise
            retry_after = _parse_retry_after(exc)
            delay = retry_after or (_LLM_BACKOFF_BASE ** attempt + random.uniform(0, 1))
            log.warning(
                "%s request failed (status=%d, attempt %d/%d), retrying in %.1fs: %s",
                label, exc.status_code, attempt + 1, _LLM_MAX_RETRIES, delay, exc,
            )
        except (APITimeoutError, APIConnectionError) as exc:
            last_exc = exc
            delay = _LLM_BACKOFF_BASE ** attempt + random.uniform(0, 1)
            log.warning(
                "%s request failed (attempt %d/%d), retrying in %.1fs: %s",
                label, attempt + 1, _LLM_MAX_RETRIES, delay, exc,
            )

        if attempt < _LLM_MAX_RETRIES - 1:
            await asyncio.sleep(delay)

    raise last_exc  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Default (GPT-5.2) completions
# ---------------------------------------------------------------------------

async def chat_completion(
    messages: list[dict[str, str]],
    *,
    temperature: float = 0.1,
    max_tokens: int = 16_000,
    response_format: dict[str, str] | None = None,
) -> str:
    """Send a chat completion request and return the assistant message content."""
    client = get_client()
    kwargs: dict[str, Any] = {
        "model": settings.azure_openai_deployment,
        "messages": messages,
        "temperature": temperature,
        "max_completion_tokens": max_tokens,
    }
    if response_format is not None:
        kwargs["response_format"] = response_format

    log.debug("LLM request: %d messages, model=%s", len(messages), settings.azure_openai_deployment)
    return await _retry_completion(client, settings.azure_openai_deployment, kwargs, label="LLM")


def _parse_retry_after(exc: APIStatusError) -> float | None:
    """Extract Retry-After header value from an API error, if present."""
    try:
        headers = exc.response.headers
        ra = headers.get("retry-after")
        if ra:
            return float(ra)
    except Exception:
        pass
    return None


async def chat_completion_json(
    messages: list[dict[str, str]],
    *,
    temperature: float = 0.1,
    max_tokens: int = 16_000,
) -> Any:
    """Like chat_completion but forces JSON output and parses it."""
    raw = await chat_completion(
        messages,
        temperature=temperature,
        max_tokens=max_tokens,
        response_format={"type": "json_object"},
    )
    if not raw or not raw.strip():
        raise ValueError(
            f"LLM returned empty response (input had {len(messages)} messages, "
            f"max_tokens={max_tokens})"
        )
    return json.loads(raw)


# ---------------------------------------------------------------------------
# Vision (GPT-4o) completions — screenshot analysis
# ---------------------------------------------------------------------------

async def chat_completion_vision(
    messages: list[dict[str, Any]],
    *,
    temperature: float = 0.1,
    max_tokens: int = 16_000,
    response_format: dict[str, str] | None = None,
) -> str:
    """Send a vision completion with image content (base64 PNG).

    *messages* may contain multimodal content blocks, e.g.::

        [{"role": "user", "content": [
            {"type": "text", "text": "..."},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}},
        ]}]
    """
    client = get_vision_client()
    if client is None:
        raise RuntimeError("Vision model not configured (set AZURE_VISION_ENDPOINT and AZURE_VISION_API_KEY)")
    kwargs: dict[str, Any] = {
        "model": settings.azure_vision_deployment,
        "messages": messages,
        "temperature": temperature,
        "max_completion_tokens": max_tokens,
    }
    if response_format is not None:
        kwargs["response_format"] = response_format
    log.debug("Vision request: %d messages, model=%s", len(messages), settings.azure_vision_deployment)
    return await _retry_completion(client, settings.azure_vision_deployment, kwargs, label="Vision")


async def chat_completion_vision_json(
    messages: list[dict[str, Any]],
    *,
    temperature: float = 0.1,
    max_tokens: int = 16_000,
) -> Any:
    """Vision completion that forces JSON output and parses it."""
    raw = await chat_completion_vision(
        messages,
        temperature=temperature,
        max_tokens=max_tokens,
        response_format={"type": "json_object"},
    )
    if not raw or not raw.strip():
        raise ValueError("Vision model returned empty response")
    return json.loads(raw)


def encode_image_base64(image_bytes: bytes) -> str:
    """Encode raw image bytes as a data-URI string for the vision API."""
    b64 = base64.b64encode(image_bytes).decode("utf-8")
    return f"data:image/png;base64,{b64}"


# ---------------------------------------------------------------------------
# Claude Opus 4.6 (Azure AI Foundry) completions — complex extraction
# ---------------------------------------------------------------------------

async def chat_completion_claude(
    messages: list[dict[str, str]],
    *,
    temperature: float = 0.1,
    max_tokens: int = 16_000,
    response_format: dict[str, str] | None = None,
) -> str:
    """Send a completion to Claude Opus 4.6 via Azure AI Foundry Anthropic API."""
    result = await chat_completion_claude_with_meta(
        messages,
        temperature=temperature,
        max_tokens=max_tokens,
        response_format=response_format,
    )
    return result.get("content", "")


def _openai_to_anthropic_messages(messages: list[dict[str, str]]) -> tuple[str | None, list[dict[str, Any]]]:
    """Convert OpenAI-style chat messages to Anthropic Messages payload."""
    system_parts: list[str] = []
    converted: list[dict[str, Any]] = []
    for m in messages:
        role = (m.get("role") or "user").strip().lower()
        content = m.get("content") or ""
        if role == "system":
            if content:
                system_parts.append(content)
            continue
        if role not in ("user", "assistant"):
            role = "user"
        converted.append({"role": role, "content": content})
    system_text = "\n\n".join(system_parts) if system_parts else None
    if not converted:
        converted = [{"role": "user", "content": "ping"}]
    return system_text, converted


async def chat_completion_claude_with_meta(
    messages: list[dict[str, str]],
    *,
    temperature: float = 0.1,
    max_tokens: int = 16_000,
    response_format: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Claude completion returning content + usage + latency metadata."""
    if not _claude_is_configured():
        raise RuntimeError("Claude model not configured (set AZURE_CLAUDE_ENDPOINT and AZURE_CLAUDE_API_KEY)")

    disabled, retry_after_s = _claude_is_temporarily_disabled()
    if disabled:
        raise RuntimeError(f"Claude circuit breaker open; retry after ~{retry_after_s}s")

    endpoint = _build_claude_endpoint()
    client = _get_claude_http_client()
    system_text, anthropic_messages = _openai_to_anthropic_messages(messages)

    if response_format and response_format.get("type") == "json_object":
        if system_text:
            system_text += "\n\nReturn ONLY valid JSON. No markdown fencing."
        else:
            system_text = "Return ONLY valid JSON. No markdown fencing."

    payload: dict[str, Any] = {
        "model": settings.azure_claude_deployment,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "messages": anthropic_messages,
    }
    if system_text:
        payload["system"] = system_text

    headers = {
        "api-key": settings.azure_claude_api_key,
        "x-api-key": settings.azure_claude_api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }

    last_exc: Exception | None = None
    for attempt in range(_LLM_MAX_RETRIES):
        started = time.perf_counter()
        try:
            resp = await client.post(endpoint, headers=headers, json=payload)
            if resp.status_code in _LLM_RETRYABLE_STATUS_CODES:
                raise RuntimeError(f"Claude transient status {resp.status_code}: {resp.text[:300]}")
            resp.raise_for_status()
            data = resp.json()
            parts = data.get("content", [])
            text_parts: list[str] = []
            for part in parts:
                if isinstance(part, dict) and part.get("type") == "text":
                    text_parts.append(str(part.get("text") or ""))
            content = "\n".join(p for p in text_parts if p).strip()
            if not content:
                log.warning(
                    "Claude returned empty text content; stop_reason=%s, raw_parts=%d",
                    data.get("stop_reason", "unknown"),
                    len(parts),
                )

            usage = data.get("usage") or {}
            input_tokens = int(usage.get("input_tokens") or 0)
            output_tokens = int(usage.get("output_tokens") or 0)
            est_cost = (
                (input_tokens / 1_000_000) * settings.claude_input_cost_per_mtok
                + (output_tokens / 1_000_000) * settings.claude_output_cost_per_mtok
            )
            latency_ms = (time.perf_counter() - started) * 1000
            _record_claude_success()
            return {
                "content": content,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "estimated_cost_usd": round(est_cost, 6),
                "latency_ms": round(latency_ms, 2),
            }
        except Exception as exc:
            last_exc = exc
            _record_claude_failure()
            delay = _LLM_BACKOFF_BASE ** attempt + random.uniform(0, 1)
            log.warning(
                "Claude request failed (attempt %d/%d), retrying in %.1fs: %s",
                attempt + 1, _LLM_MAX_RETRIES, delay, exc,
            )
            if attempt < _LLM_MAX_RETRIES - 1:
                await asyncio.sleep(delay)

    raise last_exc if last_exc else RuntimeError("Claude request failed")


async def chat_completion_claude_json(
    messages: list[dict[str, str]],
    *,
    temperature: float = 0.1,
    max_tokens: int = 16_000,
) -> Any:
    """Claude completion that forces JSON output and parses it."""
    raw = await chat_completion_claude(
        messages,
        temperature=temperature,
        max_tokens=max_tokens,
        response_format={"type": "json_object"},
    )
    if not raw or not raw.strip():
        raise ValueError("Claude model returned empty response")
    text = raw.strip()
    # Strip markdown fences Claude often wraps JSON in
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*\n?", "", text)
        text = re.sub(r"\n?```\s*$", "", text)
    return json.loads(text)


async def ping_openai_default() -> dict[str, Any]:
    start = time.perf_counter()
    try:
        await chat_completion(
            [{"role": "user", "content": "ping"}],
            max_tokens=8,
            temperature=0,
        )
        return {
            "status": "ok",
            "latency_ms": round((time.perf_counter() - start) * 1000, 2),
            "model": settings.azure_openai_deployment,
        }
    except Exception as exc:
        return {
            "status": "error",
            "latency_ms": round((time.perf_counter() - start) * 1000, 2),
            "model": settings.azure_openai_deployment,
            "error": str(exc),
        }


async def ping_openai_vision() -> dict[str, Any]:
    start = time.perf_counter()
    if get_vision_client() is None:
        return {
            "status": "disabled",
            "latency_ms": 0.0,
            "model": settings.azure_vision_deployment,
            "error": "Vision not configured",
        }
    try:
        await chat_completion_vision(
            [{"role": "user", "content": [{"type": "text", "text": "reply with ok"}]}],
            max_tokens=8,
            temperature=0,
        )
        return {
            "status": "ok",
            "latency_ms": round((time.perf_counter() - start) * 1000, 2),
            "model": settings.azure_vision_deployment,
        }
    except Exception as exc:
        return {
            "status": "error",
            "latency_ms": round((time.perf_counter() - start) * 1000, 2),
            "model": settings.azure_vision_deployment,
            "error": str(exc),
        }


async def ping_claude() -> dict[str, Any]:
    start = time.perf_counter()
    if not _claude_is_configured():
        return {
            "status": "disabled",
            "latency_ms": 0.0,
            "model": settings.azure_claude_deployment,
            "error": "Claude not configured",
        }
    disabled, retry_after_s = _claude_is_temporarily_disabled()
    if disabled:
        return {
            "status": "degraded",
            "latency_ms": 0.0,
            "model": settings.azure_claude_deployment,
            "error": f"Circuit breaker open ({retry_after_s}s remaining)",
        }
    try:
        await chat_completion_claude(
            [{"role": "user", "content": "ping"}],
            max_tokens=16,
            temperature=0,
        )
        return {
            "status": "ok",
            "latency_ms": round((time.perf_counter() - start) * 1000, 2),
            "model": settings.azure_claude_deployment,
            "consecutive_errors": _claude_consecutive_errors,
        }
    except Exception as exc:
        return {
            "status": "error",
            "latency_ms": round((time.perf_counter() - start) * 1000, 2),
            "model": settings.azure_claude_deployment,
            "error": str(exc),
            "consecutive_errors": _claude_consecutive_errors,
        }


def get_claude_runtime_state() -> dict[str, Any]:
    disabled, retry_after_s = _claude_is_temporarily_disabled()
    return {
        "configured": _claude_is_configured(),
        "circuit_breaker_enabled": settings.claude_circuit_breaker_enabled,
        "consecutive_errors": _claude_consecutive_errors,
        "disabled": disabled,
        "retry_after_s": retry_after_s,
    }
