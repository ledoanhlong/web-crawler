/**
 * Shared HTML/text parsing utilities for marketplace adapters.
 *
 * Import what you need:
 *   import { cleanText, extractDtDdPairs, findValueByLabels } from '../lib/parse-utils.js';
 */

export function decodeEntities(str = '') {
  return str
    .replace(/&nbsp;/g, ' ')
    .replace(/&amp;/g, '&')
    .replace(/&quot;/g, '"')
    .replace(/&#39;/g, "'")
    .replace(/&lt;/g, '<')
    .replace(/&gt;/g, '>')
    .replace(/\u00a0/g, ' ');
}

export function cleanText(str) {
  if (!str) return '';
  return str
    .replace(/<[^>]+>/g, '')
    .replace(/&amp;/g, '&')
    .replace(/&lt;/g, '<')
    .replace(/&gt;/g, '>')
    .replace(/&quot;/g, '"')
    .replace(/&#39;/g, "'")
    .replace(/&nbsp;/g, ' ')
    .replace(/\\u002F/g, '/')
    .replace(/\s+/g, ' ')
    .trim();
}

export function stripTagsKeepLines(html = '') {
  return html
    .replace(/<script\b[^>]*>[\s\S]*?<\/script>/gi, '\n')
    .replace(/<style\b[^>]*>[\s\S]*?<\/style>/gi, '\n')
    .replace(/<br\s*\/?>/gi, '\n')
    .replace(/<\/(p|div|li|tr|td|th|section|article|h\d|dd|dt)>/gi, '\n')
    .replace(/<[^>]+>/g, ' ')
    .replace(/\n[ \t]+/g, '\n')
    .replace(/[ \t]+\n/g, '\n');
}

/**
 * Extract all dt/dd key-value pairs from HTML.
 * @returns {Object} Map of { label: value }
 */
export function extractDtDdPairs(html) {
  const result = {};
  const re = /<dt[^>]*>([\s\S]*?)<\/dt>\s*<dd[^>]*>([\s\S]*?)<\/dd>/gi;
  let m;
  while ((m = re.exec(html)) !== null) {
    const k = cleanText(stripTagsKeepLines(m[1])).replace(/\n+/g, ' ');
    const v = cleanText(stripTagsKeepLines(m[2])).replace(/\n+/g, ' ');
    if (k && v) result[k] = v;
  }
  return result;
}

/**
 * Find a value in a key-value map by trying multiple label names.
 * Tries exact match first, then substring match.
 */
export function findValueByLabels(map, labels) {
  const entries = Object.entries(map);
  const normalize = (s) => (s || '').toLowerCase().replace(/\s+/g, ' ').replace(/[:\-]+$/g, '').trim();

  // exact match
  for (const label of labels) {
    const n = normalize(label);
    const hit = entries.find(([k]) => normalize(k) === n);
    if (hit?.[1]) return hit[1];
  }

  // contains match
  for (const label of labels) {
    const n = normalize(label);
    const hit = entries.find(([k]) => normalize(k).includes(n));
    if (hit?.[1]) return hit[1];
  }

  return '';
}

/**
 * Extract text content from the first element with a given data-test-id.
 */
export function extractByTestId(html, id) {
  const m = html.match(new RegExp(`data-test-id="${id}"[^>]*>([^<]+)`, 'i'));
  return m ? cleanText(m[1]) : '';
}

/**
 * Extract text content from ALL elements with a given data-test-id.
 */
export function extractAllByTestId(html, id) {
  const results = [];
  const re = new RegExp(`data-test-id="${id}"[^>]*>([^<]+)`, 'gi');
  let m;
  while ((m = re.exec(html)) !== null) {
    const val = cleanText(m[1]);
    if (val) results.push(val);
  }
  return results;
}

/**
 * Extract email addresses from text, filtering out noise/system emails.
 */
export function extractEmails(text, excludes = []) {
  const emailRegex = /[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}/g;
  const matches = text.match(emailRegex) || [];

  const defaultExcludes = [
    'sentry.io', 'analytics', 'tracking', 'monitoring',
    'noreply', 'no-reply', 'donotreply', 'placeholder', 'example.com',
  ];
  const allExcludes = [...defaultExcludes, ...excludes];

  return matches.filter((email) => {
    const lower = email.toLowerCase();
    return !allExcludes.some((p) => lower.includes(p)) && !/^\\u/.test(email);
  });
}

/**
 * Check if a value looks like a valid VAT number.
 */
export function looksLikeVat(value) {
  const v = (value || '').trim();
  return /^(?:[A-Z]{2}\s*)?[A-Z0-9 -]{8,20}$/i.test(v) && /\d{8,}/.test(v);
}
