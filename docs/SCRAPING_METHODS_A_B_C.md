# Scraping Methods A/B/C/D/E (Codebase Deep Dive)

This document explains the practical differences between the extraction methods used in this project.

In code, these methods are represented by `ExtractionMethod`:
- Method A -> `css`
- Method B -> `smart_scraper`
- Method C -> `crawl4ai`
- Method D -> `universal_scraper`
- Method E -> `claude`

Source: `app/models/schemas.py` (`class ExtractionMethod`).

## 1. Quick Mapping

| Method | Enum value | Core idea |
|---|---|---|
| A | `css` | Deterministic DOM extraction using planner-generated CSS selectors |
| B | `smart_scraper` | LLM-based extraction from HTML (with CSS fallback) |
| C | `crawl4ai` | Local async fetching with markdown output via Crawl4AI |
| D | `universal_scraper` | AI-powered BeautifulSoup code generation with caching |
| E | `claude` | Last-resort extraction via Claude Opus 4.6 (Azure AI Foundry Anthropic endpoint) |

## 2. Where Each Method Is Used

- Preview stage compares all available methods side-by-side and recommends one.
  - `app/services/orchestrator.py` (`run_preview_scrape`)
  - `app/agents/scraper_agent.py` (`scrape_preview_dual`)
- Full run uses the user-selected method from preview confirmation (`/crawl/{job_id}/confirm`).
  - `app/api/routes.py` (`confirm_preview`)
  - `app/models/schemas.py` (`ConfirmPreviewRequest.extraction_method`)
- If selected method underperforms, orchestrator can auto-switch methods.
  - `app/services/orchestrator.py` (`_build_method_attempt_order`, `run_full`)

## 3. Method A (`css`) Details

### How it works
- Planner generates:
  - `item_container_selector`
  - `field_selectors`
  - `field_attributes`
  - optional `detail_link_selector`
- Scraper parses HTML with BeautifulSoup and extracts each field by selectors.
- Main extractor: `app/agents/scraper_agent.py` (`_extract_items`).

### Strengths
- Fast and low-cost (no LLM extraction cost during item extraction).
- Deterministic and repeatable when selectors are correct.
- Easy to debug because behavior is explicit.

### Weaknesses
- Sensitive to DOM/CSS changes.
- Can return 0 items if selectors are slightly wrong.
- Less robust against heavily obfuscated or irregular markup.

### Best fit
- Stable listing layouts with consistent DOM structure.

## 4. Method B (`smart_scraper`) Details

### How it works
- Uses direct LLM extraction over HTML (`smart_extract_items`) instead of strict selectors.
- For large pages, scraper chunks item containers before LLM extraction (`_smart_extract_chunked`) to avoid token truncation.
- If Smart extraction fails/returns empty, falls back to CSS extraction.
- Files:
  - `app/utils/smart_scraper.py` (`smart_extract_items`)
  - `app/agents/scraper_agent.py` (`_extract_items_with_fallback`, `_smart_extract_chunked`)

### Strengths
- More tolerant of messy or partially changing HTML.
- Can still extract useful fields even when selectors underperform.
- Chunking design improves recall on large listing pages.

### Weaknesses
- Slower than pure CSS extraction.
- LLM output can be less deterministic.
- Quality depends on HTML quality and prompt context.

### Best fit
- Hard-to-template pages where CSS precision is low.
- Cases where CSS preview is sparse but visible data exists.

## 5. Method C (`crawl4ai`) Details

### How it works
- Crawl4AI performs local async fetching and converts page content to clean markdown output.
- Runs entirely locally with no external API dependency.
- Extracted markdown is then processed through the project extraction pipeline.
- Files:
  - `app/agents/scraper_agent.py` (Crawl4AI branch)
  - `app/utils/crawl4ai.py`

### Strengths
- Runs locally -- no external API keys or third-party service required.
- Async fetching provides good performance on JS-heavy pages.
- Markdown output is clean and well-structured for downstream extraction.
- No per-request API cost.

### Weaknesses
- Depends on local browser/rendering environment.
- Markdown conversion may lose some structural nuance present in raw HTML.
- Less battle-tested against aggressive anti-bot protections compared to commercial services.

### Best fit
- JS-heavy pages where standard HTTP fetch is insufficient.
- Projects that need to avoid external API dependencies and costs.
- Sites where clean markdown representation captures the needed data well.

## 6. Method D (`universal_scraper`) Details

### How it works
- Uses AI to generate custom BeautifulSoup extraction code tailored to each target site.
- Generated scraper code is cached so subsequent runs reuse the same logic without re-generation.
- Combines the adaptability of LLM-driven approaches with the speed of deterministic code execution.
- Files:
  - `app/utils/universal_scraper.py`
  - `app/agents/scraper_agent.py` (universal_scraper branch)

### Strengths
- AI-generated BS4 code adapts to each site's specific structure.
- Caching means the first run pays the generation cost, but subsequent runs are fast.
- Deterministic execution once code is generated and cached.
- No ongoing LLM cost per extraction after initial code generation.

### Weaknesses
- Initial code generation adds latency and cost on the first run.
- Generated code quality depends on the AI model's understanding of the page structure.
- Cache invalidation is needed when site structure changes significantly.

### Best fit
- Sites that will be scraped repeatedly over time.
- Cases where you want LLM adaptability upfront but deterministic speed at runtime.
- Scenarios where per-extraction LLM cost is a concern.

## 7. Behavior Differences in Auto/Fallback Mode

When no explicit method is forced (`extraction_method=None`):
- Scraper tries CSS first.
- If CSS yields 0 items and Smart mode is enabled, tries Smart extraction.
- If Smart also fails and Claude fallback is enabled, tries Claude extraction.
- If Claude also fails, returns CSS result (possibly empty).

Source: `app/agents/scraper_agent.py` (`_extract_items_with_fallback`).

When a method is explicitly selected:
- `css`: CSS only.
- `smart_scraper`: Smart primary, CSS fallback.
- `crawl4ai`: Crawl4AI async fetch with markdown output; falls back to JS/static path if needed.
- `universal_scraper`: AI-generated BS4 code with caching; regenerates if cached code fails.
- `claude`: Claude primary, CSS fallback.

Source: `app/agents/scraper_agent.py` (`scrape`).

## 8. Preview and Recommendation Logic (Why A/B/C/D/E can differ)

Preview stage runs multi-method extraction and parses each candidate into `SellerLead`.
Then the orchestrator picks recommendation by:
- Deterministic completeness scoring first.
- If close scores, LLM comparison over candidate records.

Source: `app/services/orchestrator.py` (`run_preview_scrape`, `_select_preview_method_deterministic`, `_compare_extractions`).

This is why the recommended method can change per site even with same prompt.

## 9. Method E (`claude`) Details

### How it works
- Uses Azure AI Foundry Claude Opus 4.6 through the Anthropic Messages endpoint.
- Receives simplified HTML plus expected fields and returns JSON records.
- Used as a fallback or explicit method (depending on policy flags).

Files:
- `app/agents/scraper_agent.py` (`_claude_extract_items`)
- `app/utils/llm.py` (`chat_completion_claude_with_meta`)

### Strengths
- Strong recovery on complex/irregular pages where selectors underperform.
- Returns token/latency/cost telemetry for operational tracking.

### Weaknesses
- Higher latency/cost than CSS and usually higher than Smart/Crawl4AI.
- Requires correct Azure Foundry Anthropic endpoint and API key setup.

### Best fit
- Last-resort recovery on hard sites.
- Explicit user override when preview quality is poor across other methods.

## 10. Operational Checklist

Use Method A (`css`) when:
- Planner selectors look accurate in preview.
- You want fastest, repeatable extraction.

Use Method B (`smart_scraper`) when:
- CSS preview misses fields/items but data is visibly present.
- Layout is irregular and selector brittleness is high.

Use Method C (`crawl4ai`) when:
- You need reliable JS-rendered page fetching without external API costs.
- Clean markdown output is sufficient for your extraction needs.

Use Method D (`universal_scraper`) when:
- You plan to scrape the same site repeatedly and want cached, fast extraction.
- You want AI-adapted extraction code without ongoing per-request LLM costs.

Use Method E (`claude`) when:
- CSS/Smart/Crawl4AI/universal-scraper previews are sparse or empty.
- You need maximum extraction robustness for difficult sites.

## 11. Config Flags That Affect A/B/C/D/E

From `app/config.py`:
- `use_smart_scraper_primary`
- `use_crawl4ai`
- `use_universal_scraper`
- `use_claude_extraction`
- `claude_fallback_only`
- `claude_max_retries_per_stage`
- `claude_circuit_breaker_enabled`
- `claude_circuit_breaker_max_errors`
- `claude_circuit_breaker_cooldown_s`
- `reliability_auto_switch_enabled`
- `reliability_auto_switch_min_pages`
- `reliability_auto_switch_zero_streak`

These flags can materially change which method effectively runs and when fallback triggers.

## 12. Practical Summary

- Method A (`css`) = fastest and most deterministic when selectors are valid.
- Method B (`smart_scraper`) = more adaptive to messy markup, but slower and less deterministic.
- Method C (`crawl4ai`) = local async fetching with markdown output, no external API dependency.
- Method D (`universal_scraper`) = AI-generated BS4 code with caching, balancing adaptability and runtime speed.
- Method E (`claude`) = highest-recovery fallback for difficult pages, with circuit-breaker and cost controls.

In this codebase, the best method is intentionally site-dependent and validated in preview before full crawl.
