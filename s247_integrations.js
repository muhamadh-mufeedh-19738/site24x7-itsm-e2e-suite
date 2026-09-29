#!/usr/bin/env node
/**
 * s247_integrations.js — the LIVE third-party integration list.
 * =============================================================
 *
 * WHY
 *   Stage 3 learns integration names from the Alert Logs. Those logs are
 *   history: an integration you deleted last week still has rows, so it
 *   still appears in the report as though it were live. That is
 *   misleading, and a report that lists integrations which no longer
 *   exist cannot be trusted.
 *
 *   There is no working REST endpoint for this list on this build (the
 *   documented paths return 404). So instead of guessing URLs, this opens
 *   the Third-Party Integrations page in the already-logged-in profile and
 *   captures whatever the page itself calls. No guessing, no assumptions.
 *
 * OUTPUT
 *   integrations.json in the current folder:
 *     { "captured_at": "...", "endpoint": "...",
 *       "integrations": [ {"name": "...", "status": "...", "type": "..."} ] }
 *
 * USAGE (normally via itsm.py --integrations)
 *   cd accounts/<name>
 *   set -a; . ./account.env; set +a
 *   node ~/Documents/qg/s247_integrations.js
 *
 *   add --headed to watch it.
 */

const fs = require('fs');
const os = require('os');
const path = require('path');

const HEADED = process.argv.includes('--headed');
const PROFILE = process.env.S247_PROFILE_DIR
  || path.join(os.homedir(), '.s247-automation-profile');
const OUT = process.env.S247_INTEGRATIONS_FILE
  || path.join(process.cwd(), 'integrations.json');

function die(msg) {
  console.error('\n[BLOCKER] ' + msg);
  process.exit(2);
}

// Keys that tend to hold an integration's display name / state.
const NAME_KEYS = ['integration_name', 'name', 'display_name', 'title'];
const STATUS_KEYS = ['status', 'state', 'enabled', 'is_active'];
const TYPE_KEYS = ['third_party_app', 'type', 'service_name', 'app_name',
                   'integration_type'];

function firstKey(obj, keys, opts) {
  const wantText = opts && opts.text;
  for (const k of keys) {
    const v = obj ? obj[k] : undefined;
    if (v === undefined || v === null || v === '') continue;
    // A numeric "type" is an internal id, not an app name. Showing 20 in
    // an APP column is worse than showing nothing -- it looks like data.
    if (wantText) {
      if (typeof v === 'number') continue;
      if (typeof v === 'string' && /^\d+$/.test(v.trim())) continue;
      if (typeof v !== 'string') continue;
    }
    return v;
  }
  return null;
}

function harvest(node, found, depth) {
  if (depth > 8 || node === null || typeof node !== 'object') return;
  if (Array.isArray(node)) {
    for (const item of node) harvest(item, found, depth + 1);
    return;
  }
  const name = firstKey(node, NAME_KEYS, { text: true });
  // Two different questions, two different rules:
  //   anyType  -> "is this record an integration at all?"  (a numeric id
  //               counts perfectly well as evidence)
  //   type     -> "what do we PRINT in the App column?"    (never a number)
  // Conflating these is what made the capture return nothing.
  const anyType = firstKey(node, TYPE_KEYS);
  const type = firstKey(node, TYPE_KEYS, { text: true });
  // An integration record has a name AND looks like an integration --
  // a bare {name: ...} could be anything on the page.
  if (name && typeof name === 'string'
      && (anyType !== null || node.integration_id || node.integrationid
          || node.third_party_id || node.integration_key)) {
    found.set(String(name), {
      name: String(name),
      type: type ? String(type) : null,
      // The numeric app id, kept separately. Site24x7's own UI shows a bare
      // "43" for HALO, so the id is all the API gives us for some apps.
      type_id: (anyType !== null && anyType !== undefined)
        ? String(anyType) : null,
      status: (() => {
        // service_status from Site24x7 raw API:
        //   0 = Active, 1 = Suspended
        // Also check the generic status/enabled fields.
        const svcSt = node.service_status;
        if (svcSt === 1 || svcSt === '1') return 'Suspended';
        const v = firstKey(node, STATUS_KEYS);
        if (v === null || v === undefined) return 'Active'; // default
        if (typeof v === 'boolean') return v ? 'Active' : 'Suspended';
        const t = String(v).trim();
        if (t === '1' || /^true$/i.test(t)) return 'Active';
        if (t === '0' || /^false$/i.test(t)) return 'Suspended';
        if (/^suspend/i.test(t)) return 'Suspended';
        return t;
      })(),
      // Keep the raw record. When a column comes out wrong we can see the
      // real field names instead of guessing at them.
      raw: node,
    });
  }
  for (const k of Object.keys(node)) harvest(node[k], found, depth + 1);
}

(async () => {
  let chromium;
  try {
    ({ chromium } = require('playwright'));
  } catch (e) {
    die('cannot load playwright. Run from ~/Documents/qg.');
  }

  const grid = (process.env.S247_GRID_URL || '').replace(/\/+$/, '');
  if (!grid) die('S247_GRID_URL is not set.');

  console.log('Third-party integrations — live list');
  console.log('  grid    : ' + grid);
  console.log('  profile : ' + PROFILE);

  const context = await chromium.launchPersistentContext(PROFILE, {
    headless: !HEADED,
    channel: 'chrome',
    ignoreHTTPSErrors: true,
    args: ['--no-first-run', '--no-default-browser-check'],
  });
  const page = context.pages()[0] || await context.newPage();
  page.setDefaultTimeout(90000);

  const found = new Map();
  const endpoints = new Set();

  page.on('response', async (res) => {
    const url = res.url();
    if (!/third|integration|thirdparty/i.test(url)) return;
    if (res.status() !== 200) return;
    let body;
    try {
      body = await res.json();
    } catch (e) {
      return;                       // not JSON, ignore
    }
    const before = found.size;
    harvest(body, found, 0);
    if (found.size > before) endpoints.add(url.split('?')[0]);
  });

  try {
    console.log('\n  opening the Third-Party Integrations page...');
    await page.goto(
      grid + '/app/client#/admin/third-party-integration/thirdparty',
      { waitUntil: 'domcontentloaded' });

    // The page loads its data asynchronously; give it room, and nudge it
    // with a reload in case the hash route did not trigger a fetch.
    await page.waitForTimeout(9000);
    if (found.size === 0) {
      console.log('  nothing captured yet, reloading...');
      await page.reload({ waitUntil: 'domcontentloaded' }).catch(() => {});
      await page.waitForTimeout(9000);
    }

    if (found.size === 0) {
      await page.screenshot({
        path: path.join(os.tmpdir(), 's247_integrations_fail.png'),
        fullPage: true }).catch(() => {});
      await context.close();
      die('could not capture the integration list.\n'
          + '          Are you logged in for THIS account? Try:\n'
          + '            node ~/Documents/qg/s247_login.js\n'
          + '          Screenshot: '
          + path.join(os.tmpdir(), 's247_integrations_fail.png'));
    }

    const list = Array.from(found.values())
      .sort((a, b) => a.name.localeCompare(b.name));

    const noType = list.filter(i => !i.type);
    if (noType.length) {
      console.log(`\n  [note] ${noType.length} integration(s) had no usable`
        + ' app-type field. The raw record is kept in integrations.json so');
      console.log('         the right field can be identified. Classification'
        + ' falls back to the name for these.');
    }

    const capturedAt = new Date().toISOString();
    fs.writeFileSync(OUT, JSON.stringify({
      captured_at: capturedAt,
      grid,
      endpoints: Array.from(endpoints),
      integrations: list,
    }, null, 2));

    // ── Also write a human-readable plain-text list (integrations_list.txt) ──
    // This file is the "record" of every integration that was present at
    // the time of the run. It is appended (not overwritten) so every run
    // leaves a permanent audit trail you can diff later.
    const listFile = path.join(path.dirname(OUT), 'integrations_list.txt');
    const header = `\n${'='.repeat(70)}\nCaptured: ${capturedAt}  |  Grid: ${grid}\n${'='.repeat(70)}\n`;
    const rows = list.map((i, idx) =>
      `  ${String(idx + 1).padStart(2, ' ')}. ${i.name.padEnd(40)} ` +
      `Status: ${(i.status || 'Unknown').padEnd(10)} ` +
      `Type-ID: ${(i.type_id || '?').padEnd(4)} ` +
      (i.type ? `App: ${i.type}` : '(app name not resolved)')
    ).join('\n');
    const summary = `\nTotal: ${list.length} integration(s)\n`;
    fs.appendFileSync(listFile, header + rows + summary, 'utf8');

    console.log(`\n  [OK] ${list.length} integration(s) live in this account:`);
    console.log(`\n  ${'#'.padEnd(3)} ${'NAME'.padEnd(40)} ${'STATUS'.padEnd(12)} TYPE`);
    console.log(`  ${'-'.repeat(75)}`);
    for (const [idx, i] of list.entries()) {
      const status = i.status || 'Unknown';
      const statusIcon = /suspend/i.test(status) ? '⏸ ' : /active|ok/i.test(status) ? '✅' : '❓';
      console.log(`  ${String(idx + 1).padStart(2, ' ')}. ${i.name.padEnd(40)} ${statusIcon} ${status.padEnd(10)} ${i.type || '(type-id: ' + (i.type_id || '?') + ')'}`);
    }
    console.log(`\n  captured from: ${Array.from(endpoints).join(', ')}`);
    console.log(`  wrote: ${OUT}`);
    console.log(`  audit: ${listFile}`);
    console.log('\n  Reports will now EXCLUDE integrations that are not in');
    console.log('  this list, so deleted ones stop appearing as live.');

    await context.close();
    process.exit(0);
  } catch (err) {
    await context.close().catch(() => {});
    die('unexpected failure: ' + ((err && err.message) || err));
  }
})();
