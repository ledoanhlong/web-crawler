"""Build a GitHub issue payload for smoke regressions.

This script reads the newest smoke report and optional markdown summary,
then emits a compact JSON payload with title/body suitable for
`actions/github-script` issue creation.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_REPORT_DIR = "output/smoke_reports"
DEFAULT_SUMMARY_PATH = "output/smoke_reports/latest-summary.md"
DEFAULT_OUT_PATH = "output/smoke_reports/issue-payload.json"


def _find_latest_report(report_dir: Path) -> Path | None:
    files = sorted(report_dir.glob("smoke-*.json"))
    return files[-1] if files else None


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def build_issue_payload(
    report_payload: dict[str, Any],
    *,
    summary_markdown: str | None = None,
) -> dict[str, str]:
    summary = report_payload.get("summary") or {}
    total = int(summary.get("total", 0))
    passed = int(summary.get("passed", 0))
    failed = int(summary.get("failed", max(total - passed, 0)))

    results = report_payload.get("results") or []
    failed_profiles: list[str] = []
    for row in results:
        if isinstance(row, dict) and not bool(row.get("passed", False)):
            failed_profiles.append(str(row.get("profile_name") or "unknown-profile"))

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    title = f"[Smoke Regression] {failed}/{total} failed ({stamp})"

    lines: list[str] = []
    lines.append("## Smoke Regression Detected")
    lines.append("")
    lines.append(f"- Total profiles: **{total}**")
    lines.append(f"- Passed: **{passed}**")
    lines.append(f"- Failed: **{failed}**")
    lines.append("")

    if failed_profiles:
        lines.append("### Failed Profiles")
        for name in failed_profiles[:20]:
            lines.append(f"- `{name}`")
        lines.append("")

    lines.append("### Next Actions")
    lines.append("1. Review smoke report artifacts and diagnostics counters.")
    lines.append("2. Classify root cause using the reliability taxonomy.")
    lines.append("3. Fill `docs/POSTMORTEM_TEMPLATE.md` for this regression.")
    lines.append("")

    if summary_markdown and summary_markdown.strip():
        lines.append("---")
        lines.append("")
        lines.append("## Embedded Smoke Summary")
        lines.append("")
        lines.append(summary_markdown.strip())

    return {
        "title": title,
        "body": "\n".join(lines),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Build smoke regression issue payload JSON.")
    parser.add_argument("--report-dir", default=DEFAULT_REPORT_DIR)
    parser.add_argument("--summary", default=DEFAULT_SUMMARY_PATH)
    parser.add_argument("--out", default=DEFAULT_OUT_PATH)
    args = parser.parse_args()

    latest = _find_latest_report(Path(args.report_dir))
    if latest is None:
        print("No smoke reports found.")
        return 2

    report = _load_json(latest)
    summary_path = Path(args.summary)
    summary_md = summary_path.read_text(encoding="utf-8") if summary_path.exists() else None
    payload = build_issue_payload(report, summary_markdown=summary_md)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=True, indent=2), encoding="utf-8")
    print(f"Wrote issue payload: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
