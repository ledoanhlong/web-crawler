# CLAUDE.md

## Project Overview

Multi-agent intelligent web crawler (web-crawler-agent v0.1.0) for extracting seller, company, and exhibitor data from marketplaces, trade fairs, and directories. Uses AI-powered planning with Azure OpenAI and multiple extraction methods with intelligent fallbacks.

## Tech Stack

- **Language:** Python 3.11+
- **Framework:** FastAPI + Uvicorn
- **Scraping:** Playwright, BeautifulSoup4, httpx, Scrapy, Crawl4AI, ScrapeGraphAI, universal-scraper
- **AI:** Azure OpenAI (GPT-5.2/5.4), Azure Vision (GPT-4o), Azure AI Foundry (Claude Opus 4.6 fallback)
- **Data:** Pydantic models, Pandas for CSV output
- **Linting:** Ruff (line-length=100, target py311)
- **Testing:** pytest + pytest-asyncio

## Commands

```bash
# Install
pip install -r requirements.txt
playwright install chromium

# Run dev server
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

# Tests
pytest test/ -v

# Lint & format
ruff check app/
ruff format app/

# Smoke tests (requires running server)
python scripts/smoke_sites.py --api-base http://127.0.0.1:8000/api/v1 --profiles auto
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
```

### Key Directories

- `app/agents/` — 5-agent pipeline (router, planner, scraper, parser, output)
- `app/api/routes.py` — All FastAPI endpoints
- `app/models/schemas.py` — Pydantic models (ScrapingPlan, SellerLead, CrawlJob, etc.)
- `app/services/` — Orchestrator, job store, plan cache, template store, provider health
- `app/utils/` — Browser, HTTP, LLM, HTML processing, quality scoring, rate limiting
- `app/prompts/` — LLM prompt templates (.txt files)
- `app/templates/` — Pre-configured scraping plan templates (JSON)
- `app/frontend/index.html` — Single-page web UI
- `test/` — Test suite with fixtures in `test/fixtures/` and `conftest.py`
- `scripts/` — Smoke test runner and report tools
- `docs/` — SCRAPING_LOGIC.md, RELIABILITY_RUNBOOK.md, method comparisons

## Code Conventions

- **Async-first:** All agents and HTTP calls use `async/await`
- **Logging:** Use `get_logger(__name__)` from `app.utils.logging`
- **Config:** Pydantic Settings via `app.config.Settings`, loaded from `.env`
- **Naming:** PascalCase for classes/models, snake_case for functions, SCREAMING_SNAKE for constants
- **Agents:** Single-responsibility — each agent handles one pipeline stage
- **Error handling:** Custom exceptions for control flow (e.g., `_SwitchMethodRequested`)
- **Prompts:** Stored as `.txt` files in `app/prompts/`, loaded dynamically

## Extraction Methods

Methods are compared side-by-side during preview:
1. **CSS Selectors** — Fast, for well-structured HTML
2. **ScrapeGraphAI** — LLM-powered, handles complex layouts
3. **Crawl4AI** — Local async crawler with markdown output
4. **universal-scraper** — AI-generated BeautifulSoup code
5. **Listing API** — Intercepts structured XHR/JSON
6. **Claude** — Fallback for extremely complex sites
7. **Generated Script** — Python script generation

## Important Notes

- `.env` contains Azure credentials — never commit secrets
- `OUTPUT_DIR=./output` is where crawl results are saved
- Templates describe website *patterns*, not specific sites
- Provider health monitoring tracks API availability with circuit breakers
- Auto-switch between extraction methods after consecutive empty pages
