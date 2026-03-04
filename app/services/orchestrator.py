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
from app.models.schemas import CrawlJob, CrawlStatus, ExtractionMethod
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
        """Run planning, scrape preview with both methods, compare, then pause."""
        try:
            req = job.request

            # ---- Stage 1: Planning (skip if template plan already set) ----
            if job.plan:
                log.info("[%s] Stage 1: Using pre-set plan (template) — skipping planner", job.id)
                plan = job.plan
            else:
                job.status = CrawlStatus.PLANNING
                job.updated_at = datetime.utcnow()
                log.info("[%s] Stage 1: Planning", job.id)
                plan = await self.planner.plan(
                    req.url,
                    detail_page_url=req.detail_page_url,
                    fields_wanted=req.fields_wanted,
                )
                job.plan = plan

            # ---- Stage 1b: Dual preview scrape ----
            job.status = CrawlStatus.SCRAPING
            job.updated_at = datetime.utcnow()
            log.info("[%s] Dual preview scrape", job.id)
            css_pages, smart_pages = await self.scraper.scrape_preview_dual(plan)

            # Parse CSS result
            if css_pages and css_pages[0].items:
                css_records = await self.parser.parse(css_pages, plan)
                if css_records:
                    job.preview_record_css = css_records[0]
                    job.preview_record = css_records[0]  # default fallback
                    log.info("[%s] CSS preview: %s", job.id, css_records[0].name)

            # Parse SmartScraperGraph result
            if smart_pages and smart_pages[0].items:
                smart_records = await self.parser.parse(smart_pages, plan)
                if smart_records:
                    job.preview_record_smart = smart_records[0]
                    log.info("[%s] Smart preview: %s", job.id, smart_records[0].name)

            # LLM comparison if both produced results
            if job.preview_record_css and job.preview_record_smart:
                recommendation, recommended_method = await self._compare_extractions(
                    job.preview_record_css, job.preview_record_smart
                )
                job.preview_recommendation = recommendation
                job.preview_recommended_method = recommended_method
                log.info("[%s] LLM recommends: %s", job.id, recommended_method)
            elif job.preview_record_css:
                job.preview_recommendation = "Only CSS extraction produced results. SmartScraperGraph returned no data."
                job.preview_recommended_method = ExtractionMethod.CSS
            elif job.preview_record_smart:
                job.preview_record = job.preview_record_smart
                job.preview_recommendation = "Only SmartScraperGraph extraction produced results. CSS selectors returned no data."
                job.preview_recommended_method = ExtractionMethod.SMART_SCRAPER
            else:
                log.warning("[%s] Neither extraction method produced results", job.id)
                job.error = "Could not extract any items from the page. The page may require different scraping settings or the content structure may not be recognized."

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

    async def _compare_extractions(
        self, css_record, smart_record
    ) -> tuple[str, ExtractionMethod]:
        """Use LLM to compare two extraction results and recommend the better one."""
        from app.config import settings

        try:
            from langchain_openai import AzureChatOpenAI

            llm = AzureChatOpenAI(
                azure_endpoint=settings.azure_openai_endpoint,
                api_key=settings.azure_openai_api_key,
                api_version=settings.azure_openai_api_version,
                azure_deployment=settings.azure_openai_deployment,
                temperature=0,
            )

            css_data = css_record.model_dump(exclude_none=True)
            smart_data = smart_record.model_dump(exclude_none=True)

            prompt = (
                "Compare these two extraction results from a web scraping preview.\n\n"
                f"**CSS Selector extraction:**\n{css_data}\n\n"
                f"**SmartScraperGraph (AI) extraction:**\n{smart_data}\n\n"
                "Which extraction method produced better results? Consider:\n"
                "1. Completeness — which has more non-null fields?\n"
                "2. Accuracy — which values look more correct/clean?\n"
                "3. Coverage — which captured more useful information?\n\n"
                "Respond with a short explanation (2-3 sentences) and end with "
                "exactly one of these lines:\n"
                "RECOMMENDATION: css\n"
                "RECOMMENDATION: smart_scraper"
            )

            response = await llm.ainvoke(prompt)
            text = response.content.strip()

            if "RECOMMENDATION: smart_scraper" in text:
                method = ExtractionMethod.SMART_SCRAPER
            else:
                method = ExtractionMethod.CSS

            # Strip the RECOMMENDATION line from the explanation
            explanation = text.split("RECOMMENDATION:")[0].strip()
            return explanation, method

        except Exception as exc:
            log.warning("LLM comparison failed: %s — defaulting to CSS", exc)
            return "Comparison failed, defaulting to CSS selectors (faster and more reliable).", ExtractionMethod.CSS

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
            # (skip replanning for template-based jobs — template plans are authoritative)
            if job.user_feedback and not job.request.template_id:
                job.status = CrawlStatus.PLANNING
                job.updated_at = datetime.utcnow()
                log.info("[%s] Re-planning with user feedback: %s", job.id, job.user_feedback)
                plan = await self.planner.replan(plan, job.user_feedback)
                job.plan = plan
            elif job.user_feedback:
                log.info("[%s] Template mode — skipping replan (feedback: %s)", job.id, job.user_feedback)

            # ---- Stage 2: Full Scraping ----
            job.status = CrawlStatus.SCRAPING
            job.updated_at = datetime.utcnow()
            log.info("[%s] Stage 2: Full scraping (method=%s)", job.id, job.extraction_method)
            page_data_list = await self.scraper.scrape(
                plan, max_items=job.request.max_items,
                extraction_method=job.extraction_method,
            )
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
