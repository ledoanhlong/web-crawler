"""ScrapeGraphAI wrappers — SmartScraper, MultiGraph, and ScriptCreator.

Uses the same Azure OpenAI credentials as the rest of the pipeline.
SmartScraperGraph accepts raw HTML as source (no extra HTTP requests).
Multi and ScriptCreator graphs accept URLs and fetch pages themselves.
"""

from __future__ import annotations

import asyncio
from functools import lru_cache

from app.config import settings
from app.utils.logging import get_logger

log = get_logger(__name__)


@lru_cache(maxsize=1)
def _build_graph_config() -> dict:
    """Build a ScrapeGraphAI graph_config using our Azure OpenAI credentials."""
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


def _run_smart_scraper(prompt: str, html: str) -> dict | list | None:
    """Run SmartScraperGraph synchronously (it uses LangChain internally)."""
    from scrapegraphai.graphs import SmartScraperGraph

    graph = SmartScraperGraph(
        prompt=prompt,
        source=html,
        config=_build_graph_config(),
    )
    return graph.run()


async def smart_extract_items(
    html: str,
    fields: list[str],
    source_url: str = "",
) -> list[dict[str, str | None]]:
    """Extract listing items from raw HTML using SmartScraperGraph.

    Returns a list of dicts with field names as keys, matching the format
    produced by ScraperAgent._extract_items().
    """
    if not html or len(html) < 500:
        return []

    fields_str = ", ".join(fields)
    prompt = (
        f"Extract ALL seller/company entries from this page. "
        f"For each entry, extract these fields: {fields_str}. "
        f"Return a JSON object with a key 'items' containing a list of objects. "
        f"Each object should have the field names as keys and the extracted text as values. "
        f"If a field is not found for an entry, set it to null."
    )

    log.info("SmartScraperGraph fallback: extracting items (fields: %s)", fields_str)
    try:
        result = await asyncio.to_thread(_run_smart_scraper, prompt, html)
    except Exception as exc:
        log.warning("SmartScraperGraph item extraction failed: %s", exc)
        return []

    if result is None:
        return []

    # Normalise result into list[dict]
    items: list[dict[str, str | None]] = []
    raw_items: list = []

    if isinstance(result, list):
        raw_items = result
    elif isinstance(result, dict):
        # Try common wrapper keys
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

    log.info("SmartScraperGraph extracted %d items", len(items))
    return items


async def smart_extract_detail(
    html: str,
    fields: list[str],
    source_url: str = "",
) -> dict[str, str]:
    """Extract structured data from a single detail page using SmartScraperGraph.

    Returns a dict of field_name -> value, suitable for enrichment in the
    parser pipeline.
    """
    if not html or len(html) < 200:
        return {}

    fields_str = ", ".join(fields)
    prompt = (
        f"Extract the following information about this seller/company from the page: "
        f"{fields_str}. "
        f"Also extract any contact information (email, phone, address, website) "
        f"and social media links you can find. "
        f"Return a flat JSON object with field names as keys."
    )

    log.info("SmartScraperGraph: extracting detail page fields")
    try:
        result = await asyncio.to_thread(_run_smart_scraper, prompt, html)
    except Exception as exc:
        log.warning("SmartScraperGraph detail extraction failed: %s", exc)
        return {}

    if not isinstance(result, dict):
        return {}

    # Flatten to string values
    detail: dict[str, str] = {}
    for k, v in result.items():
        if v is not None:
            detail[k] = str(v) if not isinstance(v, str) else v

    log.info("SmartScraperGraph extracted %d detail fields", len(detail))
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
        result = await asyncio.to_thread(_run_smart_scraper_multi, prompt, urls)
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
        result = await asyncio.to_thread(_run_script_creator, prompt, url, library)
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
        result = await asyncio.to_thread(_run_script_creator_multi, prompt, urls, library)
    except Exception as exc:
        log.error("ScriptCreatorMultiGraph failed: %s", exc)
        raise
    log.info("ScriptCreatorMultiGraph: merged script generated (%d chars)", len(result))
    return result
