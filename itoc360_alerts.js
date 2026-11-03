#!/usr/bin/env node
/**
 * itoc360_alerts.js — read one monitor's alerts INSIDE iTOC360.
 * =============================================================
 *
 * WHY
 *   iTOC360 never hands its alert id back to Site24x7 (store_request_id is
 *   false), so the Alert Log only holds Site24x7's own WH_<id>_<time>
 *   reference. The only proof that iTOC360 created and resolved the alert
 *   is iTOC360 itself. Its web app reads its data from
 *   api.itoc360.app/rest/v1 (alerts, alert_events) with the signed-in
 *   user's session; this script makes the same two calls.
 *
 *   The session comes from the profile that itoc360_login.js signed in
 *   once. The auth headers are taken from the page's own request and kept
 *   in memory only; nothing secret is printed or written.
 *
 * USAGE
 *   node itoc360_alerts.js --monitor-id 73970000002868180 \
 *        --since 2026-11-03T06:19:00Z --until 2026-11-03T06:40:00Z \
 *        --out itoc360_73970000002868180.json
 *
 * EXIT  0 = read OK (even if no alert was found — that is the caller's
 *       verdict), 3 = not signed in / session expired, 2 = other failure.
 */

const fs = require('fs');
const os = require('os');
const path = require('path');

function arg(name, dflt) {
  const i = process.argv.indexOf(name);
  return i > -1 ? process.argv[i + 1] : dflt;
}
const MONITOR_ID = arg('--monitor-id');
const SINCE = arg('--since');
const UNTIL = arg('--until');
const OUT = arg('--out', 'itoc360_alerts.json');
const BASE = (process.env.ITOC_URL || 'https://itoc360.app').replace(/\/+$/, '');
const API = (process.env.ITOC_API_URL || 'https://api.itoc360.app').replace(/\/+$/, '');
const ACCOUNT = process.env.S247_ACCOUNT || 'default';
const PROFILE = process.env.ITOC_PROFILE_DIR
  || path.join(os.homedir(), `.s247-itoc360-profile-${ACCOUNT}`);

function finish(obj, code) {
  fs.writeFileSync(OUT, JSON.stringify(obj, null, 2));
  console.log(`  ${obj.ok ? '[OK]' : '[BLOCKED]'} ${obj.message}`);
  process.exit(code);
}

(async () => {
  if (!MONITOR_ID || !SINCE) {
    console.error('usage: --monitor-id ID --since ISO [--until ISO] [--out FILE]');
    process.exit(2);
  }
  const { chromium } = require('playwright');
  let channel = 'chromium';
  try {
    channel = fs.readFileSync(path.join(PROFILE, '.itoc360_channel'), 'utf8').trim() || channel;
  } catch (e) { /* default */ }

  const base = { ok: false, monitor_id: MONITOR_ID, since: SINCE, until: UNTIL,
                 read_at: new Date().toISOString(), alerts: [] };
  // JENKINS: no browser profile there. Its own iTOC360 session is a saved
  // browser state (itsm.py --itoc360-jenkins-login, uploaded as the
  // S247_<ACCT>_ITOC_STATE credential and seeded into ITOC_STATE_DIR). The
  // refreshed state is written back after every read, so the session lives on.
  const STATE_FILE = process.env.ITOC_STATE_DIR
    ? path.join(process.env.ITOC_STATE_DIR, `state-${ACCOUNT}.json`) : null;
  if (STATE_FILE && !fs.existsSync(STATE_FILE)) {
    finish({ ...base, reason: 'no_session',
             message: `no iTOC360 session for Jenkins. On the Mac run: python3 itsm.py `
                      + `-a ${ACCOUNT} --itoc360-jenkins-login, then python3 jenkins_secrets.py` }, 3);
  }
  if (!STATE_FILE && !fs.existsSync(PROFILE)) {
    finish({ ...base, reason: 'no_session',
             message: `iTOC360 is not signed in on this machine. Run once: `
                      + `python3 itsm.py -a ${ACCOUNT} --itoc360-login` }, 3);
  }

  let context, browser = null;
  const launchOpts = {
    headless: true, channel: STATE_FILE ? 'chromium' : channel,
    ignoreDefaultArgs: ['--enable-automation'],
    args: ['--no-first-run', '--no-default-browser-check',
           '--disable-blink-features=AutomationControlled'],
  };
  try {
    if (STATE_FILE) {
      browser = await chromium.launch(launchOpts);
      context = await browser.newContext({ storageState: STATE_FILE });
    } else {
      context = await chromium.launchPersistentContext(PROFILE, launchOpts);
    }
  } catch (e) {
    finish({ ...base, reason: 'browser',
             message: 'browser could not start: ' + String(e.message).split('\n')[0] }, 2);
  }
  const page = context.pages()[0] || await context.newPage();
  page.setDefaultTimeout(90000);

  // The page's own call to the data API carries the session headers.
  let headers = null;
  page.on('request', (req) => {
    if (!headers && req.url().startsWith(API + '/rest/v1/')) {
      const h = req.headers();
      if (h.authorization && h.apikey) {
        headers = { authorization: h.authorization, apikey: h.apikey,
                    accept: 'application/json' };
      }
    }
  });

  try {
    await page.goto(BASE + '/alerts', { waitUntil: 'networkidle' });
    if (/sign-in/.test(page.url())) {
      await context.close();
      finish({ ...base, reason: 'session_expired',
               message: `iTOC360 session expired. Sign in again: `
                        + `python3 itsm.py -a ${ACCOUNT} --itoc360-login` }, 3);
    }
    // Opening any alert makes the page call the API if the list did not.
    for (let i = 0; i < 10 && !headers; i++) await page.waitForTimeout(1000);
    if (!headers) {
      // The list is rendered on iTOC360's server (no data call). Opening one
      // alert's detail panel does call the API, so open the first alert.
      const html = await page.content();
      const m = html.match(/\\?"id\\?":\\?"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\\?",\\?"status/);
      if (m) {
        await page.goto(`${BASE}/alerts?alert=${m[1]}`, { waitUntil: 'networkidle' });
        for (let i = 0; i < 10 && !headers; i++) await page.waitForTimeout(1000);
      }
    }
    if (!headers) {
      await context.close();
      finish({ ...base, reason: 'no_api_call',
               message: 'signed in, but the iTOC360 page made no data call to '
                        + 'read the session from' }, 2);
    }

    const get = async (q) => {
      const r = await context.request.get(`${API}/rest/v1/${q}`, { headers });
      if (r.status() !== 200) {
        throw new Error(`HTTP ${r.status()} for ${q.split('?')[0]}`);
      }
      return r.json();
    };
    const filt = [`metadata->>MONITOR_ID=eq.${encodeURIComponent(MONITOR_ID)}`,
                  `created_at=gte.${encodeURIComponent(SINCE)}`];
    if (UNTIL) filt.push(`created_at=lte.${encodeURIComponent(UNTIL)}`);
    const alerts = await get('alerts?select=id,status,priority,fingerprint,metadata,'
      + 'created_at,resolved_at&' + filt.join('&') + '&order=created_at.asc&limit=50');

    const out = [];
    for (const a of alerts) {
      const ev = await get(`alert_events?select=id,event:events(type,created_at,payload)`
        + `&alert_id=eq.${a.id}&order=created_at.asc&limit=100`);
      const md = a.metadata || {};
      out.push({
        id: a.id, status: a.status, priority: a.priority,
        created_at: a.created_at, resolved_at: a.resolved_at,
        monitor_id: md.MONITOR_ID, monitor_name: md.MONITORNAME,
        event_id: md.EVENT_ID || null, attribute: md.ATTRIBUTE_NAME || null,
        events: ev.map((x) => ({
          type: x.event && x.event.type,
          status: x.event && x.event.payload && x.event.payload.STATUS,
          created_at: x.event && x.event.created_at,
          alert_time_iso: x.event && x.event.payload && x.event.payload.ALERT_TIME_ISO,
          event_id: x.event && x.event.payload && x.event.payload.EVENT_ID || null,
        })),
      });
    }
    if (STATE_FILE) {
      await context.storageState({ path: STATE_FILE });   // keep the refreshed session
      fs.chmodSync(STATE_FILE, 0o600);
    }
    await context.close();
    if (browser) await browser.close();
    finish({ ...base, ok: true, alerts: out,
             message: `${out.length} iTOC360 alert(s) for monitor ${MONITOR_ID} `
                      + `since ${SINCE}` }, 0);
  } catch (err) {
    await context.close().catch(() => {});
    finish({ ...base, reason: 'error',
             message: 'could not read iTOC360: ' + String((err && err.message) || err).split('\n')[0] }, 2);
  }
})();
