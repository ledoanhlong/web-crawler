"""API route tests for crawl diagnostics behavior."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.api.routes import _jobs
from app.main import app
from app.models.schemas import CrawlJob, CrawlRequest, CrawlStatus


client = TestClient(app)


def setup_function() -> None:
    """Ensure in-memory job store is reset between tests."""
    _jobs.clear()


def test_confirm_reject_records_diagnostics_failure() -> None:
    job = CrawlJob(request=CrawlRequest(url="https://example.com"), status=CrawlStatus.PREVIEW)
    _jobs[job.id] = job

    response = client.post(
        f"/api/v1/crawl/{job.id}/confirm",
        json={"approved": False, "feedback": "missing company names"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "failed"
    assert body["error"] == "Crawl aborted by user after preview."
    assert body["diagnostics"]["status_timeline"]
    assert body["diagnostics"]["failures"]

    failure = body["diagnostics"]["failures"][-1]
    assert failure["category"] == "quality_threshold"
    assert failure["stage"] == "planning"
    assert failure["retryable"] is True


def test_get_diagnostics_endpoint_returns_structured_payload() -> None:
    job = CrawlJob(request=CrawlRequest(url="https://example.com"))
    _jobs[job.id] = job

    response = client.get(f"/api/v1/crawl/{job.id}/diagnostics")

    assert response.status_code == 200
    payload = response.json()
    assert set(payload.keys()) == {"counters", "stage_confidences", "failures", "status_timeline", "parser_metrics"}
    assert payload["counters"]["scrape_attempts"] == 0
    assert payload["parser_metrics"]["record_count"] == 0


def test_get_diagnostics_returns_404_for_unknown_job() -> None:
    response = client.get("/api/v1/crawl/unknown-job/diagnostics")

    assert response.status_code == 404
    assert response.json()["detail"] == "Job not found"
