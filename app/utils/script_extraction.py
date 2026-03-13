"""Script-based extraction — Claude generates a BS4 extraction function.

Generates a `def extract_data(html_content) -> list[dict]` function using
Claude Opus 4.6, caches it per domain+structural-hash, and executes it
against page HTML via exec() in an isolated namespace.

Used as a 7th extraction method alongside CSS, SmartScraper, Crawl4AI,
UniversalScraper, ListingAPI, and Claude direct extraction.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from app.utils.logging import get_logger

log = get_logger(__name__)

_CACHE_DIR = Path("temp/cache")
_MAX_HTML_CHARS = 80_000  # limit HTML sample sent to Claude


# ---------------------------------------------------------------------------
# Structural hashing — same HTML structure → same cached script
# ---------------------------------------------------------------------------
def _structural_hash(html: str) -> str:
    """Hash the HTML tag skeleton so the same script works across pages."""
    # Strip text content and attributes, keep only tag names and nesting
    skeleton = re.sub(r">([^<]+)<", "><", html)          # remove text nodes
    skeleton = re.sub(r"\s[^>]*(?=>)", "", skeleton)      # remove attributes
    skeleton = skeleton[:20_000]                          # cap size
    return hashlib.sha256(skeleton.encode("utf-8", errors="replace")).hexdigest()[:16]


def _cache_path(url: str, struct_hash: str) -> Path:
    """Return the cache file path for a given URL + structure hash."""
    domain = urlparse(url).netloc.replace(":", "_")
    return _CACHE_DIR / f"{domain}_{struct_hash}.script.py"


def _load_cached_script(url: str, html: str) -> str | None:
    """Return cached script source if available, else None."""
    sh = _structural_hash(html)
    path = _cache_path(url, sh)
    if path.exists():
        log.info("Script cache hit: %s", path.name)
        return path.read_text(encoding="utf-8")
    return None


def load_cached_script_by_hash(html: str) -> str | None:
    """Find a cached script matching the HTML's structural hash (any domain)."""
    sh = _structural_hash(html)
    if not _CACHE_DIR.exists():
        return None
    matches = list(_CACHE_DIR.glob(f"*_{sh}.script.py"))
    if matches:
        log.info("Script cache hit (by hash): %s", matches[0].name)
        return matches[0].read_text(encoding="utf-8")
    return None


def _save_cached_script(url: str, html: str, script: str) -> None:
    """Write generated script to the cache directory."""
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    sh = _structural_hash(html)
    path = _cache_path(url, sh)
    header = (
        f"# Cached extraction script\n"
        f"# URL: {url}\n"
        f"# Structural Hash: {sh}\n"
        f"# Generated at: {datetime.now(timezone.utc).isoformat()}\n\n"
    )
    path.write_text(header + script, encoding="utf-8")
    log.info("Script cached: %s (%d chars)", path.name, len(script))


# ---------------------------------------------------------------------------
# Script generation via Claude Opus 4.6
# ---------------------------------------------------------------------------
_SYSTEM_PROMPT = """\
You are an expert Python web scraping engineer.
Generate a complete Python function with this EXACT signature:

def extract_data(html_content: str) -> list:

The function must:
- Import BeautifulSoup and re INSIDE the function body
- Parse html_content with BeautifulSoup("html.parser")
- Find all repeating item containers (company cards, listing rows, etc.)
- Extract these fields from each item: {fields}
- Return a list of dicts with string values (use None for missing fields)
- Handle errors gracefully with try/except per field extraction
- Include a detail_link field if items have links to detail/profile pages

Return ONLY the Python function definition starting with 'def extract_data'.
No markdown fences, no imports outside the function, no explanation."""


async def generate_extraction_script(
    html: str,
    fields: list[str],
    url: str,
) -> str:
    """Generate an extraction function using Claude, with caching.

    Returns the Python source code of an ``extract_data(html_content)`` function.
    """
    # Check cache first
    cached = _load_cached_script(url, html)
    if cached:
        return cached

    from app.utils.llm import chat_completion_claude

    fields_str = ", ".join(fields)
    system = _SYSTEM_PROMPT.format(fields=fields_str)
    html_sample = html[:_MAX_HTML_CHARS]

    user_msg = (
        f"URL: {url}\n\n"
        f"Fields to extract: {fields_str}\n\n"
        f"Page HTML sample:\n{html_sample}"
    )

    log.info("Generating extraction script with Claude for %s (fields: %s)", url, fields_str)

    try:
        result = await asyncio.wait_for(
            chat_completion_claude(
                [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user_msg},
                ],
                max_tokens=8_000,
                temperature=0.1,
            ),
            timeout=120,
        )
    except asyncio.TimeoutError:
        log.error("Extraction script generation timed out after 120s")
        raise TimeoutError("Extraction script generation timed out")
    except Exception as exc:
        log.error("Extraction script generation failed: %s", exc)
        raise

    # Strip markdown fences if the model wrapped the code
    script = re.sub(r"^```(?:python)?\n?", "", result.strip())
    script = re.sub(r"\n?```$", "", script)

    # Validate the script defines extract_data
    if "def extract_data" not in script:
        raise ValueError("Generated script does not define extract_data()")

    log.info("Extraction script generated (%d chars)", len(script))

    # Cache for reuse
    _save_cached_script(url, html, script)

    return script


# ---------------------------------------------------------------------------
# Script execution
# ---------------------------------------------------------------------------
def execute_extraction_script(
    script_source: str,
    html: str,
) -> list[dict[str, str | None]]:
    """Execute a generated extraction script against HTML.

    The script must define ``extract_data(html_content) -> list[dict]``.
    Returns a sanitized list of dicts with string values.
    """
    from app.utils.script_executor import validate_script

    # Safety check
    warnings = validate_script(script_source)
    if warnings:
        log.warning("Extraction script blocked by safety scan: %s", warnings)
        return []

    # Execute in isolated namespace
    namespace: dict = {}
    try:
        exec(script_source, namespace)  # noqa: S102
    except Exception as exc:
        log.warning("Extraction script compilation failed: %s", exc)
        return []

    extract_fn = namespace.get("extract_data")
    if not callable(extract_fn):
        log.warning("Extraction script does not define callable extract_data()")
        return []

    try:
        raw_items = extract_fn(html)
    except Exception as exc:
        log.warning("Extraction script execution failed: %s", exc)
        return []

    if not isinstance(raw_items, list):
        log.warning("extract_data() returned %s instead of list", type(raw_items).__name__)
        return []

    # Sanitize: ensure all values are str | None
    sanitized: list[dict[str, str | None]] = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        clean: dict[str, str | None] = {}
        for k, v in item.items():
            if v is None:
                clean[k] = None
            elif isinstance(v, str):
                clean[k] = v
            elif isinstance(v, (list, dict)):
                import json
                clean[k] = json.dumps(v, ensure_ascii=False)
            else:
                clean[k] = str(v)
        if clean:
            sanitized.append(clean)

    log.info("Extraction script produced %d items", len(sanitized))
    return sanitized
