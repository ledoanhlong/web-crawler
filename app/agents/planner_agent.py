"""PlannerAgent — Analyses the HTML structure of a listing page and produces a ScrapingPlan.

Flow
----
1. Fetch the target URL (httpx first; Playwright fallback if the page looks JS-heavy).
2. Optionally fetch a user-provided detail page URL.
3. Simplify the HTML (strip scripts/styles/boilerplate).
4. Send the simplified HTML to GPT with a structured prompt asking it to identify
   item containers, field selectors, pagination strategy, and whether JS rendering
   is required.
4. Parse the JSON response into a ``ScrapingPlan``.
5. If a detail link is found, fetch one sample detail page and analyse it to get
   accurate field selectors and identify sub-links worth following.
"""

from __future__ import annotations

import asyncio
import json
import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from app.models.schemas import DetailApiPlan, DetailPagePlan, ScrapingPlan, TemplateHints
from app.config import settings
from app.prompts import load_prompt
from app.utils.html import simplify_html
from app.utils.http import fetch_page
from app.utils.llm import chat_completion_json
from app.utils.logging import get_logger
from app.utils.structured_source import detect_embedded_structured_source

log = get_logger(__name__)

SYSTEM_PROMPT = load_prompt("planner_listing")

_MIN_LISTING_CONTAINERS = 3
_DETAIL_SHELL_FIELD_PREFIXES = (
    "meta_",
    "og_",
    "canonical_",
    "cookie_",
    "portal_",
    "map_",
    "legend_",
    "search_",
    "menu_",
    "attribution_",
)
_DETAIL_SHELL_URL_MARKERS = (
    "floorplan",
    "hallplan",
    "hall-map",
    "hall_map",
    "showexhibitor",
)
_DETAIL_SHELL_HTML_MARKERS = (
    "floorplan",
    "hall plan",
    "hall map",
    "legend",
    "svg-pan-zoom",
    "booth finder",
    "showexhibitor",
)
_DETAIL_USEFUL_FIELD_MARKERS = (
    "name",
    "email",
    "phone",
    "website",
    "description",
    "address",
    "contact",
    "city",
    "country",
    "postal",
    "zip",
    "booth",
    "hall",
    "brand",
    "category",
    "social",
)


def _detail_plan_looks_like_shell(
    detail_url: str,
    html_raw: str,
    detail_plan: DetailPagePlan | None,
) -> bool:
    field_names = list((detail_plan.field_selectors or {}).keys()) if detail_plan else []
    field_names_l = [name.lower() for name in field_names]

    junk_hits = sum(
        1
        for name in field_names_l
        if name.startswith(_DETAIL_SHELL_FIELD_PREFIXES)
    )
    useful_hits = sum(
        1
        for name in field_names_l
        if not name.startswith(_DETAIL_SHELL_FIELD_PREFIXES)
        and any(marker in name for marker in _DETAIL_USEFUL_FIELD_MARKERS)
    )
    mostly_junk_fields = bool(field_names_l) and junk_hits >= max(1, len(field_names_l) // 2)

    detail_url_l = detail_url.lower()
    url_looks_like_shell = any(marker in detail_url_l for marker in _DETAIL_SHELL_URL_MARKERS)

    html_raw_l = html_raw.lower()
    html_shell_hits = sum(1 for marker in _DETAIL_SHELL_HTML_MARKERS if marker in html_raw_l)
    html_looks_like_shell = html_shell_hits >= 2 and "mailto:" not in html_raw_l and "tel:" not in html_raw_l

    if mostly_junk_fields and useful_hits == 0:
        return True
    if (url_looks_like_shell or html_looks_like_shell) and useful_hits == 0:
        return True
    return False


def _sanitize_plan_data(plan_data: dict) -> dict:
    """Clean up LLM output before Pydantic validation.

    GPT sometimes returns nested dicts where flat ``dict[str, str]`` is expected.
    For example ``detail_page_fields`` may come back as::

        {"fields_to_extract": {"email": "$.email", "phone": "$.phone"}}

    instead of the expected::

        {"email": "$.email", "phone": "$.phone"}

    This helper flattens such cases and strips any non-string values.
    """
    for key in ("detail_page_fields", "detail_page_field_attributes", "api_params"):
        val = plan_data.get(key)
        if not isinstance(val, dict):
            continue
        # If any value is itself a dict, flatten one level
        flat: dict[str, str] = {}
        for k, v in val.items():
            if isinstance(v, dict):
                for inner_k, inner_v in v.items():
                    flat[inner_k] = str(inner_v) if inner_v is not None else ""
            elif isinstance(v, str):
                flat[k] = v
            else:
                flat[k] = str(v) if v is not None else ""
        plan_data[key] = flat

    # Same treatment for target.field_selectors / field_attributes
    target = plan_data.get("target")
    if isinstance(target, dict):
        for key in ("field_selectors", "field_attributes"):
            val = target.get(key)
            if not isinstance(val, dict):
                continue
            flat = {}
            for k, v in val.items():
                if isinstance(v, dict):
                    for inner_k, inner_v in v.items():
                        flat[inner_k] = str(inner_v) if inner_v is not None else ""
                elif isinstance(v, str):
                    flat[k] = v
                else:
                    flat[k] = str(v) if v is not None else ""
            target[key] = flat

    # Sanitise detail_link_selector — the LLM sometimes returns a
    # comma-separated group where one part is broken (e.g. ", > a[...]").
    target = plan_data.get("target")
    if isinstance(target, dict):
        dls = target.get("detail_link_selector")
        if isinstance(dls, str) and "," in dls:
            parts = [p.strip() for p in dls.split(",") if p.strip()]
            valid: list[str] = []
            for p in parts:
                # A selector starting with a combinator (>, +, ~) is invalid
                if p[0] in (">", "+", "~"):
                    continue
                valid.append(p)
            target["detail_link_selector"] = ", ".join(valid) if valid else None

    # Strip non-standard CSS pseudo-classes the LLM sometimes emits
    # (e.g. `:contains()` — a jQuery extension invalid in querySelectorAll).
    # We drop the entire comma-separated part if it contains such a pseudo.
    _NON_STANDARD_RE = re.compile(r":(?:contains|eq|gt|lt|even|odd)\b\(")
    for sel_key in ("pagination_selector", "inner_pagination_selector",
                     "alphabet_tab_selector", "wait_selector"):
        raw = plan_data.get(sel_key)
        if not isinstance(raw, str):
            continue
        parts = [p.strip() for p in raw.split(",") if p.strip()]
        valid_parts = [p for p in parts if not _NON_STANDARD_RE.search(p)]
        plan_data[sel_key] = ", ".join(valid_parts) if valid_parts else None
    # Also clean selectors inside target
    target = plan_data.get("target")
    if isinstance(target, dict):
        for sel_key in ("item_container_selector", "detail_link_selector",
                        "detail_button_selector"):
            raw = target.get(sel_key)
            if not isinstance(raw, str):
                continue
            parts = [p.strip() for p in raw.split(",") if p.strip()]
            valid_parts = [p for p in parts if not _NON_STANDARD_RE.search(p)]
            if valid_parts:
                target[sel_key] = ", ".join(valid_parts)
            elif sel_key == "item_container_selector":
                # item_container_selector is required — keep the original
                # (a bad selector that matches nothing is better than None
                # which causes a Pydantic validation crash)
                log.warning(
                    "All parts of %s contain non-standard pseudo-classes; "
                    "keeping original value: %s", sel_key, raw,
                )
            else:
                target[sel_key] = None

    # Ensure item_container_selector is never None — the LLM sometimes
    # returns null for this required field.  Use a placeholder that will
    # match 0 elements so _validate_selectors triggers its re-prompt loop.
    target = plan_data.get("target")
    if isinstance(target, dict):
        ics = target.get("item_container_selector")
        if not ics or not isinstance(ics, str) or not ics.strip():
            target["item_container_selector"] = "#__placeholder_no_match__"
            log.warning(
                "LLM returned empty/null item_container_selector — "
                "using placeholder to trigger selector re-prompt",
            )

    # Strip selector_metrics — this is computed by the validator, not the LLM.
    # The LLM sometimes echoes it back with wrong types (e.g. "high" instead
    # of a float), causing Pydantic validation errors.
    plan_data.pop("selector_metrics", None)

    return plan_data


async def _fetch_html(url: str) -> tuple[str, bool]:
    """Fetch a page — try httpx first, fall back to Playwright.

    Returns (html, needs_js) so callers can override ``requires_javascript``.

    Only falls back to Playwright for *rendering* failures (empty / JS-shell
    pages).  Hard network errors (DNS, connection refused, HTTP 4xx/5xx) are
    re-raised immediately so the caller can surface them to the user rather
    than silently launching Playwright — which would fail with the same error
    and produce a more confusing traceback.
    """
    import httpx

    html_raw = ""
    needs_js = False

    # Heuristic 1: URL hash routing → always needs JS
    if "#/" in url or "#!/" in url:
        needs_js = True
        log.info("URL contains hash routing — will use Playwright for %s", url)

    if not needs_js:
        rendering_failure = False
        try:
            html_raw = await fetch_page(url)
            if len(html_raw) < 2_000 or "<noscript>" in html_raw.lower():
                rendering_failure = True

            # Heuristic 2: SPA framework markers in raw HTML
            if not rendering_failure:
                lower = html_raw.lower()
                # High-confidence SPA markers (framework-specific)
                spa_markers_strong = [
                    'id="__next"', 'id="__nuxt"',
                    "ng-app", "data-ng-app", "<app-root", "data-reactroot",
                    "__vue_app__", 'id="__svelte"', 'id="svelte"',
                    "ember-application", "data-turbo",
                ]
                # Weak markers — only flag as SPA if body text is also short
                spa_markers_weak = [
                    'id="app"', 'id="root"',
                ]
                if any(m in lower for m in spa_markers_strong):
                    rendering_failure = True
                    log.info("SPA framework marker detected in HTML for %s", url)

            # Parse the HTML once for all remaining heuristics that need it
            if not rendering_failure:
                soup = BeautifulSoup(html_raw, "lxml")
                body_tag = soup.find("body")

                # Heuristic 2b: Weak SPA markers — only flag if body text is short
                if not rendering_failure and any(m in lower for m in spa_markers_weak):
                    if body_tag and len(body_tag.get_text(strip=True)) < 500:
                        rendering_failure = True
                        log.info("Weak SPA marker + sparse body text for %s", url)

                # Heuristic 3: Custom HTML elements (web components) that are
                # major content containers.
                if not rendering_failure and body_tag:
                    for tag in body_tag.find_all(True, recursive=False):
                        if "-" in tag.name and len(str(tag)) > 500:
                            rendering_failure = True
                            log.info(
                                "Large custom element <%s> detected — likely JS-rendered for %s",
                                tag.name, url,
                            )
                            break
                    if not rendering_failure:
                        # Check one level deeper (common pattern: <div><my-app>)
                        for wrapper in body_tag.find_all(True, recursive=False):
                            for tag in wrapper.find_all(True, recursive=False):
                                if "-" in tag.name and len(str(tag)) > 500:
                                    rendering_failure = True
                                    log.info(
                                        "Large custom element <%s> detected — likely JS-rendered for %s",
                                        tag.name, url,
                                    )
                                    break
                            if rendering_failure:
                                break

                # Heuristic 4: Large HTML shell with almost no visible text
                if not rendering_failure and len(html_raw) >= 2_000:
                    if body_tag and len(body_tag.get_text(strip=True)) < 200:
                        rendering_failure = True
                        log.info("HTML shell has <200 chars of text — likely JS-rendered for %s", url)

        except (httpx.HTTPStatusError, httpx.ConnectError, httpx.TimeoutException) as exc:
            # Hard network errors — Playwright won't help, re-raise immediately.
            log.error("Network error fetching %s: %s", url, exc)
            raise
        except Exception:
            # Unexpected parse/decode error — treat as rendering failure and try Playwright.
            rendering_failure = True

        needs_js = rendering_failure

    if needs_js:
        log.info("httpx fetch insufficient; falling back to Playwright for %s", url)
        from app.utils.browser import fetch_page_js, get_browser

        async with get_browser() as browser:
            html_raw = await fetch_page_js(browser, url)

    return html_raw, needs_js


def _build_planner_prompt(
    url: str,
    html_simple: str,
    *,
    item_description: str | None = None,
    site_notes: str | None = None,
    detail_html_simple: str | None = None,
    detail_page_url: str | None = None,
    fields_wanted: str | None = None,
    template_hints: TemplateHints | None = None,
    pagination_type: str | None = None,
    exploration_notes: str | None = None,
) -> str:
    """Build the user prompt for the planner LLM call."""
    user_content = (
        f"Analyse the following HTML of the listing page at:\n{url}\n\n"
        f"```html\n{html_simple}\n```"
    )

    if item_description:
        user_content += (
            f"\n\n━━ ITEM DESCRIPTION (from user) ━━\n"
            f"The user describes each item on this page as: {item_description}\n"
            f"Use this to identify the correct repeating container and fields."
        )

    if site_notes:
        user_content += (
            f"\n\n━━ SITE NOTES (from user) ━━\n"
            f"The user provided these observations about the site: {site_notes}\n"
            f"Take these into account when building the scraping plan."
        )

    if detail_html_simple:
        user_content += (
            f"\n\nThe user also provided an example DETAIL page at:\n{detail_page_url}\n\n"
            f"Study this to identify the CSS selectors for ``detail_page_fields``.\n"
            f"```html\n{detail_html_simple}\n```"
        )

    if fields_wanted:
        user_content += (
            f"\n\nThe user specifically wants these fields extracted: {fields_wanted}\n"
            f"Make sure the plan captures all of these fields — from the listing page "
            f"if available, otherwise from the detail page."
        )

    if template_hints:
        user_content += (
            "\n\n━━ TEMPLATE HINTS (structural guidance) ━━\n"
            "The user selected a website pattern template. Use these hints "
            "to guide your analysis — but always derive actual CSS "
            "selectors from the HTML above.\n"
            f"- requires_javascript: {template_hints.requires_javascript}\n"
            f"- has_detail_pages: {template_hints.has_detail_pages}\n"
            f"- has_detail_api: {template_hints.has_detail_api}\n"
        )
        if not pagination_type and template_hints.pagination:
            user_content += f"- expected pagination (hint only): {template_hints.pagination}\n"
        if template_hints.notes:
            user_content += f"- pattern notes: {template_hints.notes}\n"

    if pagination_type:
        user_content += (
            f"\n\n━━ PAGINATION (user-specified — MUST USE) ━━\n"
            f"The user has explicitly specified the pagination strategy: {pagination_type}\n"
            f"You MUST set pagination to \"{pagination_type}\" in your plan.\n"
            f"Do NOT override this with your own analysis.\n"
        )

    if exploration_notes:
        user_content += (
            "\n\n━━ EXPLORATION NOTES (from interactive browsing) ━━\n"
            "The system navigated to this page and explored it interactively. "
            "Use these observations to improve your scraping plan — especially "
            "for pagination, hidden content, and detail page structure.\n\n"
            f"{exploration_notes}\n"
        )

    return user_content


class PlannerAgent:
    """Analyse a listing page and return a ScrapingPlan."""

    @staticmethod
    def _compute_selector_metrics(plan: ScrapingPlan, soup: BeautifulSoup) -> dict[str, float | int]:
        """Compute container/field hit ratios for selector preflight quality checks."""
        try:
            containers = soup.select(plan.target.item_container_selector)
        except Exception:
            containers = []
        container_count = len(containers)
        sample_n = min(container_count, max(1, settings.reliability_selector_sample_containers))

        field_hits = 0
        total_checks = 0
        if sample_n > 0 and plan.target.field_selectors:
            for container in containers[:sample_n]:
                for selector in plan.target.field_selectors.values():
                    if not selector or not selector.strip():
                        continue
                    total_checks += 1
                    try:
                        if container.select_one(selector):
                            field_hits += 1
                    except Exception:
                        continue
        hit_ratio = (field_hits / total_checks) if total_checks else 0.0
        return {
            "container_count": container_count,
            "sampled_containers": sample_n,
            "field_checks": total_checks,
            "field_hits": field_hits,
            "field_hit_ratio": round(hit_ratio, 4),
        }

    @staticmethod
    def _content_test_selectors(
        plan: ScrapingPlan,
        containers: list,
        *,
        sample_n: int = 5,
    ) -> float:
        """Test whether field selectors actually extract meaningful text content.

        Returns a score between 0.0 (no content) and 1.0 (all fields have content).
        A score below 0.3 indicates the selectors match DOM elements but
        they are empty or contain only whitespace / boilerplate.
        """
        if not plan.target.field_selectors or not containers:
            return 1.0  # nothing to test → pass

        sample = containers[:sample_n]
        total_field_checks = 0
        fields_with_content = 0

        for container in sample:
            for field_name, selector in plan.target.field_selectors.items():
                if not selector or not selector.strip():
                    continue
                total_field_checks += 1
                try:
                    el = container.select_one(selector)
                except Exception:
                    continue
                if not el:
                    continue

                # Check for attribute-based extraction
                attr = (plan.target.field_attributes or {}).get(field_name)
                if attr:
                    val = el.get(attr, "")
                    if val and str(val).strip():
                        fields_with_content += 1
                        continue

                # Check for text content (strip whitespace)
                text = el.get_text(strip=True)
                if text and len(text) >= 2:
                    fields_with_content += 1

        if total_field_checks == 0:
            return 1.0
        return fields_with_content / total_field_checks

    async def _plan_from_html(
        self,
        url: str,
        html_raw: str,
        *,
        detail_raw: str = "",
        detail_page_url: str | None = None,
        item_description: str | None = None,
        site_notes: str | None = None,
        fields_wanted: str | None = None,
        template_hints: TemplateHints | None = None,
        pagination_type: str | None = None,
        heuristic_needs_js: bool = False,
        exploration_notes: str | None = None,
    ) -> ScrapingPlan | None:
        """Run the HTML-based LLM planning pipeline.

        Returns a validated ScrapingPlan, or None if the LLM returned no data.
        """
        plan_data: dict | None = None
        for attempt, aggressive in enumerate([False, True]):
            html_simple = simplify_html(html_raw, aggressive=aggressive)
            log.debug("Simplified listing HTML: %d chars (aggressive=%s)", len(html_simple), aggressive)

            detail_html_simple = ""
            if detail_raw:
                detail_html_simple = simplify_html(detail_raw, aggressive=aggressive)
                log.debug("Simplified detail HTML: %d chars (aggressive=%s)", len(detail_html_simple), aggressive)

            user_content = _build_planner_prompt(
                url, html_simple,
                item_description=item_description,
                site_notes=site_notes,
                detail_html_simple=detail_html_simple or None,
                detail_page_url=detail_page_url,
                fields_wanted=fields_wanted,
                template_hints=template_hints,
                pagination_type=pagination_type,
                exploration_notes=exploration_notes,
            )

            messages = [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ]

            try:
                plan_data = await chat_completion_json(messages, max_tokens=16_000)
                break
            except Exception as exc:
                if attempt == 0 and "content_filter" in str(exc).lower():
                    log.warning(
                        "Content filter triggered on listing page; retrying with aggressive sanitization"
                    )
                    continue
                raise

        if not plan_data:
            return None

        log.info("Received scraping plan from LLM")

        plan_data["url"] = url
        plan_data = _sanitize_plan_data(plan_data)

        # Override requires_javascript if heuristics or template hints say so
        if heuristic_needs_js and not plan_data.get("requires_javascript"):
            log.info("Overriding requires_javascript=True (heuristic detected JS-rendered page)")
            plan_data["requires_javascript"] = True
        if template_hints and template_hints.requires_javascript and not plan_data.get("requires_javascript"):
            log.info("Overriding requires_javascript=True (template hint)")
            plan_data["requires_javascript"] = True

        # Override pagination if the user explicitly specified it
        if pagination_type:
            llm_pagination = plan_data.get("pagination", "none")
            if llm_pagination != pagination_type:
                log.info(
                    "Overriding pagination: LLM said '%s' but user specified '%s'",
                    llm_pagination, pagination_type,
                )
            plan_data["pagination"] = pagination_type
            if pagination_type in ("none", "infinite_scroll", "load_more_button"):
                plan_data["pagination_urls"] = []

        plan = ScrapingPlan.model_validate(plan_data)
        plan = await self._validate_selectors(plan, html_raw, fields_wanted)
        return plan

    @staticmethod
    def _merge_plans(html_plan: ScrapingPlan, vision_plan: ScrapingPlan) -> ScrapingPlan:
        """Combine HTML-based and vision-based plans, taking the best of both.

        The HTML plan provides precise CSS selectors derived from the raw DOM.
        The vision plan provides structural understanding of the rendered layout:
        pagination controls, navigation to detail pages, JS requirements, and
        data locations that are invisible in the raw HTML.
        """
        merged = html_plan.model_copy(deep=True)

        # Vision may detect JS rendering that static HTML analysis missed
        if vision_plan.requires_javascript:
            merged.requires_javascript = True

        # Use vision's pagination if HTML analysis found none
        if vision_plan.pagination.value != "none" and html_plan.pagination.value == "none":
            merged.pagination = vision_plan.pagination
            if vision_plan.pagination_selector and not merged.pagination_selector:
                merged.pagination_selector = vision_plan.pagination_selector

        # Vision may spot alphabet tabs, load-more, or next-button controls
        if hasattr(vision_plan, "alphabet_tab_selector") and vision_plan.alphabet_tab_selector:
            if not getattr(merged, "alphabet_tab_selector", None):
                merged.alphabet_tab_selector = vision_plan.alphabet_tab_selector

        # Vision may find navigation to detail pages that HTML analysis missed
        if vision_plan.target.detail_link_selector and not merged.target.detail_link_selector:
            merged.target.detail_link_selector = vision_plan.target.detail_link_selector
        if vision_plan.target.detail_button_selector and not merged.target.detail_button_selector:
            merged.target.detail_button_selector = vision_plan.target.detail_button_selector

        # Add fields the vision plan identified that HTML analysis missed
        for field, selector in vision_plan.target.field_selectors.items():
            if field not in merged.target.field_selectors and selector:
                merged.target.field_selectors[field] = selector
                vision_attr = (vision_plan.target.field_attributes or {}).get(field)
                if vision_attr:
                    if merged.target.field_attributes is None:
                        merged.target.field_attributes = {}
                    merged.target.field_attributes[field] = vision_attr

        # Carry over wait_selector if HTML analysis missed it
        if vision_plan.wait_selector and not merged.wait_selector:
            merged.wait_selector = vision_plan.wait_selector

        added_fields = [
            f for f in vision_plan.target.field_selectors
            if f not in html_plan.target.field_selectors
        ]
        merged.notes += (
            f"\n[Vision merged: pagination={vision_plan.pagination.value}, "
            f"js={vision_plan.requires_javascript}, "
            f"fields_added={added_fields}]"
        )
        return merged

    async def plan(
        self,
        url: str,
        *,
        detail_page_url: str | None = None,
        fields_wanted: str | None = None,
        item_description: str | None = None,
        site_notes: str | None = None,
        template_hints: TemplateHints | None = None,
        pagination_type: str | None = None,
    ) -> ScrapingPlan:
        log.info("Planning scrape for %s", url)

        # --- 1. Fetch the listing page ---
        html_raw, heuristic_needs_js = await _fetch_html(url)

        # --- 2. Optionally fetch the detail page ---
        detail_raw = ""
        if detail_page_url:
            log.info("Fetching user-provided detail page: %s", detail_page_url)
            try:
                detail_raw, _ = await _fetch_html(detail_page_url)
            except Exception as exc:
                log.warning("Failed to fetch detail page %s: %s", detail_page_url, exc)

        # --- 2b. Interactive exploration (vision-guided browsing) ---
        exploration_notes: str = ""
        should_explore = (
            settings.use_exploration_browsing
            or (settings.exploration_auto_for_js and heuristic_needs_js)
        )
        if should_explore:
            from app.utils.llm import get_vision_client
            if get_vision_client() is not None:
                try:
                    exploration_report = await self._explore_page(
                        url, needs_js=heuristic_needs_js,
                    )
                    exploration_notes = self._format_exploration_notes(exploration_report)
                    log.info(
                        "Exploration complete: %d steps, %d screenshots",
                        len(exploration_report.actions),
                        exploration_report.screenshots_taken,
                    )
                    # If exploration captured a richer HTML (after JS rendering +
                    # interactions), use that for planning instead of the initial fetch.
                    if (
                        exploration_report.final_html
                        and len(exploration_report.final_html) > len(html_raw)
                    ):
                        html_raw = exploration_report.final_html
                        heuristic_needs_js = True
                        log.info(
                            "Using exploration's post-interaction HTML for planning (%d chars)",
                            len(html_raw),
                        )
                except Exception as exc:
                    log.warning("Interactive exploration failed (non-critical): %s", exc)

        # --- 3. Run HTML-based and vision-based planning in parallel ---
        # Both analyses start immediately: HTML planning processes the DOM for
        # precise CSS selectors while vision planning takes a screenshot and
        # reasons about the rendered layout (pagination controls, navigation,
        # data location). The results are merged to get the full picture.
        gather_coros: list = [
            self._plan_from_html(
                url, html_raw,
                detail_raw=detail_raw,
                detail_page_url=detail_page_url,
                item_description=item_description,
                site_notes=site_notes,
                fields_wanted=fields_wanted,
                template_hints=template_hints,
                pagination_type=pagination_type,
                heuristic_needs_js=heuristic_needs_js,
                exploration_notes=exploration_notes or None,
            ),
        ]
        if settings.use_vision_planning:
            gather_coros.append(
                self._plan_with_vision(
                    url, html_raw,
                    fields_wanted=fields_wanted,
                    exploration_notes=exploration_notes or None,
                )
            )
            log.info("[%s] Running HTML and vision planning in parallel", url)

        results = await asyncio.gather(*gather_coros, return_exceptions=True)

        html_result = results[0]
        vision_result = results[1] if len(results) > 1 else None

        if isinstance(html_result, Exception):
            raise html_result
        if html_result is None:
            raise ValueError(
                "Planning failed: LLM returned no data after all attempts. "
                "The page content may have triggered a content filter."
            )

        plan: ScrapingPlan = html_result

        # --- 4. Merge HTML and vision insights ---
        if isinstance(vision_result, ScrapingPlan):
            plan = self._merge_plans(plan, vision_result)
            log.info(
                "Merged vision insights: pagination=%s, js=%s, detail_link=%s",
                plan.pagination.value,
                plan.requires_javascript,
                plan.target.detail_link_selector,
            )
            # Re-validate selectors after merge: vision may have added selectors
            # that don't match the actual HTML and need a re-prompt.
            plan = await self._validate_selectors(plan, html_raw, fields_wanted)
        elif isinstance(vision_result, Exception):
            log.warning("Vision planning failed (non-critical): %s", vision_result)

        # --- 4c. If the plan says JS is required but we fetched with httpx,
        #         the CSS selectors were derived from the wrong DOM.
        #         Re-fetch with Playwright and re-plan against the rendered HTML. ---
        if plan.requires_javascript and not heuristic_needs_js:
            log.info(
                "Plan says requires_javascript=True but page was fetched with httpx "
                "— re-fetching with Playwright to get the rendered DOM",
            )
            from app.utils.browser import fetch_page_js, get_browser

            async with get_browser() as browser:
                html_js = await fetch_page_js(browser, url)
            if html_js and len(html_js) > len(html_raw):
                html_raw = html_js
                heuristic_needs_js = True

                # Re-run the LLM planner with the JS-rendered HTML
                html_simple = simplify_html(html_raw, aggressive=False)
                log.debug("Re-planning with JS-rendered HTML: %d chars", len(html_simple))

                user_content = _build_planner_prompt(
                    url, html_simple,
                    item_description=item_description,
                    site_notes=site_notes,
                    fields_wanted=fields_wanted,
                    template_hints=template_hints,
                    pagination_type=pagination_type,
                )

                messages = [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_content},
                ]
                try:
                    plan_data_js = await chat_completion_json(messages, max_tokens=16_000)
                except Exception:
                    plan_data_js = None
                if plan_data_js:
                    plan_data_js["url"] = url
                    plan_data_js["requires_javascript"] = True
                    plan_data_js = _sanitize_plan_data(plan_data_js)
                    if pagination_type:
                        plan_data_js["pagination"] = pagination_type
                        if pagination_type in ("none", "infinite_scroll", "load_more_button"):
                            plan_data_js["pagination_urls"] = []
                    js_plan = ScrapingPlan.model_validate(plan_data_js)
                    js_plan = await self._validate_selectors(js_plan, html_raw, fields_wanted)
                    # Re-run vision planning with the JS-rendered HTML so the LLM
                    # can correctly match what it sees in the screenshot to real CSS
                    # selectors (the original vision call used the httpx shell).
                    if settings.use_vision_planning:
                        try:
                            js_vision = await self._plan_with_vision(
                                url, html_raw, fields_wanted=fields_wanted,
                            )
                            if isinstance(js_vision, ScrapingPlan):
                                vision_result = js_vision
                                log.info("Re-ran vision planning with JS-rendered HTML")
                        except Exception as _ve:
                            log.warning("Vision re-plan with JS HTML failed: %s", _ve)
                    # Merge the best available vision plan (updated or original).
                    if isinstance(vision_result, ScrapingPlan):
                        js_plan = self._merge_plans(js_plan, vision_result)
                        log.info("Re-merged vision insights into JS re-plan")
                    plan = js_plan
                    log.info(
                        "Re-planned with JS-rendered HTML: selector '%s'",
                        plan.target.item_container_selector,
                    )

        log.info(
            "Plan: js=%s, pagination=%s, fields=%s, detail_link=%s",
            plan.requires_javascript,
            plan.pagination.value,
            list(plan.target.field_selectors.keys()),
            plan.target.detail_link_selector,
        )

        structured_source = detect_embedded_structured_source(html_raw, source_url=url)
        if structured_source:
            plan.listing_api_plan = structured_source
            log.info(
                "Detected embedded structured source: selector=%s attr=%s path=%s count=%s",
                structured_source.html_selector,
                structured_source.html_attribute,
                structured_source.items_json_path,
                structured_source.total_count,
            )

        plan = await self._plan_detail_enrichment(html_raw, plan)

        return plan

    async def _plan_detail_enrichment(
        self,
        html_raw: str,
        plan: ScrapingPlan,
    ) -> ScrapingPlan:
        """Plan detail-page or detail-API enrichment from a sample item."""
        sample_detail_url: str | None = None
        if plan.target.detail_link_selector:
            sample_detail_url = self._extract_first_detail_link(html_raw, plan)

        if sample_detail_url:
            plan.detail_page_plan = None
            plan.detail_page_fields = {}
            plan.detail_page_field_attributes = {}
            detail_html_raw = ""
            try:
                detail_html_raw = await self._fetch_page(
                    sample_detail_url,
                    prefer_js=plan.requires_javascript,
                )
                detail_plan = await self._analyze_detail_page(
                    sample_detail_url,
                    plan.requires_javascript,
                    html_raw=detail_html_raw,
                )
                if _detail_plan_looks_like_shell(sample_detail_url, detail_html_raw, detail_plan):
                    log.info(
                        "Discarding detail page plan for shell-like detail page: %s",
                        sample_detail_url,
                    )
                else:
                    plan.detail_page_plan = detail_plan
                    plan.detail_page_fields = detail_plan.field_selectors
                    plan.detail_page_field_attributes = detail_plan.field_attributes
            except Exception as exc:
                log.warning("Detail page analysis failed: %s", exc)

        if not plan.detail_page_plan:
            try:
                if sample_detail_url:
                    detail_api = await self._discover_detail_api(
                        html_raw,
                        plan,
                        detail_url=sample_detail_url,
                    )
                elif plan.target.detail_button_selector:
                    detail_api = await self._discover_detail_api(html_raw, plan)
                else:
                    detail_api = None
                if detail_api:
                    plan.detail_api_plan = detail_api
            except Exception as exc:
                log.warning("Detail API discovery failed: %s", exc)

        return plan

    async def replan(self, current_plan: ScrapingPlan, user_feedback: str) -> ScrapingPlan:
        """Re-generate the scraping plan incorporating user feedback.

        This is called when the user reviews the preview and provides feedback
        like "I also need email and phone" or "the detail page has more info".
        """
        log.info("Re-planning with user feedback: %s", user_feedback)

        html_raw, heuristic_needs_js = await _fetch_html(current_plan.url)
        current_plan_json = current_plan.model_dump(mode="json")

        plan_data: dict | None = None
        for attempt, aggressive in enumerate([False, True]):
            html_simple = simplify_html(html_raw, aggressive=aggressive)

            messages = [
                {"role": "system", "content": load_prompt("planner_listing")},
                {
                    "role": "user",
                    "content": (
                        f"Analyse the following HTML of the listing page at:\n{current_plan.url}\n\n"
                        f"```html\n{html_simple}\n```"
                    ),
                },
                {
                    "role": "assistant",
                    "content": json.dumps(current_plan_json, ensure_ascii=False),
                },
                {
                    "role": "user",
                    "content": (
                        f"The user reviewed a preview of the scraped data and gave this feedback:\n\n"
                        f'"{user_feedback}"\n\n'
                        f"Please update the scraping plan to address this feedback. "
                        f"In particular, make sure to follow detail page links if the "
                        f"user wants fields that are only available on detail pages. "
                        f"Return the complete updated plan as JSON."
                    ),
                },
            ]

            try:
                plan_data = await chat_completion_json(messages, max_tokens=16_000)
                break
            except Exception as exc:
                if attempt == 0 and "content_filter" in str(exc).lower():
                    log.warning(
                        "Content filter triggered during replan; retrying with aggressive sanitization"
                    )
                    continue
                raise

        if not plan_data:
            raise ValueError(
                "Re-planning failed: LLM returned no data after all attempts. "
                "The page content may have triggered a content filter."
            )

        plan_data["url"] = current_plan.url
        plan_data = _sanitize_plan_data(plan_data)
        # Apply JS heuristics (same logic as plan()) so a re-plan on a
        # JS-rendered page doesn't lose the requires_javascript flag.
        if heuristic_needs_js and not plan_data.get("requires_javascript"):
            log.info("Overriding requires_javascript=True (heuristic) during replan")
            plan_data["requires_javascript"] = True
        plan = ScrapingPlan.model_validate(plan_data)
        log.info(
            "Re-plan: js=%s, pagination=%s, fields=%s, detail_link=%s",
            plan.requires_javascript,
            plan.pagination.value,
            list(plan.target.field_selectors.keys()),
            plan.target.detail_link_selector,
        )

        structured_source = detect_embedded_structured_source(html_raw, source_url=current_plan.url)
        if structured_source:
            plan.listing_api_plan = structured_source

        plan = await self._plan_detail_enrichment(html_raw, plan)

        return plan

    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    # Detail page analysis
    # ------------------------------------------------------------------
    async def _analyze_detail_page(
        self,
        detail_url: str,
        requires_js: bool,
        *,
        html_raw: str | None = None,
    ) -> DetailPagePlan:
        """Fetch one sample detail page and ask the LLM to analyse its structure."""
        log.info("Analysing sample detail page: %s", detail_url)

        if html_raw is None:
            html_raw = await self._fetch_page(detail_url, prefer_js=requires_js)

        # Try normal simplification first, then aggressive if content filter triggers
        result: dict | None = None
        for attempt, aggressive in enumerate([False, True]):
            html_simple = simplify_html(html_raw, aggressive=aggressive)
            log.debug(
                "Detail page simplified HTML: %d chars (aggressive=%s)",
                len(html_simple), aggressive,
            )

            messages = [
                {"role": "system", "content": load_prompt("planner_detail")},
                {
                    "role": "user",
                    "content": (
                        f"Analyse the following HTML of a detail page at:\n{detail_url}\n\n"
                        f"```html\n{html_simple}\n```"
                    ),
                },
            ]

            try:
                result: dict = await chat_completion_json(messages, max_tokens=16_000)
                break
            except Exception as exc:
                if attempt == 0 and "content_filter" in str(exc).lower():
                    log.warning(
                        "Content filter triggered on detail page; retrying with aggressive sanitization"
                    )
                    continue
                raise

        if not result:
            raise ValueError(
                "Detail page analysis failed: LLM returned no data after all attempts."
            )

        detail_plan = DetailPagePlan.model_validate(result)
        log.info(
            "Detail page plan: %d field selectors, %d sub-links",
            len(detail_plan.field_selectors),
            len(detail_plan.sub_links),
        )
        return detail_plan

    # ------------------------------------------------------------------
    # Detail API discovery (for SPA/JS-only detail buttons)
    # ------------------------------------------------------------------
    async def _discover_detail_api(
        self,
        html_raw: str,
        plan: ScrapingPlan,
        *,
        detail_url: str | None = None,
    ) -> DetailApiPlan | None:
        """Capture a detail API triggered by a detail interaction.

        Uses Playwright network interception to capture the XHR/fetch request,
        then asks the LLM to derive the URL template and ID extraction method.
        """
        from app.utils.browser import (
            get_browser,
            intercept_detail_api,
            intercept_detail_api_from_detail_url,
        )

        if detail_url:
            log.info("Attempting detail API discovery via detail-link navigation")
        else:
            log.info("Attempting detail API discovery via button interception")

        async with get_browser() as browser:
            if detail_url:
                api_url, api_response = await intercept_detail_api_from_detail_url(
                    browser,
                    detail_url,
                    wait_selector=None,
                )
            else:
                api_url, api_response = await intercept_detail_api(
                    browser,
                    plan.url,
                    plan.target.item_container_selector,
                    plan.target.detail_button_selector,
                    wait_selector=plan.wait_selector,
                )

        if not api_url or not api_response:
            log.warning("Could not discover detail API via interception")
            return None

        log.info("Intercepted detail API: %s", api_url)

        # Extract the first item's HTML for the LLM to find the ID
        soup = BeautifulSoup(html_raw, "lxml")
        containers = soup.select(plan.target.item_container_selector)
        if not containers:
            log.warning("No item containers found for API discovery")
            return None
        first_item_html = str(containers[0])

        # Ask the LLM to derive the URL template and ID mapping
        response_preview = json.dumps(api_response, ensure_ascii=False)
        if len(response_preview) > 3_000:
            response_preview = response_preview[:3_000] + "..."

        messages = [
            {"role": "system", "content": load_prompt("planner_detail_api")},
            {
                "role": "user",
                "content": (
                    f"The listing page URL is: {plan.url}\n\n"
                    f"When loading the first item's detail interaction, this API call was observed:\n"
                    f"GET {api_url}\n\n"
                    + (f"The detail page URL that triggered it was: {detail_url}\n\n" if detail_url else "")
                    + (
                    f"The first listing item's HTML is:\n"
                    f"```html\n{first_item_html[:4_000]}\n```\n\n"
                    f"The API response:\n"
                    f"```json\n{response_preview}\n```"
                    )
                ),
            },
        ]

        result: dict = await chat_completion_json(messages, max_tokens=16_000)
        detail_api = DetailApiPlan.model_validate(result)
        detail_api.sample_response = api_response
        log.info(
            "Detail API plan: template=%s, id_selector=%s",
            detail_api.api_url_template,
            detail_api.id_selector,
        )
        return detail_api

    # ------------------------------------------------------------------
    # Selector validation
    # ------------------------------------------------------------------
    async def _validate_selectors(
        self,
        plan: ScrapingPlan,
        html_raw: str,
        fields_wanted: str | None = None,
        *,
        max_retries: int = 3,
    ) -> ScrapingPlan:
        """Test LLM-generated CSS selectors against the actual HTML.

        If ``item_container_selector`` matches 0 elements, re-prompt the LLM
        with feedback (up to *max_retries* times).  Returns the validated or
        best-effort plan.
        """
        soup = BeautifulSoup(html_raw, "lxml")

        for attempt in range(max_retries):
            reject_reason: str | None = None

            try:
                matches = soup.select(plan.target.item_container_selector)
            except Exception as exc:
                log.warning(
                    "Invalid CSS selector '%s': %s",
                    plan.target.item_container_selector, exc,
                )
                matches = []

            if matches:
                metrics = self._compute_selector_metrics(plan, soup)
                plan.selector_metrics = metrics
                hit_ratio = float(metrics.get("field_hit_ratio", 0.0))
                container_count = int(metrics.get("container_count", 0))
                field_checks = int(metrics.get("field_checks", 0))
                log.info(
                    "Selector validation: '%s' matched %d elements",
                    plan.target.item_container_selector, len(matches),
                )

                # Check 1: All field selectors are empty
                has_defined_fields = bool(plan.target.field_selectors)
                if has_defined_fields and field_checks == 0:
                    reject_reason = (
                        "All field selectors are empty. Please provide CSS selectors "
                        "for each field relative to the container element."
                    )

                # Check 2: Too few containers for a listing page
                elif container_count < _MIN_LISTING_CONTAINERS:
                    reject_reason = (
                        f"Your container selector matched only {container_count} element(s), "
                        "which likely selects the wrapper/container rather than individual "
                        "items. Look for the repeating child elements inside that container."
                    )

                # Check 3: Low hit-ratio
                elif hit_ratio < settings.reliability_selector_min_hit_ratio and field_checks > 0:
                    reject_reason = (
                        f"Field extraction quality is low (hit_ratio={hit_ratio:.3f})."
                    )

                # Check 4: Selector content test — verify extracted fields contain
                # non-trivial text, not just empty elements or boilerplate.
                if reject_reason is None and has_defined_fields and container_count >= _MIN_LISTING_CONTAINERS:
                    content_score = self._content_test_selectors(plan, matches)
                    if content_score < 0.3:
                        reject_reason = (
                            f"Content test failed (score={content_score:.2f}): the selectors "
                            "match elements but they contain little or no meaningful text. "
                            "Please check that field_selectors point to elements that "
                            "actually contain the data (text nodes, href, src attributes)."
                        )
                        log.warning(
                            "Selector content test failed: score=%.2f (attempt %d/%d)",
                            content_score, attempt + 1, max_retries,
                        )

                # Accept if no issue found or last attempt
                if reject_reason is None or attempt >= max_retries - 1:
                    if reject_reason and attempt >= max_retries - 1:
                        log.warning(
                            "Accepting plan despite issue on final attempt: %s",
                            reject_reason,
                        )
                    if plan.notes:
                        plan.notes += "\n"
                    plan.notes += (
                        "Selector preflight metrics: "
                        f"containers={container_count}, "
                        f"field_hit_ratio={metrics.get('field_hit_ratio')}"
                    )
                    # Non-blocking checks: warn if detail_link or pagination
                    # selectors match nothing — these won't trigger a re-prompt
                    # but surface misconfiguration early.
                    if plan.target.detail_link_selector and matches:
                        try:
                            sample = matches[:5]
                            link_hits = sum(
                                1 for c in sample
                                if c.select_one(plan.target.detail_link_selector)
                            )
                            if link_hits == 0:
                                log.warning(
                                    "detail_link_selector '%s' matched 0 links "
                                    "in first %d containers — detail pages may not be fetched",
                                    plan.target.detail_link_selector, len(sample),
                                )
                        except Exception:
                            pass
                    if plan.pagination_selector:
                        try:
                            if not soup.select(plan.pagination_selector):
                                log.warning(
                                    "pagination_selector '%s' matched 0 elements "
                                    "— pagination may not work",
                                    plan.pagination_selector,
                                )
                        except Exception:
                            pass
                    return plan

                log.warning(
                    "Selector issue (attempt %d/%d): %s",
                    attempt + 1, max_retries, reject_reason,
                )

            if not matches:
                # Selector matched nothing
                log.warning(
                    "Selector '%s' matched 0 elements (attempt %d/%d) — asking LLM to revise",
                    plan.target.item_container_selector, attempt + 1, max_retries,
                )

            # Collect some real container candidates from the page
            body = soup.find("body")
            hint_parts: list[str] = []
            if body:
                for tag in body.find_all(True, recursive=False):
                    snippet = str(tag)[:300]
                    hint_parts.append(snippet)
                    if len(hint_parts) >= 8:
                        break
            structure_hint = "\n".join(hint_parts) if hint_parts else "(no body content)"

            # Use the same aggressiveness as the initial plan so the LLM
            # sees a consistent HTML snapshot during retries.
            html_simple = simplify_html(html_raw, aggressive=False)

            feedback_content = (
                f"Your previous selector `{plan.target.item_container_selector}` matched "
                f"0 elements on the page. Please revise your selectors.\n\n"
                f"Here is a snapshot of the page's top-level body structure:\n"
                f"```html\n{structure_hint}\n```\n\n"
                f"Here is the simplified HTML again:\n"
                f"```html\n{html_simple}\n```\n\n"
                f"Return the complete revised plan as JSON."
            )

            if matches and reject_reason:
                metrics = self._compute_selector_metrics(plan, soup)
                # Show inner structure of the matched container to help the
                # LLM find the actual repeating child elements.
                inner_hint = ""
                try:
                    container_el = matches[0]
                    inner_parts: list[str] = []
                    for child in container_el.find_all(True, recursive=False):
                        inner_parts.append(str(child)[:300])
                        if len(inner_parts) >= 10:
                            break
                    if inner_parts:
                        inner_hint = (
                            f"\n\nHere are the direct children inside your matched container "
                            f"`{plan.target.item_container_selector}`:\n"
                            f"```html\n" + "\n".join(inner_parts) + "\n```\n"
                            "Look for the repeating elements among these children — "
                            "those are the individual items you should target with "
                            "item_container_selector. Then provide field_selectors "
                            "relative to each item.\n"
                        )
                except Exception:
                    pass

                feedback_content = (
                    f"{reject_reason}\n\n"
                    "Please improve item_container_selector and field_selectors for robust extraction.\n\n"
                    f"Current preflight metrics: {json.dumps(metrics)}\n"
                    f"{inner_hint}\n"
                    f"Here is a snapshot of the page's top-level body structure:\n"
                    f"```html\n{structure_hint}\n```\n\n"
                    f"Here is the simplified HTML again:\n"
                    f"```html\n{html_simple}\n```\n\n"
                    "Return the complete revised plan as JSON."
                )

            messages = [
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": (
                        f"Analyse the following HTML of the listing page at:\n{plan.url}\n\n"
                        f"```html\n{html_simple}\n```"
                    ),
                },
                {
                    "role": "assistant",
                    "content": json.dumps(
                        plan.model_dump(mode="json", exclude={"url", "selector_metrics"}),
                        ensure_ascii=False,
                    ),
                },
                {"role": "user", "content": feedback_content},
            ]

            if fields_wanted:
                messages[-1]["content"] += (
                    f"\n\nReminder: the user wants these fields: {fields_wanted}"
                )

            try:
                revised = await chat_completion_json(messages, max_tokens=16_000)
                revised["url"] = plan.url
                revised = _sanitize_plan_data(revised)
                plan = ScrapingPlan.model_validate(revised)
                log.info("Received revised plan from LLM (attempt %d)", attempt + 1)
            except Exception as exc:
                log.warning("Selector validation retry failed: %s", exc)
                break

        return plan

    # ------------------------------------------------------------------
    # Interactive exploration (vision-guided browsing)
    # ------------------------------------------------------------------
    async def _explore_page(
        self,
        url: str,
        *,
        needs_js: bool = False,
        max_steps: int | None = None,
    ) -> "ExplorationReport":
        """Interactively browse the page using vision-guided actions.

        Opens a Playwright browser, takes screenshots, asks GPT-4o what to do,
        executes actions, and compiles findings into an ExplorationReport.
        """
        from app.models.schemas import ExplorationAction, ExplorationReport
        from app.utils.browser import (
            _dismiss_consent_overlays,
            create_page,
            get_browser,
            safe_click,
        )
        from app.utils.llm import chat_completion_vision_json, encode_image_base64

        max_steps = max_steps or settings.exploration_max_steps
        report = ExplorationReport()
        actions: list[ExplorationAction] = []
        exploration_prompt = load_prompt("planner_exploration")

        async with get_browser() as browser:
            page = await create_page(browser)
            try:
                await page.goto(url, wait_until="commit", timeout=120_000)
                try:
                    await page.wait_for_load_state("domcontentloaded", timeout=30_000)
                except Exception:
                    pass
                await asyncio.sleep(3)  # let JS render

                # Dismiss cookie/consent overlays BEFORE exploration starts
                # so they don't dominate screenshots and waste exploration steps.
                await _dismiss_consent_overlays(page)
                await asyncio.sleep(1)

                original_url = url

                for step in range(max_steps):
                    # 1. Take screenshot
                    screenshot_bytes = await page.screenshot(type="png", full_page=False)
                    report.screenshots_taken += 1
                    image_uri = encode_image_base64(screenshot_bytes)

                    # 2. Build context from previous actions
                    history_text = ""
                    if actions:
                        history_text = "\nPrevious exploration steps:\n"
                        for a in actions:
                            history_text += (
                                f"  Step {a.step}: [{a.action_type}] "
                                f"{a.description} -> {a.observation}\n"
                            )

                    # 3. Ask vision LLM what to do
                    user_content: list[dict] = [
                        {"type": "text", "text": (
                            f"Page URL: {url}\n"
                            f"Step {step + 1} of {max_steps}\n"
                            f"{history_text}\n"
                            f"What should I do next to understand this page's structure?"
                        )},
                        {"type": "image_url", "image_url": {
                            "url": image_uri,
                            "detail": "low",
                        }},
                    ]
                    messages = [
                        {"role": "system", "content": exploration_prompt},
                        {"role": "user", "content": user_content},
                    ]

                    try:
                        result = await chat_completion_vision_json(
                            messages, max_tokens=2_000,
                        )
                    except Exception as exc:
                        log.warning("Exploration vision call failed at step %d: %s", step + 1, exc)
                        break

                    # 4. Record observation
                    action = ExplorationAction(
                        step=step + 1,
                        action_type=result.get("action", "done"),
                        target=result.get("target_selector", ""),
                        description=result.get("target_description", ""),
                        observation=result.get("observation", ""),
                    )

                    # 5. Accumulate findings
                    findings = result.get("findings", {})
                    if findings.get("pagination_type") and findings["pagination_type"] != "unknown":
                        report.pagination_notes = (
                            f"Pagination type: {findings['pagination_type']}"
                        )
                    if findings.get("has_detail_links"):
                        report.detail_page_notes = "Detail page links detected"
                    if findings.get("has_detail_buttons"):
                        report.detail_page_notes = "Detail buttons (JS-based) detected"
                    if findings.get("hidden_content_detected"):
                        report.hidden_content_notes = "Hidden content behind tabs/accordions"

                    log.info(
                        "Exploration step %d/%d: [%s] %s -> %s",
                        step + 1, max_steps,
                        action.action_type,
                        action.description or "(no description)",
                        (action.observation or "")[:120],
                    )

                    # 6. Detect consent-related actions and auto-dismiss instead
                    _consent_kw = {"cookie", "consent", "privacy", "accept all", "gdpr", "datenschutz"}
                    _action_text = f"{action.description} {action.target}".lower()
                    if action.action_type == "click" and any(kw in _action_text for kw in _consent_kw):
                        log.info("Exploration step %d: detected consent action — auto-dismissing instead", step + 1)
                        await _dismiss_consent_overlays(page)
                        await asyncio.sleep(1)
                        action.observation += " [auto-dismissed consent overlay]"
                        actions.append(action)
                        continue

                    # 7. Execute the action
                    if action.action_type == "done":
                        report.page_structure_notes = action.observation
                        actions.append(action)
                        break
                    elif action.action_type == "click" and action.target:
                        try:
                            clicked = await safe_click(page, action.target)
                            if not clicked:
                                action.observation += " [click failed — element not found or not clickable]"
                        except Exception as exc:
                            action.observation += f" [click error: {exc}]"
                        await asyncio.sleep(2)
                    elif action.action_type == "scroll":
                        await page.evaluate("window.scrollBy(0, window.innerHeight)")
                        await asyncio.sleep(1.5)
                    elif action.action_type == "navigate" and action.target:
                        try:
                            nav_url = action.target
                            if nav_url.startswith("/"):
                                from urllib.parse import urlparse as _up
                                p = _up(original_url)
                                nav_url = f"{p.scheme}://{p.netloc}{nav_url}"
                            await page.goto(nav_url, wait_until="commit", timeout=30_000)
                            await asyncio.sleep(2)
                            # Take a detail page screenshot for the next iteration
                            # so the LLM can observe what a detail page looks like.
                            # (The loop will naturally screenshot this state.)
                        except Exception as exc:
                            action.observation += f" [navigation failed: {exc}]"
                            # Try to go back to original
                            try:
                                await page.goto(original_url, wait_until="commit", timeout=30_000)
                                await asyncio.sleep(2)
                            except Exception:
                                pass
                    else:
                        # Unknown or invalid action — stop
                        actions.append(action)
                        break

                    actions.append(action)

                # Capture final HTML state after all interactions
                try:
                    # Navigate back to original URL if we ended on a detail page
                    current = page.url
                    if current != original_url:
                        await page.goto(original_url, wait_until="commit", timeout=30_000)
                        await asyncio.sleep(2)
                    report.final_html = await page.content()
                except Exception:
                    pass

            finally:
                await page.close()

        report.actions = actions
        return report

    @staticmethod
    def _format_exploration_notes(report: "ExplorationReport") -> str:
        """Format exploration findings as text for injection into planning prompts."""
        parts: list[str] = []
        if report.page_structure_notes:
            parts.append(f"Page structure: {report.page_structure_notes}")
        if report.pagination_notes:
            parts.append(f"Pagination: {report.pagination_notes}")
        if report.hidden_content_notes:
            parts.append(f"Hidden content: {report.hidden_content_notes}")
        if report.detail_page_notes:
            parts.append(f"Detail pages: {report.detail_page_notes}")

        if report.actions:
            parts.append("\nExploration steps taken:")
            for a in report.actions:
                parts.append(
                    f"  {a.step}. [{a.action_type}] {a.description} -> {a.observation}"
                )

        return "\n".join(parts) if parts else ""

    # ------------------------------------------------------------------
    # Vision-based planning (GPT-4o screenshot analysis)
    # ------------------------------------------------------------------
    async def _plan_with_vision(
        self,
        url: str,
        html_raw: str,
        *,
        fields_wanted: str | None = None,
        wait_selector: str | None = None,
        exploration_notes: str | None = None,
    ) -> ScrapingPlan | None:
        """Take a screenshot and use the vision model to generate a scraping plan.

        Returns None if vision planning fails or is not configured.
        """
        from app.utils.llm import get_vision_client, chat_completion_vision_json, encode_image_base64
        from app.utils.browser import capture_screenshot, get_browser

        if get_vision_client() is None:
            log.info("Vision model not configured — skipping vision-based planning")
            return None

        try:
            async with get_browser() as browser:
                screenshot_bytes = await capture_screenshot(
                    browser, url, wait_selector=wait_selector,
                )
        except Exception as exc:
            log.warning("Screenshot capture failed: %s", exc)
            return None

        image_uri = encode_image_base64(screenshot_bytes)
        html_simple = simplify_html(html_raw, aggressive=True)
        # Truncate HTML to keep vision request reasonable
        if len(html_simple) > 30_000:
            html_simple = html_simple[:30_000] + "\n<!-- truncated -->"

        vision_prompt = load_prompt("planner_vision")
        user_content: list[dict] = [
            {"type": "text", "text": (
                f"Analyse this listing page at: {url}\n\n"
                f"Here is the simplified HTML:\n```html\n{html_simple}\n```"
            )},
            {"type": "image_url", "image_url": {"url": image_uri}},
        ]
        if fields_wanted:
            user_content[0]["text"] += (
                f"\n\nThe user wants these specific fields: {fields_wanted}"
            )
        if exploration_notes:
            user_content[0]["text"] += (
                f"\n\nExploration notes from interactive browsing:\n{exploration_notes}"
            )

        messages = [
            {"role": "system", "content": vision_prompt},
            {"role": "user", "content": user_content},
        ]

        try:
            plan_data = await chat_completion_vision_json(messages, max_tokens=16_000)
        except Exception as exc:
            log.warning("Vision planning LLM call failed: %s", exc)
            return None

        plan_data["url"] = url
        plan_data = _sanitize_plan_data(plan_data)

        try:
            plan = ScrapingPlan.model_validate(plan_data)
        except Exception as exc:
            log.warning("Vision plan validation failed: %s", exc)
            return None

        log.info(
            "Vision plan: selector='%s', %d fields",
            plan.target.item_container_selector,
            len(plan.target.field_selectors),
        )
        return plan

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _extract_first_detail_link(self, html: str, plan: ScrapingPlan) -> str | None:
        """Extract the first detail page URL from the listing HTML.

        Tries the first few containers rather than only the first one — some
        pages render the first card differently (e.g. a featured / sponsored
        slot) or the selector only matches from the second item onwards.
        """
        soup = BeautifulSoup(html, "lxml")
        containers = soup.select(plan.target.item_container_selector)
        if not containers:
            return None

        parsed = urlparse(plan.url)
        for container in containers[:5]:
            try:
                link_el = container.select_one(plan.target.detail_link_selector)
            except Exception as exc:
                log.warning(
                    "Invalid detail_link_selector '%s': %s",
                    plan.target.detail_link_selector, exc,
                )
                return None
            if not link_el:
                continue

            href = link_el.get("href")
            if not href:
                continue

            # Resolve relative URLs
            if href.startswith("/"):
                href = f"{parsed.scheme}://{parsed.netloc}{href}"
            elif not href.startswith("http"):
                href = urljoin(plan.url, href)

            return href

        return None

    async def _fetch_page(self, url: str, *, prefer_js: bool = False) -> str:
        """Fetch a page, with httpx-first fallback to Playwright."""
        if prefer_js:
            from app.utils.browser import fetch_page_js, get_browser

            async with get_browser() as browser:
                return await fetch_page_js(browser, url)

        html_raw = ""
        needs_js_fallback = False
        try:
            html_raw = await fetch_page(url)
            if len(html_raw) < 2_000 or "<noscript>" in html_raw.lower():
                needs_js_fallback = True
        except Exception:
            needs_js_fallback = True

        if needs_js_fallback:
            log.info("httpx fetch insufficient; falling back to Playwright for %s", url)
            from app.utils.browser import fetch_page_js, get_browser

            async with get_browser() as browser:
                html_raw = await fetch_page_js(browser, url)

        return html_raw
