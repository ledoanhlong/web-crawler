"""Prompt template loader.

Loads .txt prompt files from the app/prompts/ directory.  Prompts are
cached after the first read so there is no repeated disk I/O.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

_PROMPTS_DIR = Path(__file__).parent


@lru_cache(maxsize=None)
def load_prompt(name: str) -> str:
    """Load a prompt template by name (without the .txt extension).

    Example::

        from app.prompts import load_prompt
        prompt = load_prompt("planner_listing")
    """
    path = _PROMPTS_DIR / f"{name}.txt"
    if not path.exists():
        raise FileNotFoundError(f"Prompt template not found: {path}")
    return path.read_text(encoding="utf-8").strip()
