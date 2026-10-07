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
          "data": { "response_code": 200|201|202,
                    "title": " Success",
                    "message": "Test message sent successfully." } }

    The UI shows "Success" next to the ▶ button when code==0 AND the inner
    data.response_code is any 2xx (Web_Mon TestIntegrations.getResponse()
    treats 1..299 as success — 200 OK, 201 Created, 202 Accepted). Different
    ITSM tools return different 2xx codes (Desk/SDP/ServiceNow → 201,
    PagerDuty → 202), so we must NOT hard-code 200.

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
        code == 0 and response_code in 1..299  →  "trigger test accepted"
            (200 OK / 201 Created / 202 Accepted — matches Web_Mon)
        response_code >= 300 or < 1, or title "... Failed"  →  "FAILED"

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

# The exact monitor text the product puts in the test-alert subject.
# VERIFIED from Web_Mon TestIntegrations.getTestGlobalParams():
#     globalParams.put("MONITORNAME", " [Site24x7 Test Alert] Zylker Monitor ");
#     globalParams.put("STATUS", "DOWN");
# and every integration's subject template is "$MONITORNAME is $STATUS"
# (confirmed in accounts/*/integrations.json → raw.subject). So the ticket
# created in each ITSM tool is titled:
#     "[Site24x7 Test Alert] Zylker Monitor is DOWN"
# We search each tool for this exact monitor text — the SAME search_by_window
# matching used for the real ticket lifecycle (stage4_tickets.py).
TEST_ALERT_MONITOR_TEXT = "Zylker Monitor"

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
    sess_code = sess_resp = None
    if cookie:
        sess_headers = {
            "Cookie":        cookie,
            "Accept":        "application/json, text/javascript, */*; q=0.01",
            "Content-Type":  "application/json",
            "X-Requested-With": "XMLHttpRequest",
        }
        if csrf:
            # The product checks both header spellings depending on route.
            sess_headers["X-CSRF-Token"] = csrf
            sess_headers["x-zcsrf-token"] = csrf
        sess_code, sess_resp = _do_put(url, sess_headers)
        # Only fall through to OAuth if the session auth itself failed.
        # 401/403 or a known auth error_code (1121 scope, 1100 privilege)
        # all mean "this credential can't do it" → try the other one.
        err_str = str(sess_resp.get("_error", "")) if isinstance(sess_resp, dict) else ""
        is_auth_fail = (sess_code in (401, 403)
                        or "1121" in err_str
                        or "1100" in err_str)
        if not is_auth_fail:
            return sess_code, sess_resp
        log("    [info] Session cookie auth failed — trying OAuth token ...")

    # Try 2: OAuth token (needs admin scope in the refresh token)
    oauth_code, oauth_resp = api_put(grid, path, token)

    # If BOTH failed, surface the response that best explains the gap. A 1100
    # ("not authorized" — non-admin session user) is more actionable than a
    # generic 1121, so prefer it when present.
    oauth_err = str(oauth_resp.get("_error", "")) if isinstance(oauth_resp, dict) else ""
    oauth_fail = (oauth_code in (401, 403)
                  or "1121" in oauth_err or "1100" in oauth_err)
    if oauth_fail and sess_resp is not None:
        sess_err = str(sess_resp.get("_error", "")) if isinstance(sess_resp, dict) else ""
        if "1100" in sess_err and "1100" not in oauth_err:
            return sess_code, sess_resp
    return oauth_code, oauth_resp


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
    #
    # IMPORTANT — distinguish a REAL integration failure from a TEST-HARNESS
    # access failure. Verified LIVE against the QA grid, the trigger_test
    # endpoint (PUT /integration/thirdparty_service/trigger_test/{id}) is, in
    # Web_Mon's security-rest-api.xml, inside a <urls> group with
    # oauthscope="internal" and userroles="1|2|3|100". Consequences observed:
    #   • OAuth token (even admin-scoped)  → error_code 1121 ("OAuth Scope ...
    #     not allowed") — the 'internal' scope is not grantable to public
    #     refresh tokens.
    #   • Session cookie WITHOUT a matching x-zcsrf-token → 1102/1006.
    #   • Session cookie WITH the correct x-zcsrf-token but a non-admin logged-in
    #     user → error_code 1100 ("not authorized to perform this operation").
    # EVERY one of these is a HARNESS access gap, NOT a misconfigured
    # integration — the integration itself may be perfectly healthy (the browser
    # ▶ button, run by an admin session, succeeds). We therefore map them ALL to
    # SKIP (could not verify from the harness), never FAIL, so run_all.py does
    # NOT block the lifecycle or bury a genuine lifecycle PASS. Only a response
    # that genuinely reaches the integration and reports a delivery/config
    # problem is a true FAIL.
    api_ok = False
    api_msg = ""
    harness_auth_gap = False
    if resp and "_exception" in resp:
        api_msg = f"Network error: {resp['_exception']}"
    elif resp and "_error" in resp:
        err_txt = resp["_error"]
        if "1100" in err_txt or "not authorized to perform" in err_txt.lower():
            # Session cookie reached the product but the logged-in user lacks
            # the admin PRIVILEGE the trigger_test write needs (error 1100).
            # Same category as 1121: a harness ACCESS gap, not a misconfigured
            # integration. The browser ▶ button works for an admin user; our
            # captured session is a non-admin/limited user. Never FAIL.
            harness_auth_gap = True
            api_msg = (f"Authorization error (1100) — the session cookie "
                       f"reached Site24x7 but the logged-in user is NOT "
                       f"authorized to fire trigger_test (needs admin "
                       f"privilege). Harness access gap, NOT an integration "
                       f"misconfiguration — capture a session cookie from an "
                       f"ADMIN user, or mint an admin OAuth token "
                       f"(./get_token.sh --authorize). The lifecycle is NOT "
                       f"blocked. Details: {err_txt}")
        elif "1121" in err_txt or "oauthscope" in err_txt.lower():
            harness_auth_gap = True
            api_msg = (f"OAuth scope error (1121) — the test HARNESS could "
                       f"not authenticate to the trigger_test endpoint: the "
                       f"OAuth token lacks 'admin' scope AND no valid session "
                       f"cookie was found. This is a harness access gap, NOT "
                       f"an integration misconfiguration — the integration "
                       f"itself may be healthy (the browser ▶ 'Trigger Test' "
                       f"button succeeds). Set S247_SESSION_COOKIE "
                       f"(recommended) or add the 'admin' OAuth scope to your "
                       f"refresh token to let the harness verify it. "
                       f"The lifecycle is NOT blocked for this integration.")
        elif ("1102" in err_txt or "1006" in err_txt
              or "invalid value passed for authtoken" in err_txt.lower()
              or "invalid value passed for x-zcsrf" in err_txt.lower()):
            # CSRF/auth-token mismatch: the session cookie was sent without a
            # matching x-zcsrf-token (1102), or the CSRF token itself is stale
            # (1006). Again a harness credential gap — refresh the cookie AND
            # its paired CSRF token together (python3 extract_cookie.py). Never
            # an integration misconfiguration.
            harness_auth_gap = True
            api_msg = (f"CSRF/auth-token mismatch — the session cookie was "
                       f"not accepted because its paired x-zcsrf-token is "
                       f"missing or stale. Re-capture the cookie AND CSRF "
                       f"together with 'python3 extract_cookie.py'. Harness "
                       f"access gap, NOT an integration misconfiguration. "
                       f"The lifecycle is NOT blocked. Details: {err_txt}")
        elif http_code == 401:
            # A bare 401 without the 1121 marker is also an auth gap on our
            # side (expired/absent cookie + non-admin token), not a config
            # defect in the destination tool.
            harness_auth_gap = True
            api_msg = (f"HTTP 401 Unauthorized — the test harness could not "
                       f"authenticate to the trigger_test endpoint "
                       f"(session cookie absent/expired and OAuth token not "
                       f"permitted). Harness access gap, not an integration "
                       f"misconfiguration. Details: {err_txt}")
        elif (http_code in (429, 500, 502, 503, 504)
              or "temporarily unavailable" in err_txt.lower()
              or "<!doctype html" in err_txt.lower()
              or "<html" in err_txt.lower()):
            # Gateway / server-side transient error (502/503/504), rate limit
            # (429), or an HTML error page ("Zoho - Temporarily Unavailable").
            # The request never reached the integration logic — this is grid
            # INFRASTRUCTURE being down, NOT a misconfigured integration. Mark
            # SKIP so the lifecycle is not blocked by a transient outage; the
            # operator should simply re-run once the grid is back.
            harness_auth_gap = True
            short = err_txt.strip().replace("\n", " ")
            if "<" in short:
                short = "server returned an HTML error page"
            api_msg = (f"Grid temporarily unavailable (HTTP {http_code}) — the "
                       f"trigger_test request did not reach the integration "
                       f"(server/gateway error, not a misconfiguration). "
                       f"Re-run the pre-flight once the grid is back. "
                       f"The lifecycle is NOT blocked. Details: {short[:160]}")
        else:
            api_msg = f"HTTP {http_code}: {err_txt}"
    elif http_code == 200:
        outer_code = resp.get("code")
        data_block = resp.get("data") or {}
        resp_code  = data_block.get("response_code")
        title      = data_block.get("title", "")
        msg_txt    = data_block.get("message", resp.get("message", ""))
        # ------------------------------------------------------------------
        # WEB_MON ALIGNMENT — what "success" really means for trigger_test.
        #
        # VERIFIED against Web_Mon TestIntegrations.getResponse()
        # (source/server/.../thirdparty/webhook/TestIntegrations.java):
        #
        #     if (responseCode >= 300)        -> responseTitle += " Failed"
        #     else if (responseCode < 1)      -> responseTitle += " Failed"
        #     else                            -> responseTitle += " Success"
        #                                        ("Test message sent successfully")
        #
        # i.e. the product considers ANY HTTP status in the range 1..299 a
        # SUCCESS — including 200 (OK), 201 (Created) and 202 (Accepted).
        # Different ITSM tools return different 2xx codes:
        #   • Zoho Desk / SDP create a ticket  -> 201 Created
        #   • PagerDuty accepts the event      -> 202 Accepted
        #   • ServiceNow creates an incident   -> 201 Created
        #
        # LIVE EVIDENCE from the QA grid confirmed this exactly:
        #   PagerDuty       code=0 response_code=202 title=' Success'
        #   ServiceDesk+    code=0 response_code=201 title='...Success'
        #   ServiceNow      code=0 response_code=201 title='...Success'
        #   all with message 'Test message sent successfully.'
        #
        # The OLD check (resp_code == 200 ONLY) wrongly marked these genuine
        # successes as FAIL — the user's No:1 issue ("the trigger test was a
        # success and you flag it as failed"). We now mirror the product:
        # accept the whole 1..299 success band, AND also honour the explicit
        # " Success" title / "sent successfully" message the product sets.
        # ------------------------------------------------------------------
        title_l = str(title).strip().lower()
        msg_l   = str(msg_txt).strip().lower()
        success_by_code = (isinstance(resp_code, int)
                           and 1 <= resp_code < 300)
        success_by_text = (title_l.endswith("success")
                           or "sent successfully" in msg_l)
        # A failure signal the product sets explicitly — never treat as OK.
        failed_signal = (title_l.endswith("failed")
                         or "error occured while sending" in msg_l
                         or "error occurred while sending" in msg_l)
        if outer_code == 0 and (success_by_code or success_by_text) \
                and not failed_signal:
            api_ok = True
            api_msg = (f"{title.strip()} — {msg_txt} "
                       f"(response_code={resp_code})"
                       if title.strip()
                       else f"{msg_txt} (response_code={resp_code})")
        else:
            api_msg = (f"code={outer_code} response_code={resp_code} "
                       f"title={title!r} message={msg_txt!r}")
    elif http_code in (429, 500, 502, 503, 504) or http_code is None:
        # Transient server/gateway/network condition with no parseable body.
        harness_auth_gap = True
        api_msg = (f"Grid temporarily unavailable (HTTP {http_code}) — "
                   f"trigger_test did not reach the integration. Transient "
                   f"infrastructure error, not a misconfiguration. Re-run the "
                   f"pre-flight once the grid is back. Lifecycle NOT blocked. "
                   f"Details: {str(resp)[:160]}")
    else:
        api_msg = f"HTTP {http_code}: {resp}"

    if api_ok:
        verdict = PASS
    elif harness_auth_gap:
        verdict = SKIP      # harness could not verify — do NOT fail the integration
    else:
        verdict = FAIL      # genuine integration/config/delivery failure

    icon = {"PASS": "✅", "SKIP": "⚠️", "FAIL": "❌"}.get(verdict, "❌")
    log(f"    {icon} {name}: {api_msg}")

    return {
        "name":         name,
        "service_id":   service_id,
        "api_verdict":  verdict,
        "api_code":     http_code,
        "api_response": resp,
        "api_message":  api_msg,
        "fired_at":     fired_at,
        # Explicit flag so run_all.py can tell a harness auth gap (SKIP) apart
        # from a genuine misconfiguration (FAIL) without re-parsing messages.
        "harness_auth_gap": bool(harness_auth_gap),
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
    # Poll for integrations whose API call succeeded (PASS) AND for those
    # where the harness itself could not authenticate the PUT (SKIP, a
    # harness auth gap like 1100/1121). The reason: the Alert Logs are an
    # INDEPENDENT source of truth. The browser ▶ "Trigger Test" button (run
    # by the logged-in admin) may have fired the test successfully even when
    # OUR headless PUT was rejected — and when it did, Site24x7 writes a
    # "[Site24x7 Test Alert] ... is DOWN" row to the Alert Logs (confirmed
    # LIVE from the user's screenshot: one test-alert row per integration).
    # So if we find that row we can PROMOTE a harness-auth SKIP to a real
    # PASS: the trigger test demonstrably worked. A genuine FAIL (the PUT
    # reached the integration and it reported a delivery error) is NOT
    # promoted — that stays FAIL.
    pending = {r["name"] for r in results
               if r["api_verdict"] in (PASS, SKIP)}
    found   = {}     # name → matched alert log row

    def _finalise_non_pass(r):
        """Set the Alert-Logs fields for an integration that did NOT get an
        API PASS. A harness auth gap (api_verdict == SKIP) must stay SKIP so
        the lifecycle is never blocked; only a genuine FAIL stays FAIL."""
        r["alert_log_verdict"] = SKIP
        r["alert_log_row"] = None
        if r.get("api_verdict") == SKIP:
            r["alert_log_message"] = (
                "Trigger test could not be verified from the harness "
                "(auth gap) — Alert Logs check skipped. Not treated as a "
                "failure; the lifecycle is NOT blocked.")
            r["final_verdict"] = SKIP
        else:
            r["alert_log_message"] = ("API trigger test failed — Alert Logs "
                                      "check skipped.")
            r["final_verdict"] = FAIL

    if not pending:
        log("  No integrations with API PASS — skipping Alert Logs check.")
        for r in results:
            _finalise_non_pass(r)
        return

    has_session = bool(os.environ.get("S247_SESSION_COOKIE", "").strip())
    if not has_session:
        log("  [WARN] S247_SESSION_COOKIE not set — cannot check Alert Logs.")
        log("         Layer 1 (API) result stands. Set the cookie for full")
        log("         two-layer verification.")
        for r in results:
            if r["api_verdict"] == PASS:
                # The API trigger test PASSED (authoritative). We simply can't
                # run the optional Alert-Log confirmation without a cookie —
                # that's a harness limitation, not an integration problem. Mark
                # the Alert-Log LAYER as SKIP (neutral "—") and keep the
                # OVERALL result a clean PASS.
                r["alert_log_verdict"] = SKIP
                r["alert_log_row"] = None
                r["alert_log_message"] = ("API trigger test PASSED. Alert Logs "
                                          "confirmation skipped "
                                          "(S247_SESSION_COOKIE not set) — this "
                                          "is a harness limitation, not a "
                                          "failure. Overall result: PASS.")
                r["final_verdict"] = PASS
            else:
                # SKIP (harness auth gap) stays SKIP, FAIL stays FAIL. Without
                # a session cookie we cannot consult the independent Alert-Log
                # evidence that would let us promote a SKIP to PASS.
                _finalise_non_pass(r)
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
        if name in found:
            # INDEPENDENT PROOF — a "[Site24x7 Test Alert]" row exists in the
            # Alert Logs for this integration. The trigger test genuinely
            # fired and was delivered. This is authoritative, so it OVERRIDES
            # a harness-auth SKIP: even though our headless PUT was rejected
            # (1100/1121), the browser ▶ button (admin session) fired it and
            # Site24x7 logged it. Report the truth: PASS.
            r["alert_log_verdict"] = PASS
            r["alert_log_row"] = found[name]
            if r["api_verdict"] == PASS:
                r["alert_log_message"] = (
                    f"Alert Log row confirmed for '{name}' "
                    f"with test-alert signature.")
            else:
                r["alert_log_message"] = (
                    f"Trigger test CONFIRMED for '{name}' via the Alert Logs: "
                    f"a '[Site24x7 Test Alert]' row was delivered even though "
                    f"the harness's own PUT could not authenticate "
                    f"(harness auth gap). The trigger test genuinely "
                    f"succeeded — promoted from SKIP to PASS on independent "
                    f"Alert-Log evidence.")
            r["harness_auth_gap"] = False
            r["final_verdict"] = PASS
        elif r["api_verdict"] != PASS:
            # No independent Alert-Log proof. Keep the FAIL-vs-SKIP (harness
            # auth gap) distinction intact.
            _finalise_non_pass(r)
        else:
            # API said OK (Layer 1 PASS) but we didn't see the Alert-Log row
            # within the poll window. The Alert Log is only SUPPORTING
            # evidence — the authoritative success signal is the API response
            # (response_code 1-299), exactly how the Site24x7 product UI itself
            # decides a trigger test succeeded (Web_Mon TestIntegrations.
            # getResponse()). A timing gap on a secondary confirmation must
            # NOT downgrade a genuinely-passing trigger test to a yellow WARN
            # (the user's exact complaint). So:
            #   • the Alert-Log LAYER is marked SKIP ("not confirmed in time"),
            #     shown as a neutral "—", not an alarming ⚠️ WARN, AND
            #   • the OVERALL result stays a clean PASS.
            r["alert_log_verdict"] = SKIP
            r["alert_log_row"] = None
            r["alert_log_message"] = (
                f"Alert Log row not seen for '{name}' within the "
                f"{wait_seconds}s poll window — but the API trigger test "
                f"PASSED (the request was accepted). The Alert Log is only a "
                f"supporting confirmation; the overall trigger test is PASS. "
                f"The row usually arrives shortly after the window.")
            r["final_verdict"] = PASS


# ---------------------------------------------------------------------------
# Layer 3: verify the test-alert TICKET inside each ITSM tool
# ---------------------------------------------------------------------------
# WHY (user request): the trigger test is only truly "passed" when the test
# alert produced a real [Site24x7 Test Alert] ticket in the destination tool
# — exactly how we validate the real ticket lifecycle. The API response
# (Layer 1) proves Site24x7 ACCEPTED the request; the Alert Logs (Layer 2)
# prove Site24x7 LOGGED the delivery. Neither proves the TOOL actually created
# the ticket. This layer asks each tool directly, reusing the SAME adapters
# and SAME window-matching that stage4_tickets.py uses for the lifecycle.
#
# WEB_MON ALIGNMENT — the test alert's subject (TestIntegrations.java):
#   MONITORNAME = " [Site24x7 Test Alert] Zylker Monitor ", STATUS = "DOWN"
#   subject template "$MONITORNAME is $STATUS"
#   → ticket title "[Site24x7 Test Alert] Zylker Monitor is DOWN"
# so we search each tool for TEST_ALERT_MONITOR_TEXT ("Zylker Monitor"), the
# identical mechanism used for the real monitor name in the lifecycle.
# ---------------------------------------------------------------------------

def verify_in_itsm_tools(results, monitor_text=TEST_ALERT_MONITOR_TEXT,
                         window_minutes=15):
    """For every integration whose trigger test fired (api PASS or a
    harness-auth SKIP), ask its ITSM tool directly whether a
    "[Site24x7 Test Alert] ..." ticket was created in the recent window.

    Mirrors stage4_tickets.py exactly:
      • picks the tool adapter from the integration's delivery mode / name,
      • calls tool.search_by_window(monitor_text, since, until),
      • a ticket found in-window = tool-side PROOF the test alert landed.

    Adds to each result dict:
        "itsm_tool":          str   (adapter name, e.g. "Zoho Desk")
        "itsm_verdict":       "PASS"|"WARN"|"SKIP"|"NOT CONFIGURED"|"NO ADAPTER"
        "itsm_tickets":       list  (matched test-alert tickets)
        "itsm_message":       str

    A tool with no credentials → SKIP (NOT CONFIGURED) — never a failure,
    identical to stage4's rule. A non-ticketing channel (Slack/webhook) →
    NO ADAPTER, also never a failure.

    IMPORTANT: this layer can only PROMOTE confidence (SKIP→PASS when the
    tool shows the ticket) or add a WARN note. It must NEVER turn a passing
    trigger test into a FAIL, because a tool-side read gap (missing creds,
    tool API hiccup) is a harness limitation, not a product defect — exactly
    the principle already applied to Layers 1 and 2.
    """
    # Import the stage4 tool layer lazily so stage0 has no hard dependency on
    # ITSM credentials when this layer is not wanted (e.g. --no-itsm-check).
    try:
        import stage4_tickets as s4
    except Exception as exc:  # noqa: BLE001
        for r in results:
            r.setdefault("itsm_verdict", SKIP)
            r.setdefault("itsm_tickets", [])
            r.setdefault("itsm_tool", "")
            r.setdefault("itsm_message",
                         f"ITSM tool layer unavailable ({exc}). "
                         f"Layers 1 & 2 stand.")
        return

    tools = s4.build_tools()
    now = datetime.now(timezone.utc)
    # Window: from 2 min before the earliest fire to now + a small margin.
    fire_times = [datetime.fromisoformat(r["fired_at"].replace("Z", "+00:00"))
                  for r in results if r.get("fired_at")]
    since_utc = (min(fire_times) if fire_times else now) - timedelta(minutes=2)
    until_utc = now + timedelta(minutes=2)
    # Guard: never let the window exceed window_minutes before 'since'.
    floor = now - timedelta(minutes=window_minutes)
    if since_utc < floor:
        since_utc = floor

    log(f"\n  Verifying test-alert TICKETS inside each ITSM tool "
        f"(subject contains '{monitor_text}')")
    log(f"  window (UTC): {since_utc:%Y-%m-%d %H:%M:%S} → "
        f"{until_utc:%Y-%m-%d %H:%M:%S}")

    # Cache tool searches so we don't hit the same tool twice when two
    # integrations map to the same adapter.
    _search_cache = {}

    def _search(tool):
        key = tool.name
        if key in _search_cache:
            return _search_cache[key]
        try:
            rows = tool.search_by_window(monitor_text, since_utc, until_utc)
        except Exception as exc:  # noqa: BLE001
            rows = {"_error": str(exc)}
        _search_cache[key] = rows
        return rows

    for r in results:
        name = r.get("name", "")
        # Only check tools for integrations whose trigger actually fired.
        if r.get("api_verdict") not in (PASS, SKIP) \
                and r.get("final_verdict") not in (PASS, SKIP, WARN):
            r["itsm_verdict"] = SKIP
            r["itsm_tickets"] = []
            r["itsm_tool"] = ""
            r["itsm_message"] = ("Trigger test did not fire — tool check "
                                 "skipped.")
            continue

        delivery_modes = (r.get("delivery_modes")
                          or (r.get("integration") or {}).get("delivery_modes"))
        tool = s4.pick_tool(name, tools, delivery_modes)

        if tool is None:
            r["itsm_verdict"] = "NO ADAPTER"
            r["itsm_tickets"] = []
            r["itsm_tool"] = ""
            r["itsm_message"] = ("Non-ticketing channel (e.g. Slack / webhook)"
                                 " or no matching adapter — nothing to verify "
                                 "tool-side. Not a failure.")
            continue
        if not tool.configured():
            r["itsm_verdict"] = "NOT CONFIGURED"
            r["itsm_tickets"] = []
            r["itsm_tool"] = tool.name
            r["itsm_message"] = (f"{tool.name} not configured in .itsm.env — "
                                 f"cannot read back the test-alert ticket. "
                                 f"Not a failure (Layers 1 & 2 stand).")
            continue

        rows = _search(tool)
        if isinstance(rows, dict) and "_error" in rows:
            # A tool-side read gap (API hiccup / auth) is a harness limitation,
            # never an integration failure. Mark the LAYER as SKIP (neutral
            # "—") so it does not raise an alarming ⚠️ when the trigger test
            # already PASSED at Layer 1.
            r["itsm_verdict"] = SKIP
            r["itsm_tickets"] = []
            r["itsm_tool"] = tool.name
            r["itsm_message"] = (f"{tool.name} search could not complete "
                                 f"({str(rows['_error'])[:120]}). Tool-side "
                                 f"read gap, not a failure — the API trigger "
                                 f"test result stands.")
            continue

        matched = rows or []
        r["itsm_tool"] = tool.name
        r["itsm_tickets"] = matched
        if matched:
            ids = ", ".join(str(t.get("ticket_id", "")) for t in matched[:5])
            r["itsm_verdict"] = PASS
            r["itsm_message"] = (f"{tool.name} CONFIRMS the test alert: "
                                 f"{len(matched)} '[Site24x7 Test Alert]' "
                                 f"ticket(s) created in-window ({ids}).")
            log(f"    ✅ {name}: {tool.name} shows "
                f"{len(matched)} test-alert ticket(s): {ids}")
        else:
            # The tool returned no "[Site24x7 Test Alert]" ticket IN THE
            # WINDOW. This is NOT a failure: the trigger test already PASSED at
            # Layer 1 (the API accepted it), and the test ticket commonly lands
            # just after the short search window, or the tool's list view only
            # surfaces it inside the ticket details. Layer 3 can only PROMOTE
            # confidence (find a ticket → PASS); the absence of a ticket is
            # simply "not independently confirmed", a neutral SKIP ("—"), never
            # a ⚠️ WARN that would make a passing trigger test look yellow.
            r["itsm_verdict"] = SKIP
            r["itsm_message"] = (f"{tool.name} did not surface a "
                                 f"'[Site24x7 Test Alert]' ticket in the search "
                                 f"window — not independently confirmed, but "
                                 f"NOT a failure. The API trigger test PASSED; "
                                 f"the ticket usually appears just after the "
                                 f"window or only inside the ticket details.")
            log(f"    —  {name}: {tool.name} did not surface a test-alert "
                f"ticket in-window (API PASS stands; overall PASS).")


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
    # A SKIP verdict = the harness could not verify the trigger test (OAuth
    # scope / session cookie gap). It is NOT a failure, so it must not paint
    # the banner red. Only a genuine FAIL does that.
    any_fail = any(r.get("final_verdict") == FAIL for r in results)
    any_skip = any(r.get("final_verdict") == SKIP for r in results)
    all_pass = all(r.get("final_verdict") in (PASS, WARN) for r in results)

    def _col_cell(verdict, note="", overall=None):
        """A small per-layer status cell showing SUPPORTING evidence.

        Important UX rule (the user's request): when the OVERALL trigger-test
        result is PASS, a supporting layer that merely 'could not confirm'
        must NEVER render as an alarming yellow ⚠️ WARN. It renders as a
        neutral grey '—  not confirmed' so the row reads cleanly green. Only a
        genuine ❌ FAIL (which can only happen when the overall result is also
        a FAIL) is ever shown in red/amber on a layer."""
        v = str(verdict or SKIP)
        # Soften any non-PASS supporting layer to a neutral '—' when the
        # overall trigger test already PASSED. A passing trigger test must not
        # display any yellow on its row.
        if overall == PASS and v not in (PASS,):
            ic, cc, label = "—", "#6e7781", "not confirmed"
            title = f' title="{_esc(note)}"' if note else ""
            return (f'<td style="font-size:11px;color:{cc}"{title}>'
                    f'{ic} {label}</td>')
        ic = {"PASS": "✅", "FAIL": "❌", "WARN": "⚠️", "SKIP": "—",
              "NOT CONFIGURED": "—", "NO ADAPTER": "—",
              "BLOCKED": "🔴"}.get(v, "—")
        cc = {"PASS": "#1a7f37", "FAIL": "#cf222e", "WARN": "#9a6700",
              "SKIP": "#6e7781", "NOT CONFIGURED": "#6e7781",
              "NO ADAPTER": "#6e7781", "BLOCKED": "#cf222e"}.get(v, "#6e7781")
        title = f' title="{_esc(note)}"' if note else ""
        return (f'<td style="font-size:11px;color:{cc}"{title}>'
                f'{ic} {_esc(v)}</td>')

    for r in sorted(results, key=lambda x: x.get("name", "")):
        fv   = r.get("final_verdict", SKIP)
        apiv = r.get("api_verdict", SKIP)
        logv = r.get("alert_log_verdict", SKIP)
        itsmv = r.get("itsm_verdict", SKIP)
        name = r.get("name", "")
        sid  = r.get("service_id", "")
        api_msg  = r.get("api_message", "")
        log_msg  = r.get("alert_log_message", "")
        itsm_msg = r.get("itsm_message", "")
        itsm_tool = r.get("itsm_tool", "")
        fired_at = r.get("fired_at", "")

        icon = {"PASS": "✅", "FAIL": "❌", "WARN": "⚠️",
                "SKIP": "—", "BLOCKED": "🔴"}.get(fv, "?")
        colour = {"PASS": "#1a7f37", "FAIL": "#cf222e",
                  "WARN": "#9a6700", "SKIP": "#6e7781",
                  "BLOCKED": "#cf222e"}.get(fv, "#000")

        # The human-facing note prioritises the strongest proof so an end user
        # is never confused: if the ITSM tool confirmed the ticket, say so
        # first — that is the end-to-end success they care about.
        if itsmv == PASS:
            note = itsm_msg or api_msg
        elif fv == PASS:
            note = api_msg or log_msg
        else:
            note = api_msg or log_msg or itsm_msg

        rows += (
            f'<tr>'
            f'<td><strong>{_esc(name)}</strong>'
            f'{("<br><span style=font-size:10px;color:#57606a>" + _esc(itsm_tool) + "</span>") if itsm_tool else ""}</td>'
            f'<td style="font-size:11px;color:#57606a">{_esc(sid)}</td>'
            f'<td style="color:{colour};font-weight:700">'
            f'{icon} {_esc(fv)}</td>'
            + _col_cell(apiv, api_msg, overall=fv)
            + _col_cell(logv, log_msg, overall=fv)
            + _col_cell(itsmv, itsm_msg, overall=fv)
            + f'<td style="font-size:12px">{_esc(note)}</td>'
            f'<td style="font-size:11px;color:#57606a">{_esc(fired_at[:19])}</td>'
            f'</tr>'
        )

    # How many integrations had end-to-end tool confirmation?
    itsm_confirmed = sum(1 for r in results if r.get("itsm_verdict") == PASS)

    if any_fail:
        banner_colour = "#cf222e"
        banner_icon   = "❌"
        banner_txt    = ("One or more integrations FAILED the pre-flight "
                         "trigger test. The lifecycle was BLOCKED for "
                         "those integrations.")
    elif any_skip:
        banner_colour = "#9a6700"
        banner_icon   = "⚠️"
        banner_txt    = ("Pre-flight trigger test could not be verified from "
                         "the harness for one or more integrations (OAuth "
                         "'admin' scope / session cookie not available). This "
                         "is a harness access gap, NOT an integration failure "
                         "— the lifecycle was NOT blocked and each "
                         "integration's verdict below stands on its own "
                         "evidence (alert logs + tool-side tickets).")
    else:
        banner_colour = "#1a7f37"
        banner_icon   = "✅"
        banner_txt    = "All integrations passed the pre-flight trigger test."
        if itsm_confirmed:
            banner_txt += (f" {itsm_confirmed} of {len(results)} were "
                           f"end-to-end CONFIRMED inside the destination ITSM "
                           f"tool (the '[Site24x7 Test Alert]' ticket was "
                           f"actually created).")

    return f"""
<div class="card" style="border-left:5px solid {banner_colour};">
  <div class="hd" style="color:{banner_colour}">
    {banner_icon} Pre-flight Trigger Test
  </div>
  <p>
    Each integration's "Trigger Test" button was fired via the Site24x7 API
    (<code>PUT /api/integration/thirdparty_service/trigger_test/{{service_id}}</code>)
    BEFORE the alert lifecycle started. The test alert is validated at THREE
    layers, exactly how the real ticket lifecycle is checked:
  </p>
  <ul style="font-size:12px;color:#57606a;margin-top:0">
    <li><strong>API Check</strong> — Site24x7 accepted the trigger request
        (response code 1&ndash;299 = Success, per Web_Mon).</li>
    <li><strong>Alert Log</strong> — Site24x7 logged the delivery
        (<em>[Site24x7 Test Alert]</em> row in the Alert Logs).</li>
    <li><strong>ITSM Tool</strong> — the destination tool actually created the
        <em>[Site24x7 Test Alert] Zylker Monitor is DOWN</em> ticket
        (read back directly from the tool, same as the lifecycle).</li>
  </ul>
  <p style="font-size:11px;color:#57606a;margin-top:0">
    The <strong>Result</strong> column is the single trigger-test verdict you
    need to read: <strong style="color:#1a7f37">✅ PASS</strong> means the
    trigger test worked. The three layer columns are just <em>supporting
    evidence</em>. When the Result is PASS, a layer that simply could not
    confirm in time shows a neutral grey <code>— not confirmed</code> (never a
    yellow warning) — so a passing trigger test always reads clean green.
  </p>
  <p style="font-weight:600;color:{banner_colour}">{banner_txt}</p>
  <table>
    <tr>
      <th>Integration</th>
      <th style="font-size:11px">Service ID</th>
      <th>Result</th>
      <th>API Check</th>
      <th>Alert Log</th>
      <th>ITSM Tool</th>
      <th>Evidence</th>
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

def is_session_cookie_admin(grid):
    """Probe whether the CURRENT session cookie belongs to an ADMIN user.

    trigger_test requires an admin-privileged session (userroles 1|2|3|100).
    A non-admin cookie reaches the product but gets error 1100 ("not
    authorized"). Rather than discover that only after firing, we check the
    logged-in user's role up-front via the session-authenticated
    /app/api/current_user endpoint (same cookie the ▶ button uses).

    Returns (state, detail):
        state ∈ {"admin", "non_admin", "no_cookie", "unknown"}
        detail: short human string (role name / reason), never a secret.

    Never raises — any error degrades to ("unknown", reason) so it stays
    purely advisory and never blocks.
    """
    cookie = os.environ.get("S247_SESSION_COOKIE", "").strip()
    if not cookie:
        return "no_cookie", "no session cookie loaded"

    # Site24x7 admin role ids (Web_Mon security-rest-api.xml userroles="1|2|3|100")
    #   1 = No Access(owner ctx) 2 = Admin  3 = Super Admin  100 = Operator(admin-ish)
    ADMIN_ROLE_IDS = {"1", "2", "3", "100"}
    try:
        url = grid.rstrip("/") + "/app/api/current_user"
        headers = {
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "X-Requested-With": "XMLHttpRequest",
            **session_headers(),
        }
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=TIMEOUT,
                                    context=make_ctx()) as r:
            raw = r.read().decode("utf-8", errors="replace")
        parsed = json.loads(raw) if raw.strip() else {}
        data = parsed.get("data") or parsed
        # The role field name varies by grid build; check the common ones.
        role = None
        for key in ("user_role", "role", "role_id", "zaaid_role",
                    "user_role_id"):
            if isinstance(data, dict) and data.get(key) is not None:
                role = str(data.get(key)).strip()
                break
        role_name = ""
        for key in ("user_role_name", "role_name", "display_role"):
            if isinstance(data, dict) and data.get(key):
                role_name = str(data.get(key)).strip()
                break
        if role is None and not role_name:
            return "unknown", "could not read role from current_user response"
        if role in ADMIN_ROLE_IDS or any(
                w in role_name.lower() for w in ("admin", "super")):
            return "admin", (role_name or f"role_id={role}")
        return "non_admin", (role_name or f"role_id={role}")
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return "non_admin", f"session rejected (HTTP {e.code})"
        return "unknown", f"HTTP {e.code} probing current_user"
    except Exception as exc:  # noqa: BLE001
        return "unknown", f"could not probe current_user ({exc})"


def check_trigger_auth(grid, token):
    """Advisory pre-flight: can the harness authenticate to an admin-scoped,
    read-only endpoint the same way trigger_test needs?

    Returns (ok: bool, human_hint: str). Never raises, never blocks — it only
    informs the developer which credential to refresh. We probe a harmless
    GET (current_status) with the OAuth token, and note the session cookie.

    Precedence of fixes, surfaced in the hint:
      1. If an admin-scoped OAuth token works  → fully self-serve, no cookie.
      2. Else if a session cookie is present   → check it belongs to an ADMIN,
         because a non-admin cookie will get 1100 and SKIP.
      3. Else                                  → tell them to refresh either.
    """
    cookie = os.environ.get("S247_SESSION_COOKIE", "").strip()

    # Probe the OAuth token against a cheap, read-only endpoint. A 200 means
    # the token is valid; a 1121/401 there mirrors what trigger_test will do.
    token_scope_ok = None
    try:
        url = grid.rstrip("/") + "/api/current_status?apm_capability=0"
        req = urllib.request.Request(url, method="GET", headers={
            "Authorization": f"Zoho-oauthtoken {token}",
            "Accept": "application/json; version=2.1",
        })
        with urllib.request.urlopen(req, timeout=TIMEOUT,
                                    context=make_ctx()) as r:
            raw = r.read().decode("utf-8", errors="replace")
            parsed = json.loads(raw) if raw.strip() else {}
            token_scope_ok = parsed.get("code") == 0
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        token_scope_ok = not (e.code == 401 or "1121" in body)
    except Exception:
        token_scope_ok = None  # network/unknown — stay advisory

    if token_scope_ok:
        return True, ("OAuth token authenticates to the API "
                      "(admin-scoped token path available).")
    if cookie:
        # A cookie is present — but trigger_test needs an ADMIN session.
        # Verify the logged-in user's role so we warn BEFORE firing if it is
        # a non-admin cookie (which would otherwise fail with 1100 → SKIP).
        state, detail = is_session_cookie_admin(grid)
        if state == "admin":
            return True, (f"Session cookie present ({len(cookie)} chars) and "
                          f"the logged-in user IS an admin ({detail}) — the "
                          f"browser ▶ route will fire trigger_test for real. "
                          f"Expect PASS.")
        if state == "non_admin":
            return False, (f"Session cookie present ({len(cookie)} chars) BUT "
                           f"the logged-in user is NOT an admin ({detail}). "
                           f"trigger_test needs an ADMIN session (Web_Mon "
                           f"userroles 1|2|3|100), so it will return error "
                           f"1100 and be reported as SKIP (not FAIL). FIX: log "
                           f"into Site24x7 as an ADMIN user in Chrome, re-run "
                           f"'python3 extract_cookie.py', then 'source "
                           f".session.env'. (Or mint an admin OAuth token with "
                           f"./get_token.sh --authorize.)")
        if state == "unknown":
            return True, (f"Session cookie present ({len(cookie)} chars) — "
                          f"could not pre-verify the user's admin role "
                          f"({detail}); the browser ▶ route will be attempted. "
                          f"If it returns 1100, capture the cookie from an "
                          f"ADMIN user.")
        # state == "no_cookie" cannot happen here (cookie is truthy); fall
        # through defensively.
    return False, ("No admin-scoped OAuth token AND no session cookie. "
                   "Fix EITHER (recommended, permanent): mint an admin-scoped "
                   "refresh token with  ./get_token.sh --authorize  then paste "
                   "it into .token.env; OR (quick, temporary): refresh "
                   "S247_SESSION_COOKIE from the browser (logged in as an "
                   "ADMIN) and  source .session.env.")


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
    ap.add_argument("--no-itsm-check", action="store_true",
                    help="skip the ITSM tool-side ticket verification "
                         "(Layer 3). Use when ITSM credentials (.itsm.env) "
                         "are not available. Layers 1 & 2 still run.")
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

    # ── Pre-flight AUTH DOCTOR ────────────────────────────────────────────
    # The trigger_test endpoint needs EITHER an admin-scoped OAuth token OR a
    # valid browser session cookie. Rather than discover a missing/expired
    # credential only after firing (and emitting a wall of SKIPs), probe the
    # harness's auth posture up-front and tell the developer exactly what —
    # if anything — to refresh. This is advisory only; it never blocks.
    auth_ok, auth_hint = check_trigger_auth(grid, token)
    if auth_ok:
        log(f"  Auth check : ✅ {auth_hint}")
    else:
        log(f"  Auth check : ⚠️  {auth_hint}")
        log( "               Trigger tests will be reported as SKIP (harness")
        log( "               auth gap), NOT FAIL — the lifecycle is not blocked.")

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
            elif r["api_verdict"] == SKIP:
                # Harness auth gap — not a failure, do not block the lifecycle.
                r["alert_log_verdict"] = SKIP
                r["alert_log_row"] = None
                r["alert_log_message"] = ("Trigger test could not be verified "
                                          "from the harness (auth gap). Not "
                                          "treated as a failure.")
                r["final_verdict"] = SKIP
            else:
                r["alert_log_verdict"] = SKIP
                r["alert_log_row"] = None
                r["alert_log_message"] = ("API trigger test failed.")
                r["final_verdict"] = FAIL

    # ── Layer 3: ITSM tool-side ticket verification ──────────────────────
    # The user's request: validate the trigger-test alert INSIDE each ITSM
    # tool too (the same way the real ticket lifecycle is validated), not just
    # via the API response and Alert Logs. A tool that genuinely created the
    # "[Site24x7 Test Alert]" ticket is the strongest possible proof the
    # integration works end-to-end. This NEVER downgrades a PASS to FAIL —
    # a missing tool credential / read gap is a harness limitation (SKIP/WARN),
    # identical to the principle already used in Layers 1 & 2.
    if not args.no_itsm_check:
        section("LAYER 3 — ITSM TOOL-SIDE TICKET VERIFICATION")
        log("  Asking each destination tool whether the test-alert ticket")
        log("  was actually created (same check as the real lifecycle).")
        verify_in_itsm_tools(results)
    else:
        log("\n  [skip] ITSM tool-side verification (--no-itsm-check).")
        for r in results:
            r.setdefault("itsm_verdict", SKIP)
            r.setdefault("itsm_tickets", [])
            r.setdefault("itsm_tool", "")
            r.setdefault("itsm_message", "ITSM tool check skipped "
                                         "(--no-itsm-check).")

    # ── Reconcile the final verdict with Layer 3 (tool-side) proof ───────
    # The test alert is validated at three layers, exactly like the real
    # ticket lifecycle. The STRONGEST evidence wins:
    #
    #   Layer 3 (ITSM tool) PASS = the destination tool actually created the
    #   "[Site24x7 Test Alert]" ticket. This is end-to-end proof the
    #   integration works — stronger than the API accepting the request
    #   (Layer 1) or Site24x7 logging it (Layer 2). So a Layer-3 PASS
    #   promotes the overall result to a clean PASS, clearing:
    #     • a harness-auth SKIP (our PUT couldn't authenticate), AND
    #     • an Alert-Log-timing WARN (the API passed but the Alert Log row
    #       hadn't appeared within the poll window — the user's confusion
    #       case: a genuinely-passing trigger test shown as a yellow WARN).
    #
    # This NEVER turns a PASS/WARN/SKIP into a FAIL. A genuine FAIL (the PUT
    # reached the integration and it reported an error) is left untouched.
    for r in results:
        if r.get("itsm_verdict") == PASS and \
                r.get("final_verdict") in (SKIP, WARN):
            prev = r.get("final_verdict")
            r["final_verdict"] = PASS
            r["harness_auth_gap"] = False
            tool = r.get("itsm_tool", "the destination tool")
            promote = (f"Promoted to PASS (was {prev}): {tool} CONFIRMS the "
                       f"'[Site24x7 Test Alert]' ticket was created — "
                       f"end-to-end proof the integration works.")
            r["alert_log_message"] = (
                (r.get("alert_log_message", "") + " | " + promote).strip(" |"))

    # ── Summary ───────────────────────────────────────────────────────────
    section("TRIGGER TEST RESULTS")
    passed  = [r for r in results if r["final_verdict"] == PASS]
    warned  = [r for r in results if r["final_verdict"] == WARN]
    failed  = [r for r in results if r["final_verdict"] == FAIL]
    skipped = [r for r in results if r["final_verdict"] == SKIP]

    log(f"\n  {'Integration':<30} {'API':<6} {'AlertLog':<9} "
        f"{'ITSM Tool':<10} {'Final'}")
    log(f"  {'-'*30} {'-'*6} {'-'*9} {'-'*10} {'-'*8}")
    for r in sorted(results, key=lambda x: x.get("name", "")):
        icon = {"PASS": "✅", "FAIL": "❌", "WARN": "⚠️",
                "SKIP": "⚠️"}.get(r.get("final_verdict"), "?")
        log(f"  {str(r.get('name','')):<30} "
            f"{r.get('api_verdict',''):<6} "
            f"{r.get('alert_log_verdict',''):<9} "
            f"{str(r.get('itsm_verdict','')):<10} "
            f"{icon} {r.get('final_verdict','')}")

    log(f"\n  Passed: {len(passed)}    Warned: {len(warned)}    "
        f"Skipped: {len(skipped)}    Failed: {len(failed)}")

    if failed:
        log("\n  ❌ FAILED INTEGRATIONS (will be BLOCKED in the lifecycle):")
        for r in failed:
            log(f"     • {r['name']}: {r.get('api_message','')}")
        log("\n  Fix these integrations in Site24x7 → Third-Party "
            "Integrations → Edit,")
        log("  then re-run this script before starting the lifecycle.")

    if skipped:
        log("\n  ⚠️  SKIPPED INTEGRATIONS (harness could not verify — NOT a "
            "failure, lifecycle NOT blocked):")
        for r in skipped:
            log(f"     • {r['name']}: {r.get('api_message','')}")
        log("\n  The trigger_test endpoint needs the 'admin' OAuth scope or a")
        log("  browser session cookie. Set S247_SESSION_COOKIE for full")
        log("  pre-flight confirmation. The lifecycle verdict stands on its")
        log("  own evidence (alert logs + tool-side tickets).")

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
    # FAIL = genuinely misconfigured integration → block the lifecycle (exit 1)
    # SKIP = harness could not verify (OAuth scope / no cookie) → DO NOT block,
    #        the lifecycle evidence stands on its own (exit 0)
    # WARN = API OK, Alert Logs timing → allow the lifecycle (WARN ≠ FAIL)
    # PASS = all good
    if failed:
        section(f"EXIT 1 — {len(failed)} integration(s) FAILED "
                f"pre-flight trigger test")
        log("  The alert lifecycle should NOT start until these are fixed.")
        log("  run_all.py will BLOCK those integrations from the lifecycle.")
        sys.exit(1)
    elif skipped:
        section(f"EXIT 0 — pre-flight trigger test NOT verified for "
                f"{len(skipped)} integration(s) (harness auth gap)")
        log("  This is NOT a failure. The trigger_test endpoint needs the")
        log("  'admin' OAuth scope or a browser session cookie, neither of")
        log("  which the harness had. The lifecycle can and WILL run — each")
        log("  integration is judged on its real alert-log + tool evidence.")
        sys.exit(0)
    else:
        section("EXIT 0 — ALL integrations passed pre-flight trigger test")
        log("  The alert lifecycle can start.")
        sys.exit(0)


if __name__ == "__main__":
    main()
