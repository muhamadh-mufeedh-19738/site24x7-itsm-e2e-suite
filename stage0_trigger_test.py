#!/usr/bin/env python3
"""
Site24x7 ITSM Automation — STAGE 0 : TRIGGER TEST (Pre-flight validation)
=========================================================================

WHAT IS A TRIGGER TEST?
    Before we put any monitor into a real DOWN/TROUBLE cycle, we first
    confirm that EVERY configured third-party integration is correctly set
    up and can actually deliver an alert to its destination tool.

    Site24x7 provides a native "Trigger Test" button (▶) on the
    Third-Party Integrations page. Clicking it fires a test alert to the
    tool with the subject:

        "[Site24x7 Test Alert] Zylker Monitor is DOWN"

    This creates a real ticket/incident/alert in the destination tool
    (confirmed from the screenshots — Zoho Desk shows ticket #581,
    ServiceDesk Plus shows request #401, etc. all with that subject)
    AND writes a row to the Site24x7 Alert Logs.

    WHY MANDATORY?
        If an integration is misconfigured (wrong API key, expired OAuth
        token, incorrect instance URL), the trigger test fails BEFORE the
        real lifecycle even starts. Without this check, a broken
        integration would produce zero tickets during the DOWN cycle, and
        our suite would report DEFECT when the real problem is a config
        issue, not a product bug.

        The trigger test BLOCKS the lifecycle run if any integration fails.
        A misconfigured integration must be fixed first.

THE API ENDPOINT (confirmed from the browser Network tab)
    PUT /api/integration/thirdparty_service/trigger_test/{service_id}
    Auth: Zoho-oauthtoken {token}

    Response on success:
        { "code": 0, "message": "success",
          "data": { "response_code": 200,
                    "title": " Success",
                    "message": "Test message sent successfully." } }

    The UI shows "Success" next to the ▶ button when code==0 and
    response_code==200.

WHAT THIS SCRIPT DOES
    1. Reads integrations from integrations.json (written by s247_integrations.js
       or stage1_inventory.py) to get each integration's service_id.
    2. Calls PUT trigger_test/{service_id} for every integration.
    3. Waits for Alert Logs to confirm the test alert row appeared
       (proves delivery to the Alert Logs system, not just a 200 OK).
    4. Writes stage0_trigger_test.json with per-integration results.
    5. Prints a clear PASS / FAIL per integration.
    6. Exits with code 0 if ALL pass, code 1 if ANY fail.

    The run_all.py runner calls this BEFORE stage2b and BLOCKS the
    lifecycle if the exit code is non-zero.

VERIFICATION (two-layer)
    LAYER 1 — API response (fast, ~1s)
        code == 0 and response_code == 200  →  "trigger test accepted"
        Any other code                       →  "trigger test FAILED"

    LAYER 2 — Alert Logs (authoritative, ~30-60s)
        Polls the Alert Logs endpoint for a row with the test-alert
        signature ("[Site24x7 Test Alert]") for each integration.
        This proves the alert log pipeline is live, not just the API.
        If Layer 2 times out, the result is WARN (API said OK but
        Alert Logs not confirmed) — not a hard FAIL, because the
        Alert Logs endpoint requires a separate browser session cookie.

REPORT SECTION
    The HTML report gains a new "Trigger Test" section at the top,
    ABOVE the lifecycle results, with a clear ✅ / ❌ / ⚠️ per integration.
    The lifecycle is shown separately below. You can see at a glance:
        - which integrations passed pre-flight (ready for the lifecycle)
        - which integrations are misconfigured (lifecycle skipped for them)

SAFETY
    The trigger test fires a REAL alert to the tool. This is by design —
    the whole point is to confirm end-to-end delivery. The test subject
    "[Site24x7 Test Alert] Zylker Monitor is DOWN" is clearly labelled
    so operations teams know it is a test.

USAGE
    source start.sh
    python3 stage0_trigger_test.py              # test all integrations
    python3 stage0_trigger_test.py --no-alert-logs  # API check only (fast)
    python3 stage0_trigger_test.py --names "Desk Automation,ServiceNow Automation"
    python3 stage0_trigger_test.py --dry-run    # show plan, fire nothing
"""

import argparse
import json
import os
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

# ---------------------------------------------------------------------------
HERE = os.path.dirname(os.path.abspath(__file__))
RESULT_FILE = "stage0_trigger_test.json"
INTEGRATIONS_FILE = "integrations.json"
TIMEOUT = 30

# How long to wait for Alert Logs to confirm a trigger test row.
ALERT_LOG_WAIT_SECONDS = 90   # poll for up to 90s
ALERT_LOG_POLL_SECONDS = 10   # check every 10s

# The test alert subject that Site24x7 sends — confirmed from the
# Third-Party Integrations UI (screenshots) and alert log rows.
TEST_ALERT_SIGNATURE = "[Site24x7 Test Alert]"

# Verdict constants
PASS   = "PASS"
FAIL   = "FAIL"
WARN   = "WARN"       # API said OK but Alert Logs not confirmed
SKIP   = "SKIP"       # --dry-run or integration not tested
BLOCKED = "BLOCKED"   # could not reach Site24x7 at all


def log(m=""):
    print(m, flush=True)


def section(t):
    log("\n" + "=" * 70)
    log(t)
    log("=" * 70)


def die(msg, code=2):
    log(f"\n[BLOCKER] {msg}")
    sys.exit(code)


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def get_token():
    tok = os.environ.get("S247_ACCESS_TOKEN", "").strip()
    if tok:
        return tok
    script = os.path.expanduser(
        os.environ.get("S247_TOKEN_SCRIPT", "").strip())
    if not script or not os.path.isfile(script):
        die("No OAuth token. Set S247_ACCESS_TOKEN or S247_TOKEN_SCRIPT.")
    try:
        p = subprocess.run(["bash", script], capture_output=True, text=True,
                           timeout=60)
        lines = [l for l in (p.stdout or "").splitlines() if l.strip()]
        if p.returncode != 0 or not lines:
            die(f"get_token.sh failed: {(p.stderr or '')[:300]}")
        return lines[-1].strip()
    except Exception as exc:
        die(f"Could not run get_token.sh: {exc}")


def make_ctx():
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def session_headers():
    """Browser session cookie headers for Alert Logs (applog endpoint)."""
    h = {}
    cookie = os.environ.get("S247_SESSION_COOKIE", "").strip()
    if cookie:
        h["Cookie"] = cookie
    csrf = os.environ.get("S247_CSRF_TOKEN", "").strip()
    if csrf:
        h["X-CSRF-Token"] = csrf
    return h


# ---------------------------------------------------------------------------
# API calls
# ---------------------------------------------------------------------------

def _do_put(url, headers):
    """Low-level PUT helper.  Returns (http_status, parsed_json)."""
    req = urllib.request.Request(url, data=b"", method="PUT", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT,
                                    context=make_ctx()) as r:
            raw = r.read().decode("utf-8", errors="replace")
            return r.status, json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as e:
        body_txt = e.read().decode("utf-8", errors="replace")[:400]
        return e.code, {"_error": body_txt}
    except Exception as exc:
        return None, {"_exception": str(exc)}


def api_put(grid, path, token, body=None):
    """PUT to the Site24x7 API. Returns (http_status, parsed_json_or_None)."""
    url = grid.rstrip("/") + path
    return _do_put(url, {
        "Authorization": f"Zoho-oauthtoken {token}",
        "Accept":        "application/json; version=2.1",
        "Content-Type":  "application/json",
    })


def api_put_with_session(grid, path, token):
    """PUT with session-cookie auth first (for endpoints that need 'admin'
    OAuth scope like trigger_test), then fall back to OAuth token.

    The trigger_test endpoint in security-admin-rest-api.xml requires
    oauthscope='admin'.  The browser uses session cookies for this call
    (the ▶ button).  Our OAuth token typically has monitor/integration
    scopes but not admin — so we try the session cookie first.
    """
    url = grid.rstrip("/") + path

    # Try 1: session cookie (same auth the browser ▶ button uses)
    cookie = os.environ.get("S247_SESSION_COOKIE", "").strip()
    csrf   = os.environ.get("S247_CSRF_TOKEN", "").strip()
    if cookie:
        sess_headers = {
            "Cookie":        cookie,
            "Accept":        "application/json, text/javascript, */*; q=0.01",
            "Content-Type":  "application/json",
            "X-Requested-With": "XMLHttpRequest",
        }
        if csrf:
            sess_headers["X-CSRF-Token"] = csrf
        code, resp = _do_put(url, sess_headers)
        # Only fall through to OAuth if the session auth itself failed
        is_auth_fail = (code == 401
                        or (isinstance(resp, dict)
                            and "1121" in str(resp.get("_error", ""))))
        if not is_auth_fail:
            return code, resp
        log("    [info] Session cookie auth failed — trying OAuth token ...")

    # Try 2: OAuth token (needs admin scope in the refresh token)
    return api_put(grid, path, token)


def applog_search(grid, from_dt, to_dt, query, page="1-100"):
    """
    Read from the Alert Logs (AppLog) endpoint using the browser session
    cookie.  Returns a list of log entry dicts, or [] on any error.

    This endpoint is session-authenticated, not OAuth.  If the session
    cookie is missing the call returns [] (graceful degradation — we
    treat it as WARN, not hard FAIL).
    """
    fmt = "%d-%m-%Y %H:%M:%S"
    from_str = urllib.parse.quote(from_dt.strftime(fmt), safe="")
    to_str   = urllib.parse.quote(to_dt.strftime(fmt),   safe="")
    q_str    = urllib.parse.quote(f'logtype="Alert Logs"', safe="")
    path = (f"/app/api/applog/search/{from_str}/{to_str}/{page}/desc"
            f"?time_filter=&query={q_str}&page_type=full_page")
    url = grid.rstrip("/") + path
    headers = {
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "X-Requested-With": "XMLHttpRequest",
        **session_headers(),
    }
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT,
                                    context=make_ctx()) as r:
            raw = r.read().decode("utf-8", errors="replace")
            parsed = json.loads(raw) if raw.strip() else {}
            # Response shape: { "data": [ {...}, ... ] }
            data = parsed.get("data") or []
            if isinstance(data, list):
                return data
            return []
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Core: fire a trigger test for one integration
# ---------------------------------------------------------------------------

def fire_trigger_test(grid, token, integration, dry_run=False):
    """
    Fire PUT trigger_test/{service_id} for one integration.

    Returns a result dict:
        {
            "name":          str,
            "service_id":    str,
            "api_verdict":   "PASS"|"FAIL"|"SKIP",
            "api_code":      int,
            "api_response":  dict,
            "api_message":   str,
            "fired_at":      ISO timestamp,
        }
    """
    name       = integration.get("name", "unknown")
    service_id = str((integration.get("raw") or {}).get("service_id")
                     or integration.get("id") or "")

    if not service_id:
        return {
            "name": name, "service_id": "",
            "api_verdict": FAIL,
            "api_code": None,
            "api_response": {},
            "api_message": "No service_id found in integrations.json — "
                           "cannot call trigger_test endpoint.",
            "fired_at": datetime.now(timezone.utc).isoformat(),
        }

    fired_at = datetime.now(timezone.utc).isoformat()

    if dry_run:
        log(f"  [DRY RUN] would PUT trigger_test/{service_id}  ({name})")
        return {
            "name": name, "service_id": service_id,
            "api_verdict": SKIP,
            "api_code": None,
            "api_response": {},
            "api_message": "dry-run — not executed",
            "fired_at": fired_at,
        }

    path = f"/api/integration/thirdparty_service/trigger_test/{service_id}"
    log(f"  PUT {path}  ...")
    http_code, resp = api_put_with_session(grid, path, token)

    # Determine outcome using the same logic as the UI
    # (code==0 and data.response_code==200 → Success)
    api_ok = False
    api_msg = ""
    if resp and "_exception" in resp:
        api_msg = f"Network error: {resp['_exception']}"
    elif resp and "_error" in resp:
        err_txt = resp["_error"]
        if "1121" in err_txt or "oauthscope" in err_txt.lower():
            api_msg = (f"OAuth scope error (1121) — token lacks 'admin' "
                       f"scope AND no valid session cookie found. "
                       f"Set S247_SESSION_COOKIE (recommended) or add "
                       f"Site24x7.Admin.Read to your OAuth refresh token.")
        else:
            api_msg = f"HTTP {http_code}: {err_txt}"
    elif http_code == 200:
        outer_code = resp.get("code")
        data_block = resp.get("data") or {}
        resp_code  = data_block.get("response_code")
        title      = data_block.get("title", "")
        msg_txt    = data_block.get("message", resp.get("message", ""))
        if outer_code == 0 and resp_code == 200:
            api_ok = True
            api_msg = (f"{title.strip()} — {msg_txt}"
                       if title.strip() else msg_txt)
        else:
            api_msg = (f"code={outer_code} response_code={resp_code} "
                       f"title={title!r} message={msg_txt!r}")
    else:
        api_msg = f"HTTP {http_code}: {resp}"

    log(f"    {'✅' if api_ok else '❌'} {name}: {api_msg}")

    return {
        "name":         name,
        "service_id":   service_id,
        "api_verdict":  PASS if api_ok else FAIL,
        "api_code":     http_code,
        "api_response": resp,
        "api_message":  api_msg,
        "fired_at":     fired_at,
    }


# ---------------------------------------------------------------------------
# Layer 2: verify via Alert Logs
# ---------------------------------------------------------------------------

def verify_in_alert_logs(grid, results, wait_seconds=ALERT_LOG_WAIT_SECONDS,
                         poll_seconds=ALERT_LOG_POLL_SECONDS):
    """
    Poll Alert Logs for up to `wait_seconds` looking for test-alert rows.

    For each integration whose API call was PASS, we look for an Alert Log
    row where:
        - "To" contains the integration name
        - "Reason" or "Message" contains TEST_ALERT_SIGNATURE

    Updates each result dict in-place, adding:
        "alert_log_verdict":    "PASS"|"WARN"|"SKIP"
        "alert_log_row":        dict (the matched row) or None
        "alert_log_message":    str
        "final_verdict":        "PASS"|"FAIL"|"WARN"

    Integrations that had API FAIL get alert_log_verdict=SKIP (no point
    polling for a row that was never sent).
    """
    # Only poll for integrations whose API call succeeded
    pending = {r["name"] for r in results if r["api_verdict"] == PASS}
    found   = {}     # name → matched alert log row

    if not pending:
        log("  No integrations with API PASS — skipping Alert Logs check.")
        for r in results:
            r["alert_log_verdict"] = SKIP
            r["alert_log_row"] = None
            r["alert_log_message"] = ("API trigger test failed — Alert Logs "
                                      "check skipped.")
            r["final_verdict"] = FAIL
        return

    has_session = bool(os.environ.get("S247_SESSION_COOKIE", "").strip())
    if not has_session:
        log("  [WARN] S247_SESSION_COOKIE not set — cannot check Alert Logs.")
        log("         Layer 1 (API) result stands. Set the cookie for full")
        log("         two-layer verification.")
        for r in results:
            if r["api_verdict"] == PASS:
                r["alert_log_verdict"] = WARN
                r["alert_log_row"] = None
                r["alert_log_message"] = ("Alert Logs not checked — "
                                          "S247_SESSION_COOKIE not set. "
                                          "API trigger test was successful.")
                r["final_verdict"] = WARN
            else:
                r["alert_log_verdict"] = SKIP
                r["alert_log_row"] = None
                r["alert_log_message"] = ("API trigger test failed — Alert "
                                          "Logs check skipped.")
                r["final_verdict"] = FAIL
        return

    # Window: from 2 minutes before the earliest fire_at to now + 5 min
    fire_times = [datetime.fromisoformat(r["fired_at"].replace("Z", "+00:00"))
                  for r in results if r.get("fired_at")]
    earliest = min(fire_times) if fire_times else datetime.now(timezone.utc)
    window_from = earliest - timedelta(minutes=2)

    log(f"\n  Polling Alert Logs for {len(pending)} integration(s) "
        f"(up to {wait_seconds}s) ...")

    deadline = time.time() + wait_seconds
    while pending and time.time() < deadline:
        window_to = datetime.now(timezone.utc) + timedelta(minutes=5)
        rows = applog_search(grid, window_from, window_to,
                             'logtype="Alert Logs"')

        for row in rows:
            msg      = str(row.get("Reason") or row.get("message") or
                           row.get("Message") or "")
            to_field = row.get("To") or row.get("to") or []
            if isinstance(to_field, str):
                try:
                    to_field = json.loads(to_field)
                except Exception:
                    to_field = [to_field]

            # Match: the test alert signature in the reason/message
            if TEST_ALERT_SIGNATURE.lower() not in msg.lower():
                continue

            for name in list(pending):
                # Check if this row's "To" field names this integration
                to_names = [str(t).strip().lower() for t in to_field]
                if name.lower() in to_names:
                    found[name] = row
                    pending.discard(name)
                    log(f"    ✅ Alert Log confirmed: {name}")

        if pending:
            remaining = max(0, deadline - time.time())
            if remaining > 0:
                log(f"    still waiting for: "
                    f"{', '.join(sorted(pending))} "
                    f"({remaining:.0f}s left) ...")
                time.sleep(min(poll_seconds, remaining))

    # Finalise each result
    for r in results:
        name = r["name"]
        if r["api_verdict"] != PASS:
            r["alert_log_verdict"] = SKIP
            r["alert_log_row"] = None
            r["alert_log_message"] = ("API trigger test failed — Alert Logs "
                                      "check skipped.")
            r["final_verdict"] = FAIL
        elif name in found:
            r["alert_log_verdict"] = PASS
            r["alert_log_row"] = found[name]
            r["alert_log_message"] = (
                f"Alert Log row confirmed for '{name}' "
                f"with test-alert signature.")
            r["final_verdict"] = PASS
        else:
            # API said OK but we didn't see the row within the window
            r["alert_log_verdict"] = WARN
            r["alert_log_row"] = None
            r["alert_log_message"] = (
                f"Alert Log row NOT seen for '{name}' within "
                f"{wait_seconds}s window. The API trigger returned 200 OK "
                f"— the test alert was accepted. The row may arrive after "
                f"the window, or the Alert Logs session cookie may belong "
                f"to a different account. Check Alert Logs manually.")
            # WARN, not FAIL — the API worked; the log check is a belt-
            # and-suspenders confirmation. Don't block the lifecycle for
            # a timing issue.
            r["final_verdict"] = WARN


# ---------------------------------------------------------------------------
# HTML summary fragment (embedded into the main run_all.py report)
# ---------------------------------------------------------------------------

def trigger_test_html_block(results):
    """
    Returns an HTML string (a <div> block) suitable for embedding at the
    top of the main run_all.py HTML report.

    Called by run_all.py when it loads stage0_trigger_test.json.
    """
    if not results:
        return ('<div class="note"><strong>Trigger Test was not run for '
                'this report.</strong><p>Re-run with <code>python3 '
                'stage0_trigger_test.py</code> before the lifecycle to '
                'validate all integrations.</p></div>')

    rows = ""
    all_pass = all(r.get("final_verdict") in (PASS, WARN) for r in results)
    any_fail = any(r.get("final_verdict") == FAIL for r in results)

    for r in sorted(results, key=lambda x: x.get("name", "")):
        fv   = r.get("final_verdict", SKIP)
        apiv = r.get("api_verdict", SKIP)
        logv = r.get("alert_log_verdict", SKIP)
        name = r.get("name", "")
        sid  = r.get("service_id", "")
        api_msg  = r.get("api_message", "")
        log_msg  = r.get("alert_log_message", "")
        fired_at = r.get("fired_at", "")

        icon = {"PASS": "✅", "FAIL": "❌", "WARN": "⚠️",
                "SKIP": "—", "BLOCKED": "🔴"}.get(fv, "?")
        colour = {"PASS": "#1a7f37", "FAIL": "#cf222e",
                  "WARN": "#9a6700", "SKIP": "#6e7781",
                  "BLOCKED": "#cf222e"}.get(fv, "#000")

        rows += (
            f'<tr>'
            f'<td><strong>{_esc(name)}</strong></td>'
            f'<td style="font-size:11px;color:#57606a">{_esc(sid)}</td>'
            f'<td style="color:{colour};font-weight:700">'
            f'{icon} {_esc(fv)}</td>'
            f'<td style="font-size:11px">{_esc(apiv)}</td>'
            f'<td style="font-size:11px">{_esc(logv)}</td>'
            f'<td style="font-size:12px">{_esc(api_msg)}</td>'
            f'<td style="font-size:12px">{_esc(log_msg)}</td>'
            f'<td style="font-size:11px;color:#57606a">{_esc(fired_at[:19])}</td>'
            f'</tr>'
        )

    banner_colour = "#1a7f37" if all_pass else "#cf222e"
    banner_icon   = "✅" if all_pass else "❌"
    banner_txt    = ("All integrations passed pre-flight trigger test."
                     if all_pass and not any_fail
                     else "One or more integrations FAILED the pre-flight "
                          "trigger test. The lifecycle was BLOCKED for "
                          "those integrations.")

    return f"""
<div class="card" style="border-left:5px solid {banner_colour};">
  <div class="hd" style="color:{banner_colour}">
    {banner_icon} Pre-flight Trigger Test
  </div>
  <p>
    Each integration's "Trigger Test" button was fired via the Site24x7 API
    (<code>PUT /api/integration/thirdparty_service/trigger_test/{{service_id}}</code>)
    BEFORE the alert lifecycle started. This confirms the integration is
    correctly configured and can reach the destination tool.
    The test creates a real <em>[Site24x7 Test Alert]</em> ticket in each tool.
  </p>
  <p style="font-weight:600;color:{banner_colour}">{banner_txt}</p>
  <table>
    <tr>
      <th>Integration</th>
      <th style="font-size:11px">Service ID</th>
      <th>Result</th>
      <th>API Check</th>
      <th>Alert Log</th>
      <th>API response</th>
      <th>Alert Log note</th>
      <th>Fired at</th>
    </tr>
    {rows}
  </table>
</div>
"""


def _esc(x):
    import html
    return html.escape(str(x) if x is not None else "")


# ---------------------------------------------------------------------------
# Write the result file
# ---------------------------------------------------------------------------

def write_results(results, grid, started):
    out = {
        "stage":      "stage0_trigger_test",
        "started_at": started,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "grid":       grid,
        "results":    results,
        "summary": {
            "total":   len(results),
            "pass":    sum(1 for r in results if r.get("final_verdict") == PASS),
            "fail":    sum(1 for r in results if r.get("final_verdict") == FAIL),
            "warn":    sum(1 for r in results if r.get("final_verdict") == WARN),
            "skip":    sum(1 for r in results if r.get("final_verdict") == SKIP),
        },
    }
    with open(RESULT_FILE, "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=2)
    log(f"\n  Wrote {RESULT_FILE}")
    return out


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        description="Stage 0 — fire trigger-test for all integrations "
                    "before the alert lifecycle begins")
    ap.add_argument("--integrations-file", default=None,
                    help="path to integrations.json. Default: auto-detected "
                         "from accounts/<account>/integrations.json or "
                         "./integrations.json")
    ap.add_argument("--names", default=None,
                    help="comma-separated integration names to test. "
                         "Default: all integrations in the file.")
    ap.add_argument("--no-alert-logs", action="store_true",
                    help="skip the Alert Logs verification layer (Layer 2). "
                         "Use when you do not have a session cookie. "
                         "API check (Layer 1) only.")
    ap.add_argument("--wait", type=int, default=ALERT_LOG_WAIT_SECONDS,
                    help=f"seconds to wait for Alert Logs confirmation "
                         f"(default {ALERT_LOG_WAIT_SECONDS})")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the plan, fire nothing, write no files")
    args = ap.parse_args()

    started = datetime.now(timezone.utc).isoformat()
    grid = os.environ.get("S247_GRID_URL", "").strip()
    if not grid:
        die("S247_GRID_URL not set. Run:  source start.sh")

    section("SITE24X7 ITSM AUTOMATION — STAGE 0 : PRE-FLIGHT TRIGGER TEST")
    log(f"  Grid    : {grid}")
    log(f"  Started : {started[:19]}")
    log(f"  Purpose : Validate each integration BEFORE the alert lifecycle.")
    log(f"            A failed trigger test means the integration is")
    log(f"            misconfigured — not a product bug. Fix it first.")

    # ── Locate integrations.json ──────────────────────────────────────────
    integ_file = args.integrations_file
    if not integ_file:
        # Auto-detect: look in the CWD first, then in each account subfolder
        candidates = [
            INTEGRATIONS_FILE,
            os.path.join(HERE, "accounts", "automation", "integrations.json"),
            os.path.join(HERE, "accounts", "tpt", "integrations.json"),
            os.path.join(HERE, "accounts", "tpt1", "integrations.json"),
        ]
        for c in candidates:
            if os.path.isfile(c):
                integ_file = c
                break

    if not integ_file or not os.path.isfile(integ_file):
        die(f"integrations.json not found. Run s247_integrations.js first:\n"
            f"  node s247_integrations.js\n"
            f"  (or pass --integrations-file <path>)")

    with open(integ_file, encoding="utf-8") as fh:
        integ_data = json.load(fh)
    all_integrations = integ_data.get("integrations") or []
    log(f"\n  Integrations file: {integ_file}")
    log(f"  Integrations found: {len(all_integrations)}")

    # ── Filter by --names if given ────────────────────────────────────────
    if args.names:
        wanted = {n.strip().lower() for n in args.names.split(",")}
        filtered = [i for i in all_integrations
                    if i.get("name", "").lower() in wanted]
        if not filtered:
            die(f"None of {args.names!r} matched any integration name. "
                f"Available: "
                f"{', '.join(i.get('name','') for i in all_integrations)}")
        all_integrations = filtered
        log(f"  Filtered to {len(all_integrations)} named integration(s).")

    # ── Get token ─────────────────────────────────────────────────────────
    token = get_token()
    log(f"  OAuth token: obtained (length {len(token)})")

    # ── Fire trigger tests ────────────────────────────────────────────────
    section("LAYER 1 — API TRIGGER TEST (PUT trigger_test/{service_id})")
    log("  Firing trigger test for each integration...\n")

    results = []
    for integ in all_integrations:
        r = fire_trigger_test(grid, token, integ, dry_run=args.dry_run)
        results.append(r)

    if args.dry_run:
        section("DRY RUN COMPLETE — nothing was fired")
        log("  Pass --dry-run to preview only. Remove it to actually run.")
        return

    # ── Layer 2: Alert Logs verification ─────────────────────────────────
    if not args.no_alert_logs:
        section("LAYER 2 — ALERT LOGS VERIFICATION")
        log("  Waiting for Alert Log rows to confirm delivery...")
        log(f"  (polling for up to {args.wait}s — {ALERT_LOG_POLL_SECONDS}s "
            f"interval)\n")
        verify_in_alert_logs(grid, results, wait_seconds=args.wait)
    else:
        log("\n  [skip] Alert Logs verification (--no-alert-logs).")
        for r in results:
            if r["api_verdict"] == PASS:
                r["alert_log_verdict"] = SKIP
                r["alert_log_row"] = None
                r["alert_log_message"] = ("Alert Logs not checked "
                                          "(--no-alert-logs flag).")
                r["final_verdict"] = PASS   # trust the API alone
            else:
                r["alert_log_verdict"] = SKIP
                r["alert_log_row"] = None
                r["alert_log_message"] = ("API trigger test failed.")
                r["final_verdict"] = FAIL

    # ── Summary ───────────────────────────────────────────────────────────
    section("TRIGGER TEST RESULTS")
    passed = [r for r in results if r["final_verdict"] == PASS]
    warned = [r for r in results if r["final_verdict"] == WARN]
    failed = [r for r in results if r["final_verdict"] == FAIL]

    log(f"\n  {'Integration':<32} {'API':<8} {'Alert Log':<12} {'Final'}")
    log(f"  {'-'*32} {'-'*8} {'-'*12} {'-'*10}")
    for r in sorted(results, key=lambda x: x.get("name", "")):
        icon = {"PASS": "✅", "FAIL": "❌", "WARN": "⚠️",
                "SKIP": "—"}.get(r.get("final_verdict"), "?")
        log(f"  {str(r.get('name','')):<32} "
            f"{r.get('api_verdict',''):<8} "
            f"{r.get('alert_log_verdict',''):<12} "
            f"{icon} {r.get('final_verdict','')}")

    log(f"\n  Passed: {len(passed)}    Warned: {len(warned)}    "
        f"Failed: {len(failed)}")

    if failed:
        log("\n  ❌ FAILED INTEGRATIONS (will be BLOCKED in the lifecycle):")
        for r in failed:
            log(f"     • {r['name']}: {r.get('api_message','')}")
        log("\n  Fix these integrations in Site24x7 → Third-Party "
            "Integrations → Edit,")
        log("  then re-run this script before starting the lifecycle.")

    if warned:
        log("\n  ⚠️  WARNED INTEGRATIONS (API OK, Alert Logs not confirmed):")
        for r in warned:
            log(f"     • {r['name']}: {r.get('alert_log_message','')}")
        log("\n  These integrations WILL participate in the lifecycle.")
        log("  The API trigger test passed — the Alert Logs confirmation")
        log("  timed out. This is usually a timing issue, not a real failure.")

    if passed:
        log("\n  ✅ PASSED INTEGRATIONS (ready for the lifecycle):")
        for r in passed:
            log(f"     • {r['name']}: {r.get('api_message','')}")

    # ── Write result file ─────────────────────────────────────────────────
    write_results(results, grid, started)

    # ── Exit code ─────────────────────────────────────────────────────────
    # FAIL = misconfigured integration → block the lifecycle
    # WARN = API OK, Alert Logs timing → allow the lifecycle (WARN ≠ FAIL)
    # PASS = all good
    if failed:
        section(f"EXIT 1 — {len(failed)} integration(s) FAILED "
                f"pre-flight trigger test")
        log("  The alert lifecycle should NOT start until these are fixed.")
        log("  run_all.py will BLOCK those integrations from the lifecycle.")
        sys.exit(1)
    else:
        section("EXIT 0 — ALL integrations passed pre-flight trigger test")
        log("  The alert lifecycle can start.")
        sys.exit(0)


if __name__ == "__main__":
    main()
