"""Contract tests for planner selector quality helpers."""

from __future__ import annotations

from pathlib import Path

from bs4 import BeautifulSoup

from app.agents.planner_agent import PlannerAgent, _sanitize_plan_data
from app.models.schemas import PaginationStrategy, ScrapingPlan, ScrapingTarget


def _load_fixture(name: str) -> str:
    path = Path("test/fixtures/html") / name
    return path.read_text(encoding="utf-8")


def _make_plan(item_selector: str, fields: dict[str, str]) -> ScrapingPlan:
    return ScrapingPlan(
        url="https://example.com/directory",
        requires_javascript=False,
        pagination=PaginationStrategy.NONE,
        target=ScrapingTarget(
            item_container_selector=item_selector,
            field_selectors=fields,
            detail_link_selector="a.detail-link",
        ),
    )


def test_sanitize_plan_data_flattens_nested_selector_maps() -> None:
    raw = {
        "target": {
            "item_container_selector": ".company-card",
            "field_selectors": {
                "fields": {
                    "name": "h3.name",
                    "website": "a.website",
                }
            },
        },
        "detail_page_fields": {"nested": {"email": "a.email"}},
    }

    cleaned = _sanitize_plan_data(raw)

    assert cleaned["target"]["field_selectors"]["name"] == "h3.name"
    assert cleaned["detail_page_fields"]["email"] == "a.email"


def test_selector_metrics_high_hit_ratio_for_valid_selectors() -> None:
    html = _load_fixture("static_directory_sample.html")
    soup = BeautifulSoup(html, "lxml")
    plan = _make_plan(
        ".company-card",
        {
            "name": "h3.name",
            "website": "a.website",
            "email": "span.email",
        },
    )

    metrics = PlannerAgent._compute_selector_metrics(plan, soup)

    assert metrics["container_count"] == 3
    assert metrics["field_checks"] > 0
    assert metrics["field_hit_ratio"] >= 0.9


def test_selector_metrics_low_hit_ratio_for_bad_selectors() -> None:
    html = _load_fixture("static_directory_sample.html")
    soup = BeautifulSoup(html, "lxml")
    plan = _make_plan(
        ".company-card",
        {
            "name": ".does-not-exist",
            "website": "a.website",
            "phone": ".missing-phone",
        },
    )

    metrics = PlannerAgent._compute_selector_metrics(plan, soup)

    assert metrics["container_count"] == 3
    assert metrics["field_hit_ratio"] < 0.5
