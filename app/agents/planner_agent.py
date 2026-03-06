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

import json
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from app.models.schemas import DetailApiPlan, DetailPagePlan, ScrapingPlan, TemplateHints
from app.prompts import load_prompt
from app.utils.html import simplify_html
from app.utils.http import fetch_page
from app.utils.llm import chat_completion_json
from app.utils.logging import get_logger

log = get_logger(__name__)

SYSTEM_PROMPT = load_prompt("planner_listing")


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

    return plan_data


async def _fetch_html(url: str) -> tuple[str, bool]:
    """Fetch a page — try httpx first, fall back to Playwright.

    Returns (html, needs_js) so callers can override ``requires_javascript``.
    """
    html_raw = ""
    needs_js = False

    # Heuristic 1: URL hash routing → always needs JS
    if "#/" in url or "#!/" in url:
        needs_js = True
        log.info("URL contains hash routing — will use Playwright for %s", url)

    if not needs_js:
        try:
            html_raw = await fetch_page(url)
            if len(html_raw) < 2_000 or "<noscript>" in html_raw.lower():
                needs_js = True

            # Heuristic 2: SPA framework markers in raw HTML
            if not needs_js:
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
                    needs_js = True
                    log.info("SPA framework marker detected in HTML for %s", url)
                elif any(m in lower for m in spa_markers_weak):
                    body = BeautifulSoup(html_raw, "lxml").find("body")
                    if body and len(body.get_text(strip=True)) < 500:
                        needs_js = True
                        log.info("Weak SPA marker + sparse body text for %s", url)

            # Heuristic 3: Custom HTML elements (web components) that are
            # major content containers.  Per the spec, custom element tag names
            # always contain a hyphen.  We only flag when a custom element sits
            # near the top of the body and is large (likely a data container
            # rendered by JS), to avoid false-positives from tiny UI widgets.
            if not needs_js:
                body_tag = BeautifulSoup(html_raw, "lxml").find("body")
                if body_tag:
                    for tag in body_tag.find_all(True, recursive=False):
                        if "-" in tag.name and len(str(tag)) > 500:
                            needs_js = True
                            log.info(
                                "Large custom element <%s> detected — likely JS-rendered for %s",
                                tag.name, url,
                            )
                            break
                    if not needs_js:
                        # Check one level deeper (common pattern: <div><my-app>)
                        for wrapper in body_tag.find_all(True, recursive=False):
                            for tag in wrapper.find_all(True, recursive=False):
                                if "-" in tag.name and len(str(tag)) > 500:
                                    needs_js = True
                                    log.info(
                                        "Large custom element <%s> detected — likely JS-rendered for %s",
                                        tag.name, url,
                                    )
                                    break
                            if needs_js:
                                break

            # Heuristic 4: Large HTML shell with almost no visible text
            if not needs_js and len(html_raw) >= 2_000:
                body = BeautifulSoup(html_raw, "lxml").find("body")
                if body and len(body.get_text(strip=True)) < 200:
                    needs_js = True
                    log.info("HTML shell has <200 chars of text — likely JS-rendered for %s", url)
        except Exception:
            needs_js = True

    if needs_js:
        log.info("httpx fetch insufficient; falling back to Playwright for %s", url)
        from app.utils.browser import fetch_page_js, get_browser

        async with get_browser() as browser:
            html_raw = await fetch_page_js(browser, url)

    return html_raw, needs_js


class PlannerAgent:
    """Analyse a listing page and return a ScrapingPlan."""

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

        # --- 3. Build the prompt (retry with aggressive sanitization on content filter) ---
        plan_data: dict | None = None
        for attempt, aggressive in enumerate([False, True]):
            html_simple = simplify_html(html_raw, aggressive=aggressive)
            log.debug("Simplified listing HTML: %d chars (aggressive=%s)", len(html_simple), aggressive)

            detail_html_simple = ""
            if detail_raw:
                detail_html_simple = simplify_html(detail_raw, aggressive=aggressive)
                log.debug("Simplified detail HTML: %d chars (aggressive=%s)", len(detail_html_simple), aggressive)

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

            # Inject template hints so the LLM has structural guidance
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
                # Only mention template pagination as a weak hint when user didn't specify
                if not pagination_type and template_hints.pagination:
                    user_content += f"- expected pagination (hint only): {template_hints.pagination}\n"
                if template_hints.notes:
                    user_content += f"- pattern notes: {template_hints.notes}\n"

            # Inject user-specified pagination — this overrides everything
            if pagination_type:
                user_content += (
                    f"\n\n━━ PAGINATION (user-specified — MUST USE) ━━\n"
                    f"The user has explicitly specified the pagination strategy: {pagination_type}\n"
                    f"You MUST set pagination to \"{pagination_type}\" in your plan.\n"
                    f"Do NOT override this with your own analysis.\n"
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
            raise ValueError(
                "Planning failed: LLM returned no data after all attempts. "
                "The page content may have triggered a content filter."
            )

        log.info("Received scraping plan from LLM")

        # --- 4. Parse into ScrapingPlan ---
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
            # For 'none' (single page) and 'infinite_scroll', pre-resolved
            # pagination_urls from the LLM are irrelevant — clear them to
            # avoid scraping non-existent pages.
            if pagination_type in ("none", "infinite_scroll", "load_more_button"):
                plan_data["pagination_urls"] = []

        plan = ScrapingPlan.model_validate(plan_data)

        # --- 4b. Validate CSS selectors against the actual HTML ---
        plan = await self._validate_selectors(plan, html_raw, fields_wanted)

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
                if fields_wanted:
                    user_content += (
                        f"\n\nThe user specifically wants these fields extracted: {fields_wanted}\n"
                        f"Make sure the plan captures all of these fields — from the listing page "
                        f"if available, otherwise from the detail page."
                    )
                if template_hints:
                    user_content += (
                        "\n\n━━ TEMPLATE HINTS (structural guidance) ━━\n"
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
                    plan = ScrapingPlan.model_validate(plan_data_js)
                    plan = await self._validate_selectors(plan, html_raw, fields_wanted)
                    plan_data = plan_data_js
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

        # --- 5. Analyse a sample detail page (if detail link found) ---
        if plan.target.detail_link_selector:
            sample_detail_url = self._extract_first_detail_link(html_raw, plan)
            if sample_detail_url:
                try:
                    detail_plan = await self._analyze_detail_page(
                        sample_detail_url,
                        plan.requires_javascript,
                    )
                    plan.detail_page_plan = detail_plan
                    # Populate legacy fields for backward compat with parser
                    plan.detail_page_fields = detail_plan.field_selectors
                    plan.detail_page_field_attributes = detail_plan.field_attributes
                except Exception as exc:
                    log.warning("Detail page analysis failed: %s", exc)

        # --- 6. Try API interception for JS-only detail buttons ---
        if not plan.detail_page_plan and plan.target.detail_button_selector:
            try:
                detail_api = await self._discover_detail_api(html_raw, plan)
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

        html_raw, _ = await _fetch_html(current_plan.url)
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
        plan = ScrapingPlan.model_validate(plan_data)
        log.info(
            "Re-plan: js=%s, pagination=%s, fields=%s, detail_link=%s",
            plan.requires_javascript,
            plan.pagination.value,
            list(plan.target.field_selectors.keys()),
            plan.target.detail_link_selector,
        )

        # Re-analyse detail page if detail link is present
        if plan.target.detail_link_selector:
            sample_detail_url = self._extract_first_detail_link(html_raw, plan)
            if sample_detail_url:
                try:
                    detail_plan = await self._analyze_detail_page(
                        sample_detail_url,
                        plan.requires_javascript,
                    )
                    plan.detail_page_plan = detail_plan
                    plan.detail_page_fields = detail_plan.field_selectors
                    plan.detail_page_field_attributes = detail_plan.field_attributes
                except Exception as exc:
                    log.warning("Detail page analysis failed during replan: %s", exc)

        # Try API interception for JS-only detail buttons
        if not plan.detail_page_plan and plan.target.detail_button_selector:
            try:
                detail_api = await self._discover_detail_api(html_raw, plan)
                if detail_api:
                    plan.detail_api_plan = detail_api
            except Exception as exc:
                log.warning("Detail API discovery failed during replan: %s", exc)

        return plan

    # ------------------------------------------------------------------
    # Detail page analysis
    # ------------------------------------------------------------------
    async def _analyze_detail_page(
        self,
        detail_url: str,
        requires_js: bool,
    ) -> DetailPagePlan:
        """Fetch one sample detail page and ask the LLM to analyse its structure."""
        log.info("Analysing sample detail page: %s", detail_url)

        html_raw = await self._fetch_page(detail_url, prefer_js=requires_js)

        # Try normal simplification first, then aggressive if content filter triggers
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
                result: dict = await chat_completion_json(messages, max_tokens=8_000)
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
    ) -> DetailApiPlan | None:
        """Click a JS-only detail button and intercept the resulting API call.

        Uses Playwright network interception to capture the XHR/fetch request,
        then asks the LLM to derive the URL template and ID extraction method.
        """
        from app.utils.browser import get_browser, intercept_detail_api

        log.info("Attempting detail API discovery via button interception")

        async with get_browser() as browser:
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
                    f"When clicking the detail button on the first item, this API call was made:\n"
                    f"GET {api_url}\n\n"
                    f"The first listing item's HTML is:\n"
                    f"```html\n{first_item_html[:4_000]}\n```\n\n"
                    f"The API response:\n"
                    f"```json\n{response_preview}\n```"
                ),
            },
        ]

        result: dict = await chat_completion_json(messages, max_tokens=2_000)
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
        max_retries: int = 2,
    ) -> ScrapingPlan:
        """Test LLM-generated CSS selectors against the actual HTML.

        If ``item_container_selector`` matches 0 elements, re-prompt the LLM
        with feedback (up to *max_retries* times).  Returns the validated or
        best-effort plan.
        """
        soup = BeautifulSoup(html_raw, "lxml")

        for attempt in range(max_retries):
            try:
                matches = soup.select(plan.target.item_container_selector)
            except Exception as exc:
                log.warning(
                    "Invalid CSS selector '%s': %s",
                    plan.target.item_container_selector, exc,
                )
                matches = []

            if matches:
                log.info(
                    "Selector validation: '%s' matched %d elements",
                    plan.target.item_container_selector, len(matches),
                )
                return plan

            # Selector matched nothing — build a hint of the page's actual structure
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

            html_simple = simplify_html(html_raw, aggressive=True)

            feedback_content = (
                f"Your previous selector `{plan.target.item_container_selector}` matched "
                f"0 elements on the page. Please revise your selectors.\n\n"
                f"Here is a snapshot of the page's top-level body structure:\n"
                f"```html\n{structure_hint}\n```\n\n"
                f"Here is the simplified HTML again:\n"
                f"```html\n{html_simple}\n```\n\n"
                f"Return the complete revised plan as JSON."
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
                        plan.model_dump(mode="json", exclude={"url"}),
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
    # Helpers
    # ------------------------------------------------------------------
    def _extract_first_detail_link(self, html: str, plan: ScrapingPlan) -> str | None:
        """Extract the first detail page URL from the listing HTML."""
        soup = BeautifulSoup(html, "lxml")
        containers = soup.select(plan.target.item_container_selector)
        if not containers:
            return None

        first = containers[0]
        try:
            link_el = first.select_one(plan.target.detail_link_selector)
        except Exception as exc:
            log.warning("Invalid detail_link_selector '%s': %s", plan.target.detail_link_selector, exc)
            return None
        if not link_el:
            return None

        href = link_el.get("href")
        if not href:
            return None

        # Resolve relative URLs
        parsed = urlparse(plan.url)
        if href.startswith("/"):
            href = f"{parsed.scheme}://{parsed.netloc}{href}"
        elif not href.startswith("http"):
            href = urljoin(plan.url, href)

        return href

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
