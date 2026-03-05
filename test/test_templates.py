"""Tests for template loading and hints retrieval."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.models.schemas import ScrapingTemplate, TemplateHints
from app.services.template_loader import (
    _load_all,
    get_hints_from_template,
    get_template,
    list_templates,
)


# Clear lru_cache before every test so template changes take effect.
@pytest.fixture(autouse=True)
def _clear_cache():
    _load_all.cache_clear()
    yield
    _load_all.cache_clear()


# ---------------------------------------------------------------------------
# Template directory discovery
# ---------------------------------------------------------------------------

class TestListTemplates:
    """Verify that templates load from disk."""

    def test_returns_list(self):
        result = list_templates()
        assert isinstance(result, list)

    def test_all_entries_are_templates(self):
        for tmpl in list_templates():
            assert isinstance(tmpl, ScrapingTemplate)

    def test_known_template_ids(self):
        ids = {t.id for t in list_templates()}
        # All three bundled pattern templates should be present
        assert "dynamic-directory-detail-pages" in ids
        assert "dynamic-directory-api" in ids
        assert "static-listing" in ids


class TestGetTemplate:
    """Test get_template by ID."""

    def test_existing_template(self):
        tmpl = get_template("dynamic-directory-detail-pages")
        assert tmpl is not None
        assert tmpl.id == "dynamic-directory-detail-pages"

    def test_nonexistent_template(self):
        assert get_template("does-not-exist") is None

    def test_template_has_hints(self):
        tmpl = get_template("dynamic-directory-api")
        assert tmpl is not None
        assert isinstance(tmpl.hints, TemplateHints)


class TestGetHintsFromTemplate:
    """Test hints retrieval from template."""

    def test_returns_hints(self):
        hints = get_hints_from_template("dynamic-directory-detail-pages")
        assert isinstance(hints, TemplateHints)

    def test_detail_pages_hints(self):
        hints = get_hints_from_template("dynamic-directory-detail-pages")
        assert hints.requires_javascript is True
        assert hints.has_detail_pages is True
        assert hints.has_detail_api is False
        assert hints.pagination == "alphabet_tabs"

    def test_api_hints(self):
        hints = get_hints_from_template("dynamic-directory-api")
        assert hints.requires_javascript is True
        assert hints.has_detail_pages is False
        assert hints.has_detail_api is True
        assert hints.pagination == "alphabet_tabs"

    def test_static_listing_hints(self):
        hints = get_hints_from_template("static-listing")
        assert hints.requires_javascript is False
        assert hints.has_detail_pages is False
        assert hints.has_detail_api is False
        assert hints.pagination == "none"

    def test_invalid_template_raises(self):
        with pytest.raises(ValueError, match="Template not found"):
            get_hints_from_template("nonexistent")


class TestTemplateStructure:
    """Validate structure of each bundled template."""

    TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "app" / "templates"

    def _load_raw(self, name: str) -> dict:
        path = self.TEMPLATES_DIR / f"{name}.json"
        return json.loads(path.read_text(encoding="utf-8"))

    def test_dynamic_directory_detail_pages_hints(self):
        data = self._load_raw("dynamic-directory-detail-pages")
        hints = data["hints"]
        assert hints["requires_javascript"] is True
        assert hints["has_detail_pages"] is True
        assert hints["has_detail_api"] is False

    def test_dynamic_directory_api_hints(self):
        data = self._load_raw("dynamic-directory-api")
        hints = data["hints"]
        assert hints["requires_javascript"] is True
        assert hints["has_detail_pages"] is False
        assert hints["has_detail_api"] is True

    def test_static_listing_hints(self):
        data = self._load_raw("static-listing")
        hints = data["hints"]
        assert hints["requires_javascript"] is False
        assert hints["has_detail_pages"] is False
        assert hints["has_detail_api"] is False

    def test_no_css_selectors_in_templates(self):
        """Templates should contain only pattern hints, no site-specific CSS."""
        for name in ("dynamic-directory-detail-pages", "dynamic-directory-api", "static-listing"):
            data = self._load_raw(name)
            # Templates should NOT have a 'plan' key with CSS selectors
            assert "plan" not in data, f"Template {name} should not contain 'plan'"
            assert "hints" in data, f"Template {name} must contain 'hints'"

    @pytest.mark.parametrize("template_name", [
        "dynamic-directory-detail-pages",
        "dynamic-directory-api",
        "static-listing",
    ])
    def test_template_round_trips(self, template_name: str):
        """Template JSON → Pydantic model → JSON round-trip."""
        data = self._load_raw(template_name)
        hints = TemplateHints.model_validate(data["hints"])
        # Serialize back to JSON-compatible dict
        hints_dict = hints.model_dump(mode="json")
        # Re-validate
        hints2 = TemplateHints.model_validate(hints_dict)
        assert hints2.requires_javascript == hints.requires_javascript
        assert hints2.pagination == hints.pagination
