"""Thin wrapper around the Azure OpenAI client with retry logic."""

from __future__ import annotations

import asyncio
import json
import random
from typing import Any

from openai import AsyncAzureOpenAI, APIStatusError, APITimeoutError, APIConnectionError

from app.config import settings
from app.utils.logging import get_logger

log = get_logger(__name__)

_client: AsyncAzureOpenAI | None = None

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


async def chat_completion(
    messages: list[dict[str, str]],
    *,
    temperature: float = 0.1,
    max_tokens: int = 16_000,
    response_format: dict[str, str] | None = None,
) -> str:
    """Send a chat completion request and return the assistant message content.

    Retries on transient errors (429, 5xx, timeouts, connection errors)
    with exponential backoff and jitter.
    """
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

    last_exc: Exception | None = None
    for attempt in range(_LLM_MAX_RETRIES):
        try:
            resp = await client.chat.completions.create(**kwargs)
            content = resp.choices[0].message.content or ""
            log.debug("LLM response: %d chars", len(content))
            return content
        except APIStatusError as exc:
            last_exc = exc
            if exc.status_code not in _LLM_RETRYABLE_STATUS_CODES:
                raise
            retry_after = _parse_retry_after(exc)
            delay = retry_after or (_LLM_BACKOFF_BASE ** attempt + random.uniform(0, 1))
            log.warning(
                "LLM request failed (status=%d, attempt %d/%d), retrying in %.1fs: %s",
                exc.status_code, attempt + 1, _LLM_MAX_RETRIES, delay, exc,
            )
        except (APITimeoutError, APIConnectionError) as exc:
            last_exc = exc
            delay = _LLM_BACKOFF_BASE ** attempt + random.uniform(0, 1)
            log.warning(
                "LLM request failed (attempt %d/%d), retrying in %.1fs: %s",
                attempt + 1, _LLM_MAX_RETRIES, delay, exc,
            )

        if attempt < _LLM_MAX_RETRIES - 1:
            await asyncio.sleep(delay)

    raise last_exc  # type: ignore[misc]


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
