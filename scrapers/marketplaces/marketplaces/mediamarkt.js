/**
 * MediaMarkt (mediamarkt.nl) marketplace adapter — HTML-based with embedded JSON.
 *
 * Fetches seller HTML pages and extracts data from:
 * 1. Embedded JSON (__PRELOADED_STATE__ / GraphqlMarketplaceSeller)
 * 2. dt/dd pairs (structured company info)
 * 3. aria-label (ratings)
 * 4. Regex fallback (emails)
 *
 * Multilingual: Dutch, English, German labels supported.
 */

import { cleanText, extractDtDdPairs, findValueByLabels, extractEmails } from '../lib/parse-utils.js';

// ─── config ──────────────────────────────────────────────────────────────────
export const config = {
  id: 'mediamarkt',
  name: 'MediaMarkt',
  defaultFrom: 1,
  defaultTo: 15000,
  defaultDelay: 2000,
  defaultConcurrency: 1,
  csvColumns: [
    'sellerId',
    'businessName',
    'email',
    'phone',
    'rating',
    'ratingOutOf',
    'reviewCount',
    'companyName',
    'address',
    'zipCode',
    'city',
    'kvkNumber',
    'vatNumber',
    'sellerDataSection',
  ],
};

const HEADERS = {
  'User-Agent':
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
  'Accept':
    'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
  'Accept-Language': 'nl-NL,nl;q=0.9,en-US;q=0.8,en;q=0.7',
  'Accept-Encoding': 'gzip, deflate, br',
  'Cache-Control': 'no-cache',
  'Pragma': 'no-cache',
  'Sec-Ch-Ua': '"Not A(Brand";v="99", "Google Chrome";v="131", "Chromium";v="131"',
  'Sec-Ch-Ua-Mobile': '?0',
  'Sec-Ch-Ua-Platform': '"Windows"',
  'Sec-Fetch-Dest': 'document',
  'Sec-Fetch-Mode': 'navigate',
  'Sec-Fetch-Site': 'none',
  'Sec-Fetch-User': '?1',
  'Upgrade-Insecure-Requests': '1',
};

const NOT_FOUND_PATTERNS = [
  'Helaas kan de verkoper niet worden gevonden',
  'Unfortunately, the seller could not be found',
  'Leider konnte der Verkäufer nicht gefunden werden',
];

const MEDIAMARKT_EMAIL_EXCLUDES = [
  'mediamarkt.nl', 'mediamarkt.de', 'mediamarkt.at',
  'mediamarkt.es', 'mediamarkt.it', 'mediamarkt.be',
  'mediamarkt.com', 'privacy@', 'jan.janssen',
];

// ─── sourceUrl ───────────────────────────────────────────────────────────────
export function sourceUrl(sellerId) {
  return `https://www.mediamarkt.nl/nl/marketplace/seller/${sellerId}`;
}

// ─── fetch ───────────────────────────────────────────────────────────────────
const _fetch = async function (sellerId) {
  const url = sourceUrl(sellerId);
  const resp = await globalThis.fetch(url, {
    headers: HEADERS,
    signal: AbortSignal.timeout(30000),
  });

  if (resp.status === 404) return { notFound: true };
  if (resp.status === 429) return { rateLimited: true };
  if (!resp.ok) return { error: `HTTP ${resp.status}` };

  return { raw: await resp.text() };
};
export { _fetch as fetch };

// ─── parse ───────────────────────────────────────────────────────────────────
export function parse(raw, sellerId, url) {
  const html = raw;

  // --- Try to extract seller data from embedded JSON ---
  const sellerJson = extractSellerJson(html);

  // --- Extract rating from aria-label (most reliable) ---
  const ratingMatch = html.match(
    /aria-label="Beoordeling:\s*([\d.]+)\s*van de\s*([\d.]+)\s*sterren op basis van\s*(\d+)\s*recensies"/
  );
  const rating = ratingMatch ? parseFloat(ratingMatch[1]) : (sellerJson?.rating ?? null);
  const ratingOutOf = ratingMatch ? parseFloat(ratingMatch[2]) : (sellerJson ? 5 : null);
  const reviewCount = ratingMatch ? parseInt(ratingMatch[3]) : (sellerJson?.visibleReviewsCount ?? null);

  // --- Business name: from JSON first, then h1 fallback ---
  let businessName = sellerJson?.name || '';
  if (!businessName) {
    const h1Match = html.match(/<h1[^>]*>(.*?)<\/h1>/is);
    businessName = h1Match ? cleanText(h1Match[1]) : '';
  }

  // --- Skip "seller not found" pages ---
  if (NOT_FOUND_PATTERNS.some((p) =>
    businessName.includes(p) || html.includes(`<h1>${p}</h1>`) || html.includes(`<h2>${p}</h2>`))
  ) {
    return emptyResult(sellerId);
  }

  // --- Contact info from JSON ---
  const contactInfo = sellerJson?.contactInfoForCustomers || {};
  let email = contactInfo.email || '';
  let phone = contactInfo.phone || '';

  // --- Phone fallback: tel: href ---
  if (!phone) {
    const phoneMatch = html.match(/href=["']tel:([^"']+)["']/);
    if (phoneMatch) {
      const extracted = phoneMatch[1].trim();
      if (extracted && extracted !== 'undefined' && extracted !== 'null') {
        phone = extracted;
      }
    }
  }

  // --- Extract all dt/dd pairs ---
  const sellerDataSection = {};
  const dtDdPattern = /<dt[^>]*>(.*?)<\/dt>\s*<dd[^>]*>(.*?)<\/dd>/gis;
  let ddMatch;
  while ((ddMatch = dtDdPattern.exec(html)) !== null) {
    const key = cleanText(ddMatch[1]);
    const value = cleanText(ddMatch[2]);
    if (key && value) sellerDataSection[key] = value;
  }

  // --- Email fallback: dt/dd pairs, then regex ---
  if (!email) {
    const emailLabels = ['E-mailadres', 'Email address', 'E-Mail-Adresse', 'Email', 'E-mail'];
    for (const label of emailLabels) {
      if (sellerDataSection[label] && sellerDataSection[label].includes('@')) {
        email = sellerDataSection[label];
        break;
      }
    }
  }
  if (!email) {
    const found = extractEmails(html, MEDIAMARKT_EMAIL_EXCLUDES);
    email = found.length > 0 ? found[0] : '';
  }

  // --- Extract fields from dt/dd pairs ---
  let companyName = findValueByLabels(sellerDataSection, [
    'Officiële bedrijfsnaam', 'Official company name',
    'Offizieller Firmenname', 'Firmenname',
  ]);
  let address = findValueByLabels(sellerDataSection, [
    'Kantooradres', 'Office address', 'Geschäftsadresse', 'Adresse',
  ]);
  let zipCode = findValueByLabels(sellerDataSection, [
    'Postcode', 'ZIP code', 'Postleitzahl', 'PLZ',
  ]);
  let city = findValueByLabels(sellerDataSection, [
    'Plaats', 'City', 'Stadt', 'Ort',
  ]);
  let kvkNumber = findValueByLabels(sellerDataSection, [
    'Kamer van Koophandel nummer', 'Chamber of Commerce number', 'Handelskammernummer',
  ]);
  let vatNumber = findValueByLabels(sellerDataSection, [
    'BTW-nummer', 'VAT number', 'USt-IdNr', 'Umsatzsteuer-Identifikationsnummer',
  ]);

  // --- Parse imprint from JSON for additional company info ---
  const legalInfo = sellerJson?.legalInformation || {};
  const imprintData = parseImprint(legalInfo.imprint);

  if (!kvkNumber && imprintData.kvkNumber) kvkNumber = imprintData.kvkNumber;
  if (!vatNumber && imprintData.vatNumber) vatNumber = imprintData.vatNumber;

  // --- Additional fields from JSON into sellerDataSection ---
  if (sellerJson) {
    const sellerState = sellerJson.state || '';
    const fax = sellerJson.contactInformation?.fax || '';
    const serviceHours = contactInfo.serviceHours || '';
    const generalTermsUrl = legalInfo.generalBusinessTermsUrl || '';
    const imprintText = imprintData.imprintText || '';

    if (sellerState) sellerDataSection['Seller State'] = sellerState;
    if (fax) sellerDataSection['Fax'] = fax;
    if (serviceHours) sellerDataSection['Service Hours'] = serviceHours;
    if (generalTermsUrl) sellerDataSection['General Terms URL'] = generalTermsUrl;
    if (imprintText) sellerDataSection['Imprint'] = imprintText;
    if (sellerJson.dsaConsent != null) sellerDataSection['DSA Consent'] = String(sellerJson.dsaConsent);
    if (sellerJson.dataProtectionInformation) {
      sellerDataSection['Data Protection'] = String(sellerJson.dataProtectionInformation);
    }

    const shipping = sellerJson.sellerShippingDetails;
    if (shipping && shipping.length > 0) {
      const shippingInfo = shipping.map((s) => {
        const parts = [s.shippingCountry, s.shippingType];
        if (s.freeShippingThreshold) {
          parts.push(`free above ${s.freeShippingThreshold.amount} ${s.freeShippingThreshold.currency}`);
        }
        return parts.filter(Boolean).join(' - ');
      });
      sellerDataSection['Shipping'] = shippingInfo.join('; ');
    }
  }

  return {
    sellerId,
    businessName,
    email,
    phone,
    rating,
    ratingOutOf,
    reviewCount,
    companyName,
    address,
    zipCode,
    city,
    kvkNumber,
    vatNumber,
    sellerDataSection,
  };
}

// ─── isEmpty ─────────────────────────────────────────────────────────────────
export function isEmpty(parsed) {
  return !parsed.businessName;
}

// ─── internal helpers ────────────────────────────────────────────────────────

function extractSellerJson(html) {
  const sellerMatch = html.match(
    /"__typename"\s*:\s*"GraphqlMarketplaceSeller"\s*,\s*"id"\s*:\s*"(\d+)"\s*,\s*"name"\s*:\s*"([^"]*)"(.*?)(?="optimizelyDataFile"|"__typename"\s*:\s*"(?!Graphql))/s
  );
  if (!sellerMatch) return null;

  const jsonStr = `{"__typename":"GraphqlMarketplaceSeller","id":"${sellerMatch[1]}","name":"${sellerMatch[2]}"${sellerMatch[3]}`;

  let depth = 0;
  let end = -1;
  for (let i = 0; i < jsonStr.length; i++) {
    if (jsonStr[i] === '{') depth++;
    else if (jsonStr[i] === '}') {
      depth--;
      if (depth === 0) { end = i + 1; break; }
    }
  }
  if (end === -1) return null;

  try {
    return JSON.parse(jsonStr.substring(0, end));
  } catch {
    return null;
  }
}

function parseImprint(imprint) {
  if (!imprint) return {};
  const text = cleanText(imprint);
  const result = {};

  const kvkMatch = text.match(/(?:KvK|Kamer van Koophandel|Chamber of Commerce|Handelsregister)[:\s]*([A-Z0-9-]+)/i);
  if (kvkMatch) result.kvkNumber = kvkMatch[1].trim();

  const vatMatch = text.match(/(?:BTW|VAT|USt-IdNr|Umsatzsteuer)[:\s-]*([A-Z]{2}[A-Z0-9]+)/i);
  if (vatMatch) result.vatNumber = vatMatch[1].trim();

  result.imprintText = text;
  return result;
}

function emptyResult(sellerId) {
  return {
    sellerId,
    businessName: '',
    email: '',
    phone: '',
    rating: null,
    ratingOutOf: null,
    reviewCount: null,
    companyName: '',
    address: '',
    zipCode: '',
    city: '',
    kvkNumber: '',
    vatNumber: '',
    sellerDataSection: {},
  };
}
