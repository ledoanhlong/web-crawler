/**
 * Generic sequential-ID marketplace scraping engine.
 *
 * This file is NEVER modified per-marketplace. All marketplace-specific logic
 * lives in adapter files under marketplaces/<name>.js.
 *
 * The engine handles: CLI arguments, progress tracking with resume, CSV output,
 * retry logic with exponential backoff, batched concurrency, graceful shutdown.
 *
 * Usage:
 *   node scrape.mjs <marketplace> [options]
 *
 * Options:
 *   --from <id>          Start ID (default: adapter's defaultFrom)
 *   --to <id>            End ID (default: adapter's defaultTo)
 *   --delay <ms>         Delay between batches (default: adapter's defaultDelay)
 *   --concurrency <n>    Parallel fetches per batch (default: adapter's defaultConcurrency)
 */

import { writeFileSync, readFileSync, existsSync, mkdirSync, appendFileSync } from 'node:fs';

const MAX_RETRIES = 3;

/**
 * Run the scraping engine with the given marketplace adapter.
 *
 * @param {object} adapter - Must export: config, sourceUrl, fetch, parse, isEmpty
 *   Optional exports: setup(), teardown()
 */
export async function run(adapter) {
  const { config } = adapter;
  const args = parseArgs(process.argv.slice(3)); // skip: node, scrape.mjs, <marketplace>

  const FROM_ID = args.from ?? config.defaultFrom;
  const TO_ID = args.to ?? config.defaultTo;
  const DELAY_MS = args.delay ?? config.defaultDelay;
  const CONCURRENCY = args.concurrency ?? config.defaultConcurrency;

  const resultsDir = `results/${config.id}`;
  const csvPath = `${resultsDir}/sellers.csv`;
  const progressPath = `${resultsDir}/progress.json`;

  mkdirSync(resultsDir, { recursive: true });

  const progress = loadProgress(progressPath);
  initCsv(csvPath, config.csvColumns);

  let shuttingDown = false;

  const shutdown = () => {
    if (shuttingDown) return;
    shuttingDown = true;
    console.log('\n\nShutting down gracefully — saving progress...');
    saveProgress(progressPath, progress);
    console.log('Progress saved. Re-run the same command to resume.');
    process.exit(0);
  };
  process.on('SIGINT', shutdown);
  process.on('SIGTERM', shutdown);

  // Optional setup (e.g., get session cookie)
  if (adapter.setup) await adapter.setup();

  const total = TO_ID - FROM_ID + 1;
  let processedThisRun = 0;
  let found = 0;
  let errors = 0;

  // Build list of IDs still to process
  const pendingIds = [];
  for (let id = FROM_ID; id <= TO_ID; id++) {
    if (!progress[id]) pendingIds.push(id);
  }
  const alreadyDone = total - pendingIds.length;

  console.log(`\n${config.name} Seller Scraper`);
  console.log(`Range: ${FROM_ID} - ${TO_ID} (${total} IDs)`);
  console.log(`Concurrency: ${CONCURRENCY} | Delay: ${DELAY_MS}ms between batches`);
  console.log(`Output: ${csvPath}`);
  if (alreadyDone > 0) {
    console.log(`Resuming - ${alreadyDone} already done, ${pendingIds.length} remaining`);
  }
  console.log('');

  try {
    for (let i = 0; i < pendingIds.length; i += CONCURRENCY) {
      if (shuttingDown) break;

      const batch = pendingIds.slice(i, i + CONCURRENCY);
      const results = await Promise.all(batch.map((id) => fetchWithRetry(adapter, id)));

      for (const result of results) {
        if (shuttingDown) break;

        processedThisRun++;
        const done = processedThisRun + alreadyDone;
        const { sellerId } = result;

        if (result._error) {
          errors++;
          progress[sellerId] = { status: 'error', error: result._error };
          logLine(sellerId, `ERROR: ${result._error}`, total, done);
        } else if (adapter.isEmpty(result)) {
          progress[sellerId] = { status: 'empty' };
          logLine(sellerId, 'no seller found', total, done);
        } else {
          found++;
          progress[sellerId] = { status: 'ok' };
          appendCsvRow(csvPath, config.csvColumns, result);
          logLine(sellerId, `OK ${result.businessName || '(seller found)'}`, total, done);
        }
      }

      // Save progress periodically
      if (processedThisRun % 50 < CONCURRENCY) saveProgress(progressPath, progress);

      // Delay between batches
      if (i + CONCURRENCY < pendingIds.length && !shuttingDown) {
        await sleep(DELAY_MS);
      }
    }
  } finally {
    saveProgress(progressPath, progress);
    if (adapter.teardown) await adapter.teardown();
  }

  console.log(`\n--- Done ---`);
  console.log(`Processed this run: ${processedThisRun}`);
  console.log(`Total processed: ${processedThisRun + alreadyDone} / ${total}`);
  console.log(`Sellers found: ${found}`);
  console.log(`Errors: ${errors}`);
  console.log(`Results saved to: ${csvPath}`);
}


// ── Fetch with retry + backoff ───────────────────────────────────────────────

async function fetchWithRetry(adapter, sellerId) {
  const url = adapter.sourceUrl(sellerId);

  for (let attempt = 1; attempt <= MAX_RETRIES; attempt++) {
    try {
      const fetchResult = await adapter.fetch(sellerId);

      // Not found
      if (fetchResult.notFound) {
        return { sellerId, businessName: '', _notFound: true };
      }

      // Rate limited — backoff and retry
      if (fetchResult.rateLimited) {
        const backoff = fetchResult.retryAfterMs || 2000 * Math.pow(2, attempt);
        if (attempt < MAX_RETRIES) {
          await sleep(backoff);
          continue;
        }
        return { sellerId, _error: 'Rate limited (429)' };
      }

      // Error from fetch
      if (fetchResult.error) {
        if (attempt < MAX_RETRIES) {
          await sleep(4000 * attempt);
          continue;
        }
        return { sellerId, _error: fetchResult.error };
      }

      // Success — parse
      const parsed = adapter.parse(fetchResult.raw, sellerId, url);
      return { ...parsed, sellerId };

    } catch (err) {
      if (attempt < MAX_RETRIES) {
        await sleep(4000 * attempt);
        continue;
      }
      return { sellerId, _error: err?.message || String(err) };
    }
  }

  return { sellerId, _error: 'Unknown scraping failure' };
}


// ── CSV helpers ──────────────────────────────────────────────────────────────

function initCsv(path, columns) {
  if (!existsSync(path)) {
    writeFileSync(path, columns.join(',') + '\n', 'utf-8');
  }
}

function appendCsvRow(path, columns, data) {
  const row = columns.map((col) => csvEscape(data[col]));
  appendFileSync(path, row.join(',') + '\n', 'utf-8');
}

function csvEscape(value) {
  let val = value ?? '';
  if (typeof val === 'object') val = JSON.stringify(val);
  val = String(val);
  if (val.includes(',') || val.includes('"') || val.includes('\n')) {
    return `"${val.replace(/"/g, '""')}"`;
  }
  return val;
}


// ── Progress helpers ─────────────────────────────────────────────────────────

function loadProgress(path) {
  if (existsSync(path)) {
    return JSON.parse(readFileSync(path, 'utf-8'));
  }
  return {};
}

function saveProgress(path, progress) {
  writeFileSync(path, JSON.stringify(progress), 'utf-8');
}


// ── Utilities ────────────────────────────────────────────────────────────────

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function logLine(id, message, total, done) {
  const pct = ((done / total) * 100).toFixed(1);
  process.stdout.write(`\r[${done}/${total} ${pct}%] ID ${id}: ${message}\n`);
}

function parseArgs(argv) {
  const result = {};
  for (let i = 0; i < argv.length; i++) {
    if (argv[i] === '--from' && argv[i + 1]) result.from = parseInt(argv[i + 1], 10);
    if (argv[i] === '--to' && argv[i + 1]) result.to = parseInt(argv[i + 1], 10);
    if (argv[i] === '--delay' && argv[i + 1]) result.delay = parseInt(argv[i + 1], 10);
    if (argv[i] === '--concurrency' && argv[i + 1]) result.concurrency = parseInt(argv[i + 1], 10);
  }
  return result;
}
