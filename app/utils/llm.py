"""Thin wrapper around the Azure OpenAI client."""

from __future__ import annotations

import json
from typing import Any

from openai import AsyncAzureOpenAI

from app.config import settings
from app.utils.logging import get_logger

log = get_logger(__name__)

_client: AsyncAzureOpenAI | None = None


def get_client() -> AsyncAzureOpenAI:
    global _client
    if _client is None:
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
    """Send a chat completion request and return the assistant message content."""
    client = get_client()
    kwargs: dict[str, Any] = {
        "model": settings.azure_openai_deployment,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if response_format is not None:
        kwargs["response_format"] = response_format

    log.debug("LLM request: %d messages, model=%s", len(messages), settings.azure_openai_deployment)
    resp = await client.chat.completions.create(**kwargs)
    content = resp.choices[0].message.content or ""
    log.debug("LLM response: %d chars", len(content))
    return content


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
    return json.loads(raw)
