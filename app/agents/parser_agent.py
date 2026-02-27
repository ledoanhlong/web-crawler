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

# Target max characters of JSON sent per LLM batch call.
# With ~4 chars/token and a 128k-token context, this leaves plenty of room.
_MAX_BATCH_CHARS = 60_000

# Fields from detail API responses that carry no schema value (metadata, flags,
# internal IDs, binary-like data, etc.).  Stripped before sending to the LLM.
_API_SKIP_KEYS = frozenset({
    "type", "id", "exhSeoId", "write", "tagsAreEditable", "xtagsAreEditable",
    "exhIsLead", "logoUploadedByPubimport", "premium", "eventMap", "areaMap",
    "areaId", "locationIsVirtual", "lastModified", "events", "getInTouchEmail",
    "isGetInTouchAllowed", "media", "pdfs", "tol", "synonyms", "tags", "xtags",
})

# Max characters kept for description / free-text fields in the API response
_MAX_TEXT_CHARS = 800


def _compact_api_response(data: dict) -> dict:
    """Strip metadata and truncate long fields from a detail API response.

    Reduces token usage by removing clearly irrelevant keys and truncating
    free-text fields, while keeping everything the parser needs to fill in
    the canonical schema (address, phone, email, website, description, etc.).
    """
    compact = {k: v for k, v in data.items() if k not in _API_SKIP_KEYS}

    # Truncate long free-text / HTML description
    for key in ("text", "description"):
        if key in compact and isinstance(compact[key], str):
            compact[key] = compact[key][:_MAX_TEXT_CHARS]

    # Trim product lists inside categories — keep labels only, max 10 products
    if "categories" in compact and isinstance(compact["categories"], list):
        compact["categories"] = [
            {
                "label": c.get("label"),
                "products": [p.get("label") for p in c.get("productList", [])[:10]],
            }
            for c in compact["categories"]
            if isinstance(c, dict)
        ]

    return compact


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
        detail_texts: dict[str, str] = {}
        if detail_htmls:
            for url, html in detail_htmls.items():
                fields_text = self._extract_detail_fields(html, plan)
                sub_pages = detail_sub_pages.get(url, {})
                if sub_pages:
                    sub_parts: list[str] = []
                    for label, sub_html in sub_pages.items():
                        sub_text = simplify_html(sub_html, max_chars=2_000)
                        sub_parts.append(f"\n--- {label} page ---\n{sub_text}")
                    fields_text += "\n".join(sub_parts)
                detail_texts[url] = fields_text

        # Build enriched items and split into size-aware batches
        enriched_items = self._build_enriched_items(all_items, detail_texts, detail_api_responses)
        batches = self._split_into_batches(enriched_items)
        log.info("Split %d items into %d batch(es) for parsing", len(all_items), len(batches))

        records: list[ExhibitorRecord] = []
        for batch in batches:
            batch_records = await self._parse_batch(batch, source_url=plan.url)
            records.extend(batch_records)

        log.info("Parsed %d exhibitor records total", len(records))
        return records

    def _build_enriched_items(
        self,
        items: list[dict],
        detail_texts: dict[str, str],
        detail_api_responses: dict[str, dict],
    ) -> list[dict]:
        """Attach detail data (HTML text or compact API JSON) to each item."""
        enriched = []
        for item in items:
            entry: dict = {**item}
            detail_link = item.get("detail_link")
            if detail_link and detail_link in detail_texts:
                entry["_detail_page_data"] = detail_texts[detail_link]
            item_id = item.get("_detail_api_id")
            if item_id and item_id in detail_api_responses:
                compact = _compact_api_response(detail_api_responses[item_id])
                entry["_detail_api_data"] = json.dumps(compact, ensure_ascii=False)
            enriched.append(entry)
        return enriched

    def _split_into_batches(self, enriched_items: list[dict]) -> list[list[dict]]:
        """Split items into batches that stay within _MAX_BATCH_CHARS."""
        batches: list[list[dict]] = []
        current_batch: list[dict] = []
        current_chars = 0

        for item in enriched_items:
            item_chars = len(json.dumps(item, ensure_ascii=False))
            if current_batch and current_chars + item_chars > _MAX_BATCH_CHARS:
                batches.append(current_batch)
                current_batch = []
                current_chars = 0
            current_batch.append(item)
            current_chars += item_chars

        if current_batch:
            batches.append(current_batch)

        return batches

    async def _parse_batch(
        self,
        enriched_items: list[dict],
        *,
        source_url: str,
    ) -> list[ExhibitorRecord]:
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

        try:
            result = await chat_completion_json(messages, max_tokens=16_000)
        except Exception as exc:
            log.error("LLM parse batch failed (%d items): %s", len(enriched_items), exc)
            # If batch has more than one item, try splitting it in half
            if len(enriched_items) > 1:
                log.info("Retrying as two smaller batches")
                mid = len(enriched_items) // 2
                left = await self._parse_batch(enriched_items[:mid], source_url=source_url)
                right = await self._parse_batch(enriched_items[mid:], source_url=source_url)
                return left + right
            return []

        raw_records = result.get("records", []) if isinstance(result, dict) else []
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
