"""Tests for parser agent CSS extraction."""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, patch

import app.agents.parser_agent as parser_agent_module
from app.agents.parser_agent import ParserAgent
from app.models.schemas import DetailPagePlan, ScrapingPlan, ScrapingTarget, PaginationStrategy


# ---------------------------------------------------------------------------
# Helper to build a minimal plan with detail_page_plan selectors
# ---------------------------------------------------------------------------

def _make_plan(
    *,
    detail_page_selectors: dict[str, str] | None = None,
    detail_page_attributes: dict[str, str] | None = None,
    legacy_detail_fields: dict[str, str] | None = None,
    legacy_detail_attrs: dict[str, str] | None = None,
) -> ScrapingPlan:
    detail_page_plan = None
    if detail_page_selectors:
        detail_page_plan = DetailPagePlan(
            field_selectors=detail_page_selectors,
            field_attributes=detail_page_attributes or {},
        )
    return ScrapingPlan(
        url="https://example.com",
        requires_javascript=False,
        pagination=PaginationStrategy.NONE,
        target=ScrapingTarget(
            item_container_selector=".item",
            field_selectors={"name": "h3"},
        ),
        detail_page_plan=detail_page_plan,
        detail_page_fields=legacy_detail_fields or {},
        detail_page_field_attributes=legacy_detail_attrs or {},
    )


DETAIL_HTML = """
<html>
<body>
    <h1 class="company-name">Acme Corp</h1>
    <div class="contact-info">
        <a href="mailto:info@acme.com" class="email">info@acme.com</a>
        <span class="phone">+49 30 1234567</span>
        <a href="https://acme.com" class="website" target="_blank">Visit website</a>
    </div>
    <div class="description">
        <p>Leading manufacturer of widgets and tools.</p>
    </div>
    <div class="address">
        <span class="street">123 Main St</span>
        <span class="city">Berlin</span>
        <span class="country">Germany</span>
    </div>
</body>
</html>
"""


class TestExtractDetailFieldsCss:
    """Test ParserAgent._extract_detail_fields_css."""

    def test_basic_text_extraction(self):
        plan = _make_plan(
            detail_page_selectors={
                "name": "h1.company-name",
                "phone": ".phone",
                "city": ".city",
            },
        )
        result = ParserAgent._extract_detail_fields_css(DETAIL_HTML, plan)
        assert "name: Acme Corp" in result
        assert "phone: +49 30 1234567" in result
        assert "city: Berlin" in result

    def test_attribute_extraction(self):
        plan = _make_plan(
            detail_page_selectors={
                "email": "a.email",
                "website_url": "a.website",
            },
            detail_page_attributes={
                "email": "href",
                "website_url": "href",
            },
        )
        result = ParserAgent._extract_detail_fields_css(DETAIL_HTML, plan)
        assert "email: mailto:info@acme.com" in result
        assert "website_url: https://acme.com" in result

    def test_missing_selector_returns_empty(self):
        plan = _make_plan(
            detail_page_selectors={
                "fax": ".fax-number",  # doesn't exist in HTML
            },
        )
        result = ParserAgent._extract_detail_fields_css(DETAIL_HTML, plan)
        assert result == ""

    def test_empty_selector_skipped(self):
        plan = _make_plan(
            detail_page_selectors={
                "name": "h1.company-name",
                "bad": "",
                "also_bad": "   ",
            },
        )
        result = ParserAgent._extract_detail_fields_css(DETAIL_HTML, plan)
        assert "name: Acme Corp" in result
        assert "bad" not in result
        assert "also_bad" not in result

    def test_legacy_fallback(self):
        """When detail_page_plan is None, fall back to legacy detail_page_fields."""
        plan = _make_plan(
            detail_page_selectors=None,
            legacy_detail_fields={
                "name": "h1.company-name",
                "country": ".country",
            },
        )
        result = ParserAgent._extract_detail_fields_css(DETAIL_HTML, plan)
        assert "name: Acme Corp" in result
        assert "country: Germany" in result

    def test_legacy_attribute_fallback(self):
        plan = _make_plan(
            detail_page_selectors=None,
            legacy_detail_fields={"email": "a.email"},
            legacy_detail_attrs={"email": "href"},
        )
        result = ParserAgent._extract_detail_fields_css(DETAIL_HTML, plan)
        assert "email: mailto:info@acme.com" in result

    def test_detail_page_plan_preferred_over_legacy(self):
        """detail_page_plan should win when both are present."""
        plan = _make_plan(
            detail_page_selectors={"name": "h1.company-name"},
            legacy_detail_fields={"name": ".city"},
        )
        result = ParserAgent._extract_detail_fields_css(DETAIL_HTML, plan)
        # Should extract from h1 (detail_page_plan), not from .city (legacy)
        assert "name: Acme Corp" in result

    def test_no_selectors_returns_empty(self):
        plan = _make_plan()
        result = ParserAgent._extract_detail_fields_css(DETAIL_HTML, plan)
        assert result == ""

    def test_invalid_selector_handled(self):
        plan = _make_plan(
            detail_page_selectors={
                "name": "h1.company-name",
                "broken": "[[[invalid",
            },
        )
        # Should not raise, just skip the broken selector
        result = ParserAgent._extract_detail_fields_css(DETAIL_HTML, plan)
        assert "name: Acme Corp" in result

    def test_multiple_fields(self):
        plan = _make_plan(
            detail_page_selectors={
                "name": "h1.company-name",
                "phone": ".phone",
                "city": ".city",
                "country": ".country",
                "description": ".description p",
            },
        )
        result = ParserAgent._extract_detail_fields_css(DETAIL_HTML, plan)
        lines = result.strip().split("\n")
        assert len(lines) == 5
        assert any("Leading manufacturer" in line for line in lines)


class TestDetailFieldHeuristics:
    def test_detail_page_plan_fields_are_preferred_for_ai(self):
        plan = _make_plan(
            detail_page_selectors={
                "email": "a.email",
                "phone": ".phone",
            },
            legacy_detail_fields={
                "meta_description": "meta[name='description']",
            },
        )

        fields = ParserAgent._detail_fields_for_ai(plan)

        assert fields == ["email", "phone"]

    def test_junk_detail_fields_fall_back_to_generic_fields(self):
        plan = _make_plan(
            detail_page_selectors={
                "meta_description": "meta[name='description']",
                "map_version": ".map-version",
                "cookie_banner_text": ".cookie-banner",
            },
        )

        fields = ParserAgent._detail_fields_for_ai(plan)

        assert "email" in fields
        assert "website" in fields
        assert "description" in fields

    def test_build_enriched_items_promotes_flattened_structured_fields(self):
        parser = ParserAgent()
        items = [
            {
                "name": "Acme Corp",
                "values.description": "Structured description",
                "contact.email": "info@acme.com",
                "contact.phone": "+49 30 1234567",
                "website_url": "https://acme.com",
            }
        ]

        enriched = parser._build_enriched_items(items, {}, {})

        assert enriched[0]["description"] == "Structured description"
        assert enriched[0]["email"] == "info@acme.com"
        assert enriched[0]["phone"] == "+49 30 1234567"
        assert enriched[0]["website"] == "https://acme.com"


class TestParserProviderFallback:
    @pytest.mark.asyncio
    async def test_parser_uses_claude_as_fallback_when_openai_fails(self):
        messages = [{"role": "user", "content": "parse this"}]

        with (
            patch.object(
                parser_agent_module,
                "openai_chat_completion_json",
                new_callable=AsyncMock,
                side_effect=RuntimeError("openai unavailable"),
            ) as mock_openai,
            patch.object(
                parser_agent_module,
                "chat_completion_claude_json",
                new_callable=AsyncMock,
                return_value={"records": [{"name": "Acme Corp"}]},
            ) as mock_claude,
            patch.object(parser_agent_module.settings, "use_claude_extraction", True),
        ):
            result = await parser_agent_module.chat_completion_json(messages, max_tokens=123)

        assert result["records"][0]["name"] == "Acme Corp"
        mock_openai.assert_awaited_once()
        mock_claude.assert_awaited_once()
