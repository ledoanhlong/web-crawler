"""Orchestrator — Coordinates the four-agent pipeline for a crawl job.

Pipeline:
    PlannerAgent  →  Preview (1 item)  →  User confirms  →  ScraperAgent  →  ParserAgent  →  OutputAgent

After the planning stage the orchestrator scrapes a single item (including its
detail page), parses it, and pauses with status ``PREVIEW`` so the user can
validate the output.  Once the user confirms (via ``POST /confirm``), the full
crawl resumes.  If the user provides feedback, the planner re-plans using that
feedback before continuing.

The user can supply optional hints at submission time:
- ``detail_page_url`` — an example detail page for the planner to analyse
- ``fields_wanted`` — fields the user wants extracted
- ``test_single`` — if true, output just the 1 preview record (no full crawl)

Each stage updates the CrawlJob status so progress can be tracked via the API.
"""

from __future__ import annotations

from datetime import datetime

from app.agents.output_agent import OutputAgent
from app.agents.parser_agent import ParserAgent
from app.agents.planner_agent import PlannerAgent
from app.agents.scraper_agent import ScraperAgent
from app.models.schemas import CrawlJob, CrawlStatus
from app.utils.logging import get_logger

log = get_logger(__name__)


class Orchestrator:
    """Run the full crawl pipeline for a CrawlJob."""

    def __init__(self) -> None:
        self.planner = PlannerAgent()
        self.scraper = ScraperAgent()
        self.parser = ParserAgent()
        self.output = OutputAgent()

    # ------------------------------------------------------------------
    # Phase 1: plan + preview (called when the job is first submitted)
    # ------------------------------------------------------------------
    async def run_preview(self, job: CrawlJob) -> CrawlJob:
        """Run planning, scrape a single preview item, parse it, then pause."""
        try:
            req = job.request

            # ---- Stage 1: Planning ----
            job.status = CrawlStatus.PLANNING
            job.updated_at = datetime.utcnow()
            log.info("[%s] Stage 1: Planning", job.id)
            plan = await self.planner.plan(
                req.url,
                detail_page_url=req.detail_page_url,
                fields_wanted=req.fields_wanted,
            )
            job.plan = plan

            # ---- Stage 1b: Scrape one preview item ----
            job.status = CrawlStatus.SCRAPING
            job.updated_at = datetime.utcnow()
            log.info("[%s] Scraping single preview item", job.id)
            preview_pages = await self.scraper.scrape_preview(plan)

            if preview_pages and preview_pages[0].items:
                # Parse the single item
                records = await self.parser.parse(preview_pages, plan)
                if records:
                    job.preview_record = records[0]
                    log.info("[%s] Preview record: %s", job.id, records[0].name)

            # ---- Pause for user validation ----
            job.status = CrawlStatus.PREVIEW
            job.updated_at = datetime.utcnow()
            log.info("[%s] Preview ready — waiting for user confirmation", job.id)

        except Exception as exc:
            job.status = CrawlStatus.FAILED
            job.error = str(exc)
            job.updated_at = datetime.utcnow()
            log.error("[%s] Failed during preview: %s", job.id, exc, exc_info=True)

        return job

    # ------------------------------------------------------------------
    # Phase 2: full crawl (called after user confirms the preview)
    # ------------------------------------------------------------------
    async def run_full(self, job: CrawlJob) -> CrawlJob:
        """Continue the full crawl after user confirmation."""
        try:
            plan = job.plan
            if not plan:
                raise RuntimeError("Cannot run full crawl without a plan")

            # If test_single mode — just output the preview record, skip scraping
            if job.request.test_single:
                log.info("[%s] Test-single mode — outputting preview record only", job.id)
                job.status = CrawlStatus.OUTPUT
                job.updated_at = datetime.utcnow()
                records = [job.preview_record] if job.preview_record else []
                result = await self.output.build_output(records, job.id)
                job.result = result
                job.status = CrawlStatus.COMPLETED
                job.updated_at = datetime.utcnow()
                log.info("[%s] Test-single completed — %d record(s)", job.id, len(records))
                return job

            # If the user gave feedback, re-plan with that context
            if job.user_feedback:
                job.status = CrawlStatus.PLANNING
                job.updated_at = datetime.utcnow()
                log.info("[%s] Re-planning with user feedback: %s", job.id, job.user_feedback)
                plan = await self.planner.replan(plan, job.user_feedback)
                job.plan = plan

            # ---- Stage 2: Full Scraping ----
            job.status = CrawlStatus.SCRAPING
            job.updated_at = datetime.utcnow()
            log.info("[%s] Stage 2: Full scraping", job.id)
            page_data_list = await self.scraper.scrape(plan)
            total_items = sum(len(pd.items) for pd in page_data_list)
            log.info("[%s] Scraped %d items from %d pages", job.id, total_items, len(page_data_list))

            # ---- Stage 3: Parsing ----
            job.status = CrawlStatus.PARSING
            job.updated_at = datetime.utcnow()
            log.info("[%s] Stage 3: Parsing", job.id)
            records = await self.parser.parse(page_data_list, plan)

            # ---- Stage 4: Output ----
            job.status = CrawlStatus.OUTPUT
            job.updated_at = datetime.utcnow()
            log.info("[%s] Stage 4: Building output", job.id)
            result = await self.output.build_output(records, job.id)
            job.result = result

            # ---- Done ----
            job.status = CrawlStatus.COMPLETED
            job.updated_at = datetime.utcnow()
            log.info(
                "[%s] Completed — %d records, JSON=%s, CSV=%s",
                job.id,
                len(result.records),
                result.json_path,
                result.csv_path,
            )

        except Exception as exc:
            job.status = CrawlStatus.FAILED
            job.error = str(exc)
            job.updated_at = datetime.utcnow()
            log.error("[%s] Failed: %s", job.id, exc, exc_info=True)

        return job
