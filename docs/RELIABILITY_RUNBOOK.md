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
4. Inspect provider telemetry and cost/fallback signals from `/api/v1/crawl/{job_id}/telemetry`.
5. Inspect the preview summary log line (`Preview — CSS: ..., Smart: ..., Crawl4AI: ...`) because the UI only shows methods that produced a non-empty sample record.
6. If failed, map the top issue to one taxonomy category.
7. Apply a generic fix in planner/scraper/parser path, not site-specific hardcoding.
8. Re-run smoke profile and confirm improved status and counters.
9. Commit both code and updated profile expectations if behavior changed intentionally.

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
- Preview summary from logs if the UI showed only a subset of methods.
- `diagnostics.counters` snapshot.
- Top 3 latest failure events from `diagnostics.failures`.
- `quality_report` summary (if output exists).

Then classify root cause:

- Mostly empty pages with selector failures -> `selector_mismatch`.
- Structured source exists but CSS/Smart are empty -> usually still `selector_mismatch` or `rendering`, not a preview UI bug.
- Repeated pagination empty pages -> `pagination_mismatch`.
- Detail backlog on partial jobs -> `detail_enrichment`.
- API/timeout responses -> `network_transient` or `anti_bot`.
- universal-scraper token-limit/provider 4xx failures -> treat as provider/runtime evidence, then classify by the underlying page issue (`rendering`, `selector_mismatch`, or `unknown` if inconclusive).
- Claude provider disabled or repeated provider errors -> `unknown` (include circuit-breaker context in details).

Also check provider runtime health:
- `GET /health` should be `ok` or intentionally `degraded` (if non-critical provider disabled).
- If `claude` is degraded, verify breaker state and cooldown in health payload.
- If a job vanished after restart, check `OUTPUT_DIR/job_store` before assuming data loss. Active jobs are snapshotted and recovered only when the filesystem itself persists across restarts.

## Guardrails

- Do not add direct host/domain conditionals in extraction logic unless approved exception.
- Any patch for one site must improve at least one smoke profile that is not that site.
- Add/adjust profile expectations only after validating behavior with a full run.
- Keep `CLAUDE_FALLBACK_ONLY=true` for normal operation unless explicitly evaluating Claude-first behavior.
- Keep `CLAUDE_MAX_RETRIES_PER_STAGE=1` to control cost blast radius on bad pages.

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

Provider telemetry review:

1. Check `provider_summary.estimated_total_cost_usd` for unexpected spikes.
2. Verify `provider_events` contain fallback reasons for method switches.
3. Confirm `claude_consecutive_errors` is stable and breaker is not repeatedly opening.

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
