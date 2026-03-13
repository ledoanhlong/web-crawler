# Scraping Methods A/B/C/D/E/F/G

This document explains the extraction methods used in this project and how they differ in preview and full-crawl execution.

In code, these methods are represented by `ExtractionMethod` in `app/models/schemas.py`.

## 1. Quick Mapping

| Method | Enum value | UI label (operator mode) | Core idea |
|---|---|---|---|
| A | `css` | CSS Selectors | Deterministic DOM extraction using planner-generated selectors |
| B | `smart_scraper` | AI (Smart) | LLM extraction from HTML snippets |
| C | `crawl4ai` | Crawl4AI | Local async fetching with markdown output |
| D | `universal_scraper` | AI (Universal) | AI-generated BeautifulSoup extraction code with caching |
| E | `listing_api` | Structured Source | Embedded JSON or intercepted listing API used as authoritative source |
| F | `claude` | Claude AI | Last-resort extraction via Azure AI Foundry Claude |
| G | `script` | Generated Script | Claude-generated BS4 extraction logic executed inside the crawler |

Default sales-facing preview hides the raw method names and shows generic `Option A/B/C...` labels. Operator mode reveals the underlying method names.

## 2. Where Each Method Is Used

- Preview stage runs all enabled methods that are relevant for the current page and compares only the methods that return at least one sample record.
  - `app/services/orchestrator.py` (`run_preview_scrape`)
  - `app/agents/scraper_agent.py` (`scrape_preview_dual`)
- Full run uses the user-selected method from preview confirmation or the recommended method if the user does not override it.
  - `app/api/routes.py` (`confirm_preview`)
  - `app/models/schemas.py` (`ConfirmPreviewRequest.extraction_method`)
- If the selected method underperforms during the full run, the orchestrator can retry other methods in a guarded order.
  - `app/services/orchestrator.py` (`_build_method_attempt_order`, `run_full`)

Important behavior:
- The preview UI only shows methods that produced a non-empty parsed preview record.
- A method can be enabled, executed, and still be absent from preview if it returned `0` items.

## 3. Method A (`css`)

### How it works

- Planner generates `item_container_selector`, `field_selectors`, `field_attributes`, and optionally `detail_link_selector`.
- Scraper uses BeautifulSoup and reads each field directly from the DOM.
- Main extractor: `app/agents/scraper_agent.py` (`_extract_items`).

### Strengths

- Fastest and cheapest extraction path.
- Deterministic and easy to reason about.
- Easy to debug because selectors are explicit.

### Weaknesses

- Sensitive to wrapper/container selector mistakes.
- Can return `0` items if DOM structure shifts slightly.
- Less effective on highly irregular or JS-heavy markup.

### Best fit

- Stable listing layouts with consistent DOM structure.

## 4. Method B (`smart_scraper`)

### How it works

- Uses direct LLM extraction over HTML via `smart_extract_items`.
- For large pages, the scraper can chunk the input to reduce truncation risk.
- If Smart extraction fails or returns empty during normal extraction fallback, CSS remains the deterministic backup.

### Strengths

- More tolerant of noisy HTML than pure CSS.
- Useful when visible data exists but selectors underfit.
- Can recover fields missed by deterministic selectors.

### Weaknesses

- Slower and less deterministic than CSS.
- Still depends on the quality of the HTML slice given to it.
- Small or low-signal preview HTML can cause empty output.

### Best fit

- Pages where the data is present but markup is inconsistent.

## 5. Method C (`crawl4ai`)

### How it works

- Uses Crawl4AI for local page fetching and markdown generation.
- Markdown is then passed through the existing extraction/parsing pipeline.
- Can be used in preview and in the full run depending on feature flags and pagination compatibility.

### Strengths

- Local fetch path with no third-party fetch API dependency.
- Good on JS-heavy pages when markdown preserves the important structure.
- Often produces cleaner text than raw HTML.

### Weaknesses

- Markdown conversion can lose DOM-specific structure.
- Still depends on local browser/runtime health.
- Not suitable for every pagination mode.

### Best fit

- JS-heavy pages where a clean markdown representation works well.

## 6. Method D (`universal_scraper`)

### How it works

- Uses AI to generate site-specific BeautifulSoup extraction code.
- Generated code is cached and reused for similar pages.
- Available for preview extraction, full extraction, and detail-page enrichment.

### Strengths

- Adapts to each site while still executing deterministic code after generation.
- Good for repeated scraping of the same site.
- Reduces ongoing LLM cost after the first successful generation.

### Weaknesses

- First run is slower and more expensive.
- Can fail on very large inputs due to token limits.
- Cached logic must be regenerated when site structure changes materially.

### Best fit

- Sites you expect to scrape repeatedly over time.

## 7. Method E (`listing_api`) / Structured Source

### How it works

- Uses structured listing data instead of treating the page as pure HTML extraction.
- Two variants are supported by the same plan shape:
  - embedded HTML JSON (`source_kind="embedded_html"`)
  - intercepted listing API (`source_kind="api"`)
- Nested fields are flattened so values such as `values.description`, `contact.email`, `contact.phone`, and `website_url` can directly populate the canonical schema.

### Strengths

- Usually the cleanest and most complete source when available.
- Avoids brittle selector extraction on wrapper-heavy or SPA shells.
- Can satisfy `website`, `description`, `email`, and `phone` without a detail-page fetch.

### Weaknesses

- Availability is binary: it only works when the site exposes a usable structured source.
- Embedded JSON can still be irrelevant noise if detection picks the wrong blob.
- Often needs detail-page or detail-API enrichment for fields not present in the listing payload.

### Best fit

- Sites with embedded JSON blobs or background XHR listing feeds.

## 8. Method F (`claude`)

### How it works

- Uses Azure AI Foundry Claude Opus 4.6 through the Anthropic Messages endpoint.
- Receives simplified HTML plus expected fields and returns JSON records.
- Used as a fallback-heavy method, not as the default parser/extractor path.

### Strengths

- Strong recovery on hard layouts and irregular markup.
- Useful when multiple other methods return sparse results.
- Emits provider telemetry for operational tracking.

### Weaknesses

- Highest cost among the extraction methods.
- Slower than CSS and usually slower than local-first methods.
- Guarded by circuit-breaker and fallback-only policy for cost control.

### Best fit

- Difficult sites where lighter-weight methods underperform.

## 9. Method G (`script`)

### How it works

- Generates a BS4-based extraction script from the preview HTML and executes it inside the crawler process.
- The standalone script-generation endpoints also exist, but auto-execution there is disabled by default unless `ALLOW_GENERATED_SCRIPT_EXECUTION=true`.

### Strengths

- Can recover when other extraction strategies miss structure that is still visible in HTML.
- Produces explicit extraction logic that can be cached and inspected.
- Useful as another fallback path in preview.

### Weaknesses

- Generation adds latency.
- Generated logic can become stale quickly if page structure changes.
- Safety-sensitive for standalone script endpoints, which is why auto-execution is opt-in.

### Best fit

- Troubleshooting or hard pages where you want one more code-based extraction attempt.

## 10. Preview and Recommendation Logic

Preview can run up to seven methods:

1. `css`
2. `smart_scraper`
3. `crawl4ai`
4. `universal_scraper`
5. `listing_api` / structured source
6. `claude`
7. `script`

Important rules:

- Only methods that return a non-empty preview record are shown to the user.
- The orchestrator scores candidates deterministically first.
- If one candidate clearly wins by the configured margin, it is recommended without needing an LLM comparison.
- If scores are close, the orchestrator can ask the LLM to compare the candidate records.

This is why the recommended method can change per site and why some runs show only one or two preview options.

## 11. Full-Run Behavior

When a method is explicitly selected:

- `css`: deterministic selector extraction
- `smart_scraper`: Smart-first extraction path
- `crawl4ai`: Crawl4AI-first path, with compatibility checks
- `universal_scraper`: AI-generated BS4 path
- `listing_api`: structured-source-first extraction
- `claude`: Claude-heavy extraction path
- `script`: generated-script extraction path

When no method is explicitly selected:

- The orchestrator prefers the preview recommendation.
- Reliability controls can retry other methods if the selected path starts underperforming.

## 12. Operational Checklist

Use `css` when:

- Planner selectors look accurate in preview.
- You want the fastest, most repeatable extraction.

Use `smart_scraper` when:

- Visible data exists but CSS coverage is sparse.
- The HTML is messy but still contains the needed fields.

Use `crawl4ai` when:

- Rendering quality matters more than strict DOM fidelity.
- A clean markdown representation captures the page well.

Use `universal_scraper` when:

- You will scrape the same site repeatedly.
- You want AI adaptability but deterministic execution after generation.

Use `listing_api` / structured source when:

- The page clearly exposes embedded JSON or a background listing feed.
- You want to prefer the authoritative machine-readable source over brittle HTML parsing.

Use `claude` when:

- Other methods are sparse or empty.
- The site is genuinely hard and you accept higher latency/cost.

Use `script` when:

- You want one more code-based fallback on a hard preview.
- You are troubleshooting a site and want an inspectable extraction attempt.

## 13. Config Flags That Affect These Methods

From `app/config.py`:

- `USE_SMART_SCRAPER_PRIMARY`
- `USE_CRAWL4AI`
- `USE_CRAWL4AI_FOR_FETCHING`
- `USE_CRAWL4AI_FOR_EXTRACTION`
- `USE_UNIVERSAL_SCRAPER`
- `USE_UNIVERSAL_SCRAPER_FOR_EXTRACTION`
- `USE_LISTING_API_INTERCEPTION`
- `USE_INNER_TEXT_FALLBACK`
- `USE_CLAUDE_EXTRACTION`
- `USE_SCRIPT_EXTRACTION`
- `ALLOW_GENERATED_SCRIPT_EXECUTION`
- `CLAUDE_FALLBACK_ONLY`
- `CLAUDE_MAX_RETRIES_PER_STAGE`
- `CLAUDE_CIRCUIT_BREAKER_ENABLED`
- `CLAUDE_CIRCUIT_BREAKER_MAX_ERRORS`
- `CLAUDE_CIRCUIT_BREAKER_COOLDOWN_S`
- `RELIABILITY_AUTO_SWITCH_ENABLED`
- `RELIABILITY_AUTO_SWITCH_MIN_PAGES`
- `RELIABILITY_AUTO_SWITCH_ZERO_STREAK`

These materially change which methods run, which methods are shown in preview, and when fallback or switching occurs.

## 14. Practical Summary

- `css` is still the fastest and most deterministic baseline.
- `smart_scraper` is the first adaptive recovery path for messy HTML.
- `crawl4ai` is the local-first rendered fetch path.
- `universal_scraper` is the cached adaptive-code path for repeated sites.
- `listing_api` / structured source is often the best source when the site exposes real machine-readable data.
- `claude` is the expensive high-recall fallback.
- `script` is an additional code-based fallback and troubleshooting path.

The best method is intentionally site-dependent. Preview is designed to prove that on a sample before the full crawl starts.
