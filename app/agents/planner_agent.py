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

from app.models.schemas import DetailApiPlan, DetailPagePlan, ScrapingPlan
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

    return plan_data


async def _fetch_html(url: str) -> str:
    """Fetch a page — try httpx first, fall back to Playwright."""
    html_raw = ""
    needs_js = False
    try:
        html_raw = await fetch_page(url)
        if len(html_raw) < 2_000 or "<noscript>" in html_raw.lower():
            needs_js = True
    except Exception:
        needs_js = True

    if needs_js:
        log.info("httpx fetch insufficient; falling back to Playwright for %s", url)
        from app.utils.browser import fetch_page_js, get_browser

        async with get_browser() as browser:
            html_raw = await fetch_page_js(browser, url)

    return html_raw


class PlannerAgent:
    """Analyse a listing page and return a ScrapingPlan."""

    async def plan(
        self,
        url: str,
        *,
        detail_page_url: str | None = None,
        fields_wanted: str | None = None,
    ) -> ScrapingPlan:
        log.info("Planning scrape for %s", url)

        # --- 1. Fetch the listing page ---
        html_raw = await _fetch_html(url)
        html_simple = simplify_html(html_raw)
        log.debug("Simplified listing HTML: %d chars", len(html_simple))

        # --- 2. Optionally fetch the detail page ---
        detail_html_simple = ""
        if detail_page_url:
            log.info("Fetching user-provided detail page: %s", detail_page_url)
            try:
                detail_raw = await _fetch_html(detail_page_url)
                detail_html_simple = simplify_html(detail_raw)
                log.debug("Simplified detail HTML: %d chars", len(detail_html_simple))
            except Exception as exc:
                log.warning("Failed to fetch detail page %s: %s", detail_page_url, exc)

        # --- 3. Build the prompt ---
        user_content = (
            f"Analyse the following HTML of the listing page at:\n{url}\n\n"
            f"```html\n{html_simple}\n```"
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

        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ]

        plan_data: dict = await chat_completion_json(messages, max_tokens=8_000)
        log.info("Received scraping plan from LLM")

        # --- 4. Parse into ScrapingPlan ---
        plan_data["url"] = url
        plan_data = _sanitize_plan_data(plan_data)
        plan = ScrapingPlan.model_validate(plan_data)
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

        html_raw = await _fetch_html(current_plan.url)
        html_simple = simplify_html(html_raw)

        current_plan_json = current_plan.model_dump(mode="json")

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

        plan_data: dict = await chat_completion_json(messages, max_tokens=8_000)
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
        html_simple = simplify_html(html_raw)
        log.debug("Detail page simplified HTML: %d chars", len(html_simple))

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

        result: dict = await chat_completion_json(messages, max_tokens=4_000)
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
    # Helpers
    # ------------------------------------------------------------------
    def _extract_first_detail_link(self, html: str, plan: ScrapingPlan) -> str | None:
        """Extract the first detail page URL from the listing HTML."""
        soup = BeautifulSoup(html, "lxml")
        containers = soup.select(plan.target.item_container_selector)
        if not containers:
            return None

        first = containers[0]
        link_el = first.select_one(plan.target.detail_link_selector)
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
