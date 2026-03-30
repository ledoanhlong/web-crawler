# Automation Pipeline

End-to-end documentation of the automated scraping pipeline — from request submission to data delivery.

---

## Table of Contents

1. [Overview](#overview)
2. [Architecture Diagram](#architecture-diagram)
3. [Request Flow](#request-flow)
4. [Google Sheet: Request Format](#google-sheet-request-format)
5. [Dispatcher Agent](#dispatcher-agent)
6. [Scraping Paradigms](#scraping-paradigms)
   - [Marketplace Scrapers (Node.js)](#marketplace-scrapers-nodejs)
   - [Trade Fair Scrapers (Python)](#trade-fair-scrapers-python)
   - [Deep Research](#deep-research)
7. [Results Delivery](#results-delivery)
8. [Scheduling & Triggers](#scheduling--triggers)
9. [Cost & Budget](#cost--budget)
10. [Adding a New Marketplace](#adding-a-new-marketplace)
11. [Adding a New Trade Fair](#adding-a-new-trade-fair)
12. [Troubleshooting](#troubleshooting)
13. [Security & Credentials](#security--credentials)

---

## Overview

The automation pipeline allows team members to submit scraping requests via a Google Sheet. A Claude AI agent checks the sheet daily at **6:00 AM (Europe/Amsterdam)**, processes all new requests autonomously, uploads results to Google Drive, and updates the sheet status — all without human intervention.

**Supported request types:**

| Type | Method | Automation Level |
|------|--------|-----------------|
| **Marketplace** | Node.js sequential-ID adapters | Fully automated (existing adapters) or AI-assisted (new marketplaces) |
| **Trade Fair** | Python scrapers (httpx/BeautifulSoup) | Fully automated (existing scrapers) or AI-assisted (new events) |
| **Directory** | Same as Trade Fair | AI-assisted |
| **Deep Research** | Manual | Flagged for human review |

---

## Architecture Diagram

```
                    ┌─────────────────────────────┐
                    │   Google Sheet               │
                    │   "Target Scraper Request"   │
                    │                              │
                    │   Team members add rows      │
                    │   with Status = "New"        │
                    └──────────────┬───────────────┘
                                   │
                    ┌──────────────▼───────────────┐
                    │   Anthropic Cloud             │
                    │   Remote Trigger (daily 6AM)  │
                    │                               │
                    │   Claude Sonnet 4.6 agent     │
                    │   reads sheet via Google API   │
                    └──────────────┬───────────────┘
                                   │
                     ┌─────────────┼─────────────┐
                     │             │             │
              ┌──────▼──────┐ ┌───▼────┐ ┌──────▼──────┐
              │ Marketplace │ │ Trade  │ │    Deep     │
              │  Scraper    │ │ Fair   │ │  Research   │
              │  (Node.js)  │ │(Python)│ │  (Manual)   │
              └──────┬──────┘ └───┬────┘ └──────┬──────┘
                     │            │              │
                     │            │              │ Status = "Needs Review"
                     │            │              │
              ┌──────▼────────────▼──────┐      │
              │   Results (CSV/JSON)      │      │
              └──────────────┬───────────┘      │
                             │                   │
              ┌──────────────▼───────────┐      │
              │   Google Drive Upload     │      │
              │   folder: Phase 1         │      │
              │   (organized by type)     │      │
              └──────────────┬───────────┘      │
                             │                   │
              ┌──────────────▼───────────────────▼──┐
              │   Google Sheet Updated                │
              │   Status → Completed / Failed /       │
              │            Needs Review               │
              │   Companies Found, Date, Notes        │
              └───────────────────────────────────────┘
```

---

## Request Flow

### Step-by-step process:

1. **Submit** — Team member adds a new row to the Google Sheet with a URL, source type, and priority.
2. **Poll** — At 6:00 AM daily, the dispatcher agent reads the sheet and finds rows with `Status = "New"`.
3. **Prioritize** — Urgent requests are processed first.
4. **Update status** — The agent immediately sets the row to `"In Progress"`.
5. **Classify** — Based on `Source Type`, the agent routes to the correct scraper:
   - **Marketplace** → Node.js adapter (existing or newly created)
   - **Trade Fair / Directory** → Python scraper (existing or newly created)
   - **Deep Research** → Marked as "Needs Review"
6. **Scrape** — The appropriate scraper runs and extracts data.
7. **Upload** — Results (CSV/JSON) are uploaded to the shared Google Drive folder.
8. **Complete** — The sheet is updated with status, company count, completion date, and notes.

### Status lifecycle:

```
New → In Progress → Completed
                  → Failed (with error in Processor Notes)
                  → Needs Review (Deep Research or budget limit)
```

---

## Google Sheet: Request Format

**Sheet name:** `Target Scraper Request`
**Sheet ID:** `1-fjYFJdx7zVJY6d8WXQpHtGFLJa9go9QmL-iYoy463s`
**Location:** `GTM Pipeline / Phase 0: Web Crawler Agent /`

### Columns (Requests tab)

| Column | Name | Who fills it | Description |
|--------|------|-------------|-------------|
| A | Request # | Submitter | Sequential number (1, 2, 3...) |
| B | Date Submitted | Submitter | Date of submission |
| C | Submitted By | Submitter | Your name |
| D | Source Type | Submitter | `Trade Fair`, `Marketplace`, `Directory`, or `Deep Research` |
| E | Event / Site Name | Submitter | Name of the event or marketplace |
| F | URL | Submitter | The exhibitor list page or example seller page URL |
| G | Country / Region | Submitter | Target country |
| H | Priority | Submitter | `Urgent` or `Normal` |
| I | Fields Wanted | Submitter | Specific fields to extract (optional) |
| J | Notes | Submitter | Additional context about the site |
| K | Status | Dispatcher | `New` → `In Progress` → `Completed` / `Failed` / `Needs Review` |
| L | Results Link | Dispatcher | Google Drive link to the results |
| M | Companies Found | Dispatcher | Number of companies/sellers extracted |
| N | Completed Date | Dispatcher | Date the scrape finished |
| O | Processor Notes | Dispatcher | Summary of what was done or error details |

### How to submit a request

1. Go to the **Requests** tab
2. Add a new row at the bottom
3. Fill in columns A through J
4. Set Status (column K) to **`New`**
5. The dispatcher will pick it up at the next 6 AM run

### Tips for good requests

- **Marketplace:** Provide an example seller page URL (e.g., `https://www.mediamarkt.nl/nl/marketplace/seller/2001`). The dispatcher needs to see the URL pattern with a seller ID.
- **Trade Fair:** Provide the exhibitor list/search page URL, not the homepage.
- **Notes:** Mention if the site uses JavaScript rendering, has an API, or requires special handling.

---

## Dispatcher Agent

The dispatcher is a Claude AI agent that runs in Anthropic's cloud infrastructure. It has no persistent state — each run is a fresh session that:

1. Writes the Google service account credentials to a temp file
2. Installs required Python/Node.js packages
3. Reads the Google Sheet
4. Processes each "New" request
5. Cleans up credentials

### Files

| File | Purpose |
|------|---------|
| `scrapers/dispatcher.py` | Local fallback dispatcher (uses rclone, runs via Claude Code SDK) |
| `scrapers/run_dispatcher.bat` | Windows Task Scheduler wrapper for local dispatcher |

### Remote Trigger

| Property | Value |
|----------|-------|
| Trigger ID | `trig_01Tp5psGks3L5i6DZjVFhHY4` |
| Schedule | Daily at 4:03 UTC (6:03 AM Europe/Amsterdam) |
| Model | Claude Sonnet 4.6 |
| Budget | ~$5 per run (soft cap via prompt instruction) |
| Repo | `https://github.com/ledoanhlong/web-crawler` |
| Manage | [claude.ai/code/scheduled](https://claude.ai/code/scheduled) |

### Local fallback

If the remote trigger fails or you need to run manually:

```bash
# Requires: rclone configured with 'gdrive:', Claude Code SDK installed
cd web-crawler

# Dry run (show what would be processed)
python scrapers/dispatcher.py --dry-run

# Process all "New" requests
python scrapers/dispatcher.py

# Process a specific request
python scrapers/dispatcher.py --request 7
```

A Windows Task Scheduler task (`ScraperDispatcher`) is also configured to run `run_dispatcher.bat` daily at 6:00 AM as a backup (requires the PC to be on).

---

## Scraping Paradigms

### Marketplace Scrapers (Node.js)

For online marketplaces where seller pages use sequential numeric IDs (e.g., `/seller/1`, `/seller/2`, ...).

**Architecture:**

```
scrapers/marketplaces/
├── scrape.mjs                  ← Entry point: node scrape.mjs <marketplace>
├── package.json                ← type: "module", no dependencies needed
├── lib/
│   ├── engine.js               ← Shared engine (NEVER MODIFY)
│   └── parse-utils.js          ← Shared HTML/text parsing helpers
├── marketplaces/
│   ├── _template.js            ← Copy this for new marketplaces
│   ├── bq.js                   ← B&Q adapter (API-based)
│   └── mediamarkt.js           ← MediaMarkt adapter (HTML + embedded JSON)
└── results/
    └── <marketplace>/
        ├── sellers.csv         ← Output data
        └── progress.json       ← Resume state (tracks processed IDs)
```

**Adapter contract:** Each marketplace adapter exports 5 things:

| Export | Type | Purpose |
|--------|------|---------|
| `config` | Object | Name, ID range, delay, concurrency, CSV columns |
| `sourceUrl(sellerId)` | Function | Builds the public seller page URL from an ID |
| `fetch(sellerId)` | Async Function | Fetches raw data (HTML or JSON) for one seller |
| `parse(raw, sellerId, url)` | Function | Extracts structured data from the raw fetch |
| `isEmpty(parsed)` | Function | Returns true if "no seller found" |

**Current adapters:**

| Adapter | Site | Strategy | Concurrency | Delay | ID Range |
|---------|------|----------|-------------|-------|----------|
| `bq.js` | B&Q (diy.com) | Kingfisher API (JSON) | 5 | 500ms | 1-35,000 |
| `mediamarkt.js` | MediaMarkt (NL) | HTML + embedded JSON | 1 | 2,000ms | 1-15,000 |

**Engine features:**
- CLI arguments: `--from`, `--to`, `--delay`, `--concurrency`
- Automatic resume via `progress.json` (safe to Ctrl+C and restart)
- Retry with exponential backoff (3 attempts)
- Batched concurrency
- Graceful shutdown on SIGINT/SIGTERM
- CSV output with proper escaping

**Usage:**

```bash
cd scrapers/marketplaces

# Full B&Q scrape
node scrape.mjs bq

# MediaMarkt, IDs 1-500 only
node scrape.mjs mediamarkt --from 1 --to 500

# B&Q with custom settings
node scrape.mjs bq --concurrency 10 --delay 300
```

### Trade Fair Scrapers (Python)

For trade fair exhibitor directories and event pages. Each scraper is a standalone Python script.

```
scrapers/tradefairs/
├── scrape_ispo.py              ← ISPO 2026 (Algolia + control.buzz API)
├── scrape_ifa.py               ← IFA Berlin
├── scrape_iaw.py               ← IAW Messe
└── scrape_modefabriek.py       ← Modefabriek
```

**Common patterns:**
- Use `httpx` for HTTP requests (async or sync)
- Discover underlying APIs (Algolia, REST endpoints, embedded JSON)
- Extract exhibitor lists from API, then fetch detail pages for contacts/socials
- Save results as CSV with `pandas` or manual CSV writing
- Resume support via progress files where applicable

**Usage:**

```bash
python scrapers/tradefairs/scrape_ispo.py
python scrapers/tradefairs/scrape_ifa.py
python scrapers/tradefairs/scrape_iaw.py
python scrapers/tradefairs/scrape_modefabriek.py
```

**Output:** Results are saved to `output/<event_name>/` (e.g., `output/ispo/ispo_exhibitors_2026.csv`).

### Deep Research

Sites that are too complex for automated scraping (heavy anti-bot, requires login, etc.). These are marked as `"Needs Review"` in the sheet and require a human to start a manual Claude Code session.

---

## Results Delivery

### Google Drive

Results are uploaded to a shared Drive folder (ID: `1Fmgyw28S4Xsu5ZRwJBofGXp3BMy1bt8E`), organized as:

```
Phase 1: Raw Data Scrapped/
├── B&Q Scraped Data/
│   └── sellers.csv
├── MediaMarkt Scraped Data/
│   └── sellers.csv
├── Event Scraped Data/
│   ├── ISPO/
│   │   ├── ispo_exhibitors_2026.csv
│   │   └── ispo_exhibitors_2026_enriched.csv
│   ├── IFA Berlin/
│   │   └── ifa_exhibitors.csv
│   ├── IAW Messe/
│   │   └── iaw_exhibitors.csv
│   ├── Modefabriek/
│   │   └── modefabriek_enriched.csv
│   └── <NewEvent>/
│       └── exhibitors.csv
└── WayFair Scraped Data/
    └── (CSV/Excel/JSON by letter)
```

### Enrichment (optional post-processing)

After scraping, enrichment scripts can add revenue estimates and additional company data:

```bash
python scrapers/enrichment/ispo_enrich_revenue.py     # Add revenue to ISPO data
python scrapers/enrichment/ifa_enrich_fast.py          # Add revenue to IFA data
python scrapers/enrichment/enrich_modefabriek.py       # Add revenue to Modefabriek data
python scrapers/enrichment/import_to_db.py             # Import all data to SQLite
```

Enrichment uses Azure OpenAI (GPT-5.4) to identify companies and estimate revenue from its training knowledge.

---

## Scheduling & Triggers

### Remote Trigger (primary — runs in Anthropic cloud)

- **Schedule:** Daily at 6:03 AM Europe/Amsterdam (4:03 UTC)
- **No local machine required** — runs entirely in the cloud
- **Budget:** ~$5 per run (prompt-instructed soft cap)
- **Manage:** [claude.ai/code/scheduled](https://claude.ai/code/scheduled)
- **Trigger ID:** `trig_01Tp5psGks3L5i6DZjVFhHY4`

### Windows Task Scheduler (backup — requires PC on)

- **Schedule:** Daily at 6:00 AM
- **Task name:** `ScraperDispatcher`
- **Script:** `scrapers/run_dispatcher.bat`
- **Logs:** `output/logs/dispatcher_YYYYMMDD.log` and `output/logs/scheduler.log`

### Manual run

```bash
# Via local dispatcher
python scrapers/dispatcher.py --dry-run    # Preview what would run
python scrapers/dispatcher.py              # Process all "New" requests
python scrapers/dispatcher.py --request 7  # Process specific request

# Via individual scrapers
cd scrapers/marketplaces && node scrape.mjs bq --from 1 --to 100
python scrapers/tradefairs/scrape_ispo.py
```

---

## Cost & Budget

All costs are **Claude API token usage** billed to the Anthropic Team account. Google APIs are free.

### Cost per scenario

| Scenario | Estimated Cost |
|----------|---------------|
| Daily check, no new requests | $0.01 - $0.03 |
| Run existing marketplace adapter | $0.10 - $0.50 |
| Build new marketplace adapter + scrape | $2 - $5 |
| Build new trade fair scraper + scrape | $3 - $5 |
| Mark Deep Research as "Needs Review" | ~$0.02 |

### Monthly estimate

Assuming 2-3 new requests per week:
- 30 daily checks with no work: ~$0.50
- 4 runs with existing adapters: ~$1-2
- 4 complex runs (new scrapers): ~$15-20
- **Total: ~$15-25/month**

### Budget controls

- The agent is instructed to stay within **$5 per run**
- Urgent requests are processed first
- If budget is running low, remaining requests are marked "Needs Review"
- The model used is **Claude Sonnet 4.6** (good balance of cost and capability)

---

## Adding a New Marketplace

When the dispatcher encounters an unknown marketplace, it will attempt to create a new adapter automatically. To do it manually:

### 1. Copy the template

```bash
cp scrapers/marketplaces/marketplaces/_template.js scrapers/marketplaces/marketplaces/<name>.js
```

### 2. Analyze the target site

Visit a seller page and check (in priority order):
1. **Public API?** — Look for `/api/`, `/v1/`, `__CONFIG__` in page source
2. **Embedded JSON?** — Look for `__PRELOADED_STATE__`, `__NEXT_DATA__`
3. **Structured HTML?** — Look for `data-test-id`, `dt/dd` pairs, tables
4. **Not-found behavior?** — Visit a bad ID (999999) — does it return 404 or a 200 with an error message?

### 3. Implement the 5 exports

- `config` — ID range, delay, concurrency, CSV columns
- `sourceUrl(sellerId)` — URL pattern
- `fetch(sellerId)` — Returns `{ raw }`, `{ notFound }`, `{ rateLimited }`, or `{ error }`
- `parse(raw, sellerId, url)` — Extracts structured data
- `isEmpty(parsed)` — Returns true if no seller

### 4. Test

```bash
cd scrapers/marketplaces

# Test with one known seller ID
node scrape.mjs <name> --from <known_id> --to <known_id>

# Test with a small range
node scrape.mjs <name> --from 1 --to 50
```

### 5. Update the dispatcher

Add the URL patterns to `KNOWN_ADAPTERS` in `scrapers/dispatcher.py`:

```python
KNOWN_ADAPTERS = {
    "bq": ["diy.com", "b&q"],
    "mediamarkt": ["mediamarkt"],
    "newsite": ["newsite.com"],  # Add new entry
}
```

---

## Adding a New Trade Fair

### 1. Discover the data source

Visit the exhibitor list page and identify:
- **Algolia search API** — common for large trade fairs (look for `algolia` in network requests)
- **REST API** — custom endpoints returning JSON
- **HTML scraping** — static HTML with exhibitor cards

### 2. Write the scraper

Create `scrapers/tradefairs/scrape_<event>.py`. Use existing scrapers as reference:
- `scrape_ispo.py` — Algolia + REST API pattern
- `scrape_ifa.py` — HTML scraping pattern
- `scrape_iaw.py` — Simple listing pattern

### 3. Common template

```python
import httpx
import csv
from pathlib import Path

OUTPUT_DIR = Path("output/<event_name>")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

def fetch_exhibitors():
    """Fetch all exhibitors from the API/HTML."""
    # ... discovery + extraction logic
    pass

def fetch_detail(exhibitor_id):
    """Fetch detail page for one exhibitor."""
    # ... detail extraction logic
    pass

def main():
    exhibitors = fetch_exhibitors()
    print(f"Found {len(exhibitors)} exhibitors")

    # Enrich with detail pages
    for ex in exhibitors:
        detail = fetch_detail(ex["id"])
        ex.update(detail)

    # Save CSV
    with open(OUTPUT_DIR / "exhibitors.csv", "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=exhibitors[0].keys())
        writer.writeheader()
        writer.writerows(exhibitors)

    print(f"Saved to {OUTPUT_DIR / 'exhibitors.csv'}")

if __name__ == "__main__":
    main()
```

---

## Troubleshooting

### Dispatcher didn't run

1. Check the trigger is enabled: [claude.ai/code/scheduled](https://claude.ai/code/scheduled)
2. Check the trigger's last run status and logs
3. Verify the Google Sheet has rows with `Status = "New"`

### Scraper returned 0 results

- The website may have changed its structure
- Anti-bot protection may be blocking requests
- The API key or endpoint may have changed
- Check `Processor Notes` in the sheet for error details

### Sheet not updating

- Verify the service account has **Editor** access to the sheet
- Check that `scraper-dispatcher@channelengine-ai.iam.gserviceaccount.com` is shared on both the sheet and Drive folder

### Results not appearing in Drive

- Verify the service account has **Editor** access to Drive folder `1Fmgyw28S4Xsu5ZRwJBofGXp3BMy1bt8E`
- Check `Processor Notes` for upload errors

### Budget exceeded

- The agent stops processing and marks remaining requests as "Needs Review"
- Increase the budget instruction in the trigger prompt, or process remaining requests manually

### Running locally as fallback

```bash
cd web-crawler
python scrapers/dispatcher.py --dry-run    # Check what would run
python scrapers/dispatcher.py              # Run all "New" requests
```

---

## Security & Credentials

### Google Service Account

- **Email:** `scraper-dispatcher@channelengine-ai.iam.gserviceaccount.com`
- **Project:** `channelengine-ai`
- **Scopes:** Google Sheets API (read/write), Google Drive API (read/write)
- **Key location:** Embedded in the Remote Trigger prompt (stays within Anthropic infrastructure, never committed to git)
- **Local key file:** `channelengine-ai-e7e32c629d8d.json` (keep outside the repo, already gitignored)

### To rotate the service account key

1. Go to [Google Cloud Console > IAM > Service Accounts](https://console.cloud.google.com/iam-admin/serviceaccounts)
2. Click `scraper-dispatcher` > Keys > Add Key > JSON
3. Update the Remote Trigger prompt with the new key JSON
4. Delete the old key from Google Cloud Console

### What's NOT committed to git

- `.env` — Azure OpenAI credentials
- Service account JSON key files
- `scrapers/marketplaces/results/` — scraping output data
- `output/` — trade fair scraping output
- `data/` — SQLite database

### Anthropic account

- **Type:** Team account (work)
- **Billed to:** ChannelEngine Team subscription
- **Trigger runs:** Billed as Claude Sonnet 4.6 API usage
