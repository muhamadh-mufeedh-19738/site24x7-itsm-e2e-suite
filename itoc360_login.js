#!/usr/bin/env node
/**
 * itoc360_login.js — sign in to iTOC360 ONCE, so the suite can read alerts.
 * ========================================================================
 *
 * WHY
 *   iTOC360 does not return its alert id to Site24x7, and the integration's
 *   source token can only SEND events. To confirm an alert inside iTOC360
 *   the suite has to read the iTOC360 web app as a signed-in user. The
 *   sign-in page has a Cloudflare human check, so a person signs in once
 *   and the session is kept in a dedicated profile folder (same idea as
 *   s247_login.js). Your normal browser is never touched.
 *
 * USAGE
 *   python3 itsm.py -a automation --itoc360-login      (recommended)
 *   or directly:
 *     node itoc360_login.js --setup     opens a window: sign in, then wait
 *     node itoc360_login.js             silent check that the session works
 *
 * PROFILE   ~/.s247-itoc360-profile-<account>   (override: ITOC_PROFILE_DIR)
 *
 * OUTPUT (both modes)
 *   reports/itoc360_probe.json  — what the Alerts page loaded (URLs, status,
 *   field names, the first record), used to build and debug the checker.
 *   No cookies, tokens or passwords are written.
 */

const fs = require('fs');
const os = require('os');
const path = require('path');

const SETUP = process.argv.includes('--setup');
const BASE = (process.env.ITOC_URL || 'https://itoc360.app').replace(/\/+$/, '');
const ACCOUNT = process.env.S247_ACCOUNT || 'default';
const PROFILE = process.env.ITOC_PROFILE_DIR
  || path.join(os.homedir(), `.s247-itoc360-profile-${ACCOUNT}`);
const OUT = path.join(process.cwd(), 'reports', 'itoc360_probe.json');

function die(msg, code = 2) {
  console.error('\n[BLOCKER] ' + msg);
  process.exit(code);
}

// Keep only the SHAPE of a response: field names and the first record.
function shape(body) {
  if (Array.isArray(body)) {
    return { type: 'array', length: body.length, first: body[0] };
  }
  if (body && typeof body === 'object') {
    const out = { type: 'object', keys: Object.keys(body) };
    for (const [k, v] of Object.entries(body)) {
      if (Array.isArray(v)) out[k] = { type: 'array', length: v.length, first: v[0] };
    }
    return out;
  }
  return { type: typeof body };
}

(async () => {
  let chromium;
  try {
    ({ chromium } = require('playwright'));
  } catch (e) {
    die('cannot load playwright (run npm install in the suite folder).');
  }

  console.log('iTOC360 sign-in');
  console.log('  address : ' + BASE);
  console.log('  profile : ' + PROFILE);

  // Cloudflare's human check on the sign-in page rejects a browser that
  // announces automation ("Verification failed" in Chrome for Testing,
  // 3 Nov 2026). So: real Google Chrome when available, without the
  // automation flag. A person still signs in by hand. The channel that
  // worked is remembered in the profile, so the silent runs reopen the
  // SAME browser and can read its saved session.
  const channelFile = path.join(PROFILE, '.itoc360_channel');
  let saved = null;
  try { saved = fs.readFileSync(channelFile, 'utf8').trim(); } catch (e) { /* first run */ }
  const channels = process.env.ITOC_BROWSER_CHANNEL
    ? [process.env.ITOC_BROWSER_CHANNEL]
    : (saved ? [saved] : ['chrome', 'chromium']);
  let context = null, channel = null, lastErr = null;
  for (const ch of channels) {
    try {
      context = await chromium.launchPersistentContext(PROFILE, {
        headless: !SETUP,
        channel: ch,
        ignoreDefaultArgs: ['--enable-automation'],
        args: ['--no-first-run', '--no-default-browser-check',
               '--disable-blink-features=AutomationControlled'],
      });
      channel = ch;
      break;
    } catch (e) {
      lastErr = e;
      console.log(`  (browser '${ch}' could not start, trying the next one)`);
    }
  }
  if (!context) die('no browser could start: ' + ((lastErr && lastErr.message) || lastErr).split('\n')[0]);
  console.log('  browser : ' + channel);
  await context.addInitScript(() => {
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
  });
  const page = context.pages()[0] || await context.newPage();
  page.setDefaultTimeout(90000);

  const seen = [];
  page.on('response', async (res) => {
    const url = res.url();
    if (/\.(js|css|png|svg|ico|woff2?)(\?|$)/.test(url)) return;
    if (/cloudflare|monitoring\?|_next\/static/.test(url)) return;
    const ct = res.headers()['content-type'] || '';
    const rec = { url: url.split('?')[0], query: (url.split('?')[1] || '').slice(0, 200),
                  method: res.request().method(), status: res.status(), type: ct };
    if (/json/.test(ct)) {
      try { rec.shape = shape(await res.json()); } catch (e) { /* not JSON */ }
    } else if (/x-component|text\/plain/.test(ct)) {
      try { rec.sample = (await res.text()).slice(0, 1500); } catch (e) { /* ignore */ }
    }
    seen.push(rec);
  });

  try {
    await page.goto(BASE + '/alerts', { waitUntil: 'domcontentloaded' });
    await page.waitForTimeout(6000);

    if (/sign-in/.test(page.url())) {
      if (!SETUP) {
        await context.close();
        die('not signed in to iTOC360 (or the session expired). Run once:\n'
            + '            python3 itsm.py -a ' + ACCOUNT + ' --itoc360-login');
      }
      if (process.env.ITOC_USER) {
        await page.fill('#email', process.env.ITOC_USER).catch(() => {});
      }
      if (process.env.ITOC_PASSWORD) {
        await page.fill('#password', process.env.ITOC_PASSWORD).catch(() => {});
      }
      console.log('\n  A window is open. Please SIGN IN to iTOC360 there');
      console.log('  (complete the human check if it asks). Waiting up to 5 minutes...');
      console.log('  If the check still says "Verification failed", use the');
      console.log('  Google / Microsoft / Slack button instead of the password.');
      await page.waitForURL(u => !/sign-in|sign-up|forgot/.test(String(u)),
                            { timeout: 300000 })
        .catch(() => die('sign-in was not completed within 5 minutes.'));
      await page.goto(BASE + '/alerts', { waitUntil: 'domcontentloaded' });
    }

    // Let the Alerts page load its data, then open the first alert so its
    // payload / events request is captured too.
    await page.waitForTimeout(8000);
    const firstRow = page.locator('table tbody tr').first();
    if (await firstRow.count()) {
      await firstRow.click().catch(() => {});
      await page.waitForTimeout(6000);
    }

    try { fs.writeFileSync(channelFile, channel); } catch (e) { /* not fatal */ }
    // --export-state FILE: save this signed-in session as a browser state for
    // Jenkins (git-ignored; uploaded by jenkins_secrets.py).
    const ei = process.argv.indexOf('--export-state');
    if (ei > -1 && process.argv[ei + 1]) {
      await context.storageState({ path: process.argv[ei + 1] });
      fs.chmodSync(process.argv[ei + 1], 0o600);
      console.log(`  [OK] Jenkins session saved: ${process.argv[ei + 1]}`);
    }
    fs.mkdirSync(path.dirname(OUT), { recursive: true });
    fs.writeFileSync(OUT, JSON.stringify({
      captured_at: new Date().toISOString(), base: BASE, page_url: page.url(),
      responses: seen,
    }, null, 2));
    console.log(`\n  [OK] signed in — iTOC360 Alerts page is readable.`);
    console.log(`  probe: ${OUT}  (${seen.length} responses, no secrets)`);
    await context.close();
    process.exit(0);
  } catch (err) {
    await context.close().catch(() => {});
    die('unexpected failure: ' + ((err && err.message) || err));
  }
})();
