"""PlannerAgent — Analyses the HTML structure of a listing page and produces a ScrapingPlan.

Flow
----
1. Fetch the target URL (httpx first; Playwright fallback if the page looks JS-heavy).
2. Simplify the HTML (strip scripts/styles/boilerplate).
3. Send the simplified HTML to GPT with a structured prompt asking it to identify
   item containers, field selectors, pagination strategy, and whether JS rendering
   is required.
4. Parse the JSON response into a ``ScrapingPlan``.
"""

from __future__ import annotations

import json

from app.models.schemas import ScrapingPlan
from app.utils.html import simplify_html
from app.utils.http import fetch_page
from app.utils.llm import chat_completion_json
from app.utils.logging import get_logger

log = get_logger(__name__)

SYSTEM_PROMPT = """\
You are an expert web-scraping engineer. Your job is to analyse the HTML \
structure of an exhibitor / seller listing page and produce a detailed \
scraping plan in JSON format.

You will receive the simplified HTML of the page. Study the DOM carefully \
and return a JSON object with the following fields:

{
  "requires_javascript": <bool>,
  "pagination": "<none|next_button|page_numbers|infinite_scroll|load_more_button|alphabet_tabs|api_endpoint>",
  "pagination_selector": "<CSS selector for the pagination control, or null>",
  "pagination_urls": ["<list of predictable page URLs if applicable, else empty>"],
  "alphabet_tab_selector": "<CSS selector for alphabet/category tabs, or null>",
  "api_endpoint": "<discovered XHR/API URL, or null>",
  "api_params": {},
  "target": {
    "item_container_selector": "<CSS selector for each repeating exhibitor card/row>",
    "field_selectors": {
      "<field_name>": "<CSS selector relative to the item container>",
      ...
    },
    "field_attributes": {
      "<field_name>": "<HTML attribute to read, e.g. 'href', 'src'>",
      ...
    },
    "detail_link_selector": "<CSS selector for the link to the detail page, or null>"
  },
  "detail_page_fields": {
    "<field_name>": "<CSS selector on the detail page>",
    ...
  },
  "detail_page_field_attributes": {
    "<field_name>": "<attribute to read>",
    ...
  },
  "wait_selector": "<CSS selector to wait for before scraping on JS pages, or null>",
  "notes": "<any observations about the page>"
}

Rules:
- ``item_container_selector`` must match the *repeating* element that wraps ONE \
  exhibitor/seller.  Prefer the most specific selector possible.
- ``field_selectors`` values are relative to the item container.
- Include as many fields as you can find: name, booth/stand, country, city, \
  website link, logo image, description snippet, categories, etc.
- For links and images, put the attribute (href / src) in ``field_attributes``.
- If the page uses hash-based routing (#/), client-side rendering, or heavy JS \
  frameworks (React/Angular/Vue), set ``requires_javascript`` to true.
- If you detect an XHR/fetch API that returns exhibitor JSON, report it in \
  ``api_endpoint`` and set ``pagination`` to ``api_endpoint``.
- For alphabet navigation (tabs A-Z), use ``alphabet_tabs`` pagination and \
  supply ``alphabet_tab_selector``.
- ``pagination_urls`` should list fully qualified URLs when the pattern is \
  obvious (e.g. ?page=1 … ?page=10).  Leave empty if you cannot determine them.

IMPORTANT — Detail pages:
- Most listing pages only show a summary (name, booth, logo). The FULL details \
  (address, phone, email, website, description, product categories, brands, \
  social media) are almost always on a separate DETAIL page linked from each \
  item card (often a "Details", "More info", or company-name link).
- You MUST identify the ``detail_link_selector`` — the CSS selector (relative \
  to the item container) that points to the exhibitor's detail / profile page. \
  This is typically an <a> tag wrapping the company name or a "Details" button.
- Put "href" in ``field_attributes`` for the ``detail_link`` field so the \
  scraper can follow the link.
- For ``detail_page_fields``: provide CSS selectors for fields you expect to \
  find on the detail page (address, phone, email, website, description, etc.). \
  If you are unsure of the exact selectors on the detail page, leave \
  ``detail_page_fields`` empty — the system will use the full simplified HTML.

- Return ONLY valid JSON.  No markdown, no explanation.
"""


class PlannerAgent:
    """Analyse a listing page and return a ScrapingPlan."""

    async def plan(self, url: str) -> ScrapingPlan:
        log.info("Planning scrape for %s", url)

        # --- 1. Fetch the page (try httpx first) ----
        html_raw = ""
        needs_js_fallback = False
        try:
            html_raw = await fetch_page(url)
            # Heuristic: if the body is almost empty, the page is likely JS-rendered
            if len(html_raw) < 2_000 or "<noscript>" in html_raw.lower():
                needs_js_fallback = True
        except Exception:
            needs_js_fallback = True

        if needs_js_fallback:
            log.info("httpx fetch insufficient; falling back to Playwright for %s", url)
            from app.utils.browser import fetch_page_js, get_browser

            async with get_browser() as browser:
                html_raw = await fetch_page_js(browser, url)

        # --- 2. Simplify HTML for the LLM ---
        html_simple = simplify_html(html_raw)
        log.debug("Simplified HTML: %d chars", len(html_simple))

        # --- 3. Ask GPT to produce the plan ---
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    f"Analyse the following HTML of the listing page at:\n{url}\n\n"
                    f"```html\n{html_simple}\n```"
                ),
            },
        ]

        plan_data: dict = await chat_completion_json(messages, max_tokens=8_000)
        log.info("Received scraping plan from LLM")

        # --- 4. Parse into ScrapingPlan ---
        plan_data["url"] = url
        plan = ScrapingPlan.model_validate(plan_data)
        log.info(
            "Plan: js=%s, pagination=%s, fields=%s, detail_link=%s",
            plan.requires_javascript,
            plan.pagination.value,
            list(plan.target.field_selectors.keys()),
            plan.target.detail_link_selector,
        )
        return plan

    async def replan(self, current_plan: ScrapingPlan, user_feedback: str) -> ScrapingPlan:
        """Re-generate the scraping plan incorporating user feedback.

        This is called when the user reviews the preview and provides feedback
        like "I also need email and phone" or "the detail page has more info".
        """
        log.info("Re-planning with user feedback: %s", user_feedback)

        # Fetch the page again to get fresh HTML for context
        html_raw = ""
        try:
            html_raw = await fetch_page(current_plan.url)
            if len(html_raw) < 2_000 or "<noscript>" in html_raw.lower():
                raise ValueError("JS fallback needed")
        except Exception:
            from app.utils.browser import fetch_page_js, get_browser

            async with get_browser() as browser:
                html_raw = await fetch_page_js(browser, current_plan.url)

        html_simple = simplify_html(html_raw)

        current_plan_json = current_plan.model_dump(mode="json")

        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
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
        plan = ScrapingPlan.model_validate(plan_data)
        log.info(
            "Re-plan: js=%s, pagination=%s, fields=%s, detail_link=%s",
            plan.requires_javascript,
            plan.pagination.value,
            list(plan.target.field_selectors.keys()),
            plan.target.detail_link_selector,
        )
        return plan
