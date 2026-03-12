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

import asyncio
import logging
import time
from datetime import datetime, timezone

from app.agents.output_agent import OutputAgent
from app.agents.parser_agent import ParserAgent
from app.agents.planner_agent import PlannerAgent
from app.agents.scraper_agent import ScraperAgent
from app.config import settings
from app.models.schemas import (
    CrawlJob,
    CrawlStatus,
    ExtractionMethod,
    SellerLead,
    FailureCategory,
    FailureEvent,
    PipelineStage,
    ProviderTelemetryEvent,
    StageConfidence,
)
from app.services.plan_cache import plan_cache
from app.utils.fingerprint import fingerprint as fingerprint_page
from app.utils.http import fetch_page_full
from app.utils.llm import get_claude_runtime_state
from app.utils.logging import get_logger, log_kv
from app.utils.quality import evaluate_quality

log = get_logger(__name__)


class _SwitchMethodRequested(RuntimeError):
    """Internal signal to stop current scrape attempt and retry another method."""


class Orchestrator:
    """Run the full crawl pipeline for a CrawlJob."""

    def __init__(self) -> None:
        self.planner = PlannerAgent()
        self.scraper = ScraperAgent()
        self.parser = ParserAgent()
        self.output = OutputAgent()

    @staticmethod
    def _to_stage(status: CrawlStatus) -> PipelineStage:
        if status in (CrawlStatus.PENDING, CrawlStatus.PLANNING, CrawlStatus.PLAN_REVIEW, CrawlStatus.PREVIEW):
            return PipelineStage.PLANNING
        if status == CrawlStatus.SCRAPING:
            return PipelineStage.SCRAPING
        if status == CrawlStatus.PARSING:
            return PipelineStage.PARSING
        return PipelineStage.OUTPUT

    def _set_status(self, job: CrawlJob, status: CrawlStatus, reason: str = "") -> None:
        job.status = status
        job.updated_at = datetime.now(timezone.utc)
        stamp = job.updated_at.isoformat()
        line = f"{stamp} status={status.value} reason={reason}" if reason else f"{stamp} status={status.value}"
        job.diagnostics.status_timeline.append(line)
        log_kv(log, logging.INFO, "job_status", job_id=job.id, status=status.value, reason=reason)

    def _record_confidence(self, job: CrawlJob, stage: PipelineStage, score: float, reason: str) -> None:
        bounded = max(0.0, min(1.0, score))
        job.diagnostics.stage_confidences.append(
            StageConfidence(stage=stage, score=bounded, reason=reason)
        )
        log_kv(
            log,
            logging.INFO,
            "stage_confidence",
            job_id=job.id,
            stage=stage.value,
            score=round(bounded, 3),
            reason=reason,
        )

    def _record_failure(
        self,
        job: CrawlJob,
        *,
        category: FailureCategory,
        stage: PipelineStage,
        message: str,
        retryable: bool,
        details: dict[str, str] | None = None,
    ) -> None:
        job.diagnostics.failures.append(
            FailureEvent(
                category=category,
                stage=stage,
                message=message,
                retryable=retryable,
                details=details or {},
            )
        )
        log_kv(
            log,
            logging.WARNING,
            "failure_event",
            job_id=job.id,
            category=category.value,
            stage=stage.value,
            retryable=retryable,
            failure_message=message,
        )

    def _record_provider_event(
        self,
        job: CrawlJob,
        *,
        provider: str,
        stage: PipelineStage,
        method: str,
        latency_ms: float,
        fallback_reason: str | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        estimated_cost_usd: float | None = None,
    ) -> None:
        if estimated_cost_usd is None and provider == "openai":
            # Heuristic estimate when upstream tools do not expose token usage.
            estimated_cost_usd = 0.002 if method in ("css", "auto") else 0.006

        event = ProviderTelemetryEvent(
            provider=provider,
            stage=stage,
            method=method,
            latency_ms=round(latency_ms, 2),
            fallback_reason=fallback_reason,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            estimated_cost_usd=round(float(estimated_cost_usd), 6) if estimated_cost_usd is not None else None,
        )
        job.diagnostics.provider_events.append(event)

    @staticmethod
    def _preview_quality_score(record: object) -> float:
        """Compute deterministic quality score for a preview SellerLead-like object."""
        # Prioritize core business-contact fields for robust cross-site comparison.
        fields = [
            "name", "website", "email", "phone", "country", "city", "address",
            "description", "logo_url", "marketplace_name", "store_url",
        ]
        score = 0.0
        for field in fields:
            val = getattr(record, field, None)
            if isinstance(val, str):
                if val.strip():
                    score += 1.0
            elif val:
                score += 1.0

        # Reward additional structured coverage beyond core string fields.
        cats = getattr(record, "product_categories", None)
        brands = getattr(record, "brands", None)
        social = getattr(record, "social_media", None)
        if isinstance(cats, list) and cats:
            score += 0.75
        if isinstance(brands, list) and brands:
            score += 0.75
        if isinstance(social, dict) and social:
            score += 0.5
        return score

    def _select_preview_method_deterministic(
        self,
        candidates: dict[ExtractionMethod, object],
    ) -> tuple[ExtractionMethod, float, float]:
        """Return best method + best score + score margin over second best."""
        scored = [
            (method, self._preview_quality_score(record))
            for method, record in candidates.items()
        ]
        scored.sort(key=lambda item: item[1], reverse=True)
        best_method, best_score = scored[0]
        second_score = scored[1][1] if len(scored) > 1 else 0.0
        return best_method, best_score, max(0.0, best_score - second_score)

    @staticmethod
    def _build_method_attempt_order(preferred: ExtractionMethod | None) -> list[ExtractionMethod | None]:
        """Build ordered extraction method attempts for guarded fallback retries."""
        claude_order = [ExtractionMethod.CLAUDE]
        base_order: list[ExtractionMethod | None] = [
            preferred,
            ExtractionMethod.CSS,
            ExtractionMethod.SMART_SCRAPER,
            ExtractionMethod.CRAWL4AI,
            ExtractionMethod.UNIVERSAL_SCRAPER,
            *claude_order,
            None,  # Final auto mode in scraper
        ]
        ordered: list[ExtractionMethod | None] = []
        for method in base_order:
            if method not in ordered:
                ordered.append(method)
        return ordered

    def _apply_quality_gate(self, job: CrawlJob) -> bool:
        """Apply optional quality threshold policy; returns True if threshold passes."""
        min_score = max(0.0, float(settings.reliability_quality_min_score))
        if min_score <= 0:
            return True
        qr = job.quality_report or {}
        score = float(qr.get("overall_score", 0.0) or 0.0)
        if score >= min_score:
            return True

        self._record_failure(
            job,
            category=FailureCategory.QUALITY_THRESHOLD,
            stage=PipelineStage.OUTPUT,
            message=(
                f"Quality score {score:.3f} below configured threshold {min_score:.3f}"
            ),
            retryable=True,
            details={"quality_score": f"{score:.3f}", "min_score": f"{min_score:.3f}"},
        )
        if settings.reliability_quality_enforce:
            self._set_status(job, CrawlStatus.PARTIAL, "quality_gate_below_threshold")
            if not job.error:
                job.error = (
                    f"Quality score {score:.3f} is below threshold {min_score:.3f}."
                )
        return False

    @staticmethod
    def _compute_parser_confidence(
        records: list[SellerLead],
    ) -> tuple[float, str, dict[str, int | float]]:
        """Score parser quality from field completeness and structured coverage."""
        if not records:
            return 0.2, "Parser produced no records", {
                "record_count": 0,
                "non_empty_fields": 0,
                "total_fields": 0,
                "structured_non_empty": 0,
                "structured_total": 0,
                "name_present": 0,
            }

        scalar_fields = [
            "name", "website", "email", "phone", "country", "city",
            "address", "description", "marketplace_name", "source_url",
        ]
        structured_fields = ["product_categories", "brands", "social_media"]

        non_empty_fields = 0
        total_fields = len(records) * len(scalar_fields)
        structured_non_empty = 0
        structured_total = len(records) * len(structured_fields)
        name_present = 0

        for record in records:
            if (record.name or "").strip():
                name_present += 1

            for field in scalar_fields:
                val = getattr(record, field, None)
                if isinstance(val, str):
                    if val.strip():
                        non_empty_fields += 1
                elif val:
                    non_empty_fields += 1

            for field in structured_fields:
                val = getattr(record, field, None)
                if isinstance(val, list) and val:
                    structured_non_empty += 1
                elif isinstance(val, dict) and val:
                    structured_non_empty += 1

        scalar_ratio = (non_empty_fields / total_fields) if total_fields else 0.0
        structured_ratio = (structured_non_empty / structured_total) if structured_total else 0.0
        name_ratio = (name_present / len(records)) if records else 0.0

        score = 0.2 + (0.6 * scalar_ratio) + (0.15 * structured_ratio) + (0.05 * name_ratio)
        score = max(0.0, min(1.0, score))

        reason = (
            f"Parser coverage scalar={scalar_ratio:.2f}, "
            f"structured={structured_ratio:.2f}, names={name_ratio:.2f}"
        )
        metrics = {
            "record_count": len(records),
            "non_empty_fields": non_empty_fields,
            "total_fields": total_fields,
            "structured_non_empty": structured_non_empty,
            "structured_total": structured_total,
            "name_present": name_present,
            "scalar_ratio": round(scalar_ratio, 4),
            "structured_ratio": round(structured_ratio, 4),
            "name_ratio": round(name_ratio, 4),
        }
        return score, reason, metrics

    @staticmethod
    def _should_switch_method(*, pages_processed: int, zero_item_streak: int) -> bool:
        """Decide when an attempt is unhealthy enough to switch extraction method."""
        if not settings.reliability_auto_switch_enabled:
            return False
        return (
            pages_processed >= settings.reliability_auto_switch_min_pages
            and zero_item_streak >= settings.reliability_auto_switch_zero_streak
        )

    # ------------------------------------------------------------------
    # Phase 1a: plan only (called when the job is first submitted)
    # ------------------------------------------------------------------
    async def run_plan_only(self, job: CrawlJob) -> CrawlJob:
        """Run planning + fingerprinting, then pause at PLAN_REVIEW for user approval."""
        try:
            req = job.request

            # ---- Log request details ----
            log.info(
                "[%s] New crawl request — URL: %s | fields_wanted: %s | "
                "item_description: %s | site_notes/prompt: %s | "
                "pagination_type: %s | detail_page_url: %s | max_items: %s | "
                "test_single: %s",
                job.id,
                req.url,
                req.fields_wanted or '(auto)',
                req.item_description or '(none)',
                req.site_notes or '(none)',
                req.pagination_type or '(auto-detect)',
                req.detail_page_url or '(auto)',
                req.max_items or '(all)',
                req.test_single,
            )

            # ---- Stage 1: Planning ----
            template_hints = job.template_hints

            # Check plan cache first (only when no template hints override)
            cached = None
            if not template_hints:
                cached_data = plan_cache.get(req.url)
                if cached_data:
                    from app.models.schemas import ScrapingPlan
                    try:
                        plan = ScrapingPlan.model_validate(cached_data)
                        job.plan = plan
                        log.info("[%s] Stage 1: Using cached plan", job.id)
                        cached = True
                    except Exception:
                        pass

            if not cached:
                self._set_status(job, CrawlStatus.PLANNING, "planner_start")
                # If user provided feedback for re-analysis, use replan
                if job.user_feedback and job.plan:
                    log.info("[%s] Stage 1: Re-planning with user feedback", job.id)
                    plan = await self.planner.replan(job.plan, job.user_feedback)
                    job.user_feedback = None  # consumed
                else:
                    if template_hints:
                        log.info("[%s] Stage 1: Planning with structural hints", job.id)
                    else:
                        log.info("[%s] Stage 1: Planning", job.id)
                    plan = await self.planner.plan(
                        req.url,
                        detail_page_url=req.detail_page_url,
                        fields_wanted=req.fields_wanted,
                        item_description=req.item_description,
                        site_notes=req.site_notes,
                        template_hints=template_hints,
                        pagination_type=req.pagination_type,
                    )
                job.plan = plan
                has_pagination_signal = bool(plan.pagination_selector or plan.pagination_urls)
                planning_score = 0.85 if plan.target.field_selectors else 0.55
                if plan.requires_javascript and plan.wait_selector:
                    planning_score += 0.05
                if has_pagination_signal:
                    planning_score += 0.05
                self._record_confidence(
                    job,
                    PipelineStage.PLANNING,
                    planning_score,
                    "Plan produced selectors and runtime hints",
                )

            # ---- Platform fingerprinting ----
            try:
                result = await fetch_page_full(req.url)
                pinfo = fingerprint_page(result.text, result.headers)
                job.platform_info = pinfo.to_dict()
            except Exception as exc:
                log.debug("[%s] Fingerprinting skipped: %s", job.id, exc)

            # ---- Pause for user plan review ----
            self._set_status(job, CrawlStatus.PLAN_REVIEW, "await_plan_review")
            log.info("[%s] Plan ready — waiting for user review", job.id)

        except Exception as exc:
            stage = self._to_stage(job.status)
            self._record_failure(
                job,
                category=FailureCategory.UNKNOWN,
                stage=stage,
                message=str(exc),
                retryable=False,
            )
            self._set_status(job, CrawlStatus.FAILED, "plan_exception")
            job.error = str(exc)
            log.error("[%s] Failed during planning: %s", job.id, exc, exc_info=True)

        return job

    # ------------------------------------------------------------------
    # Phase 1b: preview scrape (called after user approves the plan)
    # ------------------------------------------------------------------
    async def run_preview_scrape(self, job: CrawlJob) -> CrawlJob:
        """Run dual preview scrape using the approved plan, then pause at PREVIEW."""
        try:
            plan = job.plan

            # ---- Dual preview scrape ----
            self._set_status(job, CrawlStatus.SCRAPING, "preview_scrape")
            log.info("[%s] Dual preview scrape", job.id)
            css_pages, smart_pages, crawl4ai_pages, us_pages, listing_api_pages, claude_pages = await self.scraper.scrape_preview_dual(plan)

            # Parse CSS result
            if css_pages and css_pages[0].items:
                css_records = await self.parser.parse(css_pages, plan)
                if css_records:
                    job.preview_record_css = css_records[0]
                    job.preview_record = css_records[0]  # default fallback
                    # Store all preview items for multi-item preview table
                    job.preview_items = [r.model_dump(exclude_none=True) for r in css_records[:5]]
                    log.info("[%s] CSS preview: %d items, first=%s", job.id, len(css_records), css_records[0].name)

            # Parse SmartScraperGraph result
            if smart_pages and smart_pages[0].items:
                smart_records = await self.parser.parse(smart_pages, plan)
                if smart_records:
                    job.preview_record_smart = smart_records[0]
                    log.info("[%s] Smart preview: %s", job.id, smart_records[0].name)

            # Parse Crawl4AI result
            if crawl4ai_pages and crawl4ai_pages[0].items:
                c4_records = await self.parser.parse(crawl4ai_pages, plan)
                if c4_records:
                    job.preview_record_crawl4ai = c4_records[0]
                    log.info("[%s] Crawl4AI preview: %s", job.id, c4_records[0].name)

            # Parse universal-scraper result
            if us_pages and us_pages[0].items:
                us_records = await self.parser.parse(us_pages, plan)
                if us_records:
                    job.preview_record_universal_scraper = us_records[0]
                    log.info("[%s] universal-scraper preview: %s", job.id, us_records[0].name)

            # Parse listing API result
            if listing_api_pages and listing_api_pages[0].items:
                la_records = await self.parser.parse(listing_api_pages, plan)
                if la_records:
                    job.preview_record_listing_api = la_records[0]
                    log.info("[%s] Listing API preview: %s", job.id, la_records[0].name)

            # Parse Claude result
            if claude_pages and claude_pages[0].items:
                claude_records = await self.parser.parse(claude_pages, plan)
                if claude_records:
                    job.preview_record_claude = claude_records[0]
                    log.info("[%s] Claude preview: %s", job.id, claude_records[0].name)

            # LLM comparison — include all methods that produced results
            candidates: dict[ExtractionMethod, object] = {}
            if job.preview_record_css:
                candidates[ExtractionMethod.CSS] = job.preview_record_css
            if job.preview_record_smart:
                candidates[ExtractionMethod.SMART_SCRAPER] = job.preview_record_smart
            if job.preview_record_crawl4ai:
                candidates[ExtractionMethod.CRAWL4AI] = job.preview_record_crawl4ai
            if job.preview_record_universal_scraper:
                candidates[ExtractionMethod.UNIVERSAL_SCRAPER] = job.preview_record_universal_scraper
            if job.preview_record_listing_api:
                candidates[ExtractionMethod.LISTING_API] = job.preview_record_listing_api
            if job.preview_record_claude and not settings.claude_fallback_only:
                candidates[ExtractionMethod.CLAUDE] = job.preview_record_claude

            if len(candidates) >= 2:
                recommended_method, best_score, margin = self._select_preview_method_deterministic(candidates)
                if margin >= settings.reliability_preview_margin_threshold:
                    recommendation = (
                        "Deterministic scoring selected the best preview result based on "
                        "field completeness and contact coverage."
                    )
                    log.info(
                        "[%s] Deterministic preview winner: %s (score=%.2f, margin=%.2f)",
                        job.id,
                        recommended_method.value,
                        best_score,
                        margin,
                    )
                else:
                    recommendation, recommended_method = await self._compare_extractions(candidates)
                job.preview_recommendation = recommendation
                job.preview_recommended_method = recommended_method
                job.preview_record = candidates[recommended_method]
                log.info("[%s] Preview recommends: %s", job.id, recommended_method)
                self._record_confidence(
                    job,
                    PipelineStage.SCRAPING,
                    0.85,
                    f"Preview comparison selected {recommended_method.value}",
                )
            elif len(candidates) == 1:
                method, record = next(iter(candidates.items()))
                job.preview_record = record
                job.preview_recommendation = f"Only {method.value} extraction produced results."
                job.preview_recommended_method = method
                self._record_confidence(
                    job,
                    PipelineStage.SCRAPING,
                    0.65,
                    f"Only {method.value} produced preview data",
                )
            else:
                log.warning("[%s] No extraction method produced results", job.id)
                job.error = (
                    "Could not extract any items from the page. "
                    "The page may require different scraping settings or "
                    "the content structure may not be recognized."
                )
                self._record_confidence(
                    job,
                    PipelineStage.SCRAPING,
                    0.1,
                    "No preview candidates produced data",
                )
                self._record_failure(
                    job,
                    category=FailureCategory.SELECTOR_MISMATCH,
                    stage=PipelineStage.SCRAPING,
                    message="No extraction method produced preview items",
                    retryable=True,
                    details={"url": job.request.url},
                )

            # ---- Suggest script_creator if page complexity signals are poor ----
            complexity_reasons: list[str] = []

            if len(candidates) == 0:
                complexity_reasons.append("no extraction method could find items on this page")

            if job.plan:
                metrics = job.plan.selector_metrics or {}
                container_count = metrics.get("container_count", -1)
                field_hit_ratio = metrics.get("field_hit_ratio", -1)
                if 0 <= container_count < 3:
                    complexity_reasons.append(
                        f"CSS selectors matched only {container_count} item containers"
                    )
                if 0 <= field_hit_ratio < 0.3:
                    complexity_reasons.append(
                        f"only {field_hit_ratio:.0%} of field selectors found data"
                    )

            if len(candidates) == 1 and job.preview_record:
                core_fields = {"name", "email", "phone", "website", "address", "city", "country"}
                record_dict = job.preview_record.model_dump(exclude_none=True)
                filled = sum(1 for f in core_fields if record_dict.get(f))
                if filled < 2:
                    complexity_reasons.append(
                        "the extracted record contains very little contact data"
                    )

            if complexity_reasons:
                job.suggest_script_creator = True
                job.script_creator_reason = (
                    "This page appears complex: "
                    + "; ".join(complexity_reasons)
                    + ". A generated script may extract data more reliably."
                )
                log.info(
                    "[%s] Suggesting script_creator: %s",
                    job.id,
                    job.script_creator_reason,
                )

            # ---- Pause for user validation ----
            self._set_status(job, CrawlStatus.PREVIEW, "await_user_confirmation")
            log.info("[%s] Preview ready — waiting for user confirmation", job.id)

        except Exception as exc:
            stage = self._to_stage(job.status)
            self._record_failure(
                job,
                category=FailureCategory.UNKNOWN,
                stage=stage,
                message=str(exc),
                retryable=False,
            )
            self._set_status(job, CrawlStatus.FAILED, "preview_exception")
            job.error = str(exc)
            log.error("[%s] Failed during preview: %s", job.id, exc, exc_info=True)

        return job

    # Keep backward compatibility
    async def run_preview(self, job: CrawlJob) -> CrawlJob:
        """Run planning + preview in one shot (legacy path)."""
        await self.run_plan_only(job)
        if job.status == CrawlStatus.PLAN_REVIEW:
            # Auto-approve and continue to preview scrape
            await self.run_preview_scrape(job)
        return job

    async def _compare_extractions(
        self, candidates: dict[ExtractionMethod, object],
    ) -> tuple[str, ExtractionMethod]:
        """Use LLM to compare extraction results and recommend the best one."""
        try:
            from app.utils.llm import chat_completion

            parts: list[str] = []
            for method, record in candidates.items():
                data = record.model_dump(exclude_none=True)
                label = {
                    ExtractionMethod.CSS: "CSS Selector",
                    ExtractionMethod.SMART_SCRAPER: "SmartScraperGraph (AI)",
                    ExtractionMethod.UNIVERSAL_SCRAPER: "universal-scraper (AI/BS4)",
                    ExtractionMethod.CRAWL4AI: "Crawl4AI (AI/Markdown)",
                    ExtractionMethod.LISTING_API: "Listing API (Direct JSON)",
                }.get(method, method.value)
                parts.append(f"**{label} extraction:**\n{data}")

            method_names = ", ".join(m.value for m in candidates)
            prompt = (
                "Compare these extraction results from a web scraping preview.\n\n"
                + "\n\n".join(parts)
                + "\n\nWhich extraction method produced the best results? Consider:\n"
                "1. Completeness — which has more non-null fields?\n"
                "2. Accuracy — which values look more correct/clean?\n"
                "3. Coverage — which captured more useful information?\n\n"
                "Respond with a short explanation (2-3 sentences) and end with "
                "exactly one of these lines:\n"
                + "\n".join(f"RECOMMENDATION: {m.value}" for m in candidates)
            )

            text = await chat_completion(
                [{"role": "user", "content": prompt}],
                temperature=0,
            )
            text = text.strip()

            # Find which method was recommended
            recommended = ExtractionMethod.CSS  # default
            for method in candidates:
                if f"RECOMMENDATION: {method.value}" in text:
                    recommended = method
                    break

            explanation = text.split("RECOMMENDATION:")[0].strip()
            return explanation, recommended

        except Exception as exc:
            log.warning("LLM comparison failed: %s — defaulting to CSS", exc)
            return "Comparison failed, defaulting to CSS selectors (faster and more reliable).", ExtractionMethod.CSS

    # ------------------------------------------------------------------
    # Phase 2: full crawl (called after user confirms the preview)
    # ------------------------------------------------------------------
    async def run_full(self, job: CrawlJob, *, timeout_s: int | None = None) -> CrawlJob:
        """Continue the full crawl after user confirmation.

        Uses a cooperative timeout: a cancel event is signalled after
        *timeout_s* seconds, causing the scraper to stop gracefully
        between batches and save partial results.
        """
        cancel_event = asyncio.Event()
        timer_handle = None
        if timeout_s:
            loop = asyncio.get_event_loop()
            timer_handle = loop.call_later(timeout_s, cancel_event.set)

        try:
            plan = job.plan
            if not plan:
                raise RuntimeError("Cannot run full crawl without a plan")

            # If test_single mode — just output the preview record, skip scraping
            if job.request.test_single:
                log.info("[%s] Test-single mode — outputting preview record only", job.id)
                self._set_status(job, CrawlStatus.OUTPUT, "test_single_output")
                records = [job.preview_record] if job.preview_record else []
                result = await self.output.build_output(records, job.id)
                job.result = result
                self._record_confidence(
                    job,
                    PipelineStage.OUTPUT,
                    0.9 if records else 0.4,
                    "Test-single output generated",
                )
                self._set_status(job, CrawlStatus.COMPLETED, "test_single_done")
                log.info("[%s] Test-single completed — %d record(s)", job.id, len(records))
                return job

            # If the user gave feedback, re-plan with that context
            if job.user_feedback:
                self._set_status(job, CrawlStatus.PLANNING, "replan_with_feedback")
                log.info("[%s] Re-planning with user feedback: %s", job.id, job.user_feedback)
                plan = await self.planner.replan(plan, job.user_feedback)
                job.plan = plan
                self._record_confidence(
                    job,
                    PipelineStage.PLANNING,
                    0.8,
                    "Re-plan completed with user feedback",
                )

            # ---- Stage 2: Full Scraping ----
            self._set_status(job, CrawlStatus.SCRAPING, "full_scrape_start")
            log.info("[%s] Stage 2: Full scraping (method=%s)", job.id, job.extraction_method)

            # Apply user's max_pages limit if provided
            original_max_pages = settings.max_pages_per_crawl
            if job.request.max_pages is not None:
                settings.max_pages_per_crawl = min(job.request.max_pages, original_max_pages)
            try:
                page_data_list = []
                enrich_result = None
                selected_method = job.extraction_method
                if selected_method is None and job.preview_recommended_method is not None:
                    selected_method = job.preview_recommended_method
                    log.info(
                        "[%s] No user method choice — using preview recommendation: %s",
                        job.id, selected_method.value,
                    )
                attempts = self._build_method_attempt_order(selected_method)
                claude_attempts = 0

                for attempt_idx, method in enumerate(attempts, start=1):
                    if cancel_event.is_set():
                        break
                    if method == ExtractionMethod.CLAUDE:
                        if settings.claude_fallback_only and claude_attempts >= settings.claude_max_retries_per_stage:
                            continue
                        claude_attempts += 1

                    job.diagnostics.counters["scrape_attempts"] += 1

                    zero_item_streak = 0
                    attempted_pages = 0
                    switch_reason = ""

                    def _progress_cb(info: dict) -> None:
                        """Called by scraper to report real-time progress and health signals."""
                        nonlocal zero_item_streak, attempted_pages, switch_reason
                        job.progress = info
                        job.updated_at = datetime.now(timezone.utc)

                        if info.get("stage") == "method_fallback":
                            job.diagnostics.counters["method_fallbacks"] = (
                                job.diagnostics.counters.get("method_fallbacks", 0) + 1
                            )
                            log.info(
                                "[%s] Method fallback: %s -> %s (reason: %s)",
                                job.id, info.get("requested_method"),
                                info.get("actual_method"), info.get("fallback_reason"),
                            )
                            return

                        if info.get("stage") != "scraping_pages":
                            return

                        attempted_pages = int(info.get("pages_processed") or 0)
                        page_items = int(info.get("page_items") or 0)
                        job.diagnostics.counters["pages_processed"] += 1
                        job.diagnostics.counters["items_extracted"] += max(page_items, 0)
                        if page_items == 0:
                            zero_item_streak += 1
                            job.diagnostics.counters["empty_pages"] += 1
                        else:
                            zero_item_streak = 0

                        if self._should_switch_method(
                            pages_processed=attempted_pages,
                            zero_item_streak=zero_item_streak,
                        ):
                            switch_reason = (
                                f"Unhealthy extraction trend: {zero_item_streak} consecutive empty pages "
                                f"after {attempted_pages} processed page(s)"
                            )
                            raise _SwitchMethodRequested(switch_reason)

                    attempt_started = time.perf_counter()
                    try:
                        page_data_list, enrich_result = await self.scraper.scrape(
                            plan,
                            max_items=job.request.max_items,
                            extraction_method=method,
                            progress_callback=_progress_cb,
                            cancel_event=cancel_event,
                        )
                        attempt_latency_ms = (time.perf_counter() - attempt_started) * 1000
                        provider_stats = self.scraper.get_provider_stats()
                        claude_stats = provider_stats.get("claude", {})
                        provider_name = "claude" if method == ExtractionMethod.CLAUDE or claude_stats else "openai"
                        self._record_provider_event(
                            job,
                            provider=provider_name,
                            stage=PipelineStage.SCRAPING,
                            method=method.value if method else "auto",
                            latency_ms=attempt_latency_ms,
                            input_tokens=int(claude_stats.get("input_tokens")) if claude_stats.get("input_tokens") is not None else None,
                            output_tokens=int(claude_stats.get("output_tokens")) if claude_stats.get("output_tokens") is not None else None,
                            estimated_cost_usd=float(claude_stats.get("estimated_cost_usd")) if claude_stats.get("estimated_cost_usd") is not None else None,
                        )
                    except _SwitchMethodRequested as exc:
                        attempt_latency_ms = (time.perf_counter() - attempt_started) * 1000
                        self._record_provider_event(
                            job,
                            provider="openai",
                            stage=PipelineStage.SCRAPING,
                            method=method.value if method else "auto",
                            latency_ms=attempt_latency_ms,
                            fallback_reason=switch_reason or str(exc),
                        )
                        job.diagnostics.counters["method_switches"] += 1
                        self._record_failure(
                            job,
                            category=FailureCategory.SELECTOR_MISMATCH,
                            stage=PipelineStage.SCRAPING,
                            message="Auto-switch triggered by low extraction confidence",
                            retryable=True,
                            details={
                                "method": method.value if method else "auto",
                                "attempt": str(attempt_idx),
                                "reason": switch_reason or str(exc),
                            },
                        )
                        continue
                    except Exception as exc:
                        attempt_latency_ms = (time.perf_counter() - attempt_started) * 1000
                        self._record_provider_event(
                            job,
                            provider="claude" if method == ExtractionMethod.CLAUDE else "openai",
                            stage=PipelineStage.SCRAPING,
                            method=method.value if method else "auto",
                            latency_ms=attempt_latency_ms,
                            fallback_reason=str(exc),
                        )
                        self._record_failure(
                            job,
                            category=FailureCategory.UNKNOWN,
                            stage=PipelineStage.SCRAPING,
                            message=f"Extraction attempt failed ({(method or 'auto')})",
                            retryable=True,
                            details={"error": str(exc), "attempt": str(attempt_idx)},
                        )
                        continue

                    total_items = sum(len(pd.items) for pd in page_data_list)
                    if total_items > 0:
                        if attempt_idx > 1:
                            job.diagnostics.counters["method_switches"] += 1
                            log.info(
                                "[%s] Fallback extraction succeeded with method=%s after %d attempt(s)",
                                job.id,
                                (method.value if method else "auto"),
                                attempt_idx,
                            )
                            self._record_failure(
                                job,
                                category=FailureCategory.SELECTOR_MISMATCH,
                                stage=PipelineStage.SCRAPING,
                                message="Primary extraction produced no items; fallback method recovered",
                                retryable=True,
                                details={
                                    "fallback_method": method.value if method else "auto",
                                    "attempt": str(attempt_idx),
                                },
                            )
                        break

                    self._record_failure(
                        job,
                        category=FailureCategory.SELECTOR_MISMATCH,
                        stage=PipelineStage.SCRAPING,
                        message=f"Extraction attempt returned 0 items ({(method or 'auto')})",
                        retryable=True,
                        details={"attempt": str(attempt_idx)},
                    )
            finally:
                settings.max_pages_per_crawl = original_max_pages
            total_items = sum(len(pd.items) for pd in page_data_list)
            log.info("[%s] Scraped %d items from %d pages", job.id, total_items, len(page_data_list))

            total_est_cost = 0.0
            total_in = 0
            total_out = 0
            for event in job.diagnostics.provider_events:
                if event.estimated_cost_usd:
                    total_est_cost += float(event.estimated_cost_usd)
                if event.input_tokens:
                    total_in += int(event.input_tokens)
                if event.output_tokens:
                    total_out += int(event.output_tokens)
            claude_runtime = get_claude_runtime_state()
            job.diagnostics.provider_summary = {
                "event_count": len(job.diagnostics.provider_events),
                "estimated_total_cost_usd": round(total_est_cost, 6),
                "estimated_input_tokens": total_in,
                "estimated_output_tokens": total_out,
                "claude_disabled": claude_runtime.get("disabled", False),
                "claude_consecutive_errors": int(claude_runtime.get("consecutive_errors", 0)),
            }

            scraping_score = 0.9 if total_items >= 10 else (0.7 if total_items > 0 else 0.2)
            self._record_confidence(
                job,
                PipelineStage.SCRAPING,
                scraping_score,
                f"Scraped {total_items} item(s) across {len(page_data_list)} page(s)",
            )

            # Track fetched/remaining detail URLs for resume
            if enrich_result:
                job.scraped_detail_urls = enrich_result.fetched_urls
                job.pending_detail_urls = enrich_result.remaining_urls
                job.diagnostics.counters["detail_pages_fetched"] = len(enrich_result.fetched_urls)
                job.diagnostics.counters["detail_pages_remaining"] = len(enrich_result.remaining_urls)

            # ---- Stage 3: Parsing ----
            self._set_status(job, CrawlStatus.PARSING, "parse_start")
            log.info("[%s] Stage 3: Parsing", job.id)
            records = await self.parser.parse(page_data_list, plan)
            parsing_score, parsing_reason, parsing_metrics = self._compute_parser_confidence(records)
            job.diagnostics.counters["parser_non_empty_fields"] = int(parsing_metrics["non_empty_fields"])
            job.diagnostics.counters["parser_total_fields"] = int(parsing_metrics["total_fields"])
            job.diagnostics.counters["parser_structured_fields"] = int(parsing_metrics["structured_non_empty"])
            job.diagnostics.parser_metrics.update(parsing_metrics)
            self._record_confidence(
                job,
                PipelineStage.PARSING,
                parsing_score,
                parsing_reason,
            )
            if records and parsing_score < 0.4:
                self._record_failure(
                    job,
                    category=FailureCategory.PARSER_SCHEMA_MISMATCH,
                    stage=PipelineStage.PARSING,
                    message="Parser output has low field completeness",
                    retryable=True,
                    details={
                        "score": f"{parsing_score:.3f}",
                        "record_count": str(parsing_metrics["record_count"]),
                    },
                )

            # ---- Stage 4: Output ----
            self._set_status(job, CrawlStatus.OUTPUT, "build_output_start")
            log.info("[%s] Stage 4: Building output", job.id)
            result = await self.output.build_output(records, job.id)
            job.result = result
            self._record_confidence(
                job,
                PipelineStage.OUTPUT,
                0.9 if result.records else 0.35,
                f"Output persisted with {len(result.records)} record(s)",
            )

            # ---- Quality report ----
            try:
                qr = evaluate_quality(
                    [r.model_dump() for r in records],
                    fields_wanted=job.request.fields_wanted,
                )
                job.quality_report = qr.to_dict()
                self._apply_quality_gate(job)
            except Exception as exc:
                log.debug("[%s] Quality report skipped: %s", job.id, exc)

            # ---- Cache successful plan ----
            if records and plan:
                try:
                    plan_cache.put(plan.url, plan.model_dump())
                except Exception as exc:
                    log.debug("[%s] Plan caching skipped: %s", job.id, exc)

            # ---- Done ----
            if job.status == CrawlStatus.PARTIAL:
                # Status may already be set to PARTIAL by quality gate enforcement.
                log.info(
                    "[%s] Partial — %d records saved (quality gate) or pending details.",
                    job.id, len(result.records),
                )
            elif job.pending_detail_urls:
                self._set_status(job, CrawlStatus.PARTIAL, "pending_detail_urls")
                log.info(
                    "[%s] Partial — %d records saved, %d detail pages remaining. Use POST /crawl/%s/resume to continue.",
                    job.id, len(result.records), len(job.pending_detail_urls), job.id,
                )
                self._record_failure(
                    job,
                    category=FailureCategory.DETAIL_ENRICHMENT,
                    stage=PipelineStage.SCRAPING,
                    message="Detail enrichment incomplete; resume available",
                    retryable=True,
                    details={"remaining": str(len(job.pending_detail_urls))},
                )
            else:
                self._set_status(job, CrawlStatus.COMPLETED, "full_crawl_done")
                log.info(
                    "[%s] Completed — %d records, JSON=%s, CSV=%s",
                    job.id,
                    len(result.records),
                    result.json_path,
                    result.csv_path,
                )

        except Exception as exc:
            stage = self._to_stage(job.status)
            self._record_failure(
                job,
                category=FailureCategory.UNKNOWN,
                stage=stage,
                message=str(exc),
                retryable=False,
            )
            self._set_status(job, CrawlStatus.FAILED, "full_crawl_exception")
            job.error = str(exc)
            log.error("[%s] Failed: %s", job.id, exc, exc_info=True)
        finally:
            if timer_handle:
                timer_handle.cancel()

        return job

    # ------------------------------------------------------------------
    # Phase 3: resume (fetch remaining detail pages from a partial job)
    # ------------------------------------------------------------------
    async def run_resume(
        self,
        job: CrawlJob,
        original_job: CrawlJob,
        *,
        timeout_s: int | None = None,
    ) -> CrawlJob:
        """Resume a partial crawl by fetching only remaining detail pages."""
        cancel_event = asyncio.Event()
        timer_handle = None
        if timeout_s:
            loop = asyncio.get_event_loop()
            timer_handle = loop.call_later(timeout_s, cancel_event.set)

        try:
            plan = job.plan
            if not plan:
                raise RuntimeError("Cannot resume without a plan")

            remaining_urls = original_job.pending_detail_urls
            if not remaining_urls:
                raise RuntimeError("No remaining detail pages to resume")

            # ---- Fetch remaining detail pages ----
            self._set_status(job, CrawlStatus.SCRAPING, "resume_detail_fetch")
            log.info("[%s] Resume: fetching %d remaining detail pages", job.id, len(remaining_urls))

            def _progress_cb(info: dict) -> None:
                job.progress = info
                job.updated_at = datetime.now(timezone.utc)

            _pages, enrich_result = await self.scraper.scrape_detail_urls_only(
                plan, remaining_urls,
                cancel_event=cancel_event,
                progress_callback=_progress_cb,
            )

            # Track what was fetched in this round
            job.scraped_detail_urls = enrich_result.fetched_urls
            job.pending_detail_urls = enrich_result.remaining_urls
            self._record_confidence(
                job,
                PipelineStage.SCRAPING,
                0.85 if enrich_result.fetched_urls else 0.25,
                f"Resume fetched {len(enrich_result.fetched_urls)} detail pages",
            )

            # ---- Parse new detail pages ----
            self._set_status(job, CrawlStatus.PARSING, "resume_parse")
            new_records = await self.parser.parse(_pages, plan)
            log.info("[%s] Resume: parsed %d new records", job.id, len(new_records))
            parsing_score, parsing_reason, parsing_metrics = self._compute_parser_confidence(new_records)
            job.diagnostics.counters["parser_non_empty_fields"] = int(parsing_metrics["non_empty_fields"])
            job.diagnostics.counters["parser_total_fields"] = int(parsing_metrics["total_fields"])
            job.diagnostics.counters["parser_structured_fields"] = int(parsing_metrics["structured_non_empty"])
            job.diagnostics.parser_metrics.update(parsing_metrics)
            self._record_confidence(
                job,
                PipelineStage.PARSING,
                parsing_score,
                parsing_reason,
            )
            if new_records and parsing_score < 0.4:
                self._record_failure(
                    job,
                    category=FailureCategory.PARSER_SCHEMA_MISMATCH,
                    stage=PipelineStage.PARSING,
                    message="Resume parser output has low field completeness",
                    retryable=True,
                    details={
                        "score": f"{parsing_score:.3f}",
                        "record_count": str(parsing_metrics["record_count"]),
                    },
                )

            # ---- Merge with original job's records ----
            previous_records = list(original_job.result.records) if original_job.result and original_job.result.records else []
            all_records = previous_records + new_records

            # ---- Output ----
            self._set_status(job, CrawlStatus.OUTPUT, "resume_output")
            result = await self.output.build_output(all_records, job.id)
            job.result = result
            self._record_confidence(
                job,
                PipelineStage.OUTPUT,
                0.9 if result.records else 0.35,
                f"Resume output persisted with {len(result.records)} total record(s)",
            )

            if job.pending_detail_urls:
                self._set_status(job, CrawlStatus.PARTIAL, "resume_pending_details")
                log.info(
                    "[%s] Resume partial — %d total records, %d detail pages still remaining",
                    job.id, len(result.records), len(job.pending_detail_urls),
                )
                self._record_failure(
                    job,
                    category=FailureCategory.DETAIL_ENRICHMENT,
                    stage=PipelineStage.SCRAPING,
                    message="Resume incomplete; detail URLs still pending",
                    retryable=True,
                    details={"remaining": str(len(job.pending_detail_urls))},
                )
            else:
                self._set_status(job, CrawlStatus.COMPLETED, "resume_completed")
                log.info(
                    "[%s] Resume completed — %d total records, JSON=%s, CSV=%s",
                    job.id, len(result.records), result.json_path, result.csv_path,
                )

        except Exception as exc:
            stage = self._to_stage(job.status)
            self._record_failure(
                job,
                category=FailureCategory.UNKNOWN,
                stage=stage,
                message=str(exc),
                retryable=False,
            )
            self._set_status(job, CrawlStatus.FAILED, "resume_exception")
            job.error = str(exc)
            log.error("[%s] Resume failed: %s", job.id, exc, exc_info=True)
        finally:
            if timer_handle:
                timer_handle.cancel()

        return job
