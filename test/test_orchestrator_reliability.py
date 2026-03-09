"""Reliability-focused unit tests for orchestrator selection helpers."""

from __future__ import annotations

from app.models.schemas import ExtractionMethod, SellerLead
from app.services.orchestrator import Orchestrator
from app.config import settings
from app.models.schemas import CrawlJob, CrawlRequest, CrawlStatus


def test_build_method_attempt_order_prefers_requested_method_first() -> None:
    order = Orchestrator._build_method_attempt_order(ExtractionMethod.SMART_SCRAPER)

    assert order[0] == ExtractionMethod.SMART_SCRAPER
    assert order[-1] is None
    assert len(order) == len(set(order))


def test_build_method_attempt_order_without_preference_has_auto_fallback() -> None:
    order = Orchestrator._build_method_attempt_order(None)

    assert order[0] is None
    assert ExtractionMethod.CSS in order
    assert ExtractionMethod.SMART_SCRAPER in order
    assert ExtractionMethod.UNIVERSAL_SCRAPER in order


def test_preview_quality_score_rewards_contact_completeness() -> None:
    sparse = SellerLead(name="Acme")
    rich = SellerLead(
        name="Acme",
        website="https://acme.example",
        email="hello@acme.example",
        phone="+1-555-0100",
        country="US",
        city="Austin",
        social_media={"linkedin": "https://linkedin.com/company/acme"},
        product_categories=["tools"],
    )

    sparse_score = Orchestrator._preview_quality_score(sparse)
    rich_score = Orchestrator._preview_quality_score(rich)

    assert rich_score > sparse_score


def test_select_preview_method_deterministic_picks_best_candidate() -> None:
    orchestrator = Orchestrator()
    candidates = {
        ExtractionMethod.CSS: SellerLead(name="Acme"),
        ExtractionMethod.SMART_SCRAPER: SellerLead(
            name="Acme",
            website="https://acme.example",
            email="hello@acme.example",
            country="US",
        ),
    }

    method, best_score, margin = orchestrator._select_preview_method_deterministic(candidates)

    assert method == ExtractionMethod.SMART_SCRAPER
    assert best_score > 0
    assert margin > 0


def test_should_switch_method_requires_minimum_signal() -> None:
    assert Orchestrator._should_switch_method(pages_processed=1, zero_item_streak=2) is False
    assert Orchestrator._should_switch_method(pages_processed=3, zero_item_streak=1) is False
    assert Orchestrator._should_switch_method(pages_processed=3, zero_item_streak=2) is True


def test_quality_gate_records_failure_when_below_threshold() -> None:
    orch = Orchestrator()
    job = CrawlJob(request=CrawlRequest(url="https://example.com"), status=CrawlStatus.OUTPUT)
    job.quality_report = {"overall_score": 0.4}

    prev_min = settings.reliability_quality_min_score
    prev_enforce = settings.reliability_quality_enforce
    settings.reliability_quality_min_score = 0.5
    settings.reliability_quality_enforce = False
    try:
        ok = orch._apply_quality_gate(job)
    finally:
        settings.reliability_quality_min_score = prev_min
        settings.reliability_quality_enforce = prev_enforce

    assert ok is False
    assert job.diagnostics.failures
    assert job.status == CrawlStatus.OUTPUT


def test_quality_gate_can_enforce_partial_status() -> None:
    orch = Orchestrator()
    job = CrawlJob(request=CrawlRequest(url="https://example.com"), status=CrawlStatus.OUTPUT)
    job.quality_report = {"overall_score": 0.2}

    prev_min = settings.reliability_quality_min_score
    prev_enforce = settings.reliability_quality_enforce
    settings.reliability_quality_min_score = 0.5
    settings.reliability_quality_enforce = True
    try:
        ok = orch._apply_quality_gate(job)
    finally:
        settings.reliability_quality_min_score = prev_min
        settings.reliability_quality_enforce = prev_enforce

    assert ok is False
    assert job.status == CrawlStatus.PARTIAL
    assert "below threshold" in (job.error or "")


def test_parser_confidence_rewards_field_coverage() -> None:
    low = [SellerLead(name="Acme")]
    high = [
        SellerLead(
            name="Acme",
            website="https://acme.example",
            email="hello@acme.example",
            phone="+1-555-0100",
            country="US",
            city="Austin",
            address="1 Main St",
            description="Industrial supplier",
            marketplace_name="Expo",
            source_url="https://example.com/seller/acme",
            product_categories=["tools"],
            brands=["Acme"],
            social_media={"linkedin": "https://linkedin.com/company/acme"},
        )
    ]

    low_score, _, low_metrics = Orchestrator._compute_parser_confidence(low)
    high_score, _, high_metrics = Orchestrator._compute_parser_confidence(high)

    assert high_score > low_score
    assert high_metrics["non_empty_fields"] > low_metrics["non_empty_fields"]


def test_parser_confidence_empty_records_baseline() -> None:
    score, reason, metrics = Orchestrator._compute_parser_confidence([])

    assert score == 0.2
    assert "no records" in reason.lower()
    assert metrics["record_count"] == 0
