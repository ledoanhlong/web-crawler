"""Verify polling timeout works."""

import asyncio
import concurrent.futures
import time

import pytest

pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)


@pytest.mark.asyncio
async def test_poll_timeout() -> None:
    future = pool.submit(time.sleep, 30)
    t0 = time.monotonic()
    timeout = 3
    while not future.done():
        elapsed = time.monotonic() - t0
        if elapsed >= timeout:
            print(f"TIMEOUT fired correctly at {elapsed:.1f}s")
            future.cancel()
            return
        await asyncio.sleep(0.5)
    print(f"Completed in {time.monotonic() - t0:.1f}s")
