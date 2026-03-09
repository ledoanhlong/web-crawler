# Evaluation: Scraping Methods A, B, C, D

This document evaluates the four extraction methods implemented in this repository:
- Method A: `css`
- Method B: `smart_scraper`
- Method C: `crawl4ai`
- Method D: `universal_scraper`

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

| Criterion | Method A (`css`) | Method B (`smart_scraper`) | Method C (`crawl4ai`) | Method D (`universal_scraper`) |
|---|---:|---:|---:|---:|
| Extraction accuracy | 4 | 4 | 4 | 4 |
| Robustness to site variation | 2 | 4 | 4 | 4 |
| Runtime performance | 5 | 2 | 4 | 4 |
| Operational cost | 5 | 2 | 4 | 3 |
| Determinism and reproducibility | 5 | 2 | 4 | 4 |
| Debuggability and maintainability | 5 | 3 | 3 | 3 |
| Anti-bot/fetch resilience | 2 | 3 | 4 | 3 |
| **Overall (simple average)** | **4.0** | **2.9** | **3.9** | **3.6** |

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

## Method C (`crawl4ai`)

Verdict: Best local-first fetch-and-extract strategy with no external API dependency.

Why it helps:
- Local async fetching handles JS-rendered pages without external services.
- Markdown output is clean and structured, improving downstream extraction quality.
- No per-request API cost -- runs entirely locally.

Main risks:
- Depends on local browser/rendering environment setup.
- Markdown conversion may lose some structural nuance from raw HTML.
- Less battle-tested against the most aggressive anti-bot protections.

Use when:
- JS-heavy pages need reliable rendering without external API costs.
- Clean markdown representation captures the target data well.
- You want to avoid third-party service dependencies.

## Method D (`universal_scraper`)

Verdict: Best long-term efficiency for repeatedly scraped sites through AI-generated cached code.

Why it helps:
- AI generates site-specific BeautifulSoup extraction code, adapting to each target's structure.
- Caching ensures the generation cost is paid only once; subsequent runs execute deterministic code.
- Balances the adaptability of LLM-driven approaches with the speed of static code.

Main risks:
- Initial code generation adds latency and LLM cost on the first run.
- Generated code quality depends on the AI model's interpretation of page structure.
- Cache invalidation is required when site structure changes significantly.

Use when:
- The same site will be scraped repeatedly over time.
- You want AI adaptability upfront but deterministic, fast extraction at runtime.
- Per-extraction LLM cost is a concern and amortizing generation cost is acceptable.

## 4. Recommended Decision Rules

1. Start with preview comparison enabled (already in your orchestrator).
2. Prefer Method A when preview quality is competitive.
3. Choose Method B when CSS is underfitting and recall is poor.
4. Choose Method C when you need reliable JS-rendered fetching without external API costs.
5. Choose Method D when the site will be scraped repeatedly and you want cached, fast extraction.
6. Keep auto-switch enabled to recover from empty-page streaks during full crawl.

## 5. Risk Register by Method

- Method A (`css`): selector drift risk.
  - Mitigation: maintain selector quality gates and plan review edits.
- Method B (`smart_scraper`): non-determinism and latency risk.
  - Mitigation: keep as fallback/targeted choice, not universal default.
- Method C (`crawl4ai`): local environment dependency risk.
  - Mitigation: ensure browser/rendering dependencies are properly configured; fall back to JS/static path if local fetch fails.
- Method D (`universal_scraper`): generated code staleness risk.
  - Mitigation: implement cache invalidation on extraction failures; monitor for site structure changes.

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
- Local fetch-and-extract: Method C (`crawl4ai`) for JS-heavy pages without external API costs.
- Cached adaptive extraction: Method D (`universal_scraper`) for repeatedly scraped sites.

This mix matches your current architecture: preview-driven selection, explicit method choice, and guarded fallback in full-run orchestration.
