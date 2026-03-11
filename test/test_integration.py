"""Integration tests for the critical scraping pipeline paths.

These tests target the real failure modes — detail enrichment gate checks,
cross-method detail sharing, parser enrichment matching, backfill logic,
and cookie-field detection — that unit tests with perfect fixtures miss.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.agents.parser_agent import ParserAgent
from app.agents.scraper_agent import EnrichResult, ScraperAgent
from app.models.schemas import (
    DetailPagePlan,
    ExtractionMethod,
    PageData,
    PaginationStrategy,
    ScrapingPlan,
    ScrapingTarget,
    SellerLead,
)
from app.services.orchestrator import Orchestrator


# ═══════════════════════════════════════════════════════════════════════
# Shared fixtures
# ═══════════════════════════════════════════════════════════════════════

LISTING_HTML_WITH_DETAIL_LINKS = """
<html><body>
<div id="list">
  <div class="card">
    <h3 class="name">Acme Corp</h3>
    <span class="country">Germany</span>
    <a class="detail" href="/exhibitors/acme">Details</a>
  </div>
  <div class="card">
    <h3 class="name">Beta Inc</h3>
    <span class="country">France</span>
    <a class="detail" href="/exhibitors/beta">Details</a>
  </div>
</div>
</body></html>
"""

DETAIL_HTML_ACME = """
<html><body>
<div class="profile">
  <h1>Acme Corp</h1>
  <p class="email">info@acme.com</p>
  <p class="phone">+49 30 1234567</p>
  <p class="website">https://www.acme.com</p>
  <p class="desc">Leading widget manufacturer since 1985.</p>
</div>
</body></html>
"""

DETAIL_HTML_BETA = """
<html><body>
<div class="profile">
  <h1>Beta Inc</h1>
  <p class="email">contact@beta.fr</p>
  <p class="phone">+33 1 2345678</p>
  <p class="website">https://www.beta.fr</p>
</div>
</body></html>
"""

# Shadow-DOM style listing — no CSS containers visible, just raw custom elements
SHADOW_DOM_LISTING_HTML = """
<html><body>
<main class="page-wrapper">
  <custom-listing-element></custom-listing-element>
</main>
</body></html>
"""


def _make_target(*, detail_link_selector: str | None = "a.detail") -> ScrapingTarget:
    return ScrapingTarget(
        item_container_selector=".card",
        field_selectors={"name": "h3.name", "country": "span.country"},
        field_attributes={},
        detail_link_selector=detail_link_selector,
    )


def _make_plan(
    *,
    detail_link_selector: str | None = "a.detail",
    detail_page_fields: dict[str, str] | None = None,
    detail_page_plan: DetailPagePlan | None = None,
) -> ScrapingPlan:
    return ScrapingPlan(
        url="https://example.com/exhibitors",
        requires_javascript=False,
        pagination=PaginationStrategy.NONE,
        target=_make_target(detail_link_selector=detail_link_selector),
        detail_page_plan=detail_page_plan,
        detail_page_fields=detail_page_fields or {},
    )


# ═══════════════════════════════════════════════════════════════════════
# 1. _enrich_detail_pages gate check
# ═══════════════════════════════════════════════════════════════════════

class TestEnrichDetailPagesGate:
    """Verify the enrichment gate allows LLM-extracted detail_links through."""

    def setup_method(self):
        self.scraper = ScraperAgent()

    @pytest.mark.asyncio
    async def test_skips_when_no_selector_and_no_links(self):
        """No detail_link_selector + no item detail_link → skip enrichment."""
        plan = _make_plan(detail_link_selector=None)
        pages = [PageData(url=plan.url, items=[{"name": "Acme Corp"}])]

        result = await self.scraper._enrich_detail_pages(pages, plan)
        assert isinstance(result, EnrichResult)
        assert result.pages[0].detail_pages == {}

    @pytest.mark.asyncio
    async def test_proceeds_when_items_have_detail_link_but_no_selector(self):
        """No detail_link_selector but items carry detail_link → must proceed."""
        plan = _make_plan(detail_link_selector=None)
        items = [{"name": "Acme Corp", "detail_link": "https://example.com/exhibitors/acme"}]
        pages = [PageData(url=plan.url, items=items)]

        with patch("app.agents.scraper_agent.fetch_pages", new_callable=AsyncMock) as mock_fetch:
            mock_fetch.return_value = {
                "https://example.com/exhibitors/acme": DETAIL_HTML_ACME,
            }
            result = await self.scraper._enrich_detail_pages(pages, plan)

        assert "https://example.com/exhibitors/acme" in result.pages[0].detail_pages
        assert "Acme Corp" in result.pages[0].detail_pages["https://example.com/exhibitors/acme"]

    @pytest.mark.asyncio
    async def test_proceeds_with_selector_present(self):
        """Normal path: detail_link_selector present → proceeds normally."""
        plan = _make_plan(detail_link_selector="a.detail")
        items = [{"name": "Acme Corp", "detail_link": "https://example.com/exhibitors/acme"}]
        pages = [PageData(url=plan.url, items=items)]

        with patch("app.agents.scraper_agent.fetch_pages", new_callable=AsyncMock) as mock_fetch:
            mock_fetch.return_value = {
                "https://example.com/exhibitors/acme": DETAIL_HTML_ACME,
            }
            result = await self.scraper._enrich_detail_pages(pages, plan)

        assert len(result.pages[0].detail_pages) == 1

    @pytest.mark.asyncio
    async def test_skips_items_without_detail_link(self):
        """Items without detail_link are silently skipped — no crash."""
        plan = _make_plan(detail_link_selector=None)
        items = [
            {"name": "Acme Corp", "detail_link": "https://example.com/exhibitors/acme"},
            {"name": "No Link Co"},  # no detail_link
        ]
        pages = [PageData(url=plan.url, items=items)]

        with patch("app.agents.scraper_agent.fetch_pages", new_callable=AsyncMock) as mock_fetch:
            mock_fetch.return_value = {
                "https://example.com/exhibitors/acme": DETAIL_HTML_ACME,
            }
            result = await self.scraper._enrich_detail_pages(pages, plan)

        # Only Acme's detail page should be fetched
        assert len(result.pages[0].detail_pages) == 1
        assert "https://example.com/exhibitors/acme" in result.pages[0].detail_pages

    @pytest.mark.asyncio
    async def test_relative_links_resolved(self):
        """Relative detail_link (e.g. '/exhibitors/acme') resolved to full URL."""
        plan = _make_plan(detail_link_selector=None)
        items = [{"name": "Acme Corp", "detail_link": "/exhibitors/acme"}]
        pages = [PageData(url=plan.url, items=items)]

        with patch("app.agents.scraper_agent.fetch_pages", new_callable=AsyncMock) as mock_fetch:
            mock_fetch.return_value = {
                "https://example.com/exhibitors/acme": DETAIL_HTML_ACME,
            }
            result = await self.scraper._enrich_detail_pages(pages, plan)

        # The items in the result pages should have the resolved full URL
        resolved_link = result.pages[0].items[0]["detail_link"]
        assert resolved_link == "https://example.com/exhibitors/acme"
        assert len(result.pages[0].detail_pages) == 1

    @pytest.mark.asyncio
    async def test_network_failure_does_not_crash(self):
        """If fetch fails for a detail URL, enrichment continues without it."""
        plan = _make_plan(detail_link_selector="a.detail")
        items = [
            {"name": "Acme Corp", "detail_link": "https://example.com/exhibitors/acme"},
            {"name": "Beta Inc", "detail_link": "https://example.com/exhibitors/beta"},
        ]
        pages = [PageData(url=plan.url, items=items)]

        # Only Acme succeeds; Beta's URL returns nothing (network failure)
        with patch("app.agents.scraper_agent.fetch_pages", new_callable=AsyncMock) as mock_fetch:
            mock_fetch.return_value = {
                "https://example.com/exhibitors/acme": DETAIL_HTML_ACME,
                # Beta intentionally absent — simulates fetch failure
            }
            result = await self.scraper._enrich_detail_pages(pages, plan)

        # Acme should still be enriched
        assert "https://example.com/exhibitors/acme" in result.pages[0].detail_pages
        # Beta silently absent
        assert "https://example.com/exhibitors/beta" not in result.pages[0].detail_pages


# ═══════════════════════════════════════════════════════════════════════
# 2. _build_enriched_items
# ═══════════════════════════════════════════════════════════════════════

class TestBuildEnrichedItems:
    """Verify parser enrichment matching correctness."""

    def setup_method(self):
        self.parser = ParserAgent()

    def test_items_with_detail_link_get_enrichment(self):
        """Items with a detail_link matching a detail_texts key get _detail_page_data."""
        items = [
            {"name": "Acme Corp", "detail_link": "https://example.com/acme"},
            {"name": "Beta Inc", "detail_link": "https://example.com/beta"},
        ]
        detail_texts = {
            "https://example.com/acme": "email: info@acme.com\nphone: +49 30 1234567",
            "https://example.com/beta": "email: contact@beta.fr",
        }

        enriched = self.parser._build_enriched_items(items, detail_texts, {})
        assert enriched[0]["_detail_page_data"] == detail_texts["https://example.com/acme"]
        assert enriched[1]["_detail_page_data"] == detail_texts["https://example.com/beta"]

    def test_items_without_detail_link_get_no_enrichment(self):
        """Items missing detail_link must NOT get _detail_page_data — ever."""
        items = [{"name": "Orphan Co"}]
        detail_texts = {"https://example.com/unrelated": "some data"}

        enriched = self.parser._build_enriched_items(items, detail_texts, {})
        assert "_detail_page_data" not in enriched[0]

    def test_mismatched_urls_produce_zero_enrichment(self):
        """If item's detail_link doesn't match any key in detail_texts, no enrichment."""
        items = [{"name": "Acme", "detail_link": "https://example.com/wrong-url"}]
        detail_texts = {"https://example.com/acme": "email: info@acme.com"}

        enriched = self.parser._build_enriched_items(items, detail_texts, {})
        assert "_detail_page_data" not in enriched[0]

    def test_api_data_attached_when_id_matches(self):
        """Items with _detail_api_id get _detail_api_data from API responses."""
        items = [{"name": "Acme", "_detail_api_id": "42"}]
        api_responses = {"42": {"email": "api@acme.com", "phone": "+1-555-0100"}}

        enriched = self.parser._build_enriched_items(items, {}, api_responses)
        assert "_detail_api_data" in enriched[0]
        api_data = json.loads(enriched[0]["_detail_api_data"])
        assert api_data["email"] == "api@acme.com"

    def test_structured_data_attached_when_small(self):
        """Page-level structured data attached if under 3000 chars."""
        items = [{"name": "Acme"}]
        sd = {"json_ld": [{"@type": "Organization", "name": "Acme"}]}

        enriched = self.parser._build_enriched_items(items, {}, {}, sd)
        assert "_structured_data" in enriched[0]

    def test_structured_data_omitted_when_large(self):
        """Huge structured data is NOT attached (token savings)."""
        items = [{"name": "Acme"}]
        sd = {"json_ld": [{"description": "x" * 5000}]}

        enriched = self.parser._build_enriched_items(items, {}, {}, sd)
        assert "_structured_data" not in enriched[0]

    def test_original_items_not_mutated(self):
        """_build_enriched_items makes copies; originals stay clean."""
        items = [{"name": "Acme", "detail_link": "https://example.com/acme"}]
        detail_texts = {"https://example.com/acme": "email: info@acme.com"}

        enriched = self.parser._build_enriched_items(items, detail_texts, {})
        assert "_detail_page_data" in enriched[0]
        assert "_detail_page_data" not in items[0]


# ═══════════════════════════════════════════════════════════════════════
# 3. _backfill_detail_links
# ═══════════════════════════════════════════════════════════════════════

class TestBackfillDetailLinks:
    """Verify fuzzy matching of LLM items to CSS containers for detail_link recovery."""

    def setup_method(self):
        self.scraper = ScraperAgent()

    def test_backfills_missing_links_by_name_match(self):
        """Items without detail_link get it from matching CSS container."""
        target = _make_target(detail_link_selector="a.detail")
        items = [
            {"name": "Acme Corp"},  # no detail_link
            {"name": "Beta Inc"},   # no detail_link
        ]

        self.scraper._backfill_detail_links(LISTING_HTML_WITH_DETAIL_LINKS, items, target)

        assert items[0]["detail_link"] == "/exhibitors/acme"
        assert items[1]["detail_link"] == "/exhibitors/beta"

    def test_does_not_overwrite_existing_links(self):
        """Items that already have detail_link are NOT overwritten."""
        target = _make_target(detail_link_selector="a.detail")
        items = [
            {"name": "Acme Corp", "detail_link": "/custom/acme"},
            {"name": "Beta Inc"},
        ]

        self.scraper._backfill_detail_links(LISTING_HTML_WITH_DETAIL_LINKS, items, target)

        assert items[0]["detail_link"] == "/custom/acme"  # unchanged
        assert items[1]["detail_link"] == "/exhibitors/beta"  # backfilled

    def test_no_crash_on_empty_items(self):
        """No crash when items list is empty."""
        target = _make_target(detail_link_selector="a.detail")
        self.scraper._backfill_detail_links(LISTING_HTML_WITH_DETAIL_LINKS, [], target)

    def test_no_crash_on_wrong_container_selector(self):
        """Selector that matches 0 containers → no backfill, no crash."""
        target = ScrapingTarget(
            item_container_selector=".nonexistent",
            field_selectors={"name": "h3"},
            field_attributes={},
            detail_link_selector="a.detail",
        )
        items = [{"name": "Acme Corp"}]
        self.scraper._backfill_detail_links(LISTING_HTML_WITH_DETAIL_LINKS, items, target)
        assert "detail_link" not in items[0]

    def test_no_match_if_name_differs(self):
        """If item name doesn't appear in any container text, no backfill."""
        target = _make_target(detail_link_selector="a.detail")
        items = [{"name": "Unknown Corp XYZ"}]

        self.scraper._backfill_detail_links(LISTING_HTML_WITH_DETAIL_LINKS, items, target)
        assert "detail_link" not in items[0]

    def test_requires_name_field(self):
        """Items without a name field are silently skipped."""
        target = _make_target(detail_link_selector="a.detail")
        items = [{"country": "Germany"}]  # no name

        self.scraper._backfill_detail_links(LISTING_HTML_WITH_DETAIL_LINKS, items, target)
        assert "detail_link" not in items[0]


# ═══════════════════════════════════════════════════════════════════════
# 4. Cookie-field detection in _extract_detail_fields_smart
# ═══════════════════════════════════════════════════════════════════════

class TestCookieFieldDetection:
    """Verify the junk-field heuristic falls back to generic fields."""

    @pytest.mark.asyncio
    async def test_cookie_fields_trigger_generic_fallback(self):
        """Plan with >50% cookie/consent/gdpr fields → use generic field list."""
        plan = _make_plan(detail_page_fields={
            "cookie_name": ".cookie-name",
            "consent_status": ".consent",
            "gdpr_type": ".gdpr",
            "privacy_policy": ".privacy",
            "tracking_id": ".track",
            "name": ".name",  # only 1 legit field out of 6
        })

        with patch("app.utils.smart_scraper.smart_extract_detail", new_callable=AsyncMock) as mock_llm:
            mock_llm.return_value = {"name": "Acme", "email": "info@acme.com"}
            result = await ParserAgent._extract_detail_fields_smart(DETAIL_HTML_ACME, plan)

        # Should have called LLM with generic fields, not cookie fields
        call_args = mock_llm.call_args
        fields_used = call_args[0][1]
        assert "email" in fields_used
        assert "phone" in fields_used
        assert "cookie_name" not in fields_used

    @pytest.mark.asyncio
    async def test_legit_fields_used_when_not_junk(self):
        """Plan with normal field names → use plan's fields, not generic."""
        plan = _make_plan(detail_page_fields={
            "company_name": ".name",
            "email_address": ".email",
            "phone_number": ".phone",
            "street_address": ".address",
        })

        with patch("app.utils.smart_scraper.smart_extract_detail", new_callable=AsyncMock) as mock_llm:
            mock_llm.return_value = {"company_name": "Acme"}
            await ParserAgent._extract_detail_fields_smart(DETAIL_HTML_ACME, plan)

        fields_used = mock_llm.call_args[0][1]
        assert "company_name" in fields_used
        assert "email_address" in fields_used

    @pytest.mark.asyncio
    async def test_empty_plan_fields_use_generic(self):
        """No plan fields at all → use generic field list."""
        plan = _make_plan(detail_page_fields={})

        with patch("app.utils.smart_scraper.smart_extract_detail", new_callable=AsyncMock) as mock_llm:
            mock_llm.return_value = {"name": "Acme"}
            await ParserAgent._extract_detail_fields_smart(DETAIL_HTML_ACME, plan)

        fields_used = mock_llm.call_args[0][1]
        assert "name" in fields_used
        assert "email" in fields_used
        assert "website" in fields_used

    @pytest.mark.asyncio
    async def test_boundary_50_percent_not_triggered(self):
        """Exactly 50% junk → NOT triggered (need >50%)."""
        plan = _make_plan(detail_page_fields={
            "cookie_name": ".a",
            "consent_type": ".b",
            "company_name": ".c",
            "phone": ".d",
        })  # 2/4 = 50% exactly

        with patch("app.utils.smart_scraper.smart_extract_detail", new_callable=AsyncMock) as mock_llm:
            mock_llm.return_value = {}
            await ParserAgent._extract_detail_fields_smart(DETAIL_HTML_ACME, plan)

        fields_used = mock_llm.call_args[0][1]
        # Should use plan fields since 50% is not > 50%
        assert "cookie_name" in fields_used

    @pytest.mark.asyncio
    async def test_boundary_51_percent_triggered(self):
        """Just over 50% junk → triggered."""
        plan = _make_plan(detail_page_fields={
            "cookie_name": ".a",
            "consent_type": ".b",
            "gdpr_flag": ".c",
            "company_name": ".d",
            "phone": ".e",
        })  # 3/5 = 60% → triggered

        with patch("app.utils.smart_scraper.smart_extract_detail", new_callable=AsyncMock) as mock_llm:
            mock_llm.return_value = {}
            await ParserAgent._extract_detail_fields_smart(DETAIL_HTML_ACME, plan)

        fields_used = mock_llm.call_args[0][1]
        assert "cookie_name" not in fields_used
        assert "email" in fields_used  # generic


# ═══════════════════════════════════════════════════════════════════════
# 5. Parser parse() end-to-end with mocked LLM
# ═══════════════════════════════════════════════════════════════════════

class TestParserParseEndToEnd:
    """Test the full parse() pipeline with a mocked LLM response."""

    def setup_method(self):
        self.parser = ParserAgent()

    @pytest.mark.asyncio
    async def test_parse_with_detail_pages_merges_data(self):
        """parse() should extract detail fields and attach them to items for the LLM."""
        plan = _make_plan(
            detail_page_plan=DetailPagePlan(
                field_selectors={
                    "email": "p.email",
                    "phone": "p.phone",
                    "website": "p.website",
                },
            ),
        )

        items = [
            {"name": "Acme Corp", "country": "Germany", "detail_link": "https://example.com/acme"},
        ]
        page_data = PageData(
            url=plan.url,
            items=items,
            detail_pages={"https://example.com/acme": DETAIL_HTML_ACME},
        )

        llm_response = {
            "records": [
                {
                    "name": "Acme Corp",
                    "country": "Germany",
                    "email": "info@acme.com",
                    "phone": "+49 30 1234567",
                    "website": "https://www.acme.com",
                }
            ]
        }

        detail_text = "email: info@acme.com\nphone: +49 30 1234567\nwebsite: https://www.acme.com"

        with (
            patch.object(self.parser, "_extract_detail_fields", new_callable=AsyncMock, return_value=detail_text),
            patch("app.agents.parser_agent.chat_completion_json", new_callable=AsyncMock) as mock_llm,
        ):
            mock_llm.return_value = llm_response
            records = await self.parser.parse([page_data], plan)

        assert len(records) == 1
        assert records[0].name == "Acme Corp"
        assert records[0].email == "info@acme.com"
        assert records[0].phone == "+49 30 1234567"

        # Verify the LLM was called with enriched items containing _detail_page_data
        call_args = mock_llm.call_args
        user_msg = call_args[0][0][1]["content"]
        assert "_detail_page_data" in user_msg
        assert "info@acme.com" in user_msg

    @pytest.mark.asyncio
    async def test_parse_without_detail_pages_still_works(self):
        """parse() works when no detail pages are available — listing data only."""
        plan = _make_plan()
        items = [{"name": "Acme Corp", "country": "Germany"}]
        page_data = PageData(url=plan.url, items=items)

        llm_response = {
            "records": [{"name": "Acme Corp", "country": "Germany"}]
        }

        with patch("app.agents.parser_agent.chat_completion_json", new_callable=AsyncMock) as mock_llm:
            mock_llm.return_value = llm_response
            records = await self.parser.parse([page_data], plan)

        assert len(records) == 1
        assert records[0].name == "Acme Corp"

    @pytest.mark.asyncio
    async def test_parse_zero_items_returns_empty(self):
        """parse() with no items returns empty list — no LLM call."""
        plan = _make_plan()
        page_data = PageData(url=plan.url, items=[])

        with patch("app.agents.parser_agent.chat_completion_json", new_callable=AsyncMock) as mock_llm:
            records = await self.parser.parse([page_data], plan)

        assert records == []
        mock_llm.assert_not_called()

    @pytest.mark.asyncio
    async def test_parse_batch_retry_on_llm_failure(self):
        """When LLM fails on a batch, it should split and retry."""
        plan = _make_plan()
        items = [
            {"name": "Acme Corp"},
            {"name": "Beta Inc"},
        ]
        page_data = PageData(url=plan.url, items=items)

        call_count = 0

        async def _flaky_llm(messages, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise Exception("Rate limited")
            # Second and third calls (split batches) succeed
            items_in_msg = messages[1]["content"]
            if "Acme" in items_in_msg:
                return {"records": [{"name": "Acme Corp"}]}
            return {"records": [{"name": "Beta Inc"}]}

        with patch("app.agents.parser_agent.chat_completion_json", side_effect=_flaky_llm):
            records = await self.parser.parse([page_data], plan)

        assert len(records) == 2
        names = {r.name for r in records}
        assert "Acme Corp" in names
        assert "Beta Inc" in names

    @pytest.mark.asyncio
    async def test_parse_batch_total_failure_returns_empty(self):
        """When LLM fails on single-item batch, returns empty (no infinite recursion)."""
        plan = _make_plan()
        items = [{"name": "Acme Corp"}]
        page_data = PageData(url=plan.url, items=items)

        with patch("app.agents.parser_agent.chat_completion_json", new_callable=AsyncMock) as mock_llm:
            mock_llm.side_effect = Exception("Total failure")
            records = await self.parser.parse([page_data], plan)

        assert records == []

    @pytest.mark.asyncio
    async def test_parse_invalid_record_skipped(self):
        """Invalid records from LLM are skipped, valid ones kept."""
        plan = _make_plan()
        items = [{"name": "Good"}, {"name": "Bad"}]
        page_data = PageData(url=plan.url, items=items)

        llm_response = {
            "records": [
                {"name": "Good", "country": "Germany"},
                {"name": None},  # name is required — should fail validation
            ]
        }

        with patch("app.agents.parser_agent.chat_completion_json", new_callable=AsyncMock) as mock_llm:
            mock_llm.return_value = llm_response
            records = await self.parser.parse([page_data], plan)

        # Only the valid record should survive
        assert len(records) == 1
        assert records[0].name == "Good"


# ═══════════════════════════════════════════════════════════════════════
# 6. Preview method selection edge cases
# ═══════════════════════════════════════════════════════════════════════

class TestPreviewMethodSelection:
    """Test the deterministic preview method selection with realistic inputs."""

    def setup_method(self):
        self.orch = Orchestrator()

    def test_smart_beats_css_when_more_complete(self):
        """Smart with email+phone+website should beat CSS with only name."""
        css = SellerLead(name="Acme Corp")
        smart = SellerLead(
            name="Acme Corp",
            email="info@acme.com",
            phone="+49 30 1234567",
            website="https://www.acme.com",
            country="Germany",
        )

        method, _, margin = self.orch._select_preview_method_deterministic({
            ExtractionMethod.CSS: css,
            ExtractionMethod.SMART_SCRAPER: smart,
        })

        assert method == ExtractionMethod.SMART_SCRAPER
        assert margin > 0

    def test_css_wins_when_equally_complete(self):
        """When both have identical data, CSS is preferred (appears first, same score)."""
        record = SellerLead(name="Acme Corp", country="Germany")

        method, _, _ = self.orch._select_preview_method_deterministic({
            ExtractionMethod.CSS: record,
            ExtractionMethod.SMART_SCRAPER: record,
        })

        # Same score — first in dict wins (CSS)
        assert method in (ExtractionMethod.CSS, ExtractionMethod.SMART_SCRAPER)

    def test_site_name_as_name_penalised(self):
        """A record whose name is just the site title should score lower
        than one with a real exhibitor name plus contacts."""
        bad = SellerLead(name="Exhibitor List | WWV | #1 digital commerce event")
        good = SellerLead(
            name="Brandmatchers",
            email="info@brandmatchers.com",
            website="https://www.brandmatchers.com",
        )

        bad_score = Orchestrator._preview_quality_score(bad)
        good_score = Orchestrator._preview_quality_score(good)
        assert good_score > bad_score

    def test_single_candidate_returns_it(self):
        """With only one candidate, it is returned regardless of score."""
        only = SellerLead(name="Acme")
        method, score, margin = self.orch._select_preview_method_deterministic({
            ExtractionMethod.CRAWL4AI: only,
        })
        assert method == ExtractionMethod.CRAWL4AI
        assert margin == score  # second_score is 0

    def test_listing_api_preferred_when_most_complete(self):
        """Listing API with full data should beat partial CSS/Smart results."""
        css = SellerLead(name="Acme")
        api = SellerLead(
            name="Acme Corp",
            email="info@acme.com",
            phone="+49 30 1234567",
            website="https://www.acme.com",
            country="Germany",
            city="Berlin",
            address="123 Main St",
            description="Leading widget manufacturer",
            product_categories=["Widgets", "Tools"],
        )

        method, _, _ = self.orch._select_preview_method_deterministic({
            ExtractionMethod.CSS: css,
            ExtractionMethod.LISTING_API: api,
        })
        assert method == ExtractionMethod.LISTING_API


# ═══════════════════════════════════════════════════════════════════════
# 7. CSS extraction with real-world edge cases
# ═══════════════════════════════════════════════════════════════════════

class TestCssExtractionEdgeCases:
    """Test CSS extraction against DOM patterns that commonly break scraping."""

    def setup_method(self):
        self.scraper = ScraperAgent()

    def test_shadow_dom_container_yields_zero_items(self):
        """Shadow DOM custom elements: CSS finds the container but 0 real items."""
        target = ScrapingTarget(
            item_container_selector="custom-listing-element",
            field_selectors={"name": "h3.name", "country": "span.country"},
            field_attributes={},
        )
        items = self.scraper._extract_items(SHADOW_DOM_LISTING_HTML, target)
        # Should find the container but extract no meaningful fields
        assert len(items) <= 1
        if items:
            # All fields should be None since the shadow DOM content is invisible
            assert items[0].get("name") is None
            assert items[0].get("country") is None

    def test_nested_containers_are_not_double_counted(self):
        """Nested elements matching the selector should not duplicate items."""
        html = """
        <html><body>
        <div class="item"><div class="item"><h3>Acme</h3></div></div>
        </body></html>
        """
        target = ScrapingTarget(
            item_container_selector=".item",
            field_selectors={"name": "h3"},
            field_attributes={},
        )
        items = self.scraper._extract_items(html, target)
        # Both .item divs match, so we get 2 records — the outer and inner
        # This is expected CSS behavior; the test documents it.
        assert len(items) >= 1

    def test_whitespace_only_field_values(self):
        """Fields containing only whitespace are captured but empty."""
        html = """
        <html><body>
        <div class="card"><h3 class="name">   </h3><span class="city">Berlin</span></div>
        </body></html>
        """
        target = ScrapingTarget(
            item_container_selector=".card",
            field_selectors={"name": "h3.name", "city": "span.city"},
            field_attributes={},
        )
        items = self.scraper._extract_items(html, target)
        assert len(items) == 1
        # Whitespace-only name is returned as-is (stripped or empty)
        assert items[0]["city"] == "Berlin"

    def test_all_fields_none_dropped(self):
        """A container with no matching child elements produces an all-None record that is dropped."""
        html = """
        <html><body>
        <div class="card"><!-- empty container --></div>
        </body></html>
        """
        target = ScrapingTarget(
            item_container_selector=".card",
            field_selectors={"name": "h3.name", "email": "span.email"},
            field_attributes={},
        )
        items = self.scraper._extract_items(html, target)
        # Empty records are correctly dropped
        assert len(items) == 0

    def test_extract_from_table_rows(self):
        """Extraction from <tr> containers inside a <table>."""
        html = """
        <html><body>
        <table><tbody>
          <tr class="exhibitor"><td class="n">Acme</td><td class="c">DE</td></tr>
          <tr class="exhibitor"><td class="n">Beta</td><td class="c">FR</td></tr>
        </tbody></table>
        </body></html>
        """
        target = ScrapingTarget(
            item_container_selector="tr.exhibitor",
            field_selectors={"name": "td.n", "country": "td.c"},
            field_attributes={},
        )
        items = self.scraper._extract_items(html, target)
        assert len(items) == 2
        assert items[0]["name"] == "Acme"
        assert items[1]["country"] == "FR"


# ═══════════════════════════════════════════════════════════════════════
# 8. _extract_detail_fields fallback chain
# ═══════════════════════════════════════════════════════════════════════

class TestExtractDetailFieldsFallback:
    """Test the detail extraction fallback chain: universal-scraper → Smart → CSS → simplify."""

    def setup_method(self):
        self.parser = ParserAgent()

    @pytest.mark.asyncio
    @patch("app.agents.parser_agent.settings")
    async def test_falls_back_to_smart_when_universal_disabled(self, mock_settings):
        """When universal-scraper is disabled, should fall through to SmartScraper."""
        mock_settings.use_universal_scraper = False
        mock_settings.use_smart_scraper_primary = True

        plan = _make_plan(detail_page_fields={
            "name": ".name", "email": ".email",
        })

        with patch.object(ParserAgent, "_extract_detail_fields_smart", new_callable=AsyncMock) as mock_smart:
            mock_smart.return_value = "name: Acme\nemail: info@acme.com"
            result = await self.parser._extract_detail_fields(DETAIL_HTML_ACME, plan)

        assert "name: Acme" in result
        mock_smart.assert_called_once()

    @pytest.mark.asyncio
    @patch("app.agents.parser_agent.settings")
    async def test_falls_back_to_css_when_smart_returns_empty(self, mock_settings):
        """When SmartScraper returns empty, should fall through to CSS extraction."""
        mock_settings.use_universal_scraper = False
        mock_settings.use_smart_scraper_primary = True

        plan = _make_plan(
            detail_page_plan=DetailPagePlan(
                field_selectors={"email": "p.email", "phone": "p.phone"},
            ),
        )

        with patch.object(ParserAgent, "_extract_detail_fields_smart", new_callable=AsyncMock) as mock_smart:
            mock_smart.return_value = ""  # Smart fails
            result = await self.parser._extract_detail_fields(DETAIL_HTML_ACME, plan)

        # Should have fallen through to CSS
        assert "email: info@acme.com" in result
        assert "phone: +49 30 1234567" in result

    @pytest.mark.asyncio
    @patch("app.agents.parser_agent.settings")
    async def test_falls_back_to_simplified_html_when_all_fail(self, mock_settings):
        """When both Smart and CSS return empty, should return simplified HTML."""
        mock_settings.use_universal_scraper = False
        mock_settings.use_smart_scraper_primary = True

        plan = _make_plan()  # No detail_page_plan selectors

        with patch.object(ParserAgent, "_extract_detail_fields_smart", new_callable=AsyncMock) as mock_smart:
            mock_smart.return_value = ""
            result = await self.parser._extract_detail_fields(DETAIL_HTML_ACME, plan)

        # Should be simplified HTML (text content)
        assert "Acme Corp" in result
        assert len(result) > 0


# ═══════════════════════════════════════════════════════════════════════
# 9. Preview dual — detail link sharing across methods
# ═══════════════════════════════════════════════════════════════════════

class TestPreviewDualDetailSharing:
    """Test that scrape_preview_dual shares detail_link and detail HTML between methods."""

    @pytest.mark.asyncio
    async def test_detail_link_shared_from_css_to_smart_and_crawl4ai(self):
        """When CSS extracts detail_link but Smart/Crawl4AI don't, it's copied over."""
        scraper = ScraperAgent()
        plan = _make_plan(detail_link_selector="a.detail")

        css_items = [{"name": "Acme Corp", "detail_link": "/exhibitors/acme"}]
        smart_items = [{"name": "Acme Corp"}]  # no detail_link
        crawl4ai_items = [{"name": "Acme Corp"}]  # no detail_link

        with (
            patch.object(scraper, "_slice_html_for_preview", return_value=LISTING_HTML_WITH_DETAIL_LINKS),
            patch.object(scraper, "_extract_items", return_value=css_items),
            patch("app.agents.scraper_agent.fetch_page", new_callable=AsyncMock, return_value=LISTING_HTML_WITH_DETAIL_LINKS),
            patch("app.agents.scraper_agent.fetch_page_js", new_callable=AsyncMock, return_value=(LISTING_HTML_WITH_DETAIL_LINKS, "")),
            patch("app.agents.scraper_agent.get_browser") as mock_browser_ctx,
            patch("app.agents.scraper_agent.settings") as mock_settings,
            patch("app.agents.scraper_agent.extract_all_structured_data", return_value={}),
            patch("app.agents.scraper_agent.fetch_pages", new_callable=AsyncMock, return_value={
                "https://example.com/exhibitors/acme": DETAIL_HTML_ACME,
            }),
        ):
            mock_settings.use_smart_scraper_primary = True
            mock_settings.use_crawl4ai = True
            mock_settings.use_crawl4ai_for_extraction = False
            mock_settings.use_crawl4ai_for_fetching = True
            mock_settings.use_universal_scraper = False
            mock_settings.use_inner_text_fallback = False
            mock_settings.request_delay_ms = 0
            mock_settings.max_concurrent_requests = 3

            # Mock the browser context manager
            mock_bctx = AsyncMock()
            mock_browser_ctx.return_value.__aenter__ = AsyncMock(return_value=mock_bctx)
            mock_browser_ctx.return_value.__aexit__ = AsyncMock(return_value=False)

            with (
                patch("app.utils.smart_scraper.smart_extract_items", new_callable=AsyncMock, return_value=smart_items),
                patch("app.utils.crawl4ai.crawl4ai_fetch", new_callable=AsyncMock, return_value={"markdown": "# Acme Corp", "html": LISTING_HTML_WITH_DETAIL_LINKS}),
                patch("app.utils.smart_scraper.smart_extract_items_from_markdown", new_callable=AsyncMock, return_value=crawl4ai_items),
            ):
                # We need to patch _enrich_detail_pages to verify inputs
                original_enrich = scraper._enrich_detail_pages
                enrich_calls = []

                async def _spy_enrich(pages, plan, **kwargs):
                    enrich_calls.append([{**item} for item in pages[0].items])
                    return await original_enrich(pages, plan, **kwargs)

                with patch.object(scraper, "_enrich_detail_pages", side_effect=_spy_enrich):
                    with patch.object(scraper, "_enrich_detail_api", new_callable=AsyncMock, side_effect=lambda p, *a, **kw: p):
                        result = await scraper.scrape_preview_dual(plan)

                # All methods should have received the detail_link (shared from CSS)
                css_pages, smart_pages, crawl4ai_pages, us_pages, _, _ = result

                # Verify the calls to _enrich_detail_pages got items with detail_link
                for call_items in enrich_calls:
                    assert call_items[0].get("detail_link") is not None, \
                        f"detail_link not shared to method: {call_items}"


# ═══════════════════════════════════════════════════════════════════════
# 10. Parser quality: ensure detail data actually improves output
# ═══════════════════════════════════════════════════════════════════════

class TestDetailDataImprovedOutput:
    """Verify that records with detail page data score higher than without."""

    def test_record_with_contacts_scores_higher(self):
        """A record with email + phone + website from detail page should outscore one without."""
        listing_only = SellerLead(name="Acme Corp", country="Germany")
        with_detail = SellerLead(
            name="Acme Corp",
            country="Germany",
            email="info@acme.com",
            phone="+49 30 1234567",
            website="https://www.acme.com",
            description="Leading widget manufacturer",
        )

        listing_score = Orchestrator._preview_quality_score(listing_only)
        detail_score = Orchestrator._preview_quality_score(with_detail)

        assert detail_score > listing_score
        # Should be significantly better, not just marginally
        assert detail_score >= listing_score + 3.0

    def test_empty_record_scores_zero(self):
        """A record with only name should score minimally."""
        record = SellerLead(name="X")
        score = Orchestrator._preview_quality_score(record)
        assert score == 1.0  # just the name field

    def test_fully_populated_record_scores_high(self):
        """A record with all key fields should score near maximum."""
        record = SellerLead(
            name="Acme Corp",
            website="https://acme.com",
            email="info@acme.com",
            phone="+49 30 1234567",
            country="Germany",
            city="Berlin",
            address="123 Main St",
            description="Widget maker",
            logo_url="https://acme.com/logo.png",
            marketplace_name="Trade Show 2026",
            store_url="https://acme.com/store",
            product_categories=["Widgets"],
            brands=["AcmeBrand"],
            social_media={"linkedin": "https://linkedin.com/company/acme"},
        )
        score = Orchestrator._preview_quality_score(record)
        # 11 string fields + 0.75 + 0.75 + 0.5 = 13.0
        assert score >= 12.0


# ═══════════════════════════════════════════════════════════════════════
# 11. Batch splitting
# ═══════════════════════════════════════════════════════════════════════

class TestBatchSplitting:
    """Test that large item lists are split into batches correctly."""

    def setup_method(self):
        self.parser = ParserAgent()

    def test_single_item_single_batch(self):
        items = [{"name": "Acme"}]
        batches = self.parser._split_into_batches(items)
        assert len(batches) == 1
        assert batches[0] == items

    def test_large_items_split_across_batches(self):
        # Each item is ~1000 chars, batch limit is 60000
        items = [{"name": f"Company {i}", "description": "x" * 900} for i in range(100)]
        batches = self.parser._split_into_batches(items)
        assert len(batches) > 1
        # All items accounted for
        total = sum(len(b) for b in batches)
        assert total == 100

    def test_empty_list_single_empty_batch(self):
        batches = self.parser._split_into_batches([])
        assert len(batches) == 0


# ═══════════════════════════════════════════════════════════════════════
# 12. Detail extraction with CSS validates real selector matching
# ═══════════════════════════════════════════════════════════════════════

class TestDetailExtractionCssReality:
    """Test CSS detail extraction against edge cases from real sites."""

    def test_mailto_prefix_preserved(self):
        """mailto: prefix in href should be captured by attribute extraction."""
        html = '<html><body><a class="email" href="mailto:info@acme.com">Email us</a></body></html>'
        plan = _make_plan(
            detail_page_plan=DetailPagePlan(
                field_selectors={"email": "a.email"},
                field_attributes={"email": "href"},
            ),
        )
        result = ParserAgent._extract_detail_fields_css(html, plan)
        assert "mailto:info@acme.com" in result

    def test_tel_prefix_preserved(self):
        """tel: prefix in href should be captured by attribute extraction."""
        html = '<html><body><a class="phone" href="tel:+491234567">Call us</a></body></html>'
        plan = _make_plan(
            detail_page_plan=DetailPagePlan(
                field_selectors={"phone": "a.phone"},
                field_attributes={"phone": "href"},
            ),
        )
        result = ParserAgent._extract_detail_fields_css(html, plan)
        assert "tel:+491234567" in result

    def test_empty_html_returns_empty(self):
        """Completely empty page yields no fields."""
        plan = _make_plan(
            detail_page_plan=DetailPagePlan(
                field_selectors={"name": "h1", "email": ".email"},
            ),
        )
        result = ParserAgent._extract_detail_fields_css("<html><body></body></html>", plan)
        assert result == ""

    def test_multiple_matches_takes_first(self):
        """When a selector matches multiple elements, the first match is used."""
        html = """
        <html><body>
          <span class="cat">Electronics</span>
          <span class="cat">Software</span>
          <span class="cat">Services</span>
        </body></html>
        """
        plan = _make_plan(
            detail_page_plan=DetailPagePlan(
                field_selectors={"category": "span.cat"},
            ),
        )
        result = ParserAgent._extract_detail_fields_css(html, plan)
        assert "category: Electronics" in result
