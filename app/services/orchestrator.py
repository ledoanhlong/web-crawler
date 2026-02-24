"""Orchestrator — Coordinates the four-agent pipeline for a crawl job.

Pipeline:
    PlannerAgent  →  ScraperAgent  →  ParserAgent  →  OutputAgent

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

    async def run(self, job: CrawlJob) -> CrawlJob:
        try:
            # ---- Stage 1: Planning ----
            job.status = CrawlStatus.PLANNING
            job.updated_at = datetime.utcnow()
            log.info("[%s] Stage 1/4: Planning", job.id)
            plan = await self.planner.plan(job.request.url)
            job.plan = plan

            # ---- Stage 2: Scraping ----
            job.status = CrawlStatus.SCRAPING
            job.updated_at = datetime.utcnow()
            log.info("[%s] Stage 2/4: Scraping", job.id)
            page_data_list = await self.scraper.scrape(plan)
            total_items = sum(len(pd.items) for pd in page_data_list)
            log.info("[%s] Scraped %d items from %d pages", job.id, total_items, len(page_data_list))

            # ---- Stage 3: Parsing ----
            job.status = CrawlStatus.PARSING
            job.updated_at = datetime.utcnow()
            log.info("[%s] Stage 3/4: Parsing", job.id)
            records = await self.parser.parse(page_data_list, plan)

            # ---- Stage 4: Output ----
            job.status = CrawlStatus.OUTPUT
            job.updated_at = datetime.utcnow()
            log.info("[%s] Stage 4/4: Building output", job.id)
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
