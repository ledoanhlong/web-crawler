"""RouterAgent — intelligently selects the best scraping strategy.

Analyses the URL(s) and user prompt, then delegates to the most
appropriate scraping method (full pipeline, SmartScraper, ScriptCreator,
or SmartScraperMulti).
"""

from __future__ import annotations

from app.models.schemas import RoutingDecision, ScrapingStrategy
from app.prompts import load_prompt
from app.utils.llm import chat_completion_json
from app.utils.logging import get_logger

log = get_logger(__name__)


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
        # Build the prompt from template
        template = load_prompt("router")
        filled = template.format(
            urls="\n".join(urls),
            prompt=prompt,
            fields_wanted=fields_wanted or "not specified",
            has_detail_url="yes" if detail_page_url else "no",
            url_count=len(urls),
        )

        log.info(
            "RouterAgent: routing %d URL(s), prompt=%r, fields_wanted=%r",
            len(urls),
            prompt,
            fields_wanted,
        )

        try:
            result = await chat_completion_json(
                messages=[{"role": "user", "content": filled}],
                temperature=0.1,
                max_tokens=500,
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
        if detail_page_url or fields_wanted:
            return ScrapingStrategy.FULL_PIPELINE
        return ScrapingStrategy.SMART_SCRAPER
