"""Quality scoring — evaluates extraction quality and provides feedback.

Analyses parsed records and produces a quality report with
field coverage, completeness scores, and actionable recommendations.

When ``fields_wanted`` is provided, scoring is based on the user's
requested fields instead of the hardcoded defaults.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.utils.logging import get_logger

log = get_logger(__name__)


@dataclass
class QualityReport:
    """Quality assessment of extracted data."""

    total_records: int = 0
    field_coverage: dict[str, float] = field(default_factory=dict)
    overall_score: float = 0.0
    warnings: list[str] = field(default_factory=list)
    recommendations: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "total_records": self.total_records,
            "field_coverage": self.field_coverage,
            "overall_score": round(self.overall_score, 2),
            "warnings": self.warnings,
            "recommendations": self.recommendations,
        }


# Default field tiers (used when no fields_wanted is provided)
_CORE_FIELDS = ["name", "website", "country"]
_IMPORTANT_FIELDS = ["city", "email", "phone", "description"]
_OPTIONAL_FIELDS = [
    "address", "postal_code", "product_categories", "brands",
    "logo_url", "social_media",
]

# Weights for scoring
_CORE_WEIGHT = 0.5
_IMPORTANT_WEIGHT = 0.35
_OPTIONAL_WEIGHT = 0.15


def _field_coverage(records: list[dict], fields: list[str]) -> dict[str, float]:
    """Calculate coverage (0.0–1.0) for each field across all records.

    For fields that live in ``raw_extra``, check there as well.
    """
    if not records:
        return {f: 0.0 for f in fields}
    coverage: dict[str, float] = {}
    n = len(records)
    for f in fields:
        filled = 0
        for r in records:
            val = r.get(f)
            # Also check raw_extra for domain-specific fields
            if not val and isinstance(r.get("raw_extra"), dict):
                val = r["raw_extra"].get(f)
            if val and str(val).strip():
                filled += 1
        coverage[f] = round(filled / n, 3)
    return coverage


def evaluate_quality(
    records: list[dict],
    *,
    fields_wanted: str | None = None,
) -> QualityReport:
    """Evaluate extraction quality and return a QualityReport.

    Parameters
    ----------
    records : list[dict]
        List of parsed records (as dicts).
    fields_wanted : str or None
        Comma-separated list of fields the user requested.
        When provided, scoring is based on these fields instead of defaults.

    Returns
    -------
    QualityReport
        Quality assessment with scores, coverage, and recommendations.
    """
    report = QualityReport(total_records=len(records))

    if not records:
        report.warnings.append("No records extracted")
        report.recommendations.append("Check that the URL contains a list of items and that CSS selectors are correct")
        return report

    # Determine fields to score
    if fields_wanted:
        user_fields = [f.strip() for f in fields_wanted.split(",") if f.strip()]
        # All user-requested fields are treated as important
        all_fields = user_fields
        coverage = _field_coverage(records, all_fields)
        report.field_coverage = coverage

        # Simple average score for user-requested fields
        report.overall_score = sum(coverage.values()) / max(len(coverage), 1)

        # Warn about low-coverage requested fields
        for f in user_fields:
            cov = coverage.get(f, 0)
            if cov < 0.3:
                report.warnings.append(
                    f"Requested field '{f}' has low coverage ({cov:.0%}) — "
                    "it may not be visible on the listing page (try detail page scraping)"
                )
    else:
        # Default field-tier scoring
        all_fields = _CORE_FIELDS + _IMPORTANT_FIELDS + _OPTIONAL_FIELDS
        coverage = _field_coverage(records, all_fields)
        report.field_coverage = coverage

        core_score = sum(coverage.get(f, 0) for f in _CORE_FIELDS) / max(len(_CORE_FIELDS), 1)
        imp_score = sum(coverage.get(f, 0) for f in _IMPORTANT_FIELDS) / max(len(_IMPORTANT_FIELDS), 1)
        opt_score = sum(coverage.get(f, 0) for f in _OPTIONAL_FIELDS) / max(len(_OPTIONAL_FIELDS), 1)
        report.overall_score = (
            core_score * _CORE_WEIGHT
            + imp_score * _IMPORTANT_WEIGHT
            + opt_score * _OPTIONAL_WEIGHT
        )

        for f in _CORE_FIELDS:
            if coverage.get(f, 0) < 0.5:
                report.warnings.append(f"Core field '{f}' has low coverage ({coverage[f]:.0%})")

    if report.total_records < 5:
        report.warnings.append(f"Only {report.total_records} records extracted — may indicate pagination or selector issues")

    # Check for duplicates (by name)
    names = [r.get("name", "").strip().lower() for r in records if r.get("name")]
    unique_names = set(names)
    if names and len(unique_names) < len(names) * 0.9:
        dup_count = len(names) - len(unique_names)
        report.warnings.append(f"~{dup_count} potential duplicate records detected")

    # Generate recommendations
    if not fields_wanted:
        if coverage.get("email", 0) < 0.3:
            report.recommendations.append(
                "Low email coverage — consider enabling detail page scraping to find contact info"
            )
        if coverage.get("website", 0) < 0.3:
            report.recommendations.append(
                "Low website coverage — detail pages or structured data may contain website URLs"
            )
        if coverage.get("description", 0) < 0.2:
            report.recommendations.append(
                "Low description coverage — enable detail page scraping for richer data"
            )

    if report.overall_score >= 0.7:
        report.recommendations.append("Good quality — data is ready for export")
    elif report.overall_score >= 0.4:
        report.recommendations.append(
            "Moderate quality — review warnings and consider tweaking the scraping plan"
        )
    else:
        report.recommendations.append(
            "Low quality — verify the URL, selectors, and try a different extraction method"
        )

    log.info("Quality score: %.2f (%d records, %d warnings)", report.overall_score, report.total_records, len(report.warnings))
    return report
