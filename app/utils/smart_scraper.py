"""Smart scraper utilities — LLM-based extraction and ScrapeGraphAI wrappers.

``smart_extract_items`` and ``smart_extract_detail`` use the project's
own async Azure OpenAI client (``chat_completion_json``) for reliable,
non-blocking extraction.  The remaining ScrapeGraphAI wrappers
(SmartScraperMultiGraph, ScriptCreatorGraph) are kept for their
multi-URL / script-generation features.
"""

from __future__ import annotations

import asyncio
from functools import lru_cache

from app.config import settings
from app.utils.logging import get_logger

log = get_logger(__name__)

# Maximum HTML size (chars) to feed into the LLM.
# Large pages cause token overflow / timeouts.
_MAX_HTML_CHARS = 100_000


@lru_cache(maxsize=1)
def _build_graph_config() -> dict:
    """Build a ScrapeGraphAI graph_config using our Azure OpenAI credentials.

    Only used by the legacy SmartScraperMultiGraph / ScriptCreator wrappers.
    """
    from langchain_openai import AzureChatOpenAI

    llm_instance = AzureChatOpenAI(
        azure_endpoint=settings.azure_openai_endpoint,
        api_key=settings.azure_openai_api_key,
        api_version=settings.azure_openai_api_version,
        azure_deployment=settings.azure_openai_deployment,
        temperature=0.1,
    )

    return {
        "llm": {
            "model_instance": llm_instance,
            "model_tokens": 128000,
        },
        "verbose": False,
        "headless": True,
    }


# ── Direct-LLM extraction (replaces ScrapeGraphAI for items/detail) ──────


async def smart_extract_items(
    html: str,
    fields: list[str],
    *,
    max_items: int | None = None,
) -> list[dict[str, str | None]]:
    """Extract listing items from raw HTML using a direct LLM call.

    Uses the project's own ``chat_completion_json`` (async Azure OpenAI)
    instead of ScrapeGraphAI, avoiding all threading / timeout issues.

    Returns a list of dicts with field names as keys, matching the format
    produced by ``ScraperAgent._extract_items()``.

    Args:
        max_items: If set, instruct the LLM to extract only this many items
                   (used during preview to avoid processing the whole page).
    """
    from app.utils.llm import chat_completion_json

    if not html or len(html) < 500:
        return []

    # Truncate oversized HTML
    if len(html) > _MAX_HTML_CHARS:
        log.warning("LLM extraction: truncating HTML from %d to %d chars", len(html), _MAX_HTML_CHARS)
        html = html[:_MAX_HTML_CHARS]

    fields_str = ", ".join(fields)
    if max_items and max_items > 0:
        item_instruction = f"Extract only the first {max_items} seller/company/exhibitor entry from this HTML."
    else:
        item_instruction = "Extract ALL seller/company/exhibitor entries from this HTML."

    prompt = (
        f"{item_instruction}\n"
        f"For each entry, extract these fields: {fields_str}.\n"
        f"Return a JSON object with a key \"items\" containing a list of objects.\n"
        f"Each object should have the field names as keys and the extracted text as values.\n"
        f"If a field is not found for an entry, set it to null.\n"
        f"For any URL fields (like detail_link, logo, logo_url), return the full or relative URL as-is from the HTML."
    )

    log.info(
        "LLM extraction: extracting items (fields: %s, html_size: %d chars)",
        fields_str, len(html),
    )
    try:
        result = await chat_completion_json(
            [
                {"role": "system", "content": "You are a precise data extraction assistant. Extract structured data from HTML."},
                {"role": "user", "content": f"{prompt}\n\n--- HTML ---\n{html}"},
            ],
            temperature=0.1,
        )
    except Exception as exc:
        log.warning("LLM item extraction failed: %s", exc)
        return []

    if result is None:
        return []

    # Normalise result into list[dict]
    items: list[dict[str, str | None]] = []
    raw_items: list = []

    if isinstance(result, list):
        raw_items = result
    elif isinstance(result, dict):
        for key in ("items", "data", "results", "records", "entries"):
            if key in result and isinstance(result[key], list):
                raw_items = result[key]
                break
        if not raw_items and not any(isinstance(v, list) for v in result.values()):
            raw_items = [result]

    for raw in raw_items:
        if isinstance(raw, dict):
            item = {k: str(v) if v is not None else None for k, v in raw.items()}
            items.append(item)

    log.info("LLM extraction: extracted %d items", len(items))
    return items


async def smart_extract_detail(
    html: str,
    fields: list[str],
) -> dict[str, str]:
    """Extract structured data from a single detail page using a direct LLM call.

    Returns a dict of field_name -> value, suitable for enrichment in the
    parser pipeline.
    """
    from app.utils.llm import chat_completion_json

    if not html or len(html) < 200:
        return {}

    if len(html) > _MAX_HTML_CHARS:
        log.warning("LLM detail extraction: truncating HTML from %d to %d chars", len(html), _MAX_HTML_CHARS)
        html = html[:_MAX_HTML_CHARS]

    fields_str = ", ".join(fields)
    prompt = (
        f"Extract the following information about this seller/company from the page: "
        f"{fields_str}.\n"
        f"Also extract any contact information (email, phone, address, website) "
        f"and social media links you can find.\n"
        f"Return a flat JSON object with field names as keys."
    )

    log.info("LLM detail extraction: extracting fields (html_size: %d chars)", len(html))
    try:
        result = await chat_completion_json(
            [
                {"role": "system", "content": "You are a precise data extraction assistant. Extract structured data from HTML."},
                {"role": "user", "content": f"{prompt}\n\n--- HTML ---\n{html}"},
            ],
            temperature=0.1,
        )
    except Exception as exc:
        log.warning("LLM detail extraction failed: %s", exc)
        return {}

    if not isinstance(result, dict):
        return {}

    # Flatten to string values
    detail: dict[str, str] = {}
    for k, v in result.items():
        if v is not None:
            detail[k] = str(v) if not isinstance(v, str) else v

    log.info("LLM detail extraction: extracted %d fields", len(detail))
    return detail


# ── Markdown-based LLM extraction (used when Crawl4AI provides markdown) ──


async def smart_extract_items_from_markdown(
    markdown: str,
    fields: list[str],
    *,
    max_items: int | None = None,
) -> list[dict[str, str | None]]:
    """Extract listing items from clean markdown using a direct LLM call.

    Identical to ``smart_extract_items`` but operates on Crawl4AI's
    markdown output instead of raw HTML.  The clean markdown dramatically
    reduces token usage and avoids truncation on large pages.
    """
    from app.utils.llm import chat_completion_json

    # Markdown is denser than HTML (no tags), so threshold is lower than
    # HTML equivalent (500 chars).  200 chars ensures enough content for
    # at least one meaningful listing entry.
    if not markdown or len(markdown) < 200:
        return []

    fields_str = ", ".join(fields)
    if max_items and max_items > 0:
        item_instruction = f"Extract only the first {max_items} seller/company/exhibitor entry from this content."
    else:
        item_instruction = "Extract ALL seller/company/exhibitor entries from this content."

    prompt = (
        f"{item_instruction}\n"
        f"For each entry, extract these fields: {fields_str}.\n"
        f"Return a JSON object with a key \"items\" containing a list of objects.\n"
        f"Each object should have the field names as keys and the extracted text as values.\n"
        f"If a field is not found for an entry, set it to null.\n"
        f"For any URL fields (like detail_link, logo, logo_url), return the full or relative URL as-is."
    )

    log.info(
        "LLM markdown extraction: extracting items (fields: %s, markdown_size: %d chars)",
        fields_str, len(markdown),
    )
    try:
        result = await chat_completion_json(
            [
                {"role": "system", "content": "You are a precise data extraction assistant. Extract structured data from the provided content."},
                {"role": "user", "content": f"{prompt}\n\n--- CONTENT ---\n{markdown}"},
            ],
            temperature=0.1,
        )
    except Exception as exc:
        log.warning("LLM markdown item extraction failed: %s", exc)
        return []

    if result is None:
        return []

    items: list[dict[str, str | None]] = []
    raw_items: list = []

    if isinstance(result, list):
        raw_items = result
    elif isinstance(result, dict):
        for key in ("items", "data", "results", "records", "entries"):
            if key in result and isinstance(result[key], list):
                raw_items = result[key]
                break
        if not raw_items and not any(isinstance(v, list) for v in result.values()):
            raw_items = [result]

    for raw in raw_items:
        if isinstance(raw, dict):
            item = {k: str(v) if v is not None else None for k, v in raw.items()}
            items.append(item)

    log.info("LLM markdown extraction: extracted %d items", len(items))
    return items


async def smart_extract_detail_from_markdown(
    markdown: str,
    fields: list[str],
) -> dict[str, str]:
    """Extract structured data from a detail page's markdown using a direct LLM call.

    Identical to ``smart_extract_detail`` but operates on clean markdown
    instead of raw HTML.
    """
    from app.utils.llm import chat_completion_json

    # Detail pages need less content than listings, but 50 chars is too
    # sparse for meaningful field extraction.
    if not markdown or len(markdown) < 100:
        return {}

    fields_str = ", ".join(fields)
    prompt = (
        f"Extract the following information about this seller/company from the content: "
        f"{fields_str}.\n"
        f"Also extract any contact information (email, phone, address, website) "
        f"and social media links you can find.\n"
        f"Return a flat JSON object with field names as keys."
    )

    log.info("LLM markdown detail extraction: extracting fields (markdown_size: %d chars)", len(markdown))
    try:
        result = await chat_completion_json(
            [
                {"role": "system", "content": "You are a precise data extraction assistant. Extract structured data from the provided content."},
                {"role": "user", "content": f"{prompt}\n\n--- CONTENT ---\n{markdown}"},
            ],
            temperature=0.1,
        )
    except Exception as exc:
        log.warning("LLM markdown detail extraction failed: %s", exc)
        return {}

    if not isinstance(result, dict):
        return {}

    detail: dict[str, str] = {}
    for k, v in result.items():
        if v is not None:
            detail[k] = str(v) if not isinstance(v, str) else v

    log.info("LLM markdown detail extraction: extracted %d fields", len(detail))
    return detail


# ---------------------------------------------------------------------------
# SmartScraperMultiGraph — scrape multiple URLs in one shot
# ---------------------------------------------------------------------------
def _run_smart_scraper_multi(
    prompt: str, urls: list[str],
) -> dict | list | str:
    """Run SmartScraperMultiGraph synchronously."""
    from scrapegraphai.graphs import SmartScraperMultiGraph

    graph = SmartScraperMultiGraph(
        prompt=prompt,
        source=urls,
        config=_build_graph_config(),
    )
    return graph.run()


async def smart_scrape_multi(
    urls: list[str],
    prompt: str,
) -> dict | list | str:
    """Scrape multiple URLs with a single prompt and merge results.

    Uses SmartScraperMultiGraph which runs SmartScraperGraph on each URL
    then merges the answers with an LLM.
    """
    log.info("SmartScraperMultiGraph: scraping %d URLs", len(urls))
    try:
        result = await asyncio.wait_for(
            asyncio.to_thread(_run_smart_scraper_multi, prompt, urls),
            timeout=300,
        )
    except asyncio.TimeoutError:
        log.error("SmartScraperMultiGraph timed out after 300s")
        raise TimeoutError("SmartScraperMultiGraph timed out after 300s")
    except Exception as exc:
        log.error("SmartScraperMultiGraph failed: %s", exc)
        raise
    log.info("SmartScraperMultiGraph completed")
    return result


# ---------------------------------------------------------------------------
# ScriptCreatorGraph — generate a Python scraping script
# ---------------------------------------------------------------------------
def _build_script_config(library: str) -> dict:
    """Build graph_config with the required 'library' key for ScriptCreator."""
    config = _build_graph_config().copy()
    config["library"] = library
    return config


def _run_script_creator(prompt: str, url: str, library: str) -> str:
    """Run ScriptCreatorGraph synchronously."""
    from scrapegraphai.graphs import ScriptCreatorGraph

    graph = ScriptCreatorGraph(
        prompt=prompt,
        source=url,
        config=_build_script_config(library),
    )
    return graph.run()


async def generate_scraper_script(
    url: str,
    prompt: str,
    library: str = "beautifulsoup4",
) -> str:
    """Generate a Python scraping script for a single URL.

    The generated script uses the specified library (beautifulsoup4, scrapy, etc.)
    to extract data described by the prompt.
    """
    log.info("ScriptCreatorGraph: generating script for %s (library=%s)", url, library)
    try:
        result = await asyncio.wait_for(
            asyncio.to_thread(_run_script_creator, prompt, url, library),
            timeout=300,
        )
    except asyncio.TimeoutError:
        log.error("ScriptCreatorGraph timed out after 300s")
        raise TimeoutError("ScriptCreatorGraph timed out after 300s")
    except Exception as exc:
        log.error("ScriptCreatorGraph failed: %s", exc)
        raise
    log.info("ScriptCreatorGraph: script generated (%d chars)", len(result))
    return result


# ---------------------------------------------------------------------------
# ScriptCreatorMultiGraph — generate a merged script for multiple URLs
# ---------------------------------------------------------------------------
def _run_script_creator_multi(
    prompt: str, urls: list[str], library: str,
) -> str:
    """Run ScriptCreatorMultiGraph synchronously."""
    from scrapegraphai.graphs import ScriptCreatorMultiGraph

    graph = ScriptCreatorMultiGraph(
        prompt=prompt,
        source=urls,
        config=_build_script_config(library),
    )
    return graph.run()


async def generate_scraper_script_multi(
    urls: list[str],
    prompt: str,
    library: str = "beautifulsoup4",
) -> str:
    """Generate a merged Python scraping script for multiple URLs.

    Runs ScriptCreatorGraph on each URL then merges the scripts with an LLM.
    """
    log.info(
        "ScriptCreatorMultiGraph: generating script for %d URLs (library=%s)",
        len(urls), library,
    )
    try:
        result = await asyncio.wait_for(
            asyncio.to_thread(_run_script_creator_multi, prompt, urls, library),
            timeout=300,
        )
    except asyncio.TimeoutError:
        log.error("ScriptCreatorMultiGraph timed out after 300s")
        raise TimeoutError("ScriptCreatorMultiGraph timed out after 300s")
    except Exception as exc:
        log.error("ScriptCreatorMultiGraph failed: %s", exc)
        raise
    log.info("ScriptCreatorMultiGraph: merged script generated (%d chars)", len(result))
    return result
