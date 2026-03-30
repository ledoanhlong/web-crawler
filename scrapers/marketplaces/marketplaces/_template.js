/**
 * Template for new marketplace adapters.
 *
 * Copy this file to marketplaces/<name>.js and fill in the 5 required exports.
 * See bq.js (API-based) and mediamarkt.js (HTML-based) for real examples.
 */

import { cleanText } from '../lib/parse-utils.js';

// ─── config ──────────────────────────────────────────────────────────────────
export const config = {
  id: 'marketplace-slug',       // lowercase, used for results folder name
  name: 'Marketplace Name',     // display name in logs
  defaultFrom: 1,
  defaultTo: 25000,
  defaultDelay: 500,            // ms between batches (higher for HTML, lower for API)
  defaultConcurrency: 5,        // parallel fetches (lower for HTML, higher for API)
  csvColumns: [
    'sellerId',
    'businessName',
    // ... add all fields that parse() returns
  ],
};

// ─── sourceUrl ───────────────────────────────────────────────────────────────
export function sourceUrl(sellerId) {
  return `https://www.example.com/seller/${sellerId}`;
}

// ─── fetch ───────────────────────────────────────────────────────────────────
// Must return one of:
//   { raw: any }                          — success (passed to parse())
//   { notFound: true }                    — seller doesn't exist
//   { rateLimited: true, retryAfterMs? }  — engine will backoff and retry
//   { error: string }                     — engine will retry then log error
const _fetch = async function (sellerId) {
  const url = sourceUrl(sellerId);
  const resp = await globalThis.fetch(url, {
    headers: { 'Accept': 'application/json' },
    signal: AbortSignal.timeout(30000),
  });

  if (resp.status === 404) return { notFound: true };
  if (resp.status === 429) return { rateLimited: true };
  if (!resp.ok) return { error: `HTTP ${resp.status}` };

  return { raw: await resp.json() };
};
export { _fetch as fetch };

// ─── parse ───────────────────────────────────────────────────────────────────
// Receives the raw data from fetch(), must return an object with keys matching csvColumns.
export function parse(raw, sellerId, url) {
  return {
    sellerId,
    businessName: cleanText(raw?.name || ''),
    // ... extract other fields
  };
}

// ─── isEmpty ─────────────────────────────────────────────────────────────────
export function isEmpty(parsed) {
  return !parsed.businessName;
}
