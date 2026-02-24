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
from app.utils.html import simplify_html
from app.utils.llm import chat_completion_json
from app.utils.logging import get_logger

log = get_logger(__name__)

SYSTEM_PROMPT = """\
You are a data-parsing specialist. You receive raw scraped exhibitor / seller \
data from trade fair and marketplace websites. Your job is to normalize each \
record into a consistent JSON structure.

For each exhibitor, return a JSON object with these fields (use null when \
the data is not available):

{
  "name": "<string>",
  "booth_or_stand": "<string or null>",
  "country": "<string or null>",
  "city": "<string or null>",
  "address": "<string or null>",
  "postal_code": "<string or null>",
  "website": "<URL string or null>",
  "email": "<string or null>",
  "phone": "<string or null>",
  "fax": "<string or null>",
  "description": "<string or null>",
  "product_categories": ["<string>", ...],
  "brands": ["<string>", ...],
  "hall": "<string or null>",
  "logo_url": "<URL string or null>",
  "social_media": {"<platform>": "<url>", ...},
  "raw_extra": {"<key>": "<value>", ...}
}

Rules:
- ``name`` is required. If you cannot determine a company name, use the most \
  prominent text as the name.
- Normalise country names to English (e.g. "Deutschland" → "Germany").
- If a field contains a combined location like "Berlin, Germany", split it \
  into ``city`` and ``country``.
- Put any leftover fields that don't map to the schema into ``raw_extra``.
- Clean up HTML entities (&amp; → &), excessive whitespace, and encoding \
  artefacts.
- If detail-page data is provided for a record, merge it with the listing \
  data (detail data takes priority when both have the same field).
- Return a JSON object: {"records": [<list of exhibitor objects>]}
- Return ONLY valid JSON. No markdown fences, no explanation.
"""

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
        for pd in page_data_list:
            all_items.extend(pd.items)
            detail_htmls.update(pd.detail_pages)

        log.info("Parsing %d raw items", len(all_items))

        if not all_items:
            return []

        # Parse detail pages into text snippets for enrichment
        detail_texts: dict[str, str] = {}
        if detail_htmls and plan.detail_page_fields:
            for url, html in detail_htmls.items():
                detail_texts[url] = self._extract_detail_fields(html, plan)

        records: list[ExhibitorRecord] = []
        for batch_start in range(0, len(all_items), _BATCH_SIZE):
            batch = all_items[batch_start : batch_start + _BATCH_SIZE]
            batch_records = await self._parse_batch(batch, detail_texts, plan.url)
            records.extend(batch_records)

        log.info("Parsed %d exhibitor records total", len(records))
        return records

    async def _parse_batch(
        self,
        items: list[dict[str, str | None]],
        detail_texts: dict[str, str],
        source_url: str,
    ) -> list[ExhibitorRecord]:
        # Build context for the LLM
        enriched_items = []
        for item in items:
            entry: dict[str, str | None] = {**item}
            # If we have detail page data, attach it
            detail_link = item.get("detail_link")
            if detail_link and detail_link in detail_texts:
                entry["_detail_page_data"] = detail_texts[detail_link]
            enriched_items.append(entry)

        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
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
