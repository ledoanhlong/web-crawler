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
    ScriptExecutionResult,
    ScriptResult,
    SmartCrawlRequest,
    SmartCrawlResult,
    SmartScrapeMultiRequest,
    SmartScrapeResult,
)
from app.models.schemas import TemplateHints
from app.services.orchestrator import Orchestrator
from app.services.plan_cache import plan_cache

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

    # Store user feedback and extraction method choice
    job.user_feedback = body.feedback
    job.extraction_method = body.extraction_method
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
    """Generate a Python scraping script for a single URL using ScriptCreatorGraph.

    When ``auto_execute`` is true (default), the script is also run in a
    sandboxed subprocess and the output is returned alongside the code.
    """
    from app.utils.smart_scraper import generate_scraper_script

    try:
        script = await generate_scraper_script(body.url, body.prompt, body.library)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    execution = None
    if body.auto_execute:
        execution = await _execute_script_safe(script)
    return ScriptResult(script=script, execution=execution)


@router.post("/generate-script-multi", response_model=ScriptResult, tags=["scrapegraphai"])
async def generate_script_multi(body: ScriptCreatorMultiRequest) -> ScriptResult:
    """Generate a merged Python scraping script for multiple URLs.

    When ``auto_execute`` is true (default), the script is also run in a
    sandboxed subprocess and the output is returned alongside the code.
    """
    from app.utils.smart_scraper import generate_scraper_script_multi

    try:
        script = await generate_scraper_script_multi(body.urls, body.prompt, body.library)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    execution = None
    if body.auto_execute:
        execution = await _execute_script_safe(script)
    return ScriptResult(script=script, execution=execution)


# ---------------------------------------------------------------------------
# Smart Crawl — intelligent routing endpoint
# ---------------------------------------------------------------------------
@router.post("/smart-crawl", response_model=SmartCrawlResult, tags=["smart-crawl"])
async def smart_crawl(
    body: SmartCrawlRequest,
    background_tasks: BackgroundTasks,
) -> SmartCrawlResult:
    """Intelligent scraping endpoint that auto-selects the best method.

    The RouterAgent analyses the URL(s) and prompt, then dispatches to:
    - Full pipeline (async job) for listing pages with pagination
    - SmartScraperGraph for single-page extraction
    - SmartScraperMultiGraph for multi-URL extraction
    - ScriptCreatorGraph + auto-execute for script generation
    """
    from app.agents.router_agent import RouterAgent
    from app.utils.smart_scraper import generate_scraper_script, smart_scrape_multi

    router_agent = RouterAgent()
    decision = await router_agent.route(
        body.urls,
        body.prompt,
        fields_wanted=body.fields_wanted,
        detail_page_url=body.detail_page_url,
    )

    result = SmartCrawlResult(
        strategy_used=decision.strategy.value,
        strategy_explanation=decision.explanation,
    )

    if decision.strategy == decision.strategy.FULL_PIPELINE:
        # Kick off the async pipeline — return a job_id for polling
        crawl_req = CrawlRequest(
            url=body.urls[0],
            fields_wanted=body.fields_wanted,
            item_description=body.item_description,
            site_notes=body.site_notes,
            detail_page_url=body.detail_page_url,
            pagination_type=body.pagination_type,
            max_items=body.max_items,
            test_single=body.test_single,
            page_type=body.page_type,
            rendering_type=body.rendering_type,
            detail_page_type=body.detail_page_type,
        )
        job = CrawlJob(request=crawl_req)

        # Infer template hints from user's multi-choice answers
        hints = _infer_hints(body.rendering_type, body.detail_page_type)
        if hints:
            job.template_hints = hints

        _jobs[job.id] = job
        background_tasks.add_task(_run_preview, job)
        result.job_id = job.id

    elif decision.strategy == decision.strategy.SMART_SCRAPER:
        # Direct single-page extraction (SmartScraperMultiGraph handles fetching)
        try:
            data = await smart_scrape_multi(body.urls[:1], body.prompt)
            result.data = data
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc))

    elif decision.strategy == decision.strategy.SMART_SCRAPER_MULTI:
        try:
            data = await smart_scrape_multi(body.urls, body.prompt)
            result.data = data
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc))

    elif decision.strategy == decision.strategy.SCRIPT_CREATOR:
        try:
            script = await generate_scraper_script(
                body.urls[0], body.prompt, "beautifulsoup4",
            )
            result.script = script
            result.execution = await _execute_script_safe(script)
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc))

    return result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _infer_hints(
    rendering_type: str | None,
    detail_page_type: str | None,
) -> TemplateHints | None:
    """Build TemplateHints from the user's multi-choice answers."""
    if not rendering_type and not detail_page_type:
        return None  # All auto-detect — let the planner figure it out

    return TemplateHints(
        requires_javascript=(rendering_type == "dynamic") if rendering_type else True,
        has_detail_pages=(detail_page_type == "separate_page"),
        has_detail_api=(detail_page_type == "popup_overlay"),
    )


async def _execute_script_safe(script: str) -> ScriptExecutionResult:
    """Execute a script and return a ScriptExecutionResult model."""
    from app.utils.script_executor import execute_script

    raw = await execute_script(script)
    return ScriptExecutionResult(**raw)


async def _run_preview(job: CrawlJob) -> None:
    """Background task: plan + scrape a single preview item."""
    await _orchestrator.run_preview(job)


async def _run_full(job: CrawlJob) -> None:
    """Background task: run the full crawl after user confirms preview."""
    await _orchestrator.run_full(job)


# ---------------------------------------------------------------------------
# Plan cache endpoints
# ---------------------------------------------------------------------------
@router.get("/plan-cache", tags=["plan-cache"])
async def list_plan_cache() -> list[dict]:
    """List all cached scraping plans."""
    return plan_cache.list_entries()


@router.delete("/plan-cache", tags=["plan-cache"])
async def clear_plan_cache() -> dict:
    """Clear all cached plans."""
    count = plan_cache.clear()
    return {"cleared": count}


@router.delete("/plan-cache/{url:path}", tags=["plan-cache"])
async def invalidate_plan_cache(url: str) -> dict:
    """Invalidate the cached plan for a specific URL."""
    found = plan_cache.invalidate(f"https://{url}" if not url.startswith("http") else url)
    return {"invalidated": found}
