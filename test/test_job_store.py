from __future__ import annotations

import shutil
from pathlib import Path
from uuid import uuid4

from app.models.schemas import CrawlJob, CrawlRequest, CrawlStatus
from app.services.job_store import JobStore


def _make_temp_dir() -> Path:
    root = Path("output") / ".job_store_tests" / uuid4().hex
    root.mkdir(parents=True, exist_ok=True)
    return root


def test_job_store_round_trip_persists_jobs() -> None:
    root = _make_temp_dir()
    try:
        store = JobStore(root)
        job = CrawlJob(request=CrawlRequest(url="https://example.com"))

        assert store.save(job, force=True) is True

        loaded: dict[str, CrawlJob] = {}
        assert store.load_into(loaded) == 1
        assert loaded[job.id].request.url == "https://example.com"
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_recover_interrupted_jobs_marks_active_work_as_failed() -> None:
    root = _make_temp_dir()
    try:
        store = JobStore(root)
        job = CrawlJob(
            request=CrawlRequest(url="https://example.com"),
            status=CrawlStatus.SCRAPING,
        )
        jobs = {job.id: job}

        recovered = store.recover_interrupted_jobs(jobs)

        assert recovered == 1
        assert jobs[job.id].status == CrawlStatus.FAILED
        assert jobs[job.id].error
    finally:
        shutil.rmtree(root, ignore_errors=True)
