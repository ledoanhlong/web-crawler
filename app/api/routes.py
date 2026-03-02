"""API routes for the web crawler service."""

from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, HTTPException
from fastapi.responses import FileResponse

from app.models.schemas import (
    ConfirmPreviewRequest,
    CrawlJob,
    CrawlRequest,
    CrawlStatus,
    ScriptCreatorMultiRequest,
    ScriptCreatorRequest,
    ScriptResult,
    SmartScrapeMultiRequest,
    SmartScrapeResult,
)
from app.services.orchestrator import Orchestrator

router = APIRouter(prefix="/api/v1", tags=["crawl"])

# In-memory job store (swap for Redis/DB in production)
_jobs: dict[str, CrawlJob] = {}
_orchestrator = Orchestrator()


@router.post("/crawl", response_model=CrawlJob, status_code=202)
async def start_crawl(request: CrawlRequest, background_tasks: BackgroundTasks) -> CrawlJob:
    """Submit a new crawl job. Returns immediately with a job ID.

    The orchestrator will plan the scrape and produce a single preview
    record, then pause with status ``preview``.  Call ``POST /confirm``
    to continue.
    """
    job = CrawlJob(request=request)
    _jobs[job.id] = job
    background_tasks.add_task(_run_preview, job)
    return job


@router.post("/crawl/{job_id}/confirm", response_model=CrawlJob)
async def confirm_preview(
    job_id: str,
    body: ConfirmPreviewRequest,
    background_tasks: BackgroundTasks,
) -> CrawlJob:
    """Confirm or reject the preview and continue (or abort) the full crawl."""
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status != CrawlStatus.PREVIEW:
        raise HTTPException(
            status_code=400,
            detail=f"Job is not in preview state (current: {job.status.value})",
        )

    if not body.approved:
        job.status = CrawlStatus.FAILED
        job.error = "Crawl aborted by user after preview."
        return job

    # Store user feedback (may be None)
    job.user_feedback = body.feedback
    background_tasks.add_task(_run_full, job)
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
    return FileResponse(path, media_type="application/json", filename="results.json")


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
    return FileResponse(path, media_type="text/csv", filename="results.csv")


@router.get("/jobs", response_model=list[CrawlJob])
async def list_jobs() -> list[CrawlJob]:
    """List all crawl jobs (most recent first)."""
    return sorted(_jobs.values(), key=lambda j: j.created_at, reverse=True)


# ---------------------------------------------------------------------------
# ScrapeGraphAI tool endpoints
# ---------------------------------------------------------------------------
@router.post("/smart-scrape-multi", response_model=SmartScrapeResult, tags=["scrapegraphai"])
async def smart_scrape_multi(body: SmartScrapeMultiRequest) -> SmartScrapeResult:
    """Scrape multiple URLs with a single prompt using SmartScraperMultiGraph."""
    from app.utils.smart_scraper import smart_scrape_multi as _smart_scrape_multi

    try:
        result = await _smart_scrape_multi(body.urls, body.prompt)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    return SmartScrapeResult(result=result)


@router.post("/generate-script", response_model=ScriptResult, tags=["scrapegraphai"])
async def generate_script(body: ScriptCreatorRequest) -> ScriptResult:
    """Generate a Python scraping script for a single URL using ScriptCreatorGraph."""
    from app.utils.smart_scraper import generate_scraper_script

    try:
        script = await generate_scraper_script(body.url, body.prompt, body.library)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    return ScriptResult(script=script)


@router.post("/generate-script-multi", response_model=ScriptResult, tags=["scrapegraphai"])
async def generate_script_multi(body: ScriptCreatorMultiRequest) -> ScriptResult:
    """Generate a merged Python scraping script for multiple URLs."""
    from app.utils.smart_scraper import generate_scraper_script_multi

    try:
        script = await generate_scraper_script_multi(body.urls, body.prompt, body.library)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    return ScriptResult(script=script)


async def _run_preview(job: CrawlJob) -> None:
    """Background task: plan + scrape a single preview item."""
    await _orchestrator.run_preview(job)


async def _run_full(job: CrawlJob) -> None:
    """Background task: run the full crawl after user confirms preview."""
    await _orchestrator.run_full(job)
