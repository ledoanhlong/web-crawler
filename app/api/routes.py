"""API routes for the web crawler service."""

from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, HTTPException
from fastapi.responses import FileResponse

from app.models.schemas import CrawlJob, CrawlRequest, CrawlStatus
from app.services.orchestrator import Orchestrator

router = APIRouter(prefix="/api/v1", tags=["crawl"])

# In-memory job store (swap for Redis/DB in production)
_jobs: dict[str, CrawlJob] = {}
_orchestrator = Orchestrator()


@router.post("/crawl", response_model=CrawlJob, status_code=202)
async def start_crawl(request: CrawlRequest, background_tasks: BackgroundTasks) -> CrawlJob:
    """Submit a new crawl job. Returns immediately with a job ID; the crawl
    runs asynchronously in the background."""
    job = CrawlJob(request=request)
    _jobs[job.id] = job
    background_tasks.add_task(_run_job, job)
    return job


@router.get("/crawl/{job_id}", response_model=CrawlJob)
async def get_crawl_status(job_id: str) -> CrawlJob:
    """Check the status (and result) of a crawl job."""
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@router.get("/crawl/{job_id}/json")
async def download_json(job_id: str) -> FileResponse:
    """Download the JSON output file for a completed crawl."""
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status != CrawlStatus.COMPLETED or not job.result or not job.result.json_path:
        raise HTTPException(status_code=400, detail="Job not completed or no output available")
    path = Path(job.result.json_path)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Output file not found")
    return FileResponse(path, media_type="application/json", filename="exhibitors.json")


@router.get("/crawl/{job_id}/csv")
async def download_csv(job_id: str) -> FileResponse:
    """Download the CSV output file for a completed crawl."""
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status != CrawlStatus.COMPLETED or not job.result or not job.result.csv_path:
        raise HTTPException(status_code=400, detail="Job not completed or no output available")
    path = Path(job.result.csv_path)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Output file not found")
    return FileResponse(path, media_type="text/csv", filename="exhibitors.csv")


@router.get("/jobs", response_model=list[CrawlJob])
async def list_jobs() -> list[CrawlJob]:
    """List all crawl jobs (most recent first)."""
    return sorted(_jobs.values(), key=lambda j: j.created_at, reverse=True)


async def _run_job(job: CrawlJob) -> None:
    """Background task that runs the orchestrator pipeline."""
    await _orchestrator.run(job)
