"""RouterAgent — intelligently selects the best scraping strategy.

Analyses the URL(s) and user prompt, then delegates to the most
appropriate scraping method (full pipeline, SmartScraper, ScriptCreator,
or SmartScraperMulti).
"""

from __future__ import annotations

import re

from app.models.schemas import RoutingDecision, ScrapingStrategy
from app.prompts import load_prompt
from app.utils.llm import chat_completion_json
from app.utils.logging import get_logger

log = get_logger(__name__)

# URL path/query patterns that strongly indicate a paginated listing page.
_LISTING_URL_RE = re.compile(
    r"(?:"
    r"/(?:directory|exhibitors?|sellers?|suppliers?|brands?|list(?:ing)?s?|members?|companies|catalogue|catalog|vendors?|partners?|companies)"
    r"|[?&](?:page|p|start|offset|pageNumber|skip)=\d*"
    r")",
    re.IGNORECASE,
)

# Prompt keywords that strongly signal a script is wanted.
_SCRIPT_PROMPT_RE = re.compile(r"\b(?:script|code|reusable|generate)\b", re.IGNORECASE)


def _heuristic_route(
    urls: list[str],
    prompt: str,
    fields_wanted: str | None,
    detail_page_url: str | None,
) -> ScrapingStrategy | None:
    """Fast rule-based pre-filter — returns a strategy when the answer is obvious.

    Returns None when the decision is ambiguous and should be delegated to the LLM.
    """
    # Multiple URLs → always multi-scrape (no LLM needed)
    if len(urls) > 1:
        return ScrapingStrategy.SMART_SCRAPER_MULTI

    # User explicitly wants a script
    if _SCRIPT_PROMPT_RE.search(prompt):
        return ScrapingStrategy.SCRIPT_CREATOR

    # URL looks like a paginated listing OR user provided a detail page URL
    # (two-level structure almost always needs the full pipeline)
    if _LISTING_URL_RE.search(urls[0]) or detail_page_url:
        return ScrapingStrategy.FULL_PIPELINE

    return None  # ambiguous — let the LLM decide


class RouterAgent:
    """Decides which scraping strategy to use for a given request."""

    async def route(
        self,
        urls: list[str],
        prompt: str,
        *,
        fields_wanted: str | None = None,
        detail_page_url: str | None = None,
    ) -> RoutingDecision:
        """Analyse the request and return a RoutingDecision."""
        log.info(
            "RouterAgent: routing %d URL(s), prompt=%r, fields_wanted=%r",
            len(urls),
            prompt,
            fields_wanted,
        )

        # --- Fast heuristic pre-filter (no LLM cost) ---
        heuristic = _heuristic_route(urls, prompt, fields_wanted, detail_page_url)
        if heuristic is not None:
            log.info("RouterAgent heuristic decision: %s", heuristic.value)
            return RoutingDecision(
                strategy=heuristic,
                explanation="Heuristic routing based on URL pattern or request structure.",
            )

        # --- LLM routing for ambiguous cases ---
        template = load_prompt("router")
        filled = template.format(
            urls="\n".join(urls),
            prompt=prompt,
            fields_wanted=fields_wanted or "not specified",
            has_detail_url="yes" if detail_page_url else "no",
            url_count=len(urls),
        )

        try:
            result = await chat_completion_json(
                messages=[{"role": "user", "content": filled}],
                temperature=0.1,
                max_tokens=500,  # routing response is tiny (~50 tokens)
            )

            strategy_raw = result.get("strategy", "smart_scraper")
            explanation = result.get("explanation", "")

            # Validate strategy value
            try:
                strategy = ScrapingStrategy(strategy_raw)
            except ValueError:
                log.warning("LLM returned unknown strategy %r, defaulting", strategy_raw)
                strategy = self._fallback_route(urls, fields_wanted, detail_page_url)
                explanation = f"LLM returned unknown strategy '{strategy_raw}', using fallback."

        except Exception as exc:
            log.warning("RouterAgent LLM call failed: %s — using fallback", exc)
            strategy = self._fallback_route(urls, fields_wanted, detail_page_url)
            explanation = f"LLM routing failed ({exc}), used rule-based fallback."

        log.info("RouterAgent decision: %s — %s", strategy.value, explanation)
        return RoutingDecision(strategy=strategy, explanation=explanation)

    @staticmethod
    def _fallback_route(
        urls: list[str],
        fields_wanted: str | None,
        detail_page_url: str | None,
    ) -> ScrapingStrategy:
        """Rule-based fallback when LLM routing fails."""
        if len(urls) > 1:
            return ScrapingStrategy.SMART_SCRAPER_MULTI
        # detail_page_url means a two-level site structure → full pipeline.
        # fields_wanted alone does NOT imply pagination; a single-page extraction
        # with specific fields is still best served by smart_scraper.
        if detail_page_url:
            return ScrapingStrategy.FULL_PIPELINE
        if _LISTING_URL_RE.search(urls[0]):
            return ScrapingStrategy.FULL_PIPELINE
        return ScrapingStrategy.SMART_SCRAPER
