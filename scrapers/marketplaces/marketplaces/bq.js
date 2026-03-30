/**
 * B&Q (diy.com) marketplace adapter — API-based.
 *
 * Calls the Kingfisher marketplace seller API directly (no browser needed).
 * API key is publicly embedded in every diy.com page.
 */

// ─── config ──────────────────────────────────────────────────────────────────
export const config = {
  id: 'bq',
  name: 'B&Q',
  defaultFrom: 1,
  defaultTo: 35000,
  defaultDelay: 500,
  defaultConcurrency: 5,
  csvColumns: [
    'sellerId',
    'businessName',
    'vatNumber',
    'registeredAddress',
    'shippedFrom',
    'sourceUrl',
  ],
};

// Kingfisher marketplace seller API key (publicly embedded in every diy.com page)
const SELLER_API_KEY = 'eyJvcmciOiI2MGFlMTA0ZGVjM2M1ZjAwMDFkMjYxYTkiLCJpZCI6IjE0NmFhMTQ5ZGIxYjQ4OGI4OWJlMTNkNTI0MmVhMmZmIiwiaCI6Im11cm11cjEyOCJ9';

// ─── sourceUrl ───────────────────────────────────────────────────────────────
export function sourceUrl(sellerId) {
  return `https://www.diy.com/verified-sellers/seller/${sellerId}`;
}

// ─── fetch ───────────────────────────────────────────────────────────────────
const _fetch = async function (sellerId) {
  const apiUrl = `https://api.kingfisher.com/v1/sellers/BQUK/${sellerId}`;
  const resp = await globalThis.fetch(apiUrl, {
    headers: {
      'Authorization': SELLER_API_KEY,
      'Accept': '*/*',
    },
    signal: AbortSignal.timeout(30000),
  });

  if (resp.status === 404 || resp.status === 410) return { notFound: true };
  if (resp.status === 429) {
    const retryAfter = parseInt(resp.headers.get('retry-after') || '0', 10);
    return { rateLimited: true, retryAfterMs: Math.max(retryAfter * 1000, 2000) };
  }
  if (!resp.ok) return { error: `API HTTP ${resp.status}` };

  return { raw: await resp.json() };
};
export { _fetch as fetch };

// ─── parse ───────────────────────────────────────────────────────────────────
export function parse(raw, sellerId, url) {
  const attrs = raw?.data?.attributes;
  if (!attrs) {
    return emptyResult(sellerId, url);
  }

  const businessName = (attrs.corporateName || attrs.sellerName || '').trim();
  const vatNumber = (attrs.taxIdentificationNumber || '').trim();
  const shippedFrom = (attrs.shippingCountry || '').trim();

  // Prefer corporateContactInformation, fall back to contactInformation
  // if the corporate fields are placeholder "TBC" values
  let registeredAddress = '';
  const corpAddr = attrs.corporateContactInformation;
  const contactAddr = attrs.contactInformation;
  const addr = (corpAddr && !isTbcAddress(corpAddr)) ? corpAddr : contactAddr;
  if (addr && typeof addr === 'object') {
    registeredAddress = [
      addr.street1 || '',
      addr.street2 || '',
      addr.city || '',
      addr.state || '',
      addr.postCode || '',
      addr.country || '',
    ].filter(Boolean).map(s => s.trim()).join(', ');
  }

  return {
    sellerId,
    businessName,
    vatNumber,
    registeredAddress,
    shippedFrom,
    sourceUrl: url,
  };
}

// ─── isEmpty ─────────────────────────────────────────────────────────────────
export function isEmpty(parsed) {
  return !parsed.businessName && !parsed.vatNumber && !parsed.registeredAddress && !parsed.shippedFrom;
}

// ─── helpers ─────────────────────────────────────────────────────────────────
function isTbcAddress(addr) {
  const vals = [addr.street1, addr.street2, addr.city, addr.state, addr.postCode, addr.country]
    .map((v) => (v || '').trim().toUpperCase())
    .filter(Boolean);
  return vals.length === 0 || vals.every((v) => v === 'TBC');
}

function emptyResult(sellerId, url) {
  return {
    sellerId,
    businessName: '',
    vatNumber: '',
    registeredAddress: '',
    shippedFrom: '',
    sourceUrl: url,
  };
}
