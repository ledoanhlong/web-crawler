"""Scrape template store — saves proven scraping recipes for reuse.

Unlike plan_cache (7-day TTL, auto-expires), templates are permanent records
of successful scrapes.  They capture everything needed to replay a scrape:
the request params, the full plan (selectors, pagination), and the extraction
method that worked best.

Templates are stored as JSON files in ``output/templates/``.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from datetime import datetime, timezone

from app.utils.logging import get_logger

log = get_logger(__name__)

_TEMPLATES_DIR = Path("output/templates")


def _slugify(text: str, max_len: int = 60) -> str:
    """Convert text to a filesystem-safe slug."""
    slug = re.sub(r"[^\w\s-]", "", text.lower())
    slug = re.sub(r"[\s_]+", "-", slug).strip("-")
    return slug[:max_len] or "untitled"


class TemplateStore:
    """File-backed store for scrape templates."""

    def __init__(self, templates_dir: Path | None = None) -> None:
        self._dir = templates_dir or _TEMPLATES_DIR

    def _ensure_dir(self) -> Path:
        self._dir.mkdir(parents=True, exist_ok=True)
        return self._dir

    def save_from_job(self, job_data: dict, name: str, description: str = "") -> dict:
        """Create a template from a completed job's data.

        Parameters
        ----------
        job_data : dict
            The full CrawlJob dict (model_dump output).
        name : str
            Human-readable template name (e.g. "Webwinkelvakdagen Exhibitors 2026").
        description : str
            Optional notes about what this template scrapes.

        Returns
        -------
        dict
            The saved template metadata.
        """
        request = job_data.get("request", {})
        plan = job_data.get("plan")
        extraction_method = job_data.get("extraction_method")
        result = job_data.get("result", {})

        template = {
            "name": name,
            "description": description,
            "source_job_id": job_data.get("id", ""),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "url": request.get("url", ""),
            "request": {
                "url": request.get("url", ""),
                "fields_wanted": request.get("fields_wanted"),
                "item_description": request.get("item_description"),
                "site_notes": request.get("site_notes"),
                "pagination_type": request.get("pagination_type"),
                "max_items": request.get("max_items"),
                "max_pages": request.get("max_pages"),
                "page_type": request.get("page_type"),
                "rendering_type": request.get("rendering_type"),
                "detail_page_type": request.get("detail_page_type"),
                "detail_page_url": request.get("detail_page_url"),
            },
            "plan": plan,
            "extraction_method": extraction_method,
            "stats": {
                "records_scraped": len(result.get("records", [])) if result else 0,
                "source_job_status": job_data.get("status", ""),
            },
        }

        slug = _slugify(name)
        filename = f"{slug}.json"
        self._ensure_dir()
        path = self._dir / filename

        # Avoid overwriting — append a counter if needed
        counter = 1
        while path.exists():
            counter += 1
            filename = f"{slug}-{counter}.json"
            path = self._dir / filename

        path.write_text(
            json.dumps(template, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        log.info("Saved template '%s' → %s", name, path)

        template["_filename"] = filename
        return template

    def list_templates(self) -> list[dict]:
        """List all saved templates with summary info."""
        if not self._dir.exists():
            return []
        entries: list[dict] = []
        for path in sorted(self._dir.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                entries.append({
                    "filename": path.stem,
                    "name": data.get("name", ""),
                    "description": data.get("description", ""),
                    "url": data.get("url", ""),
                    "extraction_method": data.get("extraction_method"),
                    "records_scraped": data.get("stats", {}).get("records_scraped", 0),
                    "created_at": data.get("created_at", ""),
                    "source_job_id": data.get("source_job_id", ""),
                })
            except Exception:
                continue
        return entries

    def get(self, filename: str) -> dict | None:
        """Load a template by filename (without .json extension)."""
        path = self._dir / f"{filename}.json"
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            log.warning("Corrupt template file %s", path)
            return None

    def delete(self, filename: str) -> bool:
        """Delete a template by filename. Returns True if it existed."""
        path = self._dir / f"{filename}.json"
        if path.exists():
            path.unlink()
            log.info("Deleted template '%s'", filename)
            return True
        return False


# Module-level singleton
template_store = TemplateStore()
