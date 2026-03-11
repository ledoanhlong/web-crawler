"""API routes for the web crawler service."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, HTTPException
from fastapi.responses import FileResponse

from app.config import settings
from app.models.schemas import (
    ConfirmPreviewRequest,
    CrawlJob,
    CrawlRequest,
    CrawlStatus,
    FailureCategory,
    FailureEvent,
    PipelineStage,
    ScriptCreatorMultiRequest,
    ScriptCreatorRequest,
    ScriptExecutionResult,
    ScriptResult,
    SmartCrawlRequest,
    SmartCrawlResult,
    SmartScrapeMultiRequest,
    SmartScrapeResult,
    UpdatePlanRequest,
)
from app.models.schemas import TemplateHints
from app.services.orchestrator import Orchestrator
from app.services.plan_cache import plan_cache
from app.utils.logging import get_logger

log = get_logger(__name__)

router = APIRouter(prefix="/api/v1", tags=["crawl"])

# In-memory job store (swap for Redis/DB in production)
_jobs: dict[str, CrawlJob] = {}
_orchestrator = Orchestrator()

# Limit concurrent crawl jobs to prevent resource exhaustion
_MAX_CONCURRENT_JOBS = 3
_job_semaphore = asyncio.Semaphore(_MAX_CONCURRENT_JOBS)


def _append_status_timeline(job: CrawlJob, status: CrawlStatus, reason: str) -> None:
    """Append a status transition entry to job diagnostics from API-level actions."""
    stamp = datetime.now(timezone.utc).isoformat()
    job.diagnostics.status_timeline.append(f"{stamp} status={status.value} reason={reason}")


def _append_failure(
    job: CrawlJob,
    *,
    category: FailureCategory,
    stage: PipelineStage,
    message: str,
    retryable: bool,
    details: dict[str, str] | None = None,
) -> None:
    """Record a normalized failure event for API-level state transitions."""
    job.diagnostics.failures.append(
        FailureEvent(
            category=category,
            stage=stage,
            message=message,
            retryable=retryable,
            details=details or {},
        )
    )


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
        job.updated_at = datetime.now(timezone.utc)
        _append_status_timeline(job, CrawlStatus.FAILED, "user_rejected_preview")
        _append_failure(
            job,
            category=FailureCategory.QUALITY_THRESHOLD,
            stage=PipelineStage.PLANNING,
            message="User rejected preview output",
            retryable=True,
            details={"feedback": body.feedback or ""},
        )
        return job

    # Store user feedback and extraction method choice
    job.user_feedback = body.feedback
    job.extraction_method = body.extraction_method
    background_tasks.add_task(_run_full, job)
    return job


@router.post("/crawl/{job_id}/update-plan", response_model=CrawlJob)
async def update_plan(
    job_id: str,
    body: UpdatePlanRequest,
) -> CrawlJob:
    """Edit plan fields while the job is in PLAN_REVIEW state."""
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status != CrawlStatus.PLAN_REVIEW:
        raise HTTPException(
            status_code=400,
            detail=f"Job is not in plan_review state (current: {job.status.value})",
        )
    if not job.plan:
        raise HTTPException(status_code=400, detail="Job has no plan to update")

    plan = job.plan
    if body.pagination is not None:
        plan.pagination = body.pagination
    if body.requires_javascript is not None:
        plan.requires_javascript = body.requires_javascript
    if body.item_container_selector is not None:
        plan.target.item_container_selector = body.item_container_selector
    if body.detail_link_selector is not None:
        plan.target.detail_link_selector = body.detail_link_selector
    if body.max_pages is not None:
        job.request.max_pages = body.max_pages

    job.updated_at = datetime.now(timezone.utc)
    _append_status_timeline(job, CrawlStatus.PLAN_REVIEW, "plan_updated_by_user")
    return job


@router.post("/crawl/{job_id}/approve-plan", response_model=CrawlJob, status_code=202)
async def approve_plan(
    job_id: str,
    background_tasks: BackgroundTasks,
) -> CrawlJob:
    """Approve the plan and start the preview scrape."""
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status != CrawlStatus.PLAN_REVIEW:
        raise HTTPException(
            status_code=400,
            detail=f"Job is not in plan_review state (current: {job.status.value})",
        )

    background_tasks.add_task(_run_preview_scrape, job)
    return job


@router.post("/crawl/{job_id}/reanalyze", response_model=CrawlJob, status_code=202)
async def reanalyze_plan(
    job_id: str,
    body: UpdatePlanRequest,
    background_tasks: BackgroundTasks,
) -> CrawlJob:
    """Re-run the planner with optional user feedback, producing a new plan."""
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status != CrawlStatus.PLAN_REVIEW:
        raise HTTPException(
            status_code=400,
            detail=f"Job is not in plan_review state (current: {job.status.value})",
        )

    # Store feedback for the planner
    if body.feedback:
        job.user_feedback = body.feedback

    _append_status_timeline(job, CrawlStatus.PLAN_REVIEW, "reanalyze_requested")
    background_tasks.add_task(_run_plan_only, job)
    return job


@router.post("/crawl/{job_id}/abort", response_model=CrawlJob)
async def abort_job(job_id: str) -> CrawlJob:
    """Abort a job in plan_review or preview state."""
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status not in (CrawlStatus.PLAN_REVIEW, CrawlStatus.PREVIEW):
        raise HTTPException(
            status_code=400,
            detail=f"Can only abort jobs in plan_review or preview state (current: {job.status.value})",
        )

    job.status = CrawlStatus.FAILED
    job.error = "Crawl aborted by user."
    job.updated_at = datetime.now(timezone.utc)
    _append_status_timeline(job, CrawlStatus.FAILED, "user_aborted")
    return job


@router.post("/crawl/{job_id}/resume", response_model=CrawlJob, status_code=202)
async def resume_crawl(
    job_id: str,
    background_tasks: BackgroundTasks,
) -> CrawlJob:
    """Resume a partial or failed crawl, fetching only remaining detail pages."""
    original = _jobs.get(job_id)
    if not original:
        raise HTTPException(status_code=404, detail="Job not found")
    if original.status not in (CrawlStatus.PARTIAL, CrawlStatus.FAILED):
        raise HTTPException(
            status_code=400,
            detail=f"Can only resume partial or failed jobs (current: {original.status.value})",
        )
    if not original.pending_detail_urls:
        raise HTTPException(
            status_code=400,
            detail="No remaining detail pages to resume",
        )

    # Create a new job that inherits plan and extraction method
    new_job = CrawlJob(request=original.request)
    new_job.plan = original.plan
    new_job.extraction_method = original.extraction_method
    new_job.resume_from_job_id = original.id
    _jobs[new_job.id] = new_job
    background_tasks.add_task(_run_resume, new_job, original)
    return new_job


@router.get("/crawl/{job_id}", response_model=CrawlJob)
async def get_crawl_status(job_id: str) -> CrawlJob:
    """Check the status (and result) of a crawl job."""
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@router.get("/crawl/{job_id}/diagnostics", tags=["crawl"])
async def get_crawl_diagnostics(job_id: str) -> dict:
    """Return structured diagnostics for confidence, failures, and status timeline."""
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    diagnostics = job.diagnostics.model_dump(mode="json")
    # Backward-compatible payload shape expected by existing clients/tests.
    return {
        "counters": diagnostics.get("counters", {}),
        "stage_confidences": diagnostics.get("stage_confidences", []),
        "failures": diagnostics.get("failures", []),
        "status_timeline": diagnostics.get("status_timeline", []),
        "parser_metrics": diagnostics.get("parser_metrics", {}),
    }


@router.get("/crawl/{job_id}/telemetry", tags=["crawl"])
async def get_crawl_telemetry(job_id: str) -> dict:
    """Return provider-level telemetry (usage, costs, fallbacks, circuit state)."""
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    diagnostics = job.diagnostics.model_dump(mode="json")
    return {
        "provider_events": diagnostics.get("provider_events", []),
        "provider_summary": diagnostics.get("provider_summary", {}),
    }


@router.get("/crawl/{job_id}/json")
async def download_json(job_id: str) -> FileResponse:
    """Download the JSON output file for a completed crawl."""
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status not in (CrawlStatus.COMPLETED, CrawlStatus.PARTIAL) or not job.result or not job.result.json_path:
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
    if job.status not in (CrawlStatus.COMPLETED, CrawlStatus.PARTIAL) or not job.result or not job.result.csv_path:
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
        log.error("smart-scrape-multi failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail="Scraping failed. Check server logs for details.")
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
        log.error("generate-script failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail="Script generation failed. Check server logs for details.")

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
        log.error("generate-script-multi failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail="Script generation failed. Check server logs for details.")

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
            max_pages=body.max_pages,
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
            log.error("smart-crawl (smart_scraper) failed: %s", exc, exc_info=True)
            raise HTTPException(status_code=500, detail="Scraping failed. Check server logs for details.")

    elif decision.strategy == decision.strategy.SMART_SCRAPER_MULTI:
        try:
            data = await smart_scrape_multi(body.urls, body.prompt)
            result.data = data
        except Exception as exc:
            log.error("smart-crawl (smart_scraper_multi) failed: %s", exc, exc_info=True)
            raise HTTPException(status_code=500, detail="Scraping failed. Check server logs for details.")

    elif decision.strategy == decision.strategy.SCRIPT_CREATOR:
        try:
            script = await generate_scraper_script(
                body.urls[0], body.prompt, "beautifulsoup4",
            )
            result.script = script
            result.execution = await _execute_script_safe(script)
        except Exception as exc:
            log.error("smart-crawl (script_creator) failed: %s", exc, exc_info=True)
            raise HTTPException(status_code=500, detail="Script generation failed. Check server logs for details.")

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
    """Background task: plan the scrape, then pause at PLAN_REVIEW."""
    async with _job_semaphore:
        await _orchestrator.run_plan_only(job)


async def _run_preview_scrape(job: CrawlJob) -> None:
    """Background task: run dual preview scrape after plan approval."""
    async with _job_semaphore:
        await _orchestrator.run_preview_scrape(job)


async def _run_plan_only(job: CrawlJob) -> None:
    """Background task: re-run planning (for reanalyze)."""
    async with _job_semaphore:
        await _orchestrator.run_plan_only(job)


async def _run_full(job: CrawlJob) -> None:
    """Background task: run the full crawl after user confirms preview.

    Uses a cooperative timeout via the orchestrator — the scraper stops
    gracefully between batches and saves partial results instead of
    losing all progress.
    """
    async with _job_semaphore:
        await _orchestrator.run_full(job, timeout_s=settings.max_job_duration_s)


async def _run_resume(job: CrawlJob, original_job: CrawlJob) -> None:
    """Background task: resume a partial crawl by fetching remaining detail pages."""
    async with _job_semaphore:
        await _orchestrator.run_resume(job, original_job, timeout_s=settings.max_job_duration_s)


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
