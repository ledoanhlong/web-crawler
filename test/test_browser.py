from __future__ import annotations

import asyncio

import pytest

from app.utils.browser import _drain_pending_tasks, _register_async_response_listener


class _FakePage:
    def __init__(self) -> None:
        self.listeners: dict[str, object] = {}

    def on(self, event_name: str, listener: object) -> None:
        self.listeners[event_name] = listener

    def remove_listener(self, event_name: str, listener: object) -> None:
        if self.listeners.get(event_name) is listener:
            self.listeners.pop(event_name, None)


@pytest.mark.asyncio
async def test_async_response_listener_schedules_and_drains_handler_tasks() -> None:
    page = _FakePage()
    seen: list[str] = []

    async def _handler(response: str) -> None:
        await asyncio.sleep(0)
        seen.append(response)

    pending, listener = _register_async_response_listener(page, _handler)
    assert "response" in page.listeners

    page.listeners["response"]("first")
    page.listeners["response"]("second")

    await _drain_pending_tasks(pending)

    assert seen == ["first", "second"]
    page.remove_listener("response", listener)
    assert "response" not in page.listeners
