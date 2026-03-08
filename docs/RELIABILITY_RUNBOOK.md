# Reliability Runbook

This runbook documents how to operate the crawler for unseen websites while avoiding one-off, site-specific fixes.

## Goals

- Reduce hard failures on unseen sites.
- Keep fixes generic and reusable.
- Catch regressions before they reach users.

## Reliability Contract

A crawl is considered healthy when all of the following are true:

- Preview stage returns at least one candidate record.
- Full run ends as `completed` or `partial`.
- `diagnostics.counters.empty_pages` does not dominate processed pages.
- Output has acceptable quality score for the profile (target >= 0.70 by default).

## Failure Taxonomy

Use these categories for triage and reporting:

- `network_transient`
- `anti_bot`
- `rendering`
- `selector_mismatch`
- `pagination_mismatch`
- `detail_enrichment`
- `parser_schema_mismatch`
- `quality_threshold`
- `unknown`

## New-Site Onboarding Process

1. Add a smoke profile entry for the site in `test/fixtures/website_profiles.example.json`.
2. Run smoke tests with `scripts/smoke_sites.py`.
3. Inspect `diagnostics.counters` and `diagnostics.failures` for the job.
4. If failed, map the top issue to one taxonomy category.
5. Apply generic fix in planner/scraper/parser path, not site-specific hardcoding.
6. Re-run smoke profile and confirm improved status and counters.
7. Commit both code and updated profile expectations if behavior changed intentionally.

Smoke profile fields for gating:

- `expect_min_records`: minimum acceptable records for pass.
- `expect_min_quality`: minimum `quality_report.overall_score` for pass.
- `allow_partial`: whether `partial` final status is acceptable.
- `auto_resume_partial`: whether smoke runner should automatically call `/resume` before evaluating.

Profile file selection:

- Use `--profiles auto` in smoke runner.
- `auto` selects `test/fixtures/website_profiles.private.json` when present.
- Otherwise it falls back to `test/fixtures/website_profiles.example.json`.

## Triage Checklist

When a site fails, collect:

- URL and profile name.
- Final job status.
- Preview recommendation and selected method.
- `diagnostics.counters` snapshot.
- Top 3 latest failure events from `diagnostics.failures`.
- `quality_report` summary (if output exists).

Then classify root cause:

- Mostly empty pages with selector failures -> `selector_mismatch`.
- Repeated pagination empty pages -> `pagination_mismatch`.
- Detail backlog on partial jobs -> `detail_enrichment`.
- API/timeout responses -> `network_transient` or `anti_bot`.

## Guardrails

- Do not add direct host/domain conditionals in extraction logic unless approved exception.
- Any patch for one site must improve at least one smoke profile that is not that site.
- Add/adjust profile expectations only after validating behavior with a full run.

## Rollout Practice

- Keep reliability features behind settings in `app/config.py`.
- Start with defaults, then tune thresholds based on smoke report trends.
- Track before/after metrics using smoke report JSON files saved to `output/smoke_reports/`.

## Smoke Report Review

For each report file:

1. Count profiles with `passed=true`.
2. Review profiles with `status=failed` first.
3. For `partial`, verify remaining detail pages and whether resume succeeds.
4. Compare `method_switches` and `empty_pages` to prior report.
5. Open a follow-up issue when trend worsens across two runs.

## Trend Guardrail Command

Use the comparator to detect regressions between recent smoke reports:

```bash
python scripts/compare_smoke_reports.py \
	--report-dir output/smoke_reports \
	--max-pass-rate-drop 0.10 \
	--max-empty-pages-increase 2.0 \
	--max-switches-increase 1.0 \
	--max-parser-scalar-drop 0.10 \
	--max-parser-structured-drop 0.10 \
	--allow-new-failures 0
```

If this command exits with code `1`, treat it as a reliability regression and open a postmortem using `docs/POSTMORTEM_TEMPLATE.md`.

Optional CI escalation:

- In GitHub Actions, set repository variable `AUTO_CREATE_SMOKE_ISSUE=true`.
- The nightly workflow will generate `output/smoke_reports/issue-payload.json` and open a regression issue automatically when trend checks fail.

After smoke runs, generate a concise markdown summary for sharing:

```bash
python scripts/smoke_trend_markdown.py \
	--report-dir output/smoke_reports \
	--out output/smoke_reports/latest-summary.md
```
