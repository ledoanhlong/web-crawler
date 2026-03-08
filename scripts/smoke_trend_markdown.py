"""Generate a Markdown summary for smoke report trends.

Reads smoke JSON reports and writes a concise markdown summary suitable for CI
artifacts or job summaries.
"""

from __future__ import annotations

import argparse
import collections
import json
from pathlib import Path
from typing import Any


DEFAULT_REPORT_DIR = "output/smoke_reports"
DEFAULT_OUT_PATH = "output/smoke_reports/latest-summary.md"


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _find_reports(report_dir: Path) -> list[Path]:
    return sorted(report_dir.glob("smoke-*.json"))


def _summary(payload: dict[str, Any]) -> dict[str, Any]:
    s = payload.get("summary") or {}
    results = payload.get("results") or []
    total = int(s.get("total", len(results) or 0))
    passed = int(s.get("passed", 0))
    failed = int(s.get("failed", max(total - passed, 0)))
    pass_rate = (passed / total) if total else 0.0

    empty_pages = []
    method_switches = []
    parser_scalar_ratios = []
    parser_structured_ratios = []
    failed_rows = []
    rows_with_counters = []
    for row in results:
        if not isinstance(row, dict):
            continue
        diag = row.get("diagnostics") or {}
        counters = diag.get("counters") or {}
        parser_metrics = diag.get("parser_metrics") or {}
        ep = int(counters.get("empty_pages", 0))
        ms = int(counters.get("method_switches", 0))
        ps = float(parser_metrics.get("scalar_ratio", 0.0) or 0.0)
        pst = float(parser_metrics.get("structured_ratio", 0.0) or 0.0)
        empty_pages.append(ep)
        method_switches.append(ms)
        parser_scalar_ratios.append(ps)
        parser_structured_ratios.append(pst)
        rows_with_counters.append({
            "profile_name": str(row.get("profile_name") or "unknown"),
            "status": str(row.get("status") or "unknown"),
            "reason": str(row.get("reason") or ""),
            "empty_pages": ep,
            "method_switches": ms,
            "parser_scalar_ratio": ps,
            "parser_structured_ratio": pst,
            "passed": bool(row.get("passed", False)),
        })
        if not bool(row.get("passed", False)):
            failed_rows.append(row)

    avg_empty = (sum(empty_pages) / len(empty_pages)) if empty_pages else 0.0
    avg_switches = (sum(method_switches) / len(method_switches)) if method_switches else 0.0
    avg_parser_scalar = (
        sum(parser_scalar_ratios) / len(parser_scalar_ratios)
        if parser_scalar_ratios else 0.0
    )
    avg_parser_structured = (
        sum(parser_structured_ratios) / len(parser_structured_ratios)
        if parser_structured_ratios else 0.0
    )

    return {
        "total": total,
        "passed": passed,
        "failed": failed,
        "pass_rate": pass_rate,
        "avg_empty_pages": avg_empty,
        "avg_method_switches": avg_switches,
        "avg_parser_scalar_ratio": avg_parser_scalar,
        "avg_parser_structured_ratio": avg_parser_structured,
        "failed_rows": failed_rows,
        "rows_with_counters": rows_with_counters,
    }


def _format_pct(value: float) -> str:
    return f"{value * 100:.1f}%"


def _top_counter_rows(rows: list[dict[str, Any]], key: str, limit: int = 5) -> list[dict[str, Any]]:
    ranked = sorted(rows, key=lambda r: int(r.get(key, 0)), reverse=True)
    ranked = [r for r in ranked if int(r.get(key, 0)) > 0]
    return ranked[:limit]


def _top_failure_reasons(rows: list[dict[str, Any]], limit: int = 5) -> list[tuple[str, int]]:
    counts: collections.Counter[str] = collections.Counter()
    for row in rows:
        reason = str(row.get("reason") or "").strip()
        if not reason:
            reason = "unspecified"
        counts[reason] += 1
    return counts.most_common(limit)


def build_markdown(
    *,
    previous_path: Path | None,
    current_path: Path,
    previous_payload: dict[str, Any] | None,
    current_payload: dict[str, Any],
) -> str:
    cur = _summary(current_payload)

    lines: list[str] = []
    lines.append("# Smoke Trend Summary")
    lines.append("")
    lines.append(f"- Current report: `{current_path.name}`")
    if previous_path:
        lines.append(f"- Previous report: `{previous_path.name}`")
    lines.append("")

    lines.append("## Current Metrics")
    lines.append("")
    lines.append(f"- Total profiles: **{cur['total']}**")
    lines.append(f"- Passed: **{cur['passed']}**")
    lines.append(f"- Failed: **{cur['failed']}**")
    lines.append(f"- Pass rate: **{_format_pct(cur['pass_rate'])}**")
    lines.append(f"- Avg empty pages: **{cur['avg_empty_pages']:.2f}**")
    lines.append(f"- Avg method switches: **{cur['avg_method_switches']:.2f}**")
    lines.append(f"- Avg parser scalar completeness: **{_format_pct(cur['avg_parser_scalar_ratio'])}**")
    lines.append(f"- Avg parser structured completeness: **{_format_pct(cur['avg_parser_structured_ratio'])}**")
    lines.append("")

    if previous_payload is not None and previous_path is not None:
        prev = _summary(previous_payload)
        lines.append("## Delta vs Previous")
        lines.append("")
        lines.append(
            f"- Pass rate: `{_format_pct(cur['pass_rate'])}` "
            f"(delta `{(cur['pass_rate'] - prev['pass_rate']) * 100:+.1f}%`)"
        )
        lines.append(
            f"- Avg empty pages: `{cur['avg_empty_pages']:.2f}` "
            f"(delta `{cur['avg_empty_pages'] - prev['avg_empty_pages']:+.2f}`)"
        )
        lines.append(
            f"- Avg method switches: `{cur['avg_method_switches']:.2f}` "
            f"(delta `{cur['avg_method_switches'] - prev['avg_method_switches']:+.2f}`)"
        )
        lines.append(
            f"- Avg parser scalar completeness: `{_format_pct(cur['avg_parser_scalar_ratio'])}` "
            f"(delta `{(cur['avg_parser_scalar_ratio'] - prev['avg_parser_scalar_ratio']) * 100:+.1f}%`)"
        )
        lines.append(
            f"- Avg parser structured completeness: `{_format_pct(cur['avg_parser_structured_ratio'])}` "
            f"(delta `{(cur['avg_parser_structured_ratio'] - prev['avg_parser_structured_ratio']) * 100:+.1f}%`)"
        )
        lines.append("")

    failed_rows = cur["failed_rows"]
    lines.append("## Failed Profiles")
    lines.append("")
    if not failed_rows:
        lines.append("No failed profiles in current report.")
    else:
        lines.append("| Profile | Status | Reason |")
        lines.append("|---|---|---|")
        for row in failed_rows[:20]:
            name = str(row.get("profile_name") or "unknown")
            status = str(row.get("status") or "unknown")
            reason = str(row.get("reason") or "")
            reason = reason.replace("|", "\\|")
            lines.append(f"| `{name}` | `{status}` | {reason} |")

    lines.append("")
    lines.append("## Hotspots")
    lines.append("")

    top_reasons = _top_failure_reasons(failed_rows)
    lines.append("### Most Common Failure Reasons")
    if not top_reasons:
        lines.append("No failure reasons to report.")
    else:
        for reason, count in top_reasons:
            safe_reason = reason.replace("|", "\\|")
            lines.append(f"- `{count}x` {safe_reason}")

    rows = cur.get("rows_with_counters", [])
    top_empty = _top_counter_rows(rows, "empty_pages")
    lines.append("")
    lines.append("### Highest Empty-Page Profiles")
    if not top_empty:
        lines.append("No empty-page outliers.")
    else:
        lines.append("| Profile | Empty Pages | Status |")
        lines.append("|---|---:|---|")
        for row in top_empty:
            lines.append(
                f"| `{row['profile_name']}` | {int(row.get('empty_pages', 0))} | `{row['status']}` |"
            )

    top_switches = _top_counter_rows(rows, "method_switches")
    lines.append("")
    lines.append("### Highest Method-Switch Profiles")
    if not top_switches:
        lines.append("No method-switch outliers.")
    else:
        lines.append("| Profile | Method Switches | Status |")
        lines.append("|---|---:|---|")
        for row in top_switches:
            lines.append(
                f"| `{row['profile_name']}` | {int(row.get('method_switches', 0))} | `{row['status']}` |"
            )

    lines.append("")
    lines.append("### Lowest Parser Scalar-Completeness Profiles")
    ranked_parser = sorted(rows, key=lambda r: float(r.get("parser_scalar_ratio", 0.0)))
    ranked_parser = ranked_parser[:5]
    if not ranked_parser:
        lines.append("No parser-completeness outliers.")
    else:
        lines.append("| Profile | Scalar Completeness | Structured Completeness | Status |")
        lines.append("|---|---:|---:|---|")
        for row in ranked_parser:
            lines.append(
                f"| `{row['profile_name']}` | {_format_pct(float(row.get('parser_scalar_ratio', 0.0)))} | {_format_pct(float(row.get('parser_structured_ratio', 0.0)))} | `{row['status']}` |"
            )

    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate markdown summary for smoke reports.")
    parser.add_argument("--report-dir", default=DEFAULT_REPORT_DIR)
    parser.add_argument("--previous", default="")
    parser.add_argument("--current", default="")
    parser.add_argument("--out", default=DEFAULT_OUT_PATH)
    args = parser.parse_args()

    if args.previous and args.current:
        prev_path = Path(args.previous)
        cur_path = Path(args.current)
        if not cur_path.exists():
            print("Current report file not found.")
            return 2
        prev_payload = _load(prev_path) if prev_path.exists() else None
        cur_payload = _load(cur_path)
    else:
        report_dir = Path(args.report_dir)
        files = _find_reports(report_dir)
        if not files:
            print("No smoke reports found.")
            return 0
        cur_path = files[-1]
        prev_path = files[-2] if len(files) >= 2 else None
        cur_payload = _load(cur_path)
        prev_payload = _load(prev_path) if prev_path else None

    md = build_markdown(
        previous_path=prev_path,
        current_path=cur_path,
        previous_payload=prev_payload,
        current_payload=cur_payload,
    )

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(md, encoding="utf-8")
    print(f"Wrote markdown summary: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
