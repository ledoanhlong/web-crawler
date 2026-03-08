"""Compare smoke report files and detect reliability regressions.

Default behavior compares the two newest files in output/smoke_reports.
Exits with code 1 when configured regression thresholds are exceeded.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEFAULT_REPORT_DIR = "output/smoke_reports"


@dataclass
class ReportSummary:
    path: Path
    total: int
    passed: int
    failed: int
    pass_rate: float
    avg_empty_pages: float
    avg_method_switches: float
    avg_parser_scalar_ratio: float
    avg_parser_structured_ratio: float
    failed_profiles: set[str]


def _load_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError(f"Failed to read report {path}: {exc}") from exc


def summarize_report(path: Path) -> ReportSummary:
    payload = _load_json(path)
    summary = payload.get("summary") or {}
    results = payload.get("results") or []

    total = int(summary.get("total", len(results) or 0))
    passed = int(summary.get("passed", 0))
    failed = int(summary.get("failed", max(total - passed, 0)))
    pass_rate = (passed / total) if total else 0.0

    empty_pages_vals: list[int] = []
    switch_vals: list[int] = []
    parser_scalar_vals: list[float] = []
    parser_structured_vals: list[float] = []
    failed_profiles: set[str] = set()

    for row in results:
        if not isinstance(row, dict):
            continue
        if not bool(row.get("passed", False)):
            name = str(row.get("profile_name") or "unknown-profile")
            failed_profiles.add(name)

        diagnostics = row.get("diagnostics") or {}
        counters = diagnostics.get("counters") or {}
        parser_metrics = diagnostics.get("parser_metrics") or {}
        empty_pages_vals.append(int(counters.get("empty_pages", 0)))
        switch_vals.append(int(counters.get("method_switches", 0)))
        parser_scalar_vals.append(float(parser_metrics.get("scalar_ratio", 0.0) or 0.0))
        parser_structured_vals.append(float(parser_metrics.get("structured_ratio", 0.0) or 0.0))

    avg_empty_pages = (
        sum(empty_pages_vals) / len(empty_pages_vals) if empty_pages_vals else 0.0
    )
    avg_method_switches = (
        sum(switch_vals) / len(switch_vals) if switch_vals else 0.0
    )
    avg_parser_scalar_ratio = (
        sum(parser_scalar_vals) / len(parser_scalar_vals) if parser_scalar_vals else 0.0
    )
    avg_parser_structured_ratio = (
        sum(parser_structured_vals) / len(parser_structured_vals) if parser_structured_vals else 0.0
    )

    return ReportSummary(
        path=path,
        total=total,
        passed=passed,
        failed=failed,
        pass_rate=pass_rate,
        avg_empty_pages=avg_empty_pages,
        avg_method_switches=avg_method_switches,
        avg_parser_scalar_ratio=avg_parser_scalar_ratio,
        avg_parser_structured_ratio=avg_parser_structured_ratio,
        failed_profiles=failed_profiles,
    )


def compare_reports(
    previous: ReportSummary,
    current: ReportSummary,
    *,
    max_pass_rate_drop: float,
    max_empty_pages_increase: float,
    max_switches_increase: float,
    max_parser_scalar_drop: float,
    max_parser_structured_drop: float,
    allow_new_failures: int,
) -> tuple[bool, list[str]]:
    issues: list[str] = []

    pass_rate_drop = previous.pass_rate - current.pass_rate
    if pass_rate_drop > max_pass_rate_drop:
        issues.append(
            f"pass_rate_drop={pass_rate_drop:.3f} exceeds threshold {max_pass_rate_drop:.3f}"
        )

    empty_increase = current.avg_empty_pages - previous.avg_empty_pages
    if empty_increase > max_empty_pages_increase:
        issues.append(
            f"avg_empty_pages_increase={empty_increase:.3f} exceeds threshold {max_empty_pages_increase:.3f}"
        )

    switch_increase = current.avg_method_switches - previous.avg_method_switches
    if switch_increase > max_switches_increase:
        issues.append(
            f"avg_method_switches_increase={switch_increase:.3f} exceeds threshold {max_switches_increase:.3f}"
        )

    parser_scalar_drop = previous.avg_parser_scalar_ratio - current.avg_parser_scalar_ratio
    if parser_scalar_drop > max_parser_scalar_drop:
        issues.append(
            f"avg_parser_scalar_drop={parser_scalar_drop:.3f} exceeds threshold {max_parser_scalar_drop:.3f}"
        )

    parser_structured_drop = previous.avg_parser_structured_ratio - current.avg_parser_structured_ratio
    if parser_structured_drop > max_parser_structured_drop:
        issues.append(
            "avg_parser_structured_drop="
            f"{parser_structured_drop:.3f} exceeds threshold {max_parser_structured_drop:.3f}"
        )

    new_failed = current.failed_profiles - previous.failed_profiles
    if len(new_failed) > allow_new_failures:
        issues.append(
            "new_failed_profiles="
            + ", ".join(sorted(new_failed))
            + f" exceeds allowed count {allow_new_failures}"
        )

    return (len(issues) == 0), issues


def _find_report_files(report_dir: Path) -> list[Path]:
    files = sorted(report_dir.glob("smoke-*.json"))
    return files


def main() -> int:
    parser = argparse.ArgumentParser(description="Compare smoke reports and fail on regressions.")
    parser.add_argument("--report-dir", default=DEFAULT_REPORT_DIR, help="Directory containing smoke-*.json reports")
    parser.add_argument("--previous", default="", help="Explicit previous report path")
    parser.add_argument("--current", default="", help="Explicit current report path")
    parser.add_argument("--max-pass-rate-drop", type=float, default=0.10)
    parser.add_argument("--max-empty-pages-increase", type=float, default=2.0)
    parser.add_argument("--max-switches-increase", type=float, default=1.0)
    parser.add_argument("--max-parser-scalar-drop", type=float, default=0.10)
    parser.add_argument("--max-parser-structured-drop", type=float, default=0.10)
    parser.add_argument("--allow-new-failures", type=int, default=0)
    args = parser.parse_args()

    if args.previous and args.current:
        previous_path = Path(args.previous)
        current_path = Path(args.current)
    else:
        report_dir = Path(args.report_dir)
        files = _find_report_files(report_dir)
        if len(files) < 2:
            print("Not enough smoke reports to compare (need at least 2).")
            return 0
        previous_path, current_path = files[-2], files[-1]

    if not previous_path.exists() or not current_path.exists():
        print("Report path not found.", file=sys.stderr)
        return 2

    previous = summarize_report(previous_path)
    current = summarize_report(current_path)

    ok, issues = compare_reports(
        previous,
        current,
        max_pass_rate_drop=args.max_pass_rate_drop,
        max_empty_pages_increase=args.max_empty_pages_increase,
        max_switches_increase=args.max_switches_increase,
        max_parser_scalar_drop=args.max_parser_scalar_drop,
        max_parser_structured_drop=args.max_parser_structured_drop,
        allow_new_failures=args.allow_new_failures,
    )

    print(f"Previous: {previous.path.name} pass_rate={previous.pass_rate:.3f} failed={previous.failed}")
    print(f"Current : {current.path.name} pass_rate={current.pass_rate:.3f} failed={current.failed}")
    print(
        "Delta   : "
        f"pass_rate={current.pass_rate - previous.pass_rate:+.3f}, "
        f"avg_empty_pages={current.avg_empty_pages - previous.avg_empty_pages:+.3f}, "
        f"avg_method_switches={current.avg_method_switches - previous.avg_method_switches:+.3f}, "
        f"avg_parser_scalar_ratio={current.avg_parser_scalar_ratio - previous.avg_parser_scalar_ratio:+.3f}, "
        f"avg_parser_structured_ratio={current.avg_parser_structured_ratio - previous.avg_parser_structured_ratio:+.3f}"
    )

    if ok:
        print("Smoke trend check: OK")
        return 0

    print("Smoke trend check: REGRESSION")
    for issue in issues:
        print(f"- {issue}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
