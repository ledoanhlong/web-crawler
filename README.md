# Web Crawler AI Agent

A multi-agent web crawler for extracting seller and company data from marketplaces, trade fairs, and directories. Powered by Azure OpenAI (GPT), Playwright, BeautifulSoup, ScrapeGraphAI, Crawl4AI, universal-scraper, and optionally Scrapy.

## Features

- **AI-Powered Planning** — LLM analyses page structure to auto-generate CSS selectors and scraping plans
- **Multi-Method Extraction** — Compares CSS selector extraction, ScrapeGraphAI (AI), Crawl4AI (markdown), and universal-scraper (AI/BS4) side-by-side
- **Crawl4AI Integration** — Local async crawler with clean markdown output for reduced LLM token usage
- **universal-scraper Integration** — AI-powered BeautifulSoup code generation with intelligent caching for ~90% cost savings on similar pages
- **Detail Page Enrichment** — Automatically follows detail links or intercepts XHR/API calls for richer data
- **Multiple Pagination Strategies** — Supports page numbers, next button, infinite scroll, load-more, alphabet tabs, and direct API endpoints
- **Template System** — Pre-configured plans for known sites (Koelnmesse, WordPress exhibitor lists, etc.)
- **Preview & Confirm Workflow** — Scrapes a single item for user validation before running the full crawl
- **Smart Routing** — Intelligent endpoint that selects the best scraping method based on URL and prompt analysis
- **Structured Output** — Normalized `SellerLead` records exported as JSON and CSV
- **LLM Quality Pass** — Final deduplication and consistency check on parsed records
- **Built-in Frontend** — Single-page web UI for submitting jobs, reviewing previews, and downloading results

## Architecture

The system uses a multi-agent pipeline:

```
RouterAgent → PlannerAgent → ScraperAgent → ParserAgent → OutputAgent
```

| Agent | Role |
|-------|------|
| **RouterAgent** | Analyses URL(s) and prompt, selects the best strategy (full pipeline, SmartScraper, ScriptCreator) |
| **PlannerAgent** | Fetches the target page, sends simplified HTML to GPT, produces a `ScrapingPlan` with CSS selectors, pagination strategy, and detail page analysis. Optionally uses Crawl4AI for markdown-based URL discovery |
| **ScraperAgent** | Executes the plan — fetches pages (Crawl4AI, httpx, Playwright, or Scrapy), extracts items with CSS selectors, ScrapeGraphAI, or universal-scraper, follows detail pages, intercepts APIs |
| **ParserAgent** | Normalizes raw scraped data into `SellerLead` records using GPT (field mapping, splitting, cleanup). Can use universal-scraper for detail page extraction |
| **OutputAgent** | Runs a GPT quality pass (dedup, consistency), then writes JSON and CSV output files |

## Prerequisites

- **Python 3.11+**
- **Azure OpenAI** deployment (endpoint, API key, deployment name)
- **Playwright browsers** (installed via `playwright install chromium`)

## Installation

1. **Clone the repository:**
   ```bash
   git clone <repo-url>
   cd web-crawler
   ```

2. **Create a virtual environment:**
   ```bash
   python -m venv .venv
   # Windows
   .venv\Scripts\activate
   # macOS/Linux
   source .venv/bin/activate
   ```

3. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

4. **Install Playwright browsers:**
   ```bash
   playwright install chromium
   ```

5. **Configure environment variables:**

   Create a `.env` file in the project root:
   ```env
   AZURE_OPENAI_ENDPOINT=https://your-resource.openai.azure.com/
   AZURE_OPENAI_API_KEY=your-api-key
   AZURE_OPENAI_API_VERSION=2025-04-01-preview
   AZURE_OPENAI_DEPLOYMENT=gpt-5.2
   ```

   Optional settings (with defaults):
   ```env
   # Crawler
   MAX_CONCURRENT_REQUESTS=5
   REQUEST_DELAY_MS=1000
   REQUEST_TIMEOUT_S=30
   MAX_PAGES_PER_CRAWL=500

   # Feature flags
   USE_SMART_SCRAPER_PRIMARY=true    # Enable ScrapeGraphAI as primary extractor
   USE_SCRAPY=false                  # Use Scrapy instead of httpx for static pages

   # Crawl4AI (local async crawler with markdown output)
   USE_CRAWL4AI=false
   USE_CRAWL4AI_FOR_FETCHING=true
   USE_CRAWL4AI_FOR_EXTRACTION=false
   CRAWL4AI_BROWSER_HEADLESS=true

   # universal-scraper (AI-powered BS4 code generation + caching)
   USE_UNIVERSAL_SCRAPER=false
   USE_UNIVERSAL_SCRAPER_FOR_EXTRACTION=true
   UNIVERSAL_SCRAPER_MODEL=azure/gpt-5.2

   # Reliability controls
   RELIABILITY_AUTO_SWITCH_ENABLED=true
   RELIABILITY_AUTO_SWITCH_MIN_PAGES=3
   RELIABILITY_AUTO_SWITCH_ZERO_STREAK=2
   RELIABILITY_PREVIEW_MARGIN_THRESHOLD=1.5
   RELIABILITY_SELECTOR_MIN_HIT_RATIO=0.2
   RELIABILITY_SELECTOR_SAMPLE_CONTAINERS=10

   # Playwright
   PLAYWRIGHT_HEADLESS=true
   PLAYWRIGHT_WS_ENDPOINT=           # Remote browser URL (e.g. Browserless)

   # Output
   OUTPUT_DIR=./output
   LOG_LEVEL=INFO
   ```

## Running the Application

### Development

```bash
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

### Docker

```bash
docker build -t web-crawler .
docker run -p 8000:8000 --env-file .env web-crawler
```

### Access the UI

Open [http://localhost:8000](http://localhost:8000) in your browser.

## API Reference

### Core Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/v1/smart-crawl` | Intelligent entry point — auto-selects strategy |
| `POST` | `/api/v1/crawl` | Submit a direct crawl job (skips router) |
| `POST` | `/api/v1/crawl/{job_id}/confirm` | Confirm or abort a preview |
| `POST` | `/api/v1/crawl/{job_id}/resume` | Resume a partial crawl |
| `GET`  | `/api/v1/crawl/{job_id}` | Check job status and results |
| `GET`  | `/api/v1/crawl/{job_id}/diagnostics` | Structured reliability diagnostics |
| `GET`  | `/api/v1/crawl/{job_id}/json` | Download JSON results |
| `GET`  | `/api/v1/crawl/{job_id}/csv` | Download CSV results |
| `GET`  | `/api/v1/jobs` | List all jobs |
| `GET`  | `/api/v1/templates` | List available templates |

### Advanced Tool Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/v1/smart-scrape-multi` | Scrape multiple URLs with ScrapeGraphAI |
| `POST` | `/api/v1/generate-script` | Generate a Python scraping script |
| `POST` | `/api/v1/generate-script-multi` | Generate a merged script for multiple URLs |

### Smart Crawl Request

```json
{
  "urls": ["https://example.com/exhibitors"],
  "prompt": "Extract all exhibitor names, emails, and phone numbers",
  "fields_wanted": "name, email, phone, website, address",
  "detail_page_url": "https://example.com/exhibitor/123",
  "max_items": 50,
  "test_single": false,
  "template_id": null
}
```

### Job Lifecycle

```
PENDING → PLANNING → SCRAPING → PREVIEW → (user confirms) → SCRAPING → PARSING → OUTPUT → COMPLETED
```

At the **PREVIEW** stage, the system pauses with a sample record for user validation. The user can:
- **Confirm** to start the full crawl
- **Provide feedback** (e.g. "I also need email and phone") to trigger re-planning
- **Choose extraction method** (CSS selectors, AI extraction, or universal-scraper extraction)
- **Abort** to cancel the job

If detail page enrichment times out, the job can finish as **PARTIAL** and later continue with:

```bash
POST /api/v1/crawl/{job_id}/resume
```

## Templates

Reusable scraping templates describing common **website patterns**. Templates provide structural hints (JS required, pagination type, detail page strategy) to guide the AI planner — they do NOT contain site-specific CSS selectors.

| Template | Pattern | Description |
|----------|---------|-------------|
| `dynamic-directory-detail-pages` | JS Directory + Detail Pages | A-Z alphabet tabs, JS-rendered, each item links to a separate detail page |
| `dynamic-directory-api` | JS Directory + API Details | A-Z alphabet tabs, JS-rendered, detail data loaded via XHR/API calls |
| `static-listing` | Static HTML Listing | Simple server-rendered list, no JavaScript, no detail pages |

To use a template, select it from the dropdown in the UI or include `"template_id": "dynamic-directory-detail-pages"` in your API request.

### Creating New Templates

1. Identify the website **pattern** (pagination type, JS requirements, detail strategy)
2. Create a new JSON file in `app/templates/` with `hints` describing the pattern
3. Do NOT include CSS selectors — the AI planner generates those from the actual page

## Project Structure

```
app/
├── main.py                    # FastAPI entry point
├── config.py                  # Settings (from .env)
├── agents/
│   ├── router_agent.py        # Strategy selection
│   ├── planner_agent.py       # HTML analysis → ScrapingPlan
│   ├── scraper_agent.py       # Plan execution → raw data
│   ├── parser_agent.py        # Normalization → SellerLead records
│   └── output_agent.py        # Quality pass → JSON/CSV files
├── api/
│   └── routes.py              # FastAPI API routes
├── frontend/
│   └── index.html             # Single-page web UI
├── models/
│   └── schemas.py             # Pydantic models (ScrapingPlan, SellerLead, etc.)
├── prompts/                   # LLM prompt templates
├── scrapy_runner/             # Scrapy spider (subprocess-based)
├── services/
│   ├── orchestrator.py        # Pipeline coordinator
│   └── template_loader.py     # Template loading from JSON files
├── templates/                 # Pre-configured scraping plans
└── utils/
    ├── browser.py             # Playwright helpers
    ├── crawl4ai.py              # Crawl4AI async crawler wrapper
    ├── universal_scraper.py     # universal-scraper AI/BS4 wrapper
    ├── html.py                # HTML simplification
    ├── http.py                # httpx client
    ├── llm.py                 # Azure OpenAI wrapper
    ├── logging.py             # Logging configuration
    ├── script_executor.py     # Sandboxed script execution
    └── smart_scraper.py       # ScrapeGraphAI wrappers
```

## Output Format

### SellerLead Schema

Each extracted record is normalized into the `SellerLead` schema:

| Field | Type | Description |
|-------|------|-------------|
| `name` | string | Company/seller name (required) |
| `country` | string | Country |
| `city` | string | City |
| `address` | string | Street address |
| `postal_code` | string | Postal/ZIP code |
| `website` | string | Company website URL |
| `store_url` | string | Marketplace storefront URL |
| `email` | string | Contact email |
| `phone` | string | Phone number |
| `description` | string | Company description |
| `product_categories` | list[string] | Product categories |
| `brands` | list[string] | Brand names |
| `marketplace_name` | string | Source marketplace/fair name |
| `logo_url` | string | Logo image URL |
| `social_media` | dict | Social media links |
| `raw_extra` | dict | Additional fields |
| `source_url` | string | Page the data was scraped from |

## Development

### Running Tests

```bash
pip install pytest pytest-asyncio
pytest test/ -v
```

### Smoke Suite (Unseen Site Regression)

Use smoke profiles to validate robustness on representative websites:

1. Copy and edit `test/fixtures/website_profiles.example.json`.
   For private real-world targets, create `test/fixtures/website_profiles.private.json` (gitignored).
2. Enable target profiles (`"enabled": true`) and adjust expectations.
3. Start the API server.
4. Run smoke suite:

```bash
python scripts/smoke_sites.py \
   --api-base http://127.0.0.1:8000/api/v1 \
   --profiles auto
```

Reports are saved to `output/smoke_reports/`.

Smoke profile options include:

- `expect_min_records` (int)
- `expect_min_quality` (0-1 float)
- `allow_partial` (bool)
- `auto_resume_partial` (bool)

Compare latest trends and fail on regressions:

```bash
python scripts/compare_smoke_reports.py \
   --report-dir output/smoke_reports \
   --max-pass-rate-drop 0.10 \
   --max-empty-pages-increase 2.0 \
   --max-switches-increase 1.0 \
   --max-parser-scalar-drop 0.10 \
   --max-parser-structured-drop 0.10 \
   --allow-new-failures 0
```

Generate a markdown summary for CI/job reporting:

```bash
python scripts/smoke_trend_markdown.py \
   --report-dir output/smoke_reports \
   --out output/smoke_reports/latest-summary.md
```

The markdown summary includes hotspot sections for:

- most common failure reasons
- highest empty-page profiles
- highest method-switch profiles
- lowest parser scalar-completeness profiles

For operational guidance, see `docs/RELIABILITY_RUNBOOK.md`.

### Code Quality

```bash
pip install ruff
ruff check app/
ruff format app/
```

## License

This project is provided as-is for educational and internal use.
