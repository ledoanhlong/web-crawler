"""File-backed persistence for crawl jobs."""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

from app.config import settings
from app.models.schemas import (
    CrawlJob,
    CrawlStatus,
    FailureCategory,
    FailureEvent,
    PipelineStage,
)
from app.utils.logging import get_logger

log = get_logger(__name__)


class JobStore:
    """Persist crawl jobs as JSON snapshots under the output directory."""

    def __init__(self, root_dir: Path | None = None) -> None:
        self._root_dir = root_dir
        self._last_saved_at: dict[str, float] = {}

    def _dir(self) -> Path:
        if self._root_dir is not None:
            return self._root_dir
        return Path(settings.output_dir) / "job_store"

    def _path(self, job_id: str) -> Path:
        return self._dir() / f"{job_id}.json"

    def load_into(self, jobs: dict[str, CrawlJob]) -> int:
        """Load persisted jobs into the provided in-memory mapping."""
        jobs.clear()
        root = self._dir()
        if not root.exists():
            return 0

        loaded = 0
        for path in sorted(root.glob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                job = CrawlJob.model_validate(payload)
            except Exception as exc:
                log.warning("Skipping corrupt job snapshot %s: %s", path.name, exc)
                continue
            jobs[job.id] = job
            loaded += 1
        if loaded:
            log.info("Loaded %d persisted job(s)", loaded)
        return loaded

    def save(
        self,
        job: CrawlJob,
        *,
        force: bool = False,
        throttle_s: float = 0.0,
    ) -> bool:
        """Persist a job snapshot to disk."""
        now = time.monotonic()
        last = self._last_saved_at.get(job.id, 0.0)
        if not force and throttle_s > 0 and (now - last) < throttle_s:
            return False

        root = self._dir()
        root.mkdir(parents=True, exist_ok=True)
        path = self._path(job.id)
        tmp_path = path.with_suffix(".json.tmp")
        tmp_path.write_text(job.model_dump_json(indent=2), encoding="utf-8")
        tmp_path.replace(path)
        self._last_saved_at[job.id] = now
        return True

    def delete(self, job_id: str) -> bool:
        """Delete a persisted job snapshot."""
        path = self._path(job_id)
        if not path.exists():
            return False
        path.unlink()
        self._last_saved_at.pop(job_id, None)
        return True

    def recover_interrupted_jobs(self, jobs: dict[str, CrawlJob]) -> int:
        """Mark jobs that were active during shutdown/restart as interrupted."""
        recovered = 0
        active_statuses = {
            CrawlStatus.PENDING,
            CrawlStatus.PLANNING,
            CrawlStatus.SCRAPING,
            CrawlStatus.PARSING,
            CrawlStatus.OUTPUT,
        }
        for job in jobs.values():
            if job.status not in active_statuses:
                continue

            previous = job.status
            job.updated_at = datetime.now(timezone.utc)
            job.diagnostics.status_timeline.append(
                f"{job.updated_at.isoformat()} status={previous.value} reason=interrupted_previous_process"
            )
            job.diagnostics.failures.append(
                FailureEvent(
                    category=FailureCategory.UNKNOWN,
                    stage=self._stage_for_status(previous),
                    message="Job was interrupted by a server restart or process exit.",
                    retryable=True,
                    details={"previous_status": previous.value},
                )
            )
            if job.pending_detail_urls:
                job.status = CrawlStatus.PARTIAL
            else:
                job.status = CrawlStatus.FAILED
                job.error = job.error or "Job was interrupted by a server restart or process exit."
            job.diagnostics.status_timeline.append(
                f"{job.updated_at.isoformat()} status={job.status.value} reason=recovered_from_interrupted_process"
            )
            self.save(job, force=True)
            recovered += 1

        if recovered:
            log.warning("Recovered %d interrupted job(s) from persisted snapshots", recovered)
        return recovered

    @staticmethod
    def _stage_for_status(status: CrawlStatus) -> PipelineStage:
        if status in (
            CrawlStatus.PENDING,
            CrawlStatus.PLANNING,
            CrawlStatus.PLAN_REVIEW,
            CrawlStatus.PREVIEW,
        ):
            return PipelineStage.PLANNING
        if status == CrawlStatus.SCRAPING:
            return PipelineStage.SCRAPING
        if status == CrawlStatus.PARSING:
            return PipelineStage.PARSING
        return PipelineStage.OUTPUT


job_store = JobStore()
