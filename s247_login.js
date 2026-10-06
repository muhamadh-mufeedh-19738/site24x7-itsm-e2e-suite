#!/usr/bin/env node
/**
 * s247_login.js — mint a Site24x7 session cookie using YOUR Chrome.
 * =================================================================
 *
 * WHY THIS VERSION
 *   The grid requires a CLIENT CERTIFICATE. A blank Playwright browser has
 *   no certificate and is refused before any page loads
 *   (ERR_BAD_SSL_CLIENT_AUTH_CERT). Real Chrome reads the certificate from
 *   the system store, so we drive real Chrome instead.
 *
 *   It uses a DEDICATED profile folder, so your normal Chrome is never
 *   touched and does not need to be closed.
 *
 * HOW IT WORKS
 *   FIRST TIME   node s247_login.js --setup
 *                A Chrome window opens. Pick the certificate if asked, log
 *                in, and wait. The profile remembers it.
 *
 *   EVERY RUN    node s247_login.js
 *                Silent. Reuses the profile, harvests a fresh cookie,
 *                writes .session.env. No typing, no DevTools, no cURL.
 *
 *   When the saved login eventually expires it says so and tells you to
 *   run --setup once more.
 *
 * PROFILE   ~/.s247-automation-profile   (override with S247_PROFILE_DIR)
 * OUTPUT    ~/itsm-automation/.session.env
 *
 * Prints no password and no cookie value.
 */

const fs = require('fs');
const os = require('os');
const path = require('path');

const SETUP = process.argv.includes('--setup');
const HEADED = SETUP || process.argv.includes('--headed');
const PROFILE = process.env.S247_PROFILE_DIR
  || path.join(os.homedir(), '.s247-automation-profile');
const OUT = process.env.S247_SESSION_FILE
  || path.join(os.homedir(), 'itsm-automation', '.session.env');
const SHOT = path.join(os.tmpdir(), 's247_login_failure.png');

function die(msg, extra) {
  console.error('\n[BLOCKER] ' + msg);
  if (extra) console.error('          ' + extra);
  process.exit(2);
}

(async () => {
  let chromium;
  try {
    ({ chromium } = require('playwright'));
  } catch (e) {
    die('cannot load playwright.',
        'Run it from where node_modules lives: node ~/Documents/qg/s247_login.js');
  }

  const grid = (process.env.S247_GRID_URL || '').replace(/\/+$/, '');
  if (!grid) die('S247_GRID_URL is not set.', 'Run: source env.big.sh');

  fs.mkdirSync(PROFILE, { recursive: true });

  console.log('Site24x7 session cookie');
  console.log('  grid    : ' + grid);
  console.log('  profile : ' + PROFILE);
  console.log('  mode    : ' + (SETUP ? 'SETUP (one-time, visible)'
                                      : (HEADED ? 'visible' : 'silent')));

  let context;
  try {
    context = await chromium.launchPersistentContext(PROFILE, {
      headless: !HEADED,
      channel: process.env.S247_BROWSER_CHANNEL || 'chrome',            // real Chrome -> real certificate store
      ignoreHTTPSErrors: true,
      args: ['--no-first-run', '--no-default-browser-check'],
    });
  } catch (e) {
    const m = (e && e.message) || String(e);
    if (/channel|executable|chrome/i.test(m)) {
      console.error('\n[BLOCKER] could not launch your installed Chrome.');
      console.error('          ' + m.split('\n')[0]);
      console.error('\n          If Chrome is not installed, try Brave/Chromium by');
      console.error('          setting the path, e.g.:');
      console.error('            export S247_BROWSER=/usr/bin/brave-browser');
      console.error('          then re-run.');
      process.exit(2);
    }
    die('failed to launch the browser: ' + m);
  }

  const page = context.pages()[0] || await context.newPage();
  page.setDefaultTimeout(120000);

  function stamp(d) {
    const p = n => String(n).padStart(2, '0');
    return `${p(d.getDate())}-${p(d.getMonth() + 1)}-${d.getFullYear()} `
         + `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
  }

  /**
   * The REAL test. A cookie named s247cname exists before you log in, so
   * its presence proves nothing -- that false pass is exactly what made
   * earlier versions of this script claim success on an empty session.
   * Instead, call the same Alert Logs endpoint stage 3 uses and insist on
   * a 200. Nothing else counts as logged in.
   */
  async function sessionWorks() {
    const cs = await context.cookies();
    const cname = cs.find(c => c.name === 's247cname');
    if (!cname) return { ok: false, why: 'no s247cname cookie yet' };

    const to = new Date();
    const from = new Date(to.getTime() - 3600 * 1000);
    const enc = t => encodeURIComponent(t).replace(/%3A/g, ':');
    const q = 'logtype=%22Alert%20Logs%22%20and%20MonitorType=%22Website%22';
    const url = `${grid}/app/api/applog/search/${enc(stamp(from))}/`
              + `${enc(stamp(to))}/1-100/desc?time_filter=&query=${q}`
              + `&page_type=full_page`;

    try {
      const res = await page.evaluate(async ({ u, tok }) => {
        try {
          const r = await fetch(u, {
            credentials: 'include',
            headers: { 'x-zcsrf-token': 's247pname=' + tok },
          });
          const t = await r.text();
          return { status: r.status, head: t.slice(0, 120) };
        } catch (e) { return { status: 0, head: String(e) }; }
      }, { u: url, tok: cname.value });

      if (res.status === 200) return { ok: true, why: 'Alert Logs returned 200' };
      return { ok: false, why: `Alert Logs returned ${res.status}` };
    } catch (e) {
      return { ok: false, why: 'probe failed: ' + (e.message || e) };
    }
  }

  async function haveCookie() { return (await sessionWorks()).ok; }

  /**
   * Log in by filling the form, using credentials from the account file.
   *
   * The saved profile alone is not enough here: this account is shared
   * with a colleague who logs out 7-8 times a day, and every logout kills
   * the stored session server-side. Re-typing the credentials makes a
   * logout a non-event -- the next run simply logs back in.
   */
  async function formLogin() {
    const user = process.env.S247_LOGIN_USER || '';
    const pass = process.env.S247_LOGIN_PASS || '';
    if (!user || !pass) {
      return { ok: false,
               why: 'S247_LOGIN_USER / S247_LOGIN_PASS are not set for this '
                    + 'account (add them to accounts/<name>/account.env)' };
    }

    const pick = async (sels) => {
      for (const sel of sels) {
        if (await page.locator(sel).count()) return sel;
      }
      return null;
    };

    console.log('  not logged in — signing in automatically...');
    await page.goto(grid, { waitUntil: 'domcontentloaded' }).catch(() => {});
    await page.waitForTimeout(2500);

    const emailSel = await pick(['#login_id', 'input[name="LOGIN_ID"]',
                                 'input[type="email"]']);
    if (!emailSel) {
      return { ok: false, why: 'no email field found on the sign-in page' };
    }
    await page.fill(emailSel, user);
    const next1 = await pick(['#nextbtn', 'button#nextbtn',
                              'button[type="submit"]']);
    if (next1) await page.click(next1);
    await page.waitForTimeout(3000);

    const passSel = await pick(['#password', 'input[name="PASSWORD"]',
                                'input[type="password"]']);
    if (!passSel) {
      const err = await page.locator('.errorMsg, #errorMsg, .error')
        .first().textContent().catch(() => null);
      return { ok: false,
               why: 'the password field never appeared'
                    + (err && err.trim() ? ' — page said: ' + err.trim()
                                         : ' (was the email rejected?)') };
    }
    await page.fill(passSel, pass);
    const next2 = await pick(['#nextbtn', 'button#nextbtn',
                              'button[type="submit"]']);
    if (next2) await page.click(next2);

    console.log('  submitted, waiting for the session...');

    /**
     * After the password, Zoho sometimes shows an extra page before the app
     * opens -- e.g. "Secure your account using MFA" (set up OneAuth) with a
     * "Skip" link, or a "Remind me later" / "Not now" prompt. A person just
     * clicks Skip; this does the same. Only skip/later-type links are ever
     * clicked -- it never enrols MFA or changes account settings.
     */
    async function skipInterstitials() {
      const body = await page.locator('body').innerText().catch(() => '');
      if (!/secure your account|multi-factor|mfa|remind me|not now|skip/i.test(body)) return false;
      const labels = [/^\s*skip\b/i, /remind me/i, /skip for now/i, /not now/i,
                      /i'?ll do it later/i, /^\s*later\s*$/i];
      let clicked = false;
      for (const re of labels) {
        const el = page.getByText(re).first();
        if (await el.isVisible().catch(() => false)) {
          await el.click().catch(() => {});
          console.log('  extra sign-in page (MFA / reminder) -- clicked "' + ((await el.textContent().catch(() => '')) || '').trim() + '", like a person would');
          clicked = true;
          await page.waitForTimeout(2000);
        }
      }
      return clicked;
    }

    const deadline = Date.now() + 120000;
    while (Date.now() < deadline) {
      const r = await sessionWorks();
      if (r.ok) return { ok: true, why: r.why };
      if (await skipInterstitials()) {
        // the app may land on a different page after skipping -- reopen the grid
        await page.goto(grid + '/app/client#/home/operations/alert-logs',
                        { waitUntil: 'domcontentloaded' }).catch(() => {});
        await page.waitForTimeout(4000);
        continue;
      }
      const err = await page.locator('.errorMsg, #errorMsg, .error')
        .first().textContent().catch(() => null);
      if (err && err.trim()) {
        return { ok: false, why: 'sign-in error: ' + err.trim() };
      }
      await page.waitForTimeout(3000);
      process.stdout.write('.');
    }
    console.log('');
    return { ok: false, why: 'signed in but no usable session after 120s' };
  }

  try {
    console.log('\n  opening the grid...');
    await page.goto(grid + '/app/client#/home/operations/alert-logs',
                    { waitUntil: 'domcontentloaded' });
    await page.waitForTimeout(6000);

    if (SETUP) {
      console.log('\n  ================================================');
      console.log('  A Chrome window is open.');
      console.log('    1. Choose the client certificate if it asks.');
      console.log('    2. Log in to Site24x7.');
      console.log('    3. Leave it on any Site24x7 page.');
      console.log('  Waiting up to 5 minutes for a WORKING session...');
      console.log('  (checked against the real Alert Logs endpoint)');
      console.log('  ================================================\n');
      const deadline = Date.now() + 300000;
      let lastWhy = '';
      while (Date.now() < deadline) {
        const r = await sessionWorks();
        if (r.ok) { console.log('\n  ' + r.why); break; }
        if (r.why !== lastWhy) {
          console.log('\n  waiting — ' + r.why);
          lastWhy = r.why;
        }
        await page.waitForTimeout(4000);
        process.stdout.write('.');
      }
      console.log('');
    }

    let verdict = await sessionWorks();

    // Session gone? Sign in again rather than giving up. This is what
    // makes a colleague's logout cost nothing.
    if (!verdict.ok && !SETUP) {
      console.log('  session check: ' + verdict.why);
      const r = await formLogin();
      if (r.ok) {
        console.log('\n  ' + r.why);
        verdict = { ok: true, why: r.why };
      } else {
        console.log('\n  automatic sign-in failed: ' + r.why);
      }
    }

    if (!verdict.ok) {
      await page.screenshot({ path: SHOT, fullPage: true }).catch(() => {});
      console.error('\n  session check: ' + verdict.why);
      if (!SETUP) {
        console.error('\n[BLOCKER] could not establish a session.');
        console.error('          ' + verdict.why);
        console.error('');
        console.error('          Checks, in order:');
        console.error('            1. S247_LOGIN_USER / S247_LOGIN_PASS set');
        console.error('               for THIS account?');
        console.error('            2. password changed recently?');
        console.error('            3. if the account now asks for an OTP, a');
        console.error('               script cannot answer it — use --setup');
        console.error('          Screenshot: ' + SHOT);
        process.exit(2);
      }
      die('the session never became usable within 5 minutes. '
          + verdict.why,
          'Did the login actually complete in the Chrome window? '
          + 'Screenshot: ' + SHOT);
    }

    const cookies = (await context.cookies())
      .filter(c => (c.domain || '').includes('site24x7'));
    const header = cookies.map(c => `${c.name}=${c.value}`).join('; ');
    const cname = cookies.find(c => c.name === 's247cname');
    if (!cname) die('s247cname cookie missing, cannot build the CSRF token.');

    const body =
      '# Session credentials for /app/api/ endpoints.\n'
      + '# WRITTEN BY s247_login.js — do not hand-edit.\n'
      + '# Generated: ' + new Date().toISOString() + '\n'
      + '# NEVER commit this file.\n'
      + `export S247_SESSION_COOKIE='${header}'\n`
      + `export S247_CSRF_TOKEN='s247pname=${cname.value}'\n`;

    fs.writeFileSync(OUT, body, { mode: 0o600 });
    fs.chmodSync(OUT, 0o600);

    console.log('\n  [OK] session VERIFIED against Alert Logs (200)');
    console.log('  [OK] wrote ' + OUT);
    console.log('       ' + cookies.length + ' cookie(s), permissions 600');
    if (SETUP) {
      console.log('\n  Setup complete. From now on just run:');
      console.log('    node ~/Documents/qg/s247_login.js');
    }
    console.log('\n  Load it with:  source .session.env');
    await context.close();
    process.exit(0);

  } catch (err) {
    await page.screenshot({ path: SHOT, fullPage: true }).catch(() => {});
    await context.close().catch(() => {});
    const m = (err && err.message) || String(err);
    if (/BAD_SSL_CLIENT_AUTH_CERT/.test(m)) {
      console.error('\n[BLOCKER] the grid refused the connection: no client '
                    + 'certificate.');
      console.error('          Real Chrome should supply it. Run the one-time '
                    + 'setup so you');
      console.error('          can pick the certificate yourself:');
      console.error('            node ~/Documents/qg/s247_login.js --setup');
      process.exit(2);
    }
    die('unexpected failure: ' + m, 'Screenshot: ' + SHOT);
  }
})();
