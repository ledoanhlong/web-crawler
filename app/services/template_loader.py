"""Template loader — reads scraping templates from app/templates/*.json."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from app.models.schemas import ScrapingPlan, ScrapingTemplate
from app.utils.logging import get_logger

log = get_logger(__name__)

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"


def list_templates() -> list[ScrapingTemplate]:
    """Return all available templates (cached after first call)."""
    return list(_load_all().values())


def get_template(template_id: str) -> ScrapingTemplate | None:
    """Return a template by ID, or None if not found."""
    return _load_all().get(template_id)


def get_plan_from_template(template_id: str, url: str) -> ScrapingPlan:
    """Load a template and return its ScrapingPlan with the URL replaced.

    Raises ValueError if the template is not found.
    """
    tmpl = get_template(template_id)
    if not tmpl:
        raise ValueError(f"Template not found: {template_id}")

    # Deep copy the plan via Pydantic serialization
    plan_data = tmpl.plan.model_dump(mode="json")
    plan_data["url"] = url
    return ScrapingPlan.model_validate(plan_data)


@lru_cache(maxsize=1)
def _load_all() -> dict[str, ScrapingTemplate]:
    """Load all .json template files from the templates directory."""
    templates: dict[str, ScrapingTemplate] = {}

    if not _TEMPLATES_DIR.exists():
        log.warning("Templates directory not found: %s", _TEMPLATES_DIR)
        return templates

    for path in sorted(_TEMPLATES_DIR.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            plan_data = data.get("plan", {})
            plan = ScrapingPlan.model_validate(plan_data)
            data["plan"] = plan
            tmpl = ScrapingTemplate.model_validate(data)
            templates[tmpl.id] = tmpl
            log.info("Loaded template: %s (%s)", tmpl.id, tmpl.name)
        except Exception as exc:
            log.error("Failed to load template %s: %s", path.name, exc)

    log.info("Loaded %d templates from %s", len(templates), _TEMPLATES_DIR)
    return templates
