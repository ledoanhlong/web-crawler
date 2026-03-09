# Scraping Methods A/B/C (Codebase Deep Dive)

This document explains the practical differences between the three extraction methods used in this project.

In code, these methods are represented by `ExtractionMethod`:
- Method A -> `css`
- Method B -> `smart_scraper`
- Method C -> `firecrawl`

Source: `app/models/schemas.py` (`class ExtractionMethod`).

## 1. Quick Mapping

| Method | Enum value | Core idea |
|---|---|---|
| A | `css` | Deterministic DOM extraction using planner-generated CSS selectors |
| B | `smart_scraper` | LLM-based extraction from HTML (with CSS fallback) |
| C | `firecrawl` | FireCrawl-assisted fetching/extraction path, then project extraction pipeline |

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

## 5. Method C (`firecrawl`) Details

### How it works
- FireCrawl can be used in two ways in this codebase:
  1. Fetching path (`/scrape`) in scraper execution (`_scrape_firecrawl`).
  2. Optional structured extraction path (`firecrawl_extract`) during preview when enabled.
- Even in FireCrawl fetch mode, extracted HTML still goes through project extraction logic (`_extract_items_with_fallback`).
- Files:
  - `app/agents/scraper_agent.py` (`_scrape_firecrawl`, preview FireCrawl branch)
  - `app/utils/firecrawl.py`
  - `app/config.py` (`use_firecrawl*` flags)

### Strengths
- Better fetching resilience for anti-bot and JS-heavy pages.
- Good fallback when direct site fetch is unstable.
- Integrates with existing enrichment pipeline.

### Weaknesses
- Feature-flag dependent and requires API credentials.
- Pagination compatibility is limited in this implementation:
  - Compatible: `none`, `page_numbers`
  - Not direct fit for interactive flows (`infinite_scroll`, `load_more_button`, `alphabet_tabs`), where code falls back to JS/static path.
- External API dependency adds operational risk/cost.

### Best fit
- Pages where standard HTTP/browser fetch reliability is poor.
- Sites with anti-bot pressure where FireCrawl fetching improves stability.

## 6. Behavior Differences in Auto/Fallback Mode

When no explicit method is forced (`extraction_method=None`):
- Scraper tries CSS first.
- If CSS yields 0 items and Smart mode is enabled, tries Smart extraction.
- If Smart also fails, returns CSS result (possibly empty).

Source: `app/agents/scraper_agent.py` (`_extract_items_with_fallback`).

When a method is explicitly selected:
- `css`: CSS only.
- `smart_scraper`: Smart primary, CSS fallback.
- `firecrawl`: FireCrawl path if pagination is compatible; otherwise logs warning and falls back to JS/static path.

Source: `app/agents/scraper_agent.py` (`scrape`).

## 7. Preview and Recommendation Logic (Why A/B/C can differ)

Preview stage runs multi-method extraction and parses each candidate into `SellerLead`.
Then the orchestrator picks recommendation by:
- Deterministic completeness scoring first.
- If close scores, LLM comparison over candidate records.

Source: `app/services/orchestrator.py` (`run_preview_scrape`, `_select_preview_method_deterministic`, `_compare_extractions`).

This is why the recommended method can change per site even with same prompt.

## 8. Operational Checklist

Use Method A (`css`) when:
- Planner selectors look accurate in preview.
- You want fastest, repeatable extraction.

Use Method B (`smart_scraper`) when:
- CSS preview misses fields/items but data is visibly present.
- Layout is irregular and selector brittleness is high.

Use Method C (`firecrawl`) when:
- Fetching/rendering reliability is the bottleneck.
- You have valid FireCrawl config and compatible pagination.

## 9. Config Flags That Affect A/B/C

From `app/config.py`:
- `use_smart_scraper_primary`
- `use_firecrawl`
- `use_firecrawl_for_fetching`
- `use_firecrawl_for_discovery`
- `use_firecrawl_for_extraction`
- `reliability_auto_switch_enabled`
- `reliability_auto_switch_min_pages`
- `reliability_auto_switch_zero_streak`

These flags can materially change which method effectively runs and when fallback triggers.

## 10. Practical Summary

- Method A (`css`) = fastest and most deterministic when selectors are valid.
- Method B (`smart_scraper`) = more adaptive to messy markup, but slower and less deterministic.
- Method C (`firecrawl`) = strongest fetch-resilience path, especially for hard sites, but constrained by feature flags and pagination compatibility.

In this codebase, the best method is intentionally site-dependent and validated in preview before full crawl.