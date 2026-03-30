# CLAUDE.md

## Project Overview

Multi-agent intelligent web crawler for extracting seller, company, and exhibitor data from marketplaces, trade fairs, and directories. Supports two scraping paradigms:

1. **Marketplace scrapers** (Node.js) — Sequential-ID scraping with pluggable adapters (B&Q, MediaMarkt, etc.)
2. **Trade fair scrapers** (Python) — Event/exhibitor directory scraping (ISPO, IFA, IAW, Modefabriek)
3. **Web crawler agent** (Python/FastAPI) — AI-powered general-purpose web scraping pipeline

## Tech Stack

- **Python:** 3.11+, FastAPI + Uvicorn, Playwright, BeautifulSoup4, httpx
- **Node.js:** >= 18, native fetch (no dependencies), sequential-ID engine
- **AI:** Azure OpenAI (GPT-5.2/5.4), Azure Vision (GPT-4o), Azure AI Foundry (Claude Opus 4.6 fallback)
- **Data:** Pydantic models, Pandas for CSV output
- **Linting:** Ruff (line-length=100, target py311)
- **Testing:** pytest + pytest-asyncio

## Commands

```bash
# === Web Crawler Agent (Python) ===
pip install -r requirements.txt
playwright install chromium
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
pytest test/ -v
ruff check app/
ruff format app/

# Smoke tests (requires running server)
python scripts/smoke_sites.py --api-base http://127.0.0.1:8000/api/v1 --profiles auto

# === Marketplace Scrapers (Node.js) ===
cd scrapers/marketplaces
node scrape.mjs bq                                  # B&Q, full range
node scrape.mjs mediamarkt --from 1 --to 500         # MediaMarkt, subset
node scrape.mjs bq --concurrency 10 --delay 300      # Custom settings

# === Trade Fair Scrapers (Python) ===
python scrapers/tradefairs/scrape_ispo.py
python scrapers/tradefairs/scrape_ifa.py

# === Enrichment Scripts (Python) ===
python scrapers/enrichment/ispo_enrich_revenue.py
python scrapers/enrichment/ifa_enrich_fast.py
```

## Architecture

### Pipeline Flow

```
CrawlRequest → RouterAgent → PlannerAgent → PreviewScrape → User Confirms
→ ScraperAgent → ParserAgent → OutputAgent → JSON/CSV
```

### Job States

```
PENDING → PLANNING → PLAN_REVIEW → PREVIEW → SCRAPING → PARSING → OUTPUT → COMPLETED
                                                                              or PARTIAL
                                                                              or FAILED
```

### Key Directories

```
web-crawler/
├── app/                           # Web crawler agent (FastAPI)
│   ├── agents/                    # 5-agent pipeline (router, planner, scraper, parser, output)
│   ├── api/routes.py              # All FastAPI endpoints (30+)
│   ├── models/schemas.py          # Pydantic models
│   ├── services/                  # Orchestrator, job store, plan cache
│   ├── utils/                     # Browser, HTTP, LLM, HTML processing
│   ├── prompts/                   # 9 LLM prompt templates (.txt)
│   ├── templates/                 # Pre-configured scraping plan templates (JSON)
│   ├── frontend/index.html        # Single-page web UI
│   └── config.py                  # Pydantic Settings (100+ options)
├── scrapers/
│   ├── marketplaces/              # Node.js sequential-ID marketplace scrapers
│   │   ├── scrape.mjs             # Entry point: node scrape.mjs <marketplace>
│   │   ├── lib/engine.js          # Shared engine (NEVER MODIFY)
│   │   ├── lib/parse-utils.js     # Shared HTML/text parsing helpers
│   │   ├── marketplaces/          # Per-marketplace adapters
│   │   │   ├── _template.js       # Copy for new marketplaces
│   │   │   ├── bq.js              # B&Q (API-based, Kingfisher)
│   │   │   └── mediamarkt.js      # MediaMarkt (HTML + embedded JSON)
│   │   └── results/<marketplace>/ # Output CSV + progress (gitignored)
│   ├── tradefairs/                # Python trade fair/event scrapers
│   │   ├── scrape_ispo.py         # ISPO 2026 (Algolia + control.buzz API)
│   │   ├── scrape_ifa.py          # IFA Berlin
│   │   ├── scrape_iaw.py          # IAW Messe
│   │   └── scrape_modefabriek.py  # Modefabriek
│   └── enrichment/                # Revenue/data enrichment scripts
│       ├── ispo_enrich_revenue.py # ISPO revenue enrichment (Azure OpenAI)
│       ├── ifa_enrich_fast.py     # IFA enrichment
│       ├── enrich_modefabriek.py  # Modefabriek enrichment
│       └── import_to_db.py        # Import to SQLite database
├── scripts/                       # Smoke tests for web crawler agent
├── test/                          # Test suite
├── docs/                          # Documentation
├── output/                        # Trade fair scraping outputs (CSV/JSON)
├── data/                          # SQLite database (crawler.db)
└── Database/                      # Consolidated scraped data (XLSX/CSV)
```

## Agents

| Agent | File | Responsibility |
|-------|------|----------------|
| **RouterAgent** | `agents/router_agent.py` | Analyzes URLs/prompts, selects strategy (full_pipeline, smart_scraper, smart_scraper_multi, script_creator) |
| **PlannerAgent** | `agents/planner_agent.py` | Fetches target page, generates ScrapingPlan via LLM, detects listing APIs, vision-guided exploration |
| **ScraperAgent** | `agents/scraper_agent.py` | Executes plan with 7 extraction methods, handles pagination/detail pages, auto-switches on failure |
| **ParserAgent** | `agents/parser_agent.py` | Normalizes raw data into SellerLead records, GPT field mapping, deduplication |
| **OutputAgent** | `agents/output_agent.py` | Final quality pass via Claude, deduplication, exports JSON/CSV |

## Marketplace Scraper (Node.js)

Sequential-ID scraping with a shared engine and pluggable adapters. Each adapter exports 5 things:

| Export | Type | Purpose |
|--------|------|---------|
| `config` | Object | Name, ID range, delay, concurrency, CSV columns |
| `sourceUrl()` | Function | Builds seller page URL from ID |
| `fetch()` | Async Fn | Fetches raw data (HTML or JSON) for one seller |
| `parse()` | Function | Extracts structured seller data from raw fetch |
| `isEmpty()` | Function | Returns true if parsed result = "no seller" |

The engine (`lib/engine.js`) handles CLI args, progress/resume, CSV output, retry with backoff, batched concurrency, and graceful shutdown. **Never modify the engine** -- only write adapters.

### Current Adapters

| Adapter | Strategy | Concurrency | Delay | ID Range |
|---------|----------|-------------|-------|----------|
| `bq.js` | Kingfisher API (JSON) | 5 | 500ms | 1-35000 |
| `mediamarkt.js` | HTML + embedded JSON | 1 | 2000ms | 1-15000 |

### Adding a New Marketplace

1. Copy `marketplaces/_template.js` to `marketplaces/<name>.js`
2. Analyze the target site (API? Embedded JSON? HTML dt/dd?)
3. Implement the 5 required exports
4. Test: `node scrape.mjs <name> --from <known_id> --to <known_id>`

## Extraction Methods

Methods are compared side-by-side during preview. Auto-switches after consecutive empty pages:

1. **CSS Selectors** — Fast, for well-structured HTML
2. **ScrapeGraphAI** — LLM-powered (SmartScraperGraph), handles complex layouts
3. **Crawl4AI** — Local async crawler with markdown output (reduces token usage)
4. **universal-scraper** — AI-generated BeautifulSoup code with intelligent caching (~90% cost savings on similar pages)
5. **Listing API** — Intercepts structured XHR/JSON APIs during page load
6. **Claude** — Azure AI Foundry (Opus 4.6) fallback for extremely complex sites
7. **Generated Script** — Claude-generated `extract_data(html_content)` BS4 scripts with structural HTML hashing for cache hits across domains

## API Routes

### Core Crawl
- `POST /api/v1/crawl` — Submit crawl job
- `POST /api/v1/smart-crawl` — Intelligent routing entry point (auto-selects best method)
- `GET /api/v1/crawl/{job_id}` — Check job status
- `POST /api/v1/crawl/{job_id}/confirm` — Confirm preview and start full scrape
- `POST /api/v1/crawl/{job_id}/update-plan` — Edit scraping plan
- `POST /api/v1/crawl/{job_id}/approve-plan` — Approve plan and trigger preview
- `POST /api/v1/crawl/{job_id}/reanalyze` — Re-run planner
- `POST /api/v1/crawl/{job_id}/resume` — Continue partial crawl
- `GET /api/v1/crawl/{job_id}/diagnostics` — Structured reliability diagnostics
- `GET /api/v1/crawl/{job_id}/telemetry` — Provider usage and cost tracking
- `GET /api/v1/crawl/{job_id}/json` — Download JSON results
- `GET /api/v1/crawl/{job_id}/csv` — Download CSV results

### ScrapeGraphAI Tools
- `POST /api/v1/smart-scrape-multi` — Multi-URL extraction
- `POST /api/v1/generate-script` — Script generation (single URL)
- `POST /api/v1/generate-script-multi` — Script generation (multi-URL)

### Templates
- `GET /api/v1/templates` — List templates
- `GET /api/v1/templates/{filename}` — Get template
- `POST /api/v1/templates/from-job/{job_id}` — Save job as template
- `POST /api/v1/templates/{filename}/run` — Run using template
- `DELETE /api/v1/templates/{filename}` — Delete template

### Plan Cache & Jobs
- `GET /api/v1/plan-cache` — List cached plans
- `DELETE /api/v1/plan-cache` — Clear all cached plans
- `DELETE /api/v1/plan-cache/{url}` — Invalidate specific URL
- `GET /api/v1/jobs` — List all jobs
- `GET /health` — Provider health status

## Utilities

| Utility | Purpose |
|---------|---------|
| `browser.py` | Playwright wrapper with stealth modes and API interception |
| `http.py` | httpx client with retry logic and shared connection pooling |
| `llm.py` | Azure OpenAI + Claude clients with circuit breaker |
| `smart_scraper.py` | ScrapeGraphAI wrappers (SmartScraper, ScriptCreator) |
| `script_extraction.py` | Claude-generated BS4 scripts with structural hashing cache |
| `universal_scraper.py` | universal-scraper async wrapper with field extraction |
| `crawl4ai.py` | Crawl4AI integration for markdown-based fetching |
| `structured_source.py` | JSON-LD, microdata, embedded API detection |
| `structured_data.py` | Microdata and Open Graph extraction |
| `script_executor.py` | Sandboxed script execution with safety checks |
| `quality.py` | Quality scoring and validation |
| `html.py` | HTML simplification, boilerplate removal |
| `fingerprint.py` | Page fingerprinting for change detection |
| `rate_limiter.py` | Configurable rate limiting |
| `sitemap.py` | Sitemap discovery and parsing |
| `crawl_cache.py` | Page caching for incremental crawls |
| `logging.py` | Structured logging utilities |

## Prompt Templates

| Prompt | Agent | Purpose |
|--------|-------|---------|
| `planner_listing.txt` | PlannerAgent | Analyze listing pages |
| `planner_detail.txt` | PlannerAgent | Analyze detail pages |
| `planner_detail_api.txt` | PlannerAgent | Analyze detail APIs |
| `planner_vision.txt` | PlannerAgent | Vision-based (GPT-4o) analysis |
| `planner_exploration.txt` | PlannerAgent | Interactive exploration guidance |
| `parser.txt` | ParserAgent | Parse raw data into SellerLead |
| `output_qa.txt` | OutputAgent | Quality check & dedup |
| `router.txt` | RouterAgent | Strategy selection |
| `claude_extraction.txt` | ScraperAgent | Claude-based full-page extraction |

## Key Configuration (Feature Flags)

Settings are managed via `app/config.py` (Pydantic Settings), loaded from `.env`:

**AI Providers:**
- `azure_openai_endpoint/api_key/deployment` — Primary LLM (GPT-5.2/5.4)
- `azure_vision_endpoint/api_key/deployment` — Vision analysis (GPT-4o)
- `azure_claude_endpoint/api_key/deployment` — Fallback extraction (Claude Opus 4.6)

**Feature Toggles:**
- `use_smart_scraper_primary` — ScrapeGraphAI as primary method (default: true)
- `use_vision_planning` — Enable screenshot-based page analysis
- `use_exploration_browsing` — Vision-guided interactive navigation (default: false)
- `exploration_auto_for_js` — Auto-enable exploration for JS-heavy sites (default: true)
- `use_crawl4ai` — Enable Crawl4AI method (default: false)
- `use_universal_scraper` — Enable universal-scraper method (default: false)
- `use_claude_extraction` — Enable Claude fallback (default: false)
- `claude_fallback_only` — Only use Claude for complex sites
- `use_scrapy` — Enable Scrapy for static sites (default: false)

**Reliability Controls:**
- `reliability_auto_switch_enabled` — Auto-switch methods on failure
- `reliability_auto_switch_zero_streak` — Switch after N consecutive empty pages
- `reliability_quality_min_score` — Quality threshold enforcement
- `claude_circuit_breaker_enabled` — Auto-disable Claude on repeated failures

**Crawler Settings:**
- `max_concurrent_requests`, `request_delay_ms`, `request_timeout_s`
- `max_pages_per_crawl`, `max_sub_links_per_detail`
- `stealth_enabled`, `stealth_randomize_viewport`, `stealth_randomize_user_agent`

## Failure Categories

- `NETWORK_TRANSIENT` — Temporary network issues
- `ANTI_BOT` — Bot detection/blocking
- `RENDERING` — JS rendering failures
- `SELECTOR_MISMATCH` — CSS selector mismatches
- `PAGINATION_MISMATCH` — Pagination detection failures
- `DETAIL_ENRICHMENT` — Detail page extraction failures
- `PARSER_SCHEMA_MISMATCH` — Field mapping failures
- `QUALITY_THRESHOLD` — Data quality issues
- `UNKNOWN` — Unclassified failures

## Code Conventions

- **Async-first:** All agents and HTTP calls use `async/await`
- **Logging:** Use `get_logger(__name__)` from `app.utils.logging`
- **Config:** Pydantic Settings via `app.config.Settings`, loaded from `.env`
- **Naming:** PascalCase for classes/models, snake_case for functions, SCREAMING_SNAKE for constants
- **Agents:** Single-responsibility — each agent handles one pipeline stage
- **Error handling:** Custom exceptions for control flow (e.g., `_SwitchMethodRequested`)
- **Prompts:** Stored as `.txt` files in `app/prompts/`, loaded dynamically

## Important Notes

- `.env` contains Azure credentials — never commit secrets
- `OUTPUT_DIR=./output` is where crawl results are saved
- Templates describe website *patterns*, not specific sites
- Provider health monitoring tracks API availability with circuit breakers
- Auto-switch between extraction methods after consecutive empty pages
- Script cache stored in `temp/cache/` with structural HTML hashing
- Frontend served at `/` from `app/frontend/index.html`
- Marketplace scraper results stored in `scrapers/marketplaces/results/<marketplace>/` (gitignored)
- Trade fair output stored in `output/` (gitignored)
- Consolidated data in `Database/` for final deliverables
