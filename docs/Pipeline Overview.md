# Web Crawler Pipeline — How It Works

This document explains, step by step, how our automated data collection pipeline works — from the moment someone submits a request to the final deduplicated data landing in our systems.

---

## The Big Picture

Our pipeline collects company and seller information from two types of sources:

- **Trade fairs and exhibitions** (e.g., ISPO, IFA, Pitti Uomo) — where companies exhibit their products at industry events
- **Online marketplaces** (e.g., B&Q, MediaMarkt) — where sellers list products on retail platforms

The goal is to build a comprehensive database of companies, including their names, websites, contact details, locations, and social media profiles. This data feeds into our GTM (Go-To-Market) pipeline for lead generation and outreach.

---

## Step-by-Step Walkthrough

### Step 1: Someone Submits a Scraping Request

Everything starts with a **Google Sheet** called "Target Scraper Request." This is the central intake form where team members submit new data collection requests.

Each request includes:

| Field | What it means |
|-------|---------------|
| **Source Type** | Is this a Trade Fair, Marketplace, or Deep Research request? |
| **Event/Source Name** | The name of the trade fair or marketplace (e.g., "ISPO 2026", "MediaMarkt") |
| **URL** | The link to the exhibitor list or seller directory |
| **Country** | The country of the event or marketplace |
| **Priority** | Normal or Urgent — urgent requests are processed first |
| **Fields Wanted** | What data points are needed (e.g., company name, email, website) |
| **Notes** | Any special instructions or context |
| **Status** | Starts as "New" — this is what triggers the automation |

Anyone on the team can add a row to this sheet. Once the status is set to "New," the system picks it up automatically.

---

### Step 2: The Dispatcher Checks for New Requests

Every day at **6:00 AM**, an automated task (Windows Task Scheduler) runs the **Dispatcher** — the brain of the pipeline. Here is what it does:

1. **Downloads the Google Sheet** to read all current requests
2. **Filters for new requests** — it only looks at rows where the Status is "New"
3. **Sorts by priority** — Urgent requests are handled first
4. **Updates the status** to "In Progress" so the team knows work has started

If there are no new requests, the dispatcher simply logs "No new requests" and stops.

---

### Step 3: The Dispatcher Routes the Request to the Right Scraper

Depending on the **source type**, the dispatcher takes a different path:

#### Path A: Marketplace Scraping

For marketplaces like B&Q or MediaMarkt, the system checks if we already have a **pre-built scraper** (called an "adapter") for that site.

- **If an adapter exists:** The system runs it immediately. These adapters are fast and reliable because they were previously built and tested for that specific marketplace.
- **If no adapter exists:** The system uses an AI agent (Claude) to analyze the marketplace website, automatically build a new scraper, test it, and then run it. This is fully automated — no human intervention needed.

Marketplace scrapers work by visiting seller pages one by one using sequential IDs (e.g., seller #1, seller #2, seller #3...) and extracting structured data from each page.

#### Path B: Trade Fair Scraping

For trade fair exhibitor lists, the system uses an AI agent to:

1. Visit the exhibitor directory page
2. Analyze how the website stores its data (is it in the HTML? Behind an API? Loaded by JavaScript?)
3. Write a custom scraper tailored to that specific website
4. Run the scraper to extract all exhibitors
5. Save the results as a structured file (CSV)

Each trade fair website is different, so the AI adapts its approach every time. It references patterns from previously successful scrapers to work more efficiently.

#### Path C: Deep Research

Some requests are flagged as "Deep Research" — these require manual handling and are skipped by the automation. The dispatcher marks them as "Needs Review" for a team member to handle.

---

### Step 4: The Scraper Collects the Data

Regardless of which path was taken, the scraper collects company information. The data typically includes:

- **Company name**
- **Website URL**
- **Country / City**
- **Email address**
- **Phone number**
- **Social media links** (LinkedIn, Instagram, Facebook, etc.)
- **Booth/stand location** (for trade fairs)
- **Description or product categories**
- **Seller ratings and reviews** (for marketplaces)

The results are saved as a CSV file (a spreadsheet-compatible format) on the local machine.

---

### Step 5: Results Are Uploaded to Google Drive

Once scraping is complete, the system automatically uploads the results to a shared **Google Drive folder** ([link to folder](https://drive.google.com/drive/folders/1Fmgyw28S4Xsu5ZRwJBofGXp3BMy1bt8E)), organized by type:

```
Google Drive (Shared Results Folder)
└── Event Scraped Data/
│   ├── ISPO 2026/
│   ├── IFA Berlin/
│   └── Pitti Uomo/
└── Marketplace Scraped Data/
    ├── B&Q/
    └── MediaMarkt/
```

This makes the data immediately accessible to the rest of the team.

---

### Step 6: The Google Sheet Is Updated

After uploading, the dispatcher updates the original Google Sheet request with:

| Field | What gets updated |
|-------|-------------------|
| **Status** | Changed to "Completed" (or "Failed" if something went wrong) |
| **Companies Found** | The number of companies/sellers extracted |
| **Results Link** | A link to the uploaded file on Google Drive |
| **Completed Date** | The date the scraping finished |
| **Processor Notes** | Details about how the request was processed |

This gives the team full visibility into what was done and where to find the results.

---

### Step 7: Data Is Imported into the Local Database

After scraping, the data is imported into a central **local database**. This database serves as the single source of truth for all company data across all sources.

During import, the system:

- **Cleans the data** — removes empty fields, fixes formatting, standardizes values
- **Deduplicates** — if a company already exists in the database (from a previous scrape or a different source), it merges the records instead of creating duplicates
- **Links companies to their source** — tracks which trade fair or marketplace each company was found at, including details like booth location or seller ratings

This means if a company appears at both ISPO and IFA, we have one company record with two event associations rather than two separate entries.

---

### Step 8: Cross-Check with HubSpot for Deduplication

Once the scraped data file is uploaded to Google Drive, it is automatically **cross-referenced with HubSpot** (our CRM system) for deduplication.

This step ensures that:

- **Companies we already have in HubSpot are not treated as new leads.** If a scraped company already exists as a contact or company in HubSpot, it is flagged as a duplicate and excluded from outreach.
- **Only genuinely new companies move forward** into the lead generation pipeline. This prevents the sales team from reaching out to companies we are already in contact with or have previously engaged.
- **Data quality stays high.** By matching against HubSpot records, we avoid cluttering the CRM with duplicate entries and ensure that any new imports represent truly new opportunities.

This cross-check happens after the Google Drive upload and before the data enters the outreach pipeline, acting as a final quality gate.

---

## Summary: The Full Flow

```
Team member submits request in Google Sheet
                    |
                    v
    Dispatcher runs daily at 6:00 AM
                    |
                    v
    Picks up "New" requests, sorts by priority
                    |
          ┌─────────┴──────────┐
          v                    v
    Marketplace            Trade Fair
    (existing adapter      (AI builds custom
     or AI builds one)      scraper on the fly)
          |                    |
          └─────────┬──────────┘
                    v
        Scraper collects company data
                    |
                    v
        Results saved as CSV file
                    |
                    v
        Uploaded to Google Drive
                    |
                    v
        Google Sheet updated with results
                    |
                    v
        Data imported into local database
        (cleaned + deduplicated)
                    |
                    v
        Cross-checked with HubSpot
        (CRM deduplication)
                    |
                    v
        New leads enter GTM pipeline
```

---

## Why the Pipeline Runs Locally

The pipeline currently runs on Long Le's dedicated workstation rather than on a cloud server. This is a deliberate decision based on the nature of the work the system performs, and it is the most reliable setup for the current stage of the project. Here is why:

**The scraping process requires real-time problem solving.** Every website is structured differently, and many of them actively block automated data collection. The system uses a full web browser behind the scenes to navigate these websites — just like a person would — and relies on AI to interpret page layouts, detect anti-blocking measures, and adapt its approach on the fly. This kind of work is resource-intensive and benefits from the consistent, high-performance environment that a dedicated local machine provides.

**Error handling is more effective in a local environment.** When a scrape encounters an issue — for example, a website changes its layout, a page fails to load, or an anti-bot system blocks access — the system needs to diagnose the problem, adjust its strategy, and retry. These recovery steps often involve launching a browser, taking screenshots for analysis, and running AI-powered diagnostics. Performing all of this locally ensures faster response times and avoids the timeout limitations and resource constraints that cloud-hosted environments typically impose.

**The AI-powered components require significant processing capacity.** The pipeline uses multiple AI models to analyze websites, generate custom scrapers, and clean the collected data. Running these processes locally avoids the latency of sending data back and forth to remote servers and ensures that the system can handle long-running jobs (some scrapes take 30 minutes or more) without interruption.

**Cost efficiency.** Cloud servers that provide equivalent processing power, persistent browser sessions, and the flexibility to run long-duration jobs would incur significant ongoing costs. Running locally eliminates these hosting expenses while delivering the same — or better — performance.

**What this means in practice:** The pipeline runs as a scheduled task on Long's machine. As long as the machine is powered on and connected to the internet, the daily 6:00 AM automation runs without any manual intervention. All results are automatically uploaded to Google Drive and synced to the shared systems, so the rest of the team has full access to the data regardless of where the pipeline physically runs.

This setup will be revisited as the project scales. If the volume of requests grows or if the pipeline needs to run around the clock, migrating to a dedicated cloud server is a straightforward next step that can be planned accordingly.

---

## Key Points

- **Fully automated**: From request to results, the entire process runs without human intervention (except for Deep Research requests).
- **Runs locally**: The pipeline operates on a dedicated local workstation for optimal performance, reliability, and cost efficiency.
- **Runs daily**: The dispatcher checks for new requests every morning at 6:00 AM.
- **Self-adapting**: For new or unknown websites, AI automatically analyzes the site and builds a custom scraper.
- **Deduplicated twice**: Once during database import (across all scraped sources) and once against HubSpot (against existing CRM records).
- **Transparent**: The Google Sheet acts as a live dashboard — anyone can see the status of their request at any time.
- **Organized**: All results are stored in a structured Google Drive folder and a central database, making them easy to find and use.
