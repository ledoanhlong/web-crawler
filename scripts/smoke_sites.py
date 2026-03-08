"""Run smoke crawls against a list of website profiles.

This script drives the existing API end-to-end:
1. Submit crawl request
2. Poll until preview/failed
3. Confirm preview (optional)
4. Poll until completed/partial/failed
5. Save a JSON report with diagnostics and pass/fail summary
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


DEFAULT_API_BASE = "http://127.0.0.1:8000/api/v1"
DEFAULT_PROFILE_PATH = "auto"
DEFAULT_REPORT_DIR = "output/smoke_reports"
PROFILE_PRIVATE_PATH = "test/fixtures/website_profiles.private.json"
PROFILE_EXAMPLE_PATH = "test/fixtures/website_profiles.example.json"


@dataclass
class SmokeResult:
    profile_name: str
    url: str
    job_id: str | None
    resume_job_id: str | None
    status: str
    passed: bool
    reason: str
    duration_s: float
    record_count: int
    diagnostics: dict[str, Any]


def _http_json(method: str, url: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = Request(url=url, data=data, headers=headers, method=method)
    try:
        with urlopen(req, timeout=60) as resp:
            raw = resp.read().decode("utf-8")
            if not raw:
                return {}
            return json.loads(raw)
    except HTTPError as exc:
        raw = exc.read().decode("utf-8") if exc.fp else ""
        msg = raw or str(exc)
        raise RuntimeError(f"HTTP {exc.code} for {url}: {msg}") from exc
    except URLError as exc:
        raise RuntimeError(f"Request failed for {url}: {exc}") from exc


def _resolve_profiles_path(profile_arg: str) -> Path:
    """Resolve profile path from user arg with private->example fallback.

    - explicit path: use as-is
    - "auto": use private profile file if present, else example file
    """
    if profile_arg != "auto":
        return Path(profile_arg)

    private_path = Path(PROFILE_PRIVATE_PATH)
    if private_path.exists():
        return private_path
    return Path(PROFILE_EXAMPLE_PATH)


def _poll_job(api_base: str, job_id: str, timeout_s: int, poll_s: float) -> dict[str, Any]:
    start = time.monotonic()
    while True:
        job = _http_json("GET", f"{api_base}/crawl/{job_id}")
        status = (job.get("status") or "").lower()
        if status in {"preview", "failed", "completed", "partial"}:
            return job
        if (time.monotonic() - start) >= timeout_s:
            raise TimeoutError(f"Timed out polling job {job_id} after {timeout_s}s")
        time.sleep(poll_s)


def _build_crawl_request(profile: dict[str, Any]) -> dict[str, Any]:
    req = {
        "url": profile["url"],
        "fields_wanted": profile.get("fields_wanted"),
        "item_description": profile.get("item_description"),
        "site_notes": profile.get("site_notes"),
        "detail_page_url": profile.get("detail_page_url"),
        "pagination_type": profile.get("pagination_type"),
        "max_items": profile.get("max_items"),
        "max_pages": profile.get("max_pages"),
        "test_single": bool(profile.get("test_single", False)),
    }
    return {k: v for k, v in req.items() if v is not None}


def _evaluate_pass(
    *,
    final_job: dict[str, Any],
    profile: dict[str, Any],
) -> tuple[bool, str, int]:
    status = (final_job.get("status") or "").lower()
    records = (((final_job.get("result") or {}).get("records") or []))
    count = len(records)
    min_records = int(profile.get("expect_min_records", 1))
    min_quality = float(profile.get("expect_min_quality", 0.0) or 0.0)
    allow_partial = bool(profile.get("allow_partial", True))

    if status == "failed":
        return False, "job failed", count
    if status == "partial" and not allow_partial:
        return False, "partial not allowed by profile", count
    if status not in {"completed", "partial"}:
        return False, f"unexpected final status={status}", count
    if count < min_records:
        return False, f"record_count {count} < expected minimum {min_records}", count
    if min_quality > 0:
        qr = final_job.get("quality_report") or {}
        qscore = float(qr.get("overall_score", 0.0) or 0.0)
        if qscore < min_quality:
            return False, f"quality_score {qscore:.3f} < expected minimum {min_quality:.3f}", count
    return True, "ok", count


def run_profile(
    *,
    api_base: str,
    profile: dict[str, Any],
    preview_timeout_s: int,
    run_timeout_s: int,
    poll_interval_s: float,
) -> SmokeResult:
    name = profile.get("name", "unnamed-profile")
    url = profile.get("url", "")
    started_at = time.monotonic()
    job_id = None
    resume_job_id = None

    try:
        submit_body = _build_crawl_request(profile)
        created = _http_json("POST", f"{api_base}/crawl", submit_body)
        job_id = created["id"]

        preview_job = _poll_job(api_base, job_id, preview_timeout_s, poll_interval_s)
        preview_status = (preview_job.get("status") or "").lower()
        if preview_status == "failed":
            diagnostics = preview_job.get("diagnostics") or {}
            return SmokeResult(
                profile_name=name,
                url=url,
                job_id=job_id,
                resume_job_id=resume_job_id,
                status="failed",
                passed=False,
                reason="failed during preview",
                duration_s=time.monotonic() - started_at,
                record_count=0,
                diagnostics=diagnostics,
            )

        auto_confirm = bool(profile.get("auto_confirm", True))
        if not auto_confirm:
            diagnostics = preview_job.get("diagnostics") or {}
            return SmokeResult(
                profile_name=name,
                url=url,
                job_id=job_id,
                resume_job_id=resume_job_id,
                status="preview",
                passed=True,
                reason="stopped at preview by profile setting",
                duration_s=time.monotonic() - started_at,
                record_count=0,
                diagnostics=diagnostics,
            )

        selected_method = profile.get("extraction_method") or preview_job.get("preview_recommended_method")
        confirm_body = {
            "approved": True,
            "feedback": profile.get("confirm_feedback"),
            "extraction_method": selected_method,
        }
        confirm_body = {k: v for k, v in confirm_body.items() if v is not None}
        _http_json("POST", f"{api_base}/crawl/{job_id}/confirm", confirm_body)

        final_job = _poll_job(api_base, job_id, run_timeout_s, poll_interval_s)
        if (final_job.get("status") or "").lower() == "partial" and bool(profile.get("auto_resume_partial", False)):
            resumed = _http_json("POST", f"{api_base}/crawl/{job_id}/resume", {})
            resume_job_id = resumed.get("id")
            if resume_job_id:
                final_job = _poll_job(api_base, resume_job_id, run_timeout_s, poll_interval_s)

        diagnostics = final_job.get("diagnostics") or {}
        passed, reason, count = _evaluate_pass(final_job=final_job, profile=profile)

        return SmokeResult(
            profile_name=name,
            url=url,
            job_id=job_id,
            resume_job_id=resume_job_id,
            status=(final_job.get("status") or "unknown"),
            passed=passed,
            reason=reason,
            duration_s=time.monotonic() - started_at,
            record_count=count,
            diagnostics=diagnostics,
        )

    except Exception as exc:
        return SmokeResult(
            profile_name=name,
            url=url,
            job_id=job_id,
            resume_job_id=resume_job_id,
            status="error",
            passed=False,
            reason=str(exc),
            duration_s=time.monotonic() - started_at,
            record_count=0,
            diagnostics={},
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="Run crawler smoke tests against profile URLs.")
    parser.add_argument("--api-base", default=DEFAULT_API_BASE, help="Base API URL, e.g. http://127.0.0.1:8000/api/v1")
    parser.add_argument(
        "--profiles",
        default=DEFAULT_PROFILE_PATH,
        help=(
            "Path to profile JSON file or 'auto' (default). "
            "In auto mode, uses private profile file when present, else example file."
        ),
    )
    parser.add_argument("--report-dir", default=DEFAULT_REPORT_DIR, help="Directory to write report JSON")
    parser.add_argument("--preview-timeout", type=int, default=180, help="Seconds to wait for preview stage")
    parser.add_argument("--run-timeout", type=int, default=1200, help="Seconds to wait for final stage")
    parser.add_argument("--poll-interval", type=float, default=2.0, help="Polling interval in seconds")
    parser.add_argument("--max-profiles", type=int, default=0, help="Optional cap on enabled profiles")
    args = parser.parse_args()

    profiles_path = _resolve_profiles_path(args.profiles)
    if not profiles_path.exists():
        print(f"Profile file not found: {profiles_path}", file=sys.stderr)
        return 2

    try:
        payload = json.loads(profiles_path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"Failed to read profiles file {profiles_path}: {exc}", file=sys.stderr)
        return 2

    if not isinstance(payload, list):
        print("Profiles JSON must be a list of profile objects", file=sys.stderr)
        return 2

    enabled_profiles = [p for p in payload if isinstance(p, dict) and p.get("enabled", True)]
    if args.max_profiles > 0:
        enabled_profiles = enabled_profiles[: args.max_profiles]

    if not enabled_profiles:
        print("No enabled profiles found. Nothing to run.")
        return 0

    results: list[SmokeResult] = []
    print(f"Running smoke suite for {len(enabled_profiles)} profile(s) against {args.api_base}")

    for idx, profile in enumerate(enabled_profiles, start=1):
        name = profile.get("name", f"profile-{idx}")
        print(f"[{idx}/{len(enabled_profiles)}] {name} ...", end="", flush=True)
        res = run_profile(
            api_base=args.api_base,
            profile=profile,
            preview_timeout_s=args.preview_timeout,
            run_timeout_s=args.run_timeout,
            poll_interval_s=args.poll_interval,
        )
        results.append(res)
        state = "PASS" if res.passed else "FAIL"
        print(f" {state} ({res.status}, records={res.record_count}, {res.duration_s:.1f}s)")

    passed = sum(1 for r in results if r.passed)
    failed = len(results) - passed

    report_dir = Path(args.report_dir)
    report_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    report_path = report_dir / f"smoke-{ts}.json"

    report = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "api_base": args.api_base,
        "profiles_file": str(profiles_path),
        "summary": {
            "total": len(results),
            "passed": passed,
            "failed": failed,
        },
        "results": [
            {
                "profile_name": r.profile_name,
                "url": r.url,
                "job_id": r.job_id,
                "resume_job_id": r.resume_job_id,
                "status": r.status,
                "passed": r.passed,
                "reason": r.reason,
                "duration_s": round(r.duration_s, 3),
                "record_count": r.record_count,
                "diagnostics": r.diagnostics,
            }
            for r in results
        ],
    }

    report_path.write_text(json.dumps(report, ensure_ascii=True, indent=2), encoding="utf-8")
    print(f"Report written: {report_path}")

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
