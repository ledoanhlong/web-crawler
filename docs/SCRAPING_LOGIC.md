# Scraping Logic — Detailed Technical Documentation

This document provides a comprehensive explanation of how the web crawler's scraping logic works, from initial URL submission through to final structured output.

---

## Table of Contents

1. [High-Level Pipeline](#1-high-level-pipeline)
2. [Stage 1: Routing (RouterAgent)](#2-stage-1-routing-routeragent)
3. [Stage 2: Planning (PlannerAgent)](#3-stage-2-planning-planneragent)
4. [Stage 3: Scraping (ScraperAgent)](#4-stage-3-scraping-scraperagent)
5. [Stage 4: Parsing (ParserAgent)](#5-stage-4-parsing-parseragent)
6. [Stage 5: Output (OutputAgent)](#6-stage-5-output-outputagent)
7. [Template System](#7-template-system)
8. [Extraction Methods](#8-extraction-methods)
9. [Detail Page Enrichment](#9-detail-page-enrichment)
10. [Pagination Strategies](#10-pagination-strategies)
11. [Error Handling & Fallbacks](#11-error-handling--fallbacks)

---

## 1. High-Level Pipeline

When a user submits a URL and prompt, the system follows this pipeline:

```
User Request
     │
     ▼
RouterAgent ──► selects strategy (full_pipeline / smart_scraper / script_creator)
     │
     ▼ (if full_pipeline)
PlannerAgent ──► analyses HTML, produces ScrapingPlan
     │
     ▼
ScraperAgent ──► executes plan, returns raw PageData
     │    ├── CSS Selector extraction
     │    ├── ScrapeGraphAI extraction (dual preview)
     │    ├── Detail page fetching
     │    └── API interception
     │
     ▼
ParserAgent ──► normalizes raw data into SellerLead records
     │
     ▼
OutputAgent ──► quality pass (dedup), writes JSON + CSV
```

The pipeline has a **preview checkpoint**: after planning, the system scrapes a single item using both CSS and AI extraction methods, parses it, and pauses for user review. Only after the user confirms does the full crawl proceed.

---

## 2. Stage 1: Routing (RouterAgent)

**File:** `app/agents/router_agent.py`

The `RouterAgent` receives the user's URL(s) and prompt, then decides which scraping strategy to use. It sends the request details to the LLM and expects a JSON response with `strategy` and `explanation`.

### Available Strategies

| Strategy | When Used | Description |
|----------|-----------|-------------|
| `full_pipeline` | Listing pages with pagination, detail pages | Full multi-agent pipeline with preview workflow |
| `smart_scraper` | Single page, simple extraction | Direct ScrapeGraphAI extraction (no CSS planning) |
| `smart_scraper_multi` | Multiple URLs, similar structure | ScrapeGraphAI across multiple URLs with merged results |
| `script_creator` | Complex pages, reusable scripts | Generates a Python script, optionally auto-executes |

### Fallback Routing

If the LLM call fails, a rule-based fallback selects the strategy:
- Multiple URLs → `smart_scraper_multi`
- Detail page URL or fields specified → `full_pipeline`
- Default → `smart_scraper`

---

## 3. Stage 2: Planning (PlannerAgent)

**File:** `app/agents/planner_agent.py`

The `PlannerAgent` is the most critical stage. It analyses the target page's HTML structure and produces a `ScrapingPlan` — a complete set of instructions for the scraper.

### Step-by-Step Flow

1. **Fetch the listing page**
   - Tries `httpx` (static HTTP) first
   - Falls back to Playwright if:
     - URL contains hash routing (`#/` or `#!/`)
     - HTML is too short (`<2000` chars)
     - Contains `<noscript>` tags
     - Has SPA framework markers (`id="app"`, `id="root"`, `ng-app`, etc.)
     - Body text is `<200` characters (JS shell)

2. **Optionally fetch a user-provided detail page**
   - Uses the same static-then-JS fallback logic

3. **Simplify the HTML**
   - Strips `<script>`, `<style>`, `<noscript>`, `<svg>`, `<iframe>`
   - Removes HTML comments, header, footer, nav, cookie banners
   - Collapses whitespace and truncates to 80k chars
   - In "aggressive" mode (retry after content filter): also strips images, data attributes, inline styles, ad containers, and the entire `<head>`

4. **LLM analysis**
   - Sends the simplified HTML to GPT with the `planner_listing` system prompt
   - Requests JSON output containing:
     - `item_container_selector` — CSS selector for each repeating item
     - `field_selectors` — map of field name → CSS selector (relative to container)
     - `field_attributes` — map of field name → HTML attribute to read
     - `pagination` strategy and selector
     - `requires_javascript` flag
     - `detail_link_selector` — link to detail pages
     - `detail_button_selector` — JS-only button (no href, triggers XHR)
   - If the content filter triggers, retries with aggressive HTML sanitization

5. **Sanitize LLM output**
   - Flattens nested dicts in `detail_page_fields`, `field_selectors`, etc.
   - Strips non-string values

6. **Heuristic override**
   - If the httpx fetch was insufficient (detected JS-rendered page), forces `requires_javascript=True` even if the LLM said otherwise

7. **Detail page analysis** (if `detail_link_selector` found)
   - Extracts the first detail link from the listing HTML
   - Fetches that detail page
   - Sends it to GPT with the `planner_detail` prompt
   - Produces a `DetailPagePlan` with its own `field_selectors` and `sub_links`

8. **Detail API discovery** (if `detail_button_selector` found, no detail page)
   - Uses Playwright to click the first item's detail button
   - Intercepts all JSON network responses after the click
   - Scores each response to find the most likely detail API (filters noise: analytics, chat widgets, trackers)
   - Sends the captured API URL and response to GPT with the `planner_detail_api` prompt
   - Produces a `DetailApiPlan` with `api_url_template`, `id_selector`, `id_attribute`, `id_regex`

### ScrapingPlan Schema (key fields)

```
ScrapingPlan:
  url: str                          # Target listing URL
  requires_javascript: bool         # Whether Playwright is needed
  pagination: PaginationStrategy    # none, next_button, page_numbers, etc.
  pagination_selector: str          # CSS selector for pagination control
  pagination_urls: list[str]        # Pre-computed page URLs
  alphabet_tab_selector: str        # CSS selector for A-Z tabs
  target:
    item_container_selector: str    # CSS selector for each item wrapper
    field_selectors: dict           # field_name → CSS selector
    field_attributes: dict          # field_name → HTML attribute
    detail_link_selector: str       # Link to detail page
    detail_button_selector: str     # JS-only detail button
  detail_page_plan: DetailPagePlan  # Detail page CSS selectors
  detail_api_plan: DetailApiPlan    # API interception configuration
  wait_selector: str                # Wait for this element before scraping
```

---

## 4. Stage 3: Scraping (ScraperAgent)

**File:** `app/agents/scraper_agent.py`

The `ScraperAgent` executes the `ScrapingPlan` and returns a list of `PageData` objects containing raw extracted items.

### Execution Path Selection

The scraper selects its execution path based on the plan:

```
plan.pagination == API_ENDPOINT?  →  _scrape_api()
plan.requires_javascript?         →  _scrape_js()
settings.use_scrapy?              →  _scrape_scrapy() (with httpx fallback)
otherwise                         →  _scrape_static()
```

### 4.1 Static Path (`_scrape_static`)

Uses `httpx` for fast, concurrent HTTP requests:

1. Resolves page URLs from `plan.pagination_urls` or just the base URL
2. Fetches all pages concurrently (respecting `max_concurrent_requests` semaphore)
3. For each page HTML, calls `_extract_items_with_fallback()` to get items
4. Enriches items with detail pages and API data

### 4.2 JavaScript Path (`_scrape_js`)

Uses Playwright for pages that require browser rendering:

| Pagination | Implementation |
|------------|---------------|
| `ALPHABET_TABS` | `click_all_tabs()` — clicks each A-Z tab, captures HTML per tab. Supports compound pagination (tabs + numbered pages within each tab) |
| `INFINITE_SCROLL` | Opens a single page, calls `scroll_to_bottom()` repeatedly until no new content loads |
| `LOAD_MORE_BUTTON` | Opens a single page, clicks the "Load More" button repeatedly until it disappears |
| `NEXT_BUTTON` | Opens a page, extracts items, clicks "Next", repeats |
| Default | Navigates to pre-resolved page URLs one by one |

#### Consent Banner Dismissal

Before interacting with page elements, the scraper dismisses cookie consent overlays:
- **Shadow DOM** consent managers (Usercentrics, etc.) — searches inside `#usercentrics-root` shadow root for accept buttons
- **Regular DOM** consent selectors — tries common CSS patterns like `[class*='cookie'] [class*='accept']`, `#onetrust-accept-btn-handler`, etc.

#### Compound Pagination (Tabs + Pages)

For sites with alphabet tabs that also have numbered pagination within each tab:
1. If `pagination_urls` are pre-computed by the planner, navigates them directly
2. Otherwise, auto-detects inner pagination by searching for numbered links using common CSS patterns (`.pagination a`, `.pager a`, etc.)
3. Extrapolates missing page URLs by detecting the varying query parameter pattern (e.g., `?page=1` → `?page=2` → ... → `?page=N`)

### 4.3 Scrapy Path (`_scrape_scrapy`)

Runs the Scrapy `PlanSpider` in a **subprocess** to avoid Twisted/asyncio reactor conflicts:

1. Serializes the `ScrapingPlan` to a temp JSON file
2. Passes Scrapy settings through environment variables
3. Launches `python -m app.scrapy_runner.run` as a subprocess
4. The `PlanSpider` uses the plan's CSS selectors, handles pagination, and follows detail links
5. Items are collected via `ItemCollectorPipeline` and written to a temp JSON file
6. The parent process reads the output and converts it to `PageData`

Scrapy is only used for compatible pagination strategies: `none`, `next_button`, `page_numbers`. JS-heavy strategies (scroll, tabs, load-more) always use Playwright.

### 4.4 API Path (`_scrape_api`)

For sites with a discovered JSON API endpoint:

1. Sends GET requests to `plan.api_endpoint` with `plan.api_params`
2. Paginates by incrementing a `page` parameter
3. Searches response JSON for list data under common wrapper keys (`data`, `items`, `results`, etc.)
4. Converts JSON items to string-value dicts for downstream parsing

### 4.5 Item Extraction (`_extract_items`)

The core CSS extraction logic:

```python
for container in soup.select(item_container_selector):
    record = {}
    for field, selector in field_selectors.items():
        # Skip empty selectors defensively
        if not selector or not selector.strip():
            record[field] = None
            continue
        el = container.select_one(selector)
        if el:
            attr = field_attributes.get(field)
            record[field] = el.get(attr) if attr else el.get_text(strip=True)
    # Also extract detail_link and _detail_api_id if configured
    items.append(record)
```

Key defensive measures:
- Empty/whitespace-only CSS selectors are skipped (prevents `ValueError`)
- Invalid CSS selectors are caught and logged (field set to `None`)
- Detail link and API ID selectors have the same defensive checks

### 4.6 Dual Preview (`scrape_preview_dual`)

During the preview stage, the scraper runs **both** extraction methods on the same HTML:

1. Fetches the page once (httpx or Playwright)
2. Runs CSS extraction via `_extract_items()`
3. Runs ScrapeGraphAI extraction via `smart_extract_items()`
4. Returns both results for side-by-side comparison

If CSS extraction finds 0 items with httpx, it retries with Playwright (the page may need JS rendering).

---

## 5. Stage 4: Parsing (ParserAgent)

**File:** `app/agents/parser_agent.py`

The `ParserAgent` normalizes raw scraped data into structured `SellerLead` records.

### Enrichment Merging

Before sending to the LLM, the parser builds enriched items:

1. **Detail page data** — For items with a `detail_link`, the parser extracts fields from the detail page HTML using:
   - ScrapeGraphAI (primary, if enabled)
   - CSS selectors from `detail_page_plan.field_selectors` (fallback)
   - Simplified HTML text (last resort)

2. **Detail API data** — For items with an `_detail_api_id`, attaches the compact API response JSON (stripped of metadata, truncated long fields)

3. **Sub-page data** — For detail pages with followed sub-links (e.g., "Products" tab), appends the simplified sub-page HTML

### Batch Processing

Items are split into size-aware batches (`_MAX_BATCH_CHARS = 60,000`) to stay within LLM context limits. Each batch is sent to GPT with the `parser` system prompt, which instructs the LLM to:

- Map messy field names to the canonical `SellerLead` schema
- Split combined fields (e.g., "Berlin, Germany" → city + country)
- Clean up whitespace, HTML entities, encoding artifacts
- Merge listing + detail page data
- Return a JSON array of `records`

If a batch fails (e.g., content filter), it's recursively split in half and retried.

---

## 6. Stage 5: Output (OutputAgent)

**File:** `app/agents/output_agent.py`

### LLM Quality Pass

If there are 3+ records, the output agent sends them through a final GPT quality pass:

- Deduplication (e.g., same company listed twice with slightly different names)
- Consistency fixes (e.g., normalize phone formats, fix email casing)
- Enrichment of incomplete records

Records are chunked into 60k-char batches for the LLM call. If a chunk fails, the original records are kept.

### File Output

1. **JSON** — Pretty-printed array of `SellerLead` dicts, excluding `None` fields
2. **CSV** — Flattened table with social media as separate columns (`social_facebook`, etc.) and raw_extra fields as `extra_*` columns

Files are written to `output/{job_id}/results.json` and `results.csv`.

---

## 7. Template System

**File:** `app/services/template_loader.py`

Templates describe common **website patterns** using structural hints. They do NOT contain site-specific CSS selectors — the AI planner always runs and generates selectors from the actual target page.

### How Templates Work

1. Templates are loaded from `app/templates/*.json` on first access (cached)
2. When a template is selected, `get_hints_from_template()` returns a `TemplateHints` object
3. The orchestrator passes these hints to `PlannerAgent`, which uses them as structural guidance alongside the actual page HTML
4. The planner always generates CSS selectors by analyzing the real page — hints just tell it what kind of structure to expect

### Template JSON Structure

```json
{
  "id": "dynamic-directory-detail-pages",
  "name": "JS Directory + Detail Pages",
  "description": "JavaScript-rendered directory with alphabet tabs and separate detail pages",
  "platform": "generic",
  "default_prompt": "Extract all company details...",
  "default_fields_wanted": "name, address, phone, email, website, ...",
  "hints": {
    "requires_javascript": true,
    "pagination": "alphabet_tabs",
    "has_detail_pages": true,
    "has_detail_api": false,
    "notes": "Each company links to a separate detail page with full contact info"
  }
}
```

### Included Templates

#### dynamic-directory-detail-pages (JS Directory + Detail Pages)
- **Pattern:** A-Z alphabet tabs, JS-rendered, each item links to a separate detail page
- **Hints:** `requires_javascript=true`, `pagination=alphabet_tabs`, `has_detail_pages=true`
- **Use for:** Sites like Koelnmesse exhibitor directories where each company has its own profile page

#### dynamic-directory-api (JS Directory + API Details)
- **Pattern:** A-Z alphabet tabs, JS-rendered, detail data loaded via hidden XHR/API calls
- **Hints:** `requires_javascript=true`, `pagination=alphabet_tabs`, `has_detail_api=true`
- **Use for:** Sites where clicking an item loads detail data from an API endpoint without navigating to a new page

#### static-listing (Static HTML Listing)
- **Pattern:** Simple server-rendered HTML list, all data on one page
- **Hints:** `requires_javascript=false`, `pagination=none`, no detail pages or API
- **Use for:** WordPress-style single-page exhibitor lists, simple HTML tables

### Creating New Templates

1. Identify the website **pattern** (how is data paginated? JS required? separate detail pages?)
2. Create a JSON file in `app/templates/` with structural `hints`
3. Do NOT include CSS selectors — the planner generates those from the real page HTML

---

## 8. Extraction Methods

### CSS Selectors (Default)

Fast, deterministic extraction using BeautifulSoup `select_one()`:
- Each field has a CSS selector relative to the item container
- Attributes like `href`, `src` can be read instead of text content
- Supports comma-separated selector fallbacks (e.g., `h3.name, .name`)

### ScrapeGraphAI (AI Extraction)

Uses LLM-powered extraction via `SmartScraperGraph`:
- Accepts raw HTML as source (no extra HTTP request)
- Builds a natural language prompt asking for specific fields
- Returns structured JSON data
- 120-second timeout to prevent hanging
- Used as fallback when CSS selectors find fewer than 3 items

### Dual Preview Comparison

During preview, both methods run on the same HTML. The orchestrator uses a separate LLM call to compare results and recommend the better method based on:
- Completeness (non-null fields)
- Accuracy (clean values)
- Coverage (useful information)

---

## 9. Detail Page Enrichment

### Three Approaches

1. **Detail Page HTML** — Follow a link to each item's profile page, extract fields via CSS or AI
2. **Detail API Interception** — Click a JS-only button, capture the XHR response, template the API URL
3. **Sub-Links** — Follow links on detail pages (e.g., "Products", "Contact") for additional data

### API Interception Flow

1. Navigate to listing page with Playwright
2. Find the first item container
3. Find the detail button within it
4. Dismiss consent overlays
5. Clear network captures
6. Click the button (JS `el.click()` for reliability)
7. Wait for network idle + 2s buffer
8. Score all captured JSON responses:
   - **Penalty:** URLs containing noise fragments (analytics, tracking, chat, cookie, etc.)
   - **Bonus:** Response keys matching detail data patterns (name, address, phone, etc.)
   - **Bonus:** URL containing keywords like "seller", "profile", "detail"
9. Select the highest-scoring response
10. LLM derives the URL template (replacing the specific ID with `{id}`) and the CSS selector + regex to extract item IDs from the listing page

### API Detail Fetching

For JS-rendered sites, API calls use **Playwright's browser context** (same session cookies):
1. Navigate to the listing page to establish session
2. Use `fetch()` inside the browser context with `credentials: "include"`
3. This ensures auth tokens and cookies are automatically included

For static sites, `httpx` is used with browser-like headers and a `Referer` header.

---

## 10. Pagination Strategies

| Strategy | Trigger | Implementation |
|----------|---------|---------------|
| `none` | Single-page listings | Fetch one URL |
| `page_numbers` | Numbered page links | Pre-computed URLs (from planner or extrapolated) |
| `next_button` | "Next" button | Click repeatedly until button disappears |
| `infinite_scroll` | Lazy-loaded content | Scroll to bottom repeatedly until height stops changing |
| `load_more_button` | "Load More" button | Click repeatedly until button disappears |
| `alphabet_tabs` | A-Z letter tabs | Click each tab, optionally detect inner pagination |
| `api_endpoint` | Discovered JSON API | Direct HTTP GET with page parameter |

### URL Extrapolation for Page Numbers

When only some page links are visible (e.g., pages 1-10 out of 80):
1. Parse page 1 and page 2 URLs
2. Find the query parameter that changes (e.g., `?offset=0` → `?offset=20`)
3. Calculate the step size
4. Generate all URLs up to the maximum page number

---

## 11. Error Handling & Fallbacks

### Layered Fallback Strategy

```
httpx fetch fails → Playwright fetch
Scrapy fails → httpx fallback
CSS selectors find 0 items → retry with Playwright (if static fetch was used)
CSS selectors find <3 items → ScrapeGraphAI fallback
ScrapeGraphAI returns 0 → CSS results used
ScrapeGraphAI times out (120s) → CSS results used
LLM content filter triggers → retry with aggressive HTML sanitization
LLM parse batch fails → split batch in half and retry recursively
QA pass chunk fails → keep original records
```

### Defensive CSS Selector Handling

- Empty or whitespace-only selectors are skipped (set field to `None`)
- Invalid CSS selectors are caught, logged, and skipped
- Applied consistently across field selectors, detail link selectors, and API ID selectors

### Timeout Protection

- HTTP requests: configurable `request_timeout_s` (default 30s)
- Playwright navigation: 120s hard timeout
- Playwright selector wait: 10-15s timeout
- ScrapeGraphAI extraction: 120s timeout
- Scrapy subprocess: configurable `scrapy_subprocess_timeout_s` (default 600s)
- Script execution: 60s sandbox timeout

### Rate Limiting

- Configurable delay between requests (`request_delay_ms`)
- Concurrent request semaphore (`max_concurrent_requests`)
- Scrapy autothrottle with configurable start/max delay
- Per-domain concurrent request limits
