from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from app.utils.llm import (
    get_claude_runtime_state,
    ping_claude,
    ping_openai_default,
    ping_openai_vision,
)

_provider_health: dict[str, Any] = {
    "status": "initializing",
    "checked_at": None,
    "providers": {
        "openai": {"status": "unknown"},
        "vision": {"status": "unknown"},
        "claude": {"status": "unknown"},
    },
}


async def refresh_provider_health() -> dict[str, Any]:
    openai, vision, claude = await asyncio.gather(
        ping_openai_default(),
        ping_openai_vision(),
        ping_claude(),
    )
    overall = "ok"
    statuses = {openai.get("status"), vision.get("status"), claude.get("status")}
    if "error" in statuses:
        overall = "degraded"

    _provider_health["status"] = overall
    _provider_health["checked_at"] = datetime.now(timezone.utc).isoformat()
    _provider_health["providers"] = {
        "openai": openai,
        "vision": vision,
        "claude": claude,
    }
    _provider_health["claude_runtime"] = get_claude_runtime_state()
    return _provider_health


def get_provider_health_snapshot() -> dict[str, Any]:
    # Return a shallow copy to avoid accidental mutation in callers
    return {
        "status": _provider_health.get("status", "unknown"),
        "checked_at": _provider_health.get("checked_at"),
        "providers": dict(_provider_health.get("providers", {})),
        "claude_runtime": dict(_provider_health.get("claude_runtime", {})),
    }
