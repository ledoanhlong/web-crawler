"""ParserAgent — Normalizes raw scraped data into structured ExhibitorRecords.

Uses GPT to:
1. Map messy, site-specific field names to our canonical schema.
2. Split combined fields (e.g. "Berlin, Germany" → city + country).
3. Clean up whitespace, encoding artefacts, and HTML entities.
4. Merge data from listing + detail pages when available.
"""

from __future__ import annotations

import json

from app.models.schemas import ExhibitorRecord, PageData, ScrapingPlan
from app.prompts import load_prompt
from app.utils.html import simplify_html
from app.utils.llm import chat_completion_json
from app.utils.logging import get_logger

log = get_logger(__name__)

# Max items to send per LLM call to stay within context limits
_BATCH_SIZE = 40


class ParserAgent:
    """Normalize raw page data into ExhibitorRecords."""

    async def parse(
        self,
        page_data_list: list[PageData],
        plan: ScrapingPlan,
    ) -> list[ExhibitorRecord]:
        # Flatten all items from all pages
        all_items: list[dict[str, str | None]] = []
        detail_htmls: dict[str, str] = {}
        detail_sub_pages: dict[str, dict[str, str]] = {}
        detail_api_responses: dict[str, dict] = {}
        for pd in page_data_list:
            all_items.extend(pd.items)
            detail_htmls.update(pd.detail_pages)
            detail_sub_pages.update(pd.detail_sub_pages)
            detail_api_responses.update(pd.detail_api_responses)

        log.info("Parsing %d raw items", len(all_items))

        if not all_items:
            return []

        # Parse detail pages into text snippets for enrichment.
        # Always process detail pages when they exist (fall back to simplified HTML
        # if no specific selectors are available).
        detail_texts: dict[str, str] = {}
        if detail_htmls:
            for url, html in detail_htmls.items():
                fields_text = self._extract_detail_fields(html, plan)
                # Append sub-page data if available
                sub_pages = detail_sub_pages.get(url, {})
                if sub_pages:
                    sub_parts: list[str] = []
                    for label, sub_html in sub_pages.items():
                        sub_text = simplify_html(sub_html, max_chars=2_000)
                        sub_parts.append(f"\n--- {label} page ---\n{sub_text}")
                    fields_text += "\n".join(sub_parts)
                detail_texts[url] = fields_text

        records: list[ExhibitorRecord] = []
        for batch_start in range(0, len(all_items), _BATCH_SIZE):
            batch = all_items[batch_start : batch_start + _BATCH_SIZE]
            batch_records = await self._parse_batch(batch, detail_texts, detail_api_responses, plan.url)
            records.extend(batch_records)

        log.info("Parsed %d exhibitor records total", len(records))
        return records

    async def _parse_batch(
        self,
        items: list[dict[str, str | None]],
        detail_texts: dict[str, str],
        detail_api_responses: dict[str, dict],
        source_url: str,
    ) -> list[ExhibitorRecord]:
        # Build context for the LLM
        enriched_items = []
        for item in items:
            entry: dict[str, str | None] = {**item}
            # If we have HTML detail page data, attach it
            detail_link = item.get("detail_link")
            if detail_link and detail_link in detail_texts:
                entry["_detail_page_data"] = detail_texts[detail_link]
            # If we have API detail JSON data, attach it
            item_id = item.get("_detail_api_id")
            if item_id and item_id in detail_api_responses:
                entry["_detail_api_data"] = json.dumps(
                    detail_api_responses[item_id], ensure_ascii=False, indent=2
                )
            enriched_items.append(entry)

        messages = [
            {"role": "system", "content": load_prompt("parser")},
            {
                "role": "user",
                "content": (
                    f"Source URL: {source_url}\n\n"
                    f"Raw scraped data ({len(enriched_items)} items):\n"
                    f"```json\n{json.dumps(enriched_items, ensure_ascii=False, indent=2)}\n```"
                ),
            },
        ]

        result = await chat_completion_json(messages, max_tokens=16_000)

        raw_records = result.get("records", [])
        records: list[ExhibitorRecord] = []
        for raw in raw_records:
            raw["source_url"] = source_url
            try:
                records.append(ExhibitorRecord.model_validate(raw))
            except Exception as exc:
                log.warning("Failed to validate record: %s — %s", raw.get("name", "?"), exc)
        return records

    @staticmethod
    def _extract_detail_fields(html: str, plan: ScrapingPlan) -> str:
        """Extract fields from a detail page HTML using the plan's selectors."""
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "lxml")
        parts: list[str] = []
        for field, selector in plan.detail_page_fields.items():
            el = soup.select_one(selector)
            if el:
                attr = plan.detail_page_field_attributes.get(field)
                val = el.get(attr) if attr else el.get_text(separator=" ", strip=True)
                parts.append(f"{field}: {val}")
        return "\n".join(parts) if parts else simplify_html(html, max_chars=4_000)
