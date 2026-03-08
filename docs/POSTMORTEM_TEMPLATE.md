# Reliability Postmortem Template

Use this template for every hard failure on a new or unseen site.

## Metadata

- Date:
- Owner:
- Profile name:
- URL:
- Job ID:
- Final status (`failed`, `partial`, `completed`):

## Impact

- User-visible impact:
- Records expected:
- Records produced:
- Severity:

## Detection

- How was it detected (smoke run, user report, logs):
- First failure timestamp:
- Detection latency:

## Diagnostics Snapshot

- `diagnostics.counters`:
- Latest failure events (`diagnostics.failures`):
- Status timeline excerpt:
- Quality report summary:

## Root Cause

- Primary failure category:
- Technical root cause:
- Why existing safeguards did not prevent it:

## Fix

- Generic code change (avoid site-specific hardcoding):
- Files changed:
- Settings/threshold changes:

## Verification

- Smoke profile(s) rerun:
- Before/after metrics:
- Test cases added:

## Preventive Actions

- New regression fixture/profile added:
- Runbook updates:
- Monitoring/alert updates:
- Follow-up issue links:
