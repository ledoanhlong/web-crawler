"""Template loader — reads scraping templates from app/templates/*.json.

Templates describe **website patterns** (e.g. "JS directory with A-Z tabs and
detail pages") — not site-specific CSS selectors.  When a user selects a
template, the structural hints are passed to the planner agent which analyses
the *actual* target page to generate CSS selectors at runtime.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from app.models.schemas import ScrapingTemplate, TemplateHints
from app.utils.logging import get_logger

log = get_logger(__name__)

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"


def list_templates() -> list[ScrapingTemplate]:
    """Return all available templates (cached after first call)."""
    return list(_load_all().values())


def get_template(template_id: str) -> ScrapingTemplate | None:
    """Return a template by ID, or None if not found."""
    return _load_all().get(template_id)


def get_hints_from_template(template_id: str) -> TemplateHints:
    """Load a template and return its TemplateHints.

    Raises ValueError if the template is not found.
    """
    tmpl = get_template(template_id)
    if not tmpl:
        raise ValueError(f"Template not found: {template_id}")
    return tmpl.hints


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
            hints_data = data.get("hints", {})
            hints = TemplateHints.model_validate(hints_data)
            data["hints"] = hints
            tmpl = ScrapingTemplate.model_validate(data)
            templates[tmpl.id] = tmpl
            log.info("Loaded template: %s (%s)", tmpl.id, tmpl.name)
        except Exception as exc:
            log.error("Failed to load template %s: %s", path.name, exc)

    log.info("Loaded %d templates from %s", len(templates), _TEMPLATES_DIR)
    return templates
