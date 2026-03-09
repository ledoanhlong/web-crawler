"""ParserAgent — Normalizes raw scraped data into structured SellerLead records.

Uses GPT to:
1. Map messy, site-specific field names to our canonical schema.
2. Split combined fields (e.g. "Berlin, Germany" → city + country).
3. Clean up whitespace, encoding artefacts, and HTML entities.
4. Merge data from listing + detail pages when available.
"""

from __future__ import annotations

import json

from app.config import settings
from app.models.schemas import SellerLead, PageData, ScrapingPlan
from app.prompts import load_prompt
from app.utils.html import simplify_html
from app.utils.llm import chat_completion_json
from app.utils.logging import get_logger

log = get_logger(__name__)

# Target max characters of JSON sent per LLM batch call.
# With ~4 chars/token and a 128k-token context, this leaves plenty of room.
_MAX_BATCH_CHARS = 60_000

# Fields from detail API responses that carry no schema value (metadata, flags,
# timestamps, etc.).  Stripped before sending to the LLM to save tokens.
# Only include keys that are universally noise — never domain-specific content.
_API_SKIP_KEYS = frozenset({
    "lastModified", "createdAt", "updatedAt", "modifiedAt",
    "write", "_links", "_embedded", "__typename",
})

# Max characters kept for description / free-text fields in the API response
_MAX_TEXT_CHARS = 1500


def _compact_api_response(data: dict) -> dict:
    """Strip metadata and truncate long fields from a detail API response.

    Reduces token usage by removing clearly irrelevant keys and truncating
    free-text fields, while keeping everything the parser needs to fill in
    the canonical schema (address, phone, email, website, description, etc.).
    """
    compact = {k: v for k, v in data.items() if k not in _API_SKIP_KEYS}

    # Truncate long free-text / HTML description
    for key in ("text", "description", "about", "bio", "summary"):
        if key in compact and isinstance(compact[key], str):
            compact[key] = compact[key][:_MAX_TEXT_CHARS]

    # Generically truncate large nested lists to save tokens
    for key, val in list(compact.items()):
        if isinstance(val, list) and len(val) > 20:
            compact[key] = val[:20]

    return compact


class ParserAgent:
    """Normalize raw page data into SellerLead records."""

    async def parse(
        self,
        page_data_list: list[PageData],
        plan: ScrapingPlan,
    ) -> list[SellerLead]:
        # Flatten all items from all pages
        all_items: list[dict[str, str | None]] = []
        detail_htmls: dict[str, str] = {}
        detail_sub_pages: dict[str, dict[str, str]] = {}
        detail_api_responses: dict[str, dict] = {}
        merged_structured_data: dict = {}
        for pd in page_data_list:
            all_items.extend(pd.items)
            detail_htmls.update(pd.detail_pages)
            detail_sub_pages.update(pd.detail_sub_pages)
            detail_api_responses.update(pd.detail_api_responses)
            # Merge structured data from all pages (first non-empty wins per key)
            if pd.structured_data:
                for key in ("json_ld", "open_graph", "microdata"):
                    val = pd.structured_data.get(key)
                    if val and key not in merged_structured_data:
                        merged_structured_data[key] = val

        log.info("Parsing %d raw items", len(all_items))

        if not all_items:
            return []

        # Parse detail pages into text snippets for enrichment.
        detail_texts: dict[str, str] = {}
        if detail_htmls:
            for url, html in detail_htmls.items():
                fields_text = await self._extract_detail_fields(html, plan, url=url)
                sub_pages = detail_sub_pages.get(url, {})
                if sub_pages:
                    sub_parts: list[str] = []
                    for label, sub_html in sub_pages.items():
                        sub_text = simplify_html(sub_html, max_chars=2_000)
                        sub_parts.append(f"\n--- {label} page ---\n{sub_text}")
                    fields_text += "\n".join(sub_parts)
                detail_texts[url] = fields_text

        # Build enriched items and split into size-aware batches
        enriched_items = self._build_enriched_items(
            all_items, detail_texts, detail_api_responses, merged_structured_data,
        )
        batches = self._split_into_batches(enriched_items)
        log.info("Split %d items into %d batch(es) for parsing", len(all_items), len(batches))

        records: list[SellerLead] = []
        for batch in batches:
            batch_records = await self._parse_batch(batch, source_url=plan.url)
            records.extend(batch_records)

        log.info("Parsed %d seller lead records total", len(records))
        return records

    def _build_enriched_items(
        self,
        items: list[dict],
        detail_texts: dict[str, str],
        detail_api_responses: dict[str, dict],
        structured_data: dict | None = None,
    ) -> list[dict]:
        """Attach detail data (HTML text or compact API JSON) to each item.

        Also attaches page-level structured data (JSON-LD, Open Graph, Microdata)
        to every item so the parser LLM can use it for enrichment.
        """
        enriched = []
        matched_detail = 0
        matched_api = 0
        for item in items:
            entry: dict = {**item}
            detail_link = item.get("detail_link")
            if detail_link and detail_link in detail_texts:
                entry["_detail_page_data"] = detail_texts[detail_link]
                matched_detail += 1
            item_id = item.get("_detail_api_id")
            if item_id and item_id in detail_api_responses:
                compact = _compact_api_response(detail_api_responses[item_id])
                entry["_detail_api_data"] = json.dumps(compact, ensure_ascii=False)
                matched_api += 1
            # Attach page-level structured data (compact form) so LLM has extra signals
            if structured_data:
                sd_compact = json.dumps(structured_data, ensure_ascii=False)
                # Only attach if it's reasonably sized (< 3 000 chars) to not blow up tokens
                if len(sd_compact) < 3_000:
                    entry["_structured_data"] = sd_compact
            enriched.append(entry)
        log.info(
            "Enrichment matching: %d/%d items got detail page data, %d/%d got API data "
            "(detail_texts has %d keys)",
            matched_detail, len(items), matched_api, len(items), len(detail_texts),
        )
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
    ) -> list[SellerLead]:
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

        # Use higher max_tokens when items carry detail page data (more output needed)
        has_detail = any("_detail_page_data" in item or "_detail_api_data" in item for item in enriched_items)
        max_tokens = 32_000 if has_detail else 16_000

        try:
            result = await chat_completion_json(messages, max_tokens=max_tokens)
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
        records: list[SellerLead] = []
        for raw in raw_records:
            raw["source_url"] = source_url
            try:
                records.append(SellerLead.model_validate(raw))
            except Exception as exc:
                log.warning("Failed to validate record: %s — %s", raw.get("name", "?"), exc)
        return records

    @staticmethod
    def _extract_detail_fields_css(html: str, plan: ScrapingPlan) -> str:
        """Extract fields from a detail page HTML using the plan's CSS selectors.

        Uses detail_page_plan.field_selectors (preferred) or falls back to the
        legacy plan.detail_page_fields for backward compatibility.
        """
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "lxml")
        parts: list[str] = []

        # Prefer detail_page_plan (new-style), fall back to legacy fields
        field_selectors = {}
        field_attributes = {}
        if plan.detail_page_plan and plan.detail_page_plan.field_selectors:
            field_selectors = plan.detail_page_plan.field_selectors
            field_attributes = plan.detail_page_plan.field_attributes
        elif plan.detail_page_fields:
            field_selectors = plan.detail_page_fields
            field_attributes = plan.detail_page_field_attributes or {}

        for field, selector in field_selectors.items():
            if not selector or not selector.strip():
                continue
            try:
                el = soup.select_one(selector)
                if el:
                    attr = field_attributes.get(field)
                    val = el.get(attr) if attr else el.get_text(separator=" ", strip=True)
                    parts.append(f"{field}: {val}")
            except Exception as exc:
                log.debug("CSS selector '%s' failed for field '%s': %s", selector, field, exc)
        return "\n".join(parts)

    @staticmethod
    async def _extract_detail_fields_smart(html: str, plan: ScrapingPlan) -> str:
        """Extract fields from a detail page using LLM-based extraction."""
        from app.utils.smart_scraper import smart_extract_detail

        # Use plan fields if available, otherwise use a broad generic set
        fields = list(plan.detail_page_fields.keys()) if plan.detail_page_fields else [
            "name", "country", "city", "address", "postal_code",
            "email", "phone", "website", "description",
            "product_categories", "brands", "logo_url",
            "store_url", "social_media",
            "industry",
        ]
        detail = await smart_extract_detail(html, fields)
        if detail:
            return "\n".join(f"{k}: {v}" for k, v in detail.items() if v)
        return ""

    async def _extract_detail_fields(self, html: str, plan: ScrapingPlan, *, url: str | None = None) -> str:
        """Extract fields from detail page — universal-scraper / SmartScraperGraph / CSS."""
        # universal-scraper extraction (requires URL, handles its own fetching)
        if url and settings.use_universal_scraper and settings.use_universal_scraper_for_extraction:
            from app.utils.universal_scraper import universal_scraper_extract_detail

            fields = list(plan.detail_page_fields.keys()) if plan.detail_page_fields else [
                "name", "country", "city", "address", "postal_code",
                "email", "phone", "website", "description",
                "product_categories", "brands", "logo_url",
                "store_url", "social_media",
                "industry",
            ]
            us_result = await universal_scraper_extract_detail(url, fields=fields)
            if us_result:
                result_str = "\n".join(f"{k}: {v}" for k, v in us_result.items() if v)
                if result_str:
                    log.info("universal-scraper extracted detail fields for %s", url)
                    return result_str
            log.warning("universal-scraper detail extraction empty — falling back to SmartScraper")

        # Primary: SmartScraperGraph
        if settings.use_smart_scraper_primary:
            smart_result = await self._extract_detail_fields_smart(html, plan)
            if smart_result:
                log.info("SmartScraperGraph extracted detail fields (primary)")
                return smart_result
            log.warning("SmartScraperGraph detail extraction empty — falling back to CSS")

        # Backup: CSS selectors
        css_result = self._extract_detail_fields_css(html, plan)
        if css_result:
            return css_result

        return simplify_html(html, max_chars=4_000)
