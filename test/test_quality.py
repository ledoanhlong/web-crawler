"""Tests for app.utils.quality — quality scoring."""

from __future__ import annotations

import pytest

from app.utils.quality import QualityReport, evaluate_quality


def _make_record(**kwargs) -> dict:
    """Helper to create a record with defaults."""
    defaults = {
        "name": "Test Corp",
        "website": "https://test.com",
        "country": "US",
        "city": "New York",
        "email": "info@test.com",
        "phone": "+1-555-0100",
        "description": "A company",
    }
    defaults.update(kwargs)
    return defaults


class TestEvaluateQuality:
    def test_empty_records(self):
        report = evaluate_quality([])
        assert report.total_records == 0
        assert report.overall_score == 0.0
        assert len(report.warnings) > 0

    def test_perfect_records(self):
        records = [_make_record() for _ in range(20)]
        report = evaluate_quality(records)
        assert report.overall_score > 0.7
        assert report.total_records == 20

    def test_partial_records(self):
        records = [
            {"name": "Corp A", "country": "DE"},
            {"name": "Corp B", "country": "US"},
            {"name": "Corp C"},
        ]
        report = evaluate_quality(records)
        assert 0.0 < report.overall_score < 0.7
        assert report.field_coverage["name"] == 1.0
        assert report.field_coverage["email"] == 0.0

    def test_low_coverage_warnings(self):
        records = [{"name": "X"}]
        report = evaluate_quality(records)
        # website and country have 0 coverage
        warnings_text = " ".join(report.warnings)
        assert "website" in warnings_text or "country" in warnings_text

    def test_duplicate_detection(self):
        records = [_make_record(name="Same Corp") for _ in range(10)]
        report = evaluate_quality(records)
        assert any("duplicate" in w for w in report.warnings)

    def test_few_records_warning(self):
        records = [_make_record()]
        report = evaluate_quality(records)
        assert any("Only" in w for w in report.warnings)

    def test_recommendations_for_low_email(self):
        records = [{"name": "X", "country": "DE", "website": "https://x.com"} for _ in range(10)]
        report = evaluate_quality(records)
        assert any("email" in r.lower() for r in report.recommendations)

    def test_field_coverage_values(self):
        records = [
            _make_record(email="a@b.com"),
            _make_record(email=None),
            _make_record(email=""),
        ]
        report = evaluate_quality(records)
        # 1 out of 3 has email
        assert report.field_coverage["email"] == pytest.approx(1 / 3, abs=0.01)


class TestQualityReport:
    def test_to_dict(self):
        report = QualityReport(
            total_records=5,
            overall_score=0.756,
            warnings=["Low coverage"],
            recommendations=["Try more"],
        )
        d = report.to_dict()
        assert d["total_records"] == 5
        assert d["overall_score"] == 0.76  # rounded
        assert len(d["warnings"]) == 1
