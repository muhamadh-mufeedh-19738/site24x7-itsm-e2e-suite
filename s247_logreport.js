#!/usr/bin/env node
/**
 * s247_logreport.js — the monitor's OWN record of when it changed state.
 * ======================================================================
 *
 * WHY THIS EXISTS
 *   Until now the cycle's transition times came from the runner polling
 *   /api/current_status every 20 seconds. "DOWN at 15:36:57" therefore
 *   meant "noticed DOWN somewhere in the preceding 20s". That is not good
 *   enough to prove a specific ticket came from a specific state change.
 *
 *   The Log Report is the product's own poll-by-poll record. Every poll
 *   has a status, so a transition is simply the first row whose status
 *   differs from the one before it -- an exact time, from Site24x7, not
 *   an observation of it.
 *
 *   That time is the ANCHOR:
 *       monitor changes at T
 *         -> ticket created in the ITSM tool at ~T+30..120s
 *           -> Alert Log receipt written after that
 *
 * NO GUESSED URLS
 *   The endpoint is captured from the page itself, the same way the
 *   integration list was. Guessing paths produced 404s and wasted hours.
 *
 * USAGE (normally via itsm.py)
 *   cd accounts/<name>
 *   set -a; . ./account.env; set +a
 *   node ~/Documents/qg/s247_logreport.js <monitor_id> [--hours 2] [--headed]
 *
 * OUTPUT
 *   logreport_<monitor_id>.json
 *     { monitor_id, captured_at, endpoints[], rows[], transitions[], raw[] }
 *   raw[] keeps the untouched payloads, so if the parsing is wrong the
 *   real field names are visible instead of being guessed at.
 */

const fs = require('fs');
const os = require('os');
const path = require('path');

const args = process.argv.slice(2);
const HEADED = args.includes('--headed');
const MONITORS = args.filter(a => /^\d+$/.test(a) && a.length > 6);
const hoursArg = args.indexOf('--hours');
const HOURS = hoursArg >= 0 ? parseInt(args[hoursArg + 1], 10) || 2 : 2;

const PROFILE = process.env.S247_PROFILE_DIR
  || path.join(os.homedir(), '.s247-automation-profile');

function die(msg) {
  console.error('\n[BLOCKER] ' + msg);
  process.exit(2);
}

if (!MONITORS.length) {
  die('give one or more monitor ids:\n'
      + '            node ~/Documents/qg/s247_logreport.js <id> [<id> ...]');
}

const OUT_DIR = process.env.S247_LOGREPORT_DIR || process.cwd();

// Site24x7 status codes, confirmed against the UI.
const STATUS = { 0: 'DOWN', 1: 'UP', 2: 'TROUBLE', 3: 'CRITICAL',
                 5: 'SUSPENDED', 7: 'MAINTENANCE', 9: 'DISCOVERING',
                 10: 'CONFIG ERROR' };

const TIME_KEYS = ['collection_time', 'collectiontime', 'time', 'timestamp',
                   '_zl_timestamp', 'polled_time', 'collected_time', 'date'];
const STATUS_KEYS = ['status', 'availability', 'state', 'monitor_status'];

function firstKey(obj, keys) {
  for (const k of keys) {
    if (obj && obj[k] !== undefined && obj[k] !== null && obj[k] !== '') {
      return obj[k];
    }
  }
  return null;
}

function statusText(v) {
  if (v === null || v === undefined) return null;
  const s = String(v).trim();
  if (/^\d+$/.test(s)) return STATUS[parseInt(s, 10)] || `code ${s}`;
  return s.toUpperCase();
}

/** Pull poll rows out of any shape of payload. */
function harvestRows(node, rows, depth) {
  if (depth > 8 || node === null || typeof node !== 'object') return;
  if (Array.isArray(node)) {
    for (const item of node) harvestRows(item, rows, depth + 1);
    return;
  }
  const t = firstKey(node, TIME_KEYS);
  const st = firstKey(node, STATUS_KEYS);
  // A poll row needs BOTH a time and a status. Anything else is page
  // furniture and must not be mistaken for data.
  if (t !== null && st !== null
      && typeof t !== 'object' && typeof st !== 'object') {
    rows.push({ time_raw: t, status_raw: st, status: statusText(st) });
    return;
  }
  for (const k of Object.keys(node)) harvestRows(node[k], rows, depth + 1);
}

function toMillis(v) {
  const s = String(v).trim();
  if (/^\d+$/.test(s)) {
    const n = parseInt(s, 10);
    return n > 1e11 ? n : n * 1000;
  }
  const d = Date.parse(s.replace(/ /, 'T'));
  return Number.isNaN(d) ? null : d;
}

(async () => {
  let chromium;
  try {
    ({ chromium } = require('playwright'));
  } catch (e) {
    die('cannot load playwright. Run from ~/Documents/qg.');
  }

  const grid = (process.env.S247_GRID_URL || '').replace(/\/+$/, '');
  if (!grid) die('S247_GRID_URL is not set. Run: source account.env');

  console.log('Log Report — exact status-change times');
  console.log('  grid     : ' + grid);
  console.log('  monitors : ' + MONITORS.length + '  (' +
    MONITORS.join(', ') + ')');
  console.log('  window   : last ' + HOURS + 'h');

  const context = await chromium.launchPersistentContext(PROFILE, {
    headless: !HEADED,
    channel: 'chrome',
    ignoreHTTPSErrors: true,
    args: ['--no-first-run', '--no-default-browser-check'],
  });
  const page = context.pages()[0] || await context.newPage();
  page.setDefaultTimeout(90000);

  let rows = [];
  let endpoints = new Set();
  let raw = [];

  page.on('response', async (res) => {
    const url = res.url();
    if (!/log|report|poll|availab/i.test(url)) return;
    if (res.status() !== 200) return;
    let body;
    try {
      body = await res.json();
    } catch (e) {
      return;
    }
    const before = rows.length;
    harvestRows(body, rows, 0);
    if (rows.length > before) {
      endpoints.add(url.split('?')[0]);
      if (raw.length < 2) raw.push({ url, body });
    }
  });

  let anyOk = false;
  const failures = [];

  for (const MONITOR of MONITORS) {
  const OUT = process.env.S247_LOGREPORT_FILE && MONITORS.length === 1
    ? process.env.S247_LOGREPORT_FILE
    : path.join(OUT_DIR, `logreport_${MONITOR}.json`);
  rows = [];
  endpoints = new Set();
  raw = [];
  try {
    console.log(`\n  ── ${MONITOR} ──────────────────────────────────`);
    console.log('  opening the Log Report tab...');
    await page.goto(
      `${grid}/app/client?a=f#/web/URL/${MONITOR}/LogReport`,
      { waitUntil: 'domcontentloaded' });
    await page.waitForTimeout(10000);
    if (rows.length === 0) {
      console.log('  nothing captured yet, reloading...');
      await page.reload({ waitUntil: 'domcontentloaded' }).catch(() => {});
      await page.waitForTimeout(10000);
    }

    if (rows.length === 0) {
      const shot = path.join(os.tmpdir(),
                             `s247_logreport_fail_${MONITOR}.png`);
      await page.screenshot({ path: shot, fullPage: true }).catch(() => {});
      console.log(`  [!! ] no poll rows for ${MONITOR}. Screenshot: ${shot}`);
      failures.push(MONITOR);
      continue;
    }

    // oldest first, so a transition is "differs from the row before it"
    rows.forEach(r => { r.time_ms = toMillis(r.time_raw); });
    const ordered = rows.filter(r => r.time_ms)
      .sort((a, b) => a.time_ms - b.time_ms);

    const transitions = [];
    for (let i = 1; i < ordered.length; i++) {
      const prev = ordered[i - 1];
      const cur = ordered[i];
      if (prev.status && cur.status && prev.status !== cur.status) {
        transitions.push({
          from: prev.status,
          to: cur.status,
          at_raw: cur.time_raw,
          at_utc: new Date(cur.time_ms).toISOString(),
          at_ms: cur.time_ms,
        });
      }
    }

    fs.writeFileSync(OUT, JSON.stringify({
      monitor_id: MONITOR,
      captured_at: new Date().toISOString(),
      grid,
      endpoints: Array.from(endpoints),
      row_count: ordered.length,
      rows: ordered.slice(-200),
      transitions,
      raw,
    }, null, 2));

    console.log(`\n  [OK] ${ordered.length} poll row(s) captured`);
    console.log(`  [OK] ${transitions.length} status change(s) found:`);
    for (const t of transitions.slice(-12)) {
      console.log(`      ${t.at_raw}   ${t.from} -> ${t.to}`);
    }
    if (!transitions.length) {
      console.log('      (none — the monitor held one status for the whole '
        + 'window)');
    }
    console.log('\n  captured from: ' + Array.from(endpoints).join(', '));
    console.log('  wrote ' + OUT);
    anyOk = true;
  } catch (err) {
    console.log(`  [!! ] ${MONITOR} failed: `
                + ((err && err.message) || err));
    failures.push(MONITOR);
  }
  }

  console.log('\n  These are each monitor\'s OWN change times. A ticket for');
  console.log('  a change must be created AFTER the time shown, typically');
  console.log('  within 30-120s.');
  if (failures.length) {
    console.log(`\n  [!! ] ${failures.length} monitor(s) produced nothing: `
                + failures.join(', '));
  }
  await context.close().catch(() => {});
  process.exit(anyOk ? 0 : 2);
})();
