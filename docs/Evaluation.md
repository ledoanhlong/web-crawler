# Evaluation: Scraping Methods A-G

This document evaluates the extraction methods implemented in this repository:

- Method A: `css`
- Method B: `smart_scraper`
- Method C: `crawl4ai`
- Method D: `universal_scraper`
- Method E: `listing_api` / structured source
- Method F: `claude`
- Method G: `script`

Scope note:

- This is an implementation-level evaluation based on the current code architecture and control flow.
- Scores are qualitative, not benchmark-derived.
- Preview only shows methods that returned a non-empty sample record, so method availability in the UI is narrower than method availability in code.

## 1. Evaluation Criteria

Scoring scale: 1 (weak) to 5 (strong).

- Extraction accuracy: how often the method returns correct, usable fields
- Robustness to site variation: how well it handles changing or irregular structure
- Runtime performance: typical end-to-end speed
- Operational cost: token/API/runtime cost implications
- Determinism and reproducibility: consistency of output across runs
- Debuggability and maintainability: ease of diagnosis and repair
- Availability: how often the method is actually applicable on arbitrary sites

## 2. Scorecard

| Criterion | A `css` | B `smart_scraper` | C `crawl4ai` | D `universal_scraper` | E `listing_api` | F `claude` | G `script` |
|---|---:|---:|---:|---:|---:|---:|---:|
| Extraction accuracy | 4 | 4 | 4 | 4 | 5 | 5 | 4 |
| Robustness to site variation | 2 | 4 | 4 | 4 | 2 | 5 | 3 |
| Runtime performance | 5 | 2 | 4 | 4 | 5 | 2 | 2 |
| Operational cost | 5 | 2 | 4 | 3 | 5 | 1 | 2 |
| Determinism and reproducibility | 5 | 2 | 4 | 4 | 5 | 3 | 3 |
| Debuggability and maintainability | 5 | 3 | 3 | 3 | 4 | 3 | 3 |
| Availability | 5 | 4 | 3 | 3 | 2 | 3 | 3 |
| **Overall (simple average)** | **4.4** | **3.0** | **3.7** | **3.6** | **4.0** | **3.1** | **2.9** |

Interpretation:

- `listing_api` / structured source scores very high when it exists, but availability is low because many sites do not expose a usable structured source.
- `claude` remains the most robust hard-site fallback, but it is intentionally constrained by cost controls.
- `script` is valuable as another recovery path, but not as a default operating mode.

## 3. Method-by-Method Evaluation

## Method A (`css`)

Verdict: Best default for stable sites and production repeatability.

Why it scores high:

- Deterministic selector-based extraction is predictable and easy to verify.
- Fastest path with the lowest operational cost.
- Strongly debuggable because selector mismatches are concrete.

Main risks:

- Fragile when planner selectors drift toward wrapper elements.
- Can return zero items when the item container selector is only slightly wrong.

Use when:

- Preview output is good.
- Site structure looks stable and repeatable.

## Method B (`smart_scraper`)

Verdict: Strong adaptive recovery when selectors underfit but HTML still contains the data.

Why it helps:

- More tolerant to irregular markup and noisy structure.
- Works well when visible data exists but selectors are sparse.

Main risks:

- Slower and more expensive than CSS.
- Small or low-signal preview HTML can still produce empty output.
- Harder to root-cause exact misses than CSS.

Use when:

- CSS preview misses obvious fields.
- You need better recall from messy HTML.

## Method C (`crawl4ai`)

Verdict: Best local-first rendered fetch path when markdown preserves the right information.

Why it helps:

- Good for JS-heavy pages without using an external fetch service.
- Markdown output can be cleaner than raw DOM extraction.
- No per-request external fetch cost.

Main risks:

- Markdown can discard useful DOM nuance.
- Depends on local rendering/runtime health.
- Compatibility varies with pagination mode.

Use when:

- JS rendering matters.
- The page reads well as markdown.

## Method D (`universal_scraper`)

Verdict: Best long-term efficiency for repeated sites once generation succeeds.

Why it helps:

- AI-generated extraction code adapts to each site.
- Cached code makes later runs faster and cheaper.
- Blends adaptability with deterministic execution.

Main risks:

- First run has extra latency and LLM cost.
- Can hit token limits on very large inputs.
- Generated code must be refreshed when the site changes materially.

Use when:

- You plan repeated crawls of the same site.
- Amortizing setup cost is acceptable.

## Method E (`listing_api`) / Structured Source

Verdict: Often the best source when available because it bypasses brittle HTML extraction entirely.

Why it helps:

- Uses authoritative machine-readable data from embedded JSON or background APIs.
- Usually cleaner and more complete than DOM extraction.
- Can directly provide fields like `website`, `description`, `email`, and `phone`.

Main risks:

- Many sites have no usable structured source.
- Detection can still find irrelevant JSON if the page embeds multiple blobs.
- May still need detail enrichment for fields not present in the listing payload.

Use when:

- Planner or preview detects embedded structured data or a listing API.
- You want to prefer the real data source over rendered HTML.

## Method F (`claude`)

Verdict: Highest-recovery fallback for difficult pages, constrained by cost controls.

Why it helps:

- Strong extraction quality on irregular layouts.
- Useful when several lighter-weight methods return sparse or empty samples.
- Provider telemetry gives operational visibility.

Main risks:

- Highest cost among extraction methods.
- Slower than CSS and usually slower than local-first methods.
- Availability is intentionally limited by fallback-only policy and circuit breaker state.

Use when:

- Other methods underperform.
- Maximum recall matters more than cost or latency.

## Method G (`script`)

Verdict: Useful recovery path and troubleshooting tool, but not a primary operating mode.

Why it helps:

- Generates explicit extraction logic for a hard page.
- Can recover from cases where other methods fail to map the structure.
- Gives an inspectable code-based attempt.

Main risks:

- Generation latency is high relative to CSS.
- Generated logic can become stale quickly.
- Standalone script endpoint execution is safety-sensitive, so auto-execution is disabled by default.

Use when:

- You want one more extraction attempt on a hard preview.
- You are troubleshooting and want inspectable generated logic.

## 4. Recommended Decision Rules

1. Keep preview comparison enabled and trust it as the main site-specific decision point.
2. Prefer `css` when preview quality is competitive.
3. Prefer `listing_api` / structured source whenever a valid structured source is detected and coverage is good.
4. Choose `smart_scraper` when data is visible but selectors are weak.
5. Choose `crawl4ai` when rendering quality matters and markdown looks clean.
6. Choose `universal_scraper` when you expect repeated runs on the same site.
7. Keep `claude` as the expensive fallback path.
8. Use `script` as a troubleshooting/recovery option, not the default.

## 5. Risk Register by Method

- Method A (`css`): selector drift risk
  - Mitigation: stronger selector quality gates and wrapper-selector rejection
- Method B (`smart_scraper`): non-determinism and latency risk
  - Mitigation: keep it as a targeted recovery path
- Method C (`crawl4ai`): local runtime dependency risk
  - Mitigation: maintain browser/runtime health and fallback paths
- Method D (`universal_scraper`): token-limit and generated-code staleness risk
  - Mitigation: input-size controls, cache invalidation, fallback extraction
- Method E (`listing_api`): low applicability / wrong-blob detection risk
  - Mitigation: structured-source validation and preview comparison
- Method F (`claude`): cost and provider instability risk
  - Mitigation: circuit breaker, fallback-only policy, retry caps
- Method G (`script`): generation latency and safety risk
  - Mitigation: use selectively and keep standalone auto-execution gated

## 6. KPI Suggestions for Empirical Validation

Track these per method in smoke runs:

- Item yield: items per page and total unique records
- Field completeness: non-empty ratio for core fields (`name`, `website`, `email`, `phone`, `description`)
- Preview availability: how often the method produces a non-empty preview candidate
- Error profile: empty-page streaks, timeout rate, token-limit failures
- Cost profile: average runtime/job and API or token cost/job
- Stability: variance across repeated runs on the same URL

## 7. Final Recommendation

- Primary baseline: `css`
- First adaptive recovery: `smart_scraper`
- Local rendered fetch path: `crawl4ai`
- Repeated-site efficiency path: `universal_scraper`
- Prefer when available: `listing_api` / structured source
- Last-resort high-recall path: `claude`
- Troubleshooting/code-based recovery: `script`

This mix matches the current architecture: structured-source-first enrichment, preview-driven selection, explicit user choice, and guarded fallback during the full run.
