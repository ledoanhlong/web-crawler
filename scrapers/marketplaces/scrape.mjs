#!/usr/bin/env node

/**
 * Marketplace seller scraper — unified entry point.
 *
 * Usage:
 *   node scrape.mjs <marketplace> [options]
 *
 * Examples:
 *   node scrape.mjs bq                                 # B&Q, full range
 *   node scrape.mjs mediamarkt --from 1 --to 500       # MediaMarkt, IDs 1-500
 *   node scrape.mjs bq --concurrency 10 --delay 300    # B&Q, faster
 *
 * Available marketplaces:
 *   bq          — B&Q (diy.com) via Kingfisher API
 *   mediamarkt  — MediaMarkt (mediamarkt.nl) via HTML scraping
 *
 * Add new marketplaces by creating an adapter in marketplaces/<name>.js
 * (see marketplaces/_template.js for the adapter contract).
 */

import { run } from './lib/engine.js';

const marketplace = process.argv[2];

if (!marketplace || marketplace.startsWith('--')) {
  console.error('Usage: node scrape.mjs <marketplace> [options]');
  console.error('');
  console.error('Available marketplaces:');
  console.error('  bq          — B&Q (diy.com)');
  console.error('  mediamarkt  — MediaMarkt (mediamarkt.nl)');
  console.error('');
  console.error('Options:');
  console.error('  --from <id>          Start ID');
  console.error('  --to <id>            End ID');
  console.error('  --delay <ms>         Delay between batches');
  console.error('  --concurrency <n>    Parallel fetches per batch');
  process.exit(1);
}

try {
  const adapter = await import(`./marketplaces/${marketplace}.js`);
  await run(adapter);
} catch (err) {
  if (err.code === 'ERR_MODULE_NOT_FOUND') {
    console.error(`Unknown marketplace: "${marketplace}"`);
    console.error('Check marketplaces/ for available adapters, or create a new one from _template.js');
    process.exit(1);
  }
  throw err;
}
