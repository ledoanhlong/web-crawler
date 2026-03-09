# Evaluation: Scraping Methods A, B, C

This document evaluates the three extraction methods implemented in this repository:
- Method A: `css`
- Method B: `smart_scraper`
- Method C: `firecrawl`

Scope note:
- This is an implementation-level evaluation based on your code architecture and control flow.
- Scores are qualitative (not benchmark-derived) and should be validated with your smoke suite for target sites.

## 1. Evaluation Criteria

Scoring scale: 1 (weak) to 5 (strong).

- Extraction accuracy: How often the method returns correct, usable fields.
- Robustness to site variation: How well it handles changing/irregular HTML.
- Runtime performance: Typical speed for end-to-end extraction.
- Operational cost: Token/API/runtime cost implications.
- Determinism and reproducibility: Consistency of output across runs.
- Debuggability and maintainability: Ease of diagnosing/fixing issues.
- Anti-bot/fetch resilience: Ability to still fetch content on hard sites.

## 2. Scorecard

| Criterion | Method A (`css`) | Method B (`smart_scraper`) | Method C (`firecrawl`) |
|---|---:|---:|---:|
| Extraction accuracy | 4 | 4 | 4 |
| Robustness to site variation | 2 | 4 | 4 |
| Runtime performance | 5 | 2 | 3 |
| Operational cost | 5 | 2 | 2 |
| Determinism and reproducibility | 5 | 2 | 3 |
| Debuggability and maintainability | 5 | 3 | 3 |
| Anti-bot/fetch resilience | 2 | 3 | 5 |
| **Overall (simple average)** | **4.0** | **2.9** | **3.4** |

## 3. Method-by-Method Evaluation

## Method A (`css`)

Verdict: Best default for stable sites and production repeatability.

Why it scores high:
- Deterministic selector-based extraction is predictable and easy to verify.
- Fastest path with low operational cost.
- Strongly debuggable: selector mismatches are concrete and fixable.

Main risks:
- Fragile when DOM classes/structure change.
- Can return zero items when planner selectors are slightly off.

Use when:
- Preview output is good.
- Site structure is relatively stable.
- Throughput and consistency matter most.

## Method B (`smart_scraper`)

Verdict: Best recovery/backup when selectors are brittle or page HTML is messy.

Why it helps:
- More tolerant to irregular markup and noisy structure.
- Chunked extraction logic in your scraper improves large-page handling.

Main risks:
- Slower and more expensive than CSS extraction.
- Output may vary between runs due to LLM behavior.
- Harder to root-cause exact extraction misses than strict CSS.

Use when:
- CSS preview has obvious misses but visible data exists.
- You prioritize recall over strict determinism.

## Method C (`firecrawl`)

Verdict: Best fetch-resilience strategy for difficult targets; not always best pure extractor.

Why it helps:
- Stronger page-fetching reliability on JS-heavy or anti-bot sites.
- Integrates with your existing extraction and enrichment flow.

Main risks:
- External API dependency and cost.
- In current implementation, direct compatibility is strongest for `none` and `page_numbers` pagination.
- Adds operational complexity (keys, feature flags, service availability).

Use when:
- Primary blocker is fetching/rendering reliability.
- Standard HTTP/browser fetch path is unstable.

## 4. Recommended Decision Rules

1. Start with preview comparison enabled (already in your orchestrator).
2. Prefer Method A when preview quality is competitive.
3. Choose Method B when CSS is underfitting and recall is poor.
4. Choose Method C when site access/rendering reliability is the bottleneck.
5. Keep auto-switch enabled to recover from empty-page streaks during full crawl.

## 5. Risk Register by Method

- Method A (`css`): selector drift risk.
  - Mitigation: maintain selector quality gates and plan review edits.
- Method B (`smart_scraper`): non-determinism and latency risk.
  - Mitigation: keep as fallback/targeted choice, not universal default.
- Method C (`firecrawl`): third-party dependency and compatibility risk.
  - Mitigation: health checks, fallback to JS/static, constrain to suitable pagination patterns.

## 6. KPI Suggestions for Empirical Validation

Track these per method in smoke runs:
- Item yield: items/page and total unique records.
- Field completeness: non-empty ratio for core fields (`name`, `website`, `email`, `phone`).
- Error profile: empty-page streaks, timeout rate, retry count.
- Cost profile: average runtime/job and API/token cost/job.
- Stability: variance of outputs across repeated runs on same URL.

## 7. Final Recommendation

- Primary baseline: Method A (`css`).
- Strategic fallback: Method B (`smart_scraper`) for extraction recovery.
- Reliability path: Method C (`firecrawl`) for hard-to-fetch targets.

This mix matches your current architecture: preview-driven selection, explicit method choice, and guarded fallback in full-run orchestration.