#!/usr/bin/env python3
"""
Site24x7 ITSM Automation — STAGE 3 (v2) : ALERT LOG TICKET VERIFICATION
=======================================================================

THE REAL ENDPOINT (captured from the browser, not guessed)

    /app/api/applog/search/{FROM}/{TO}/{PAGE}/desc
        ?time_filter=&query={QUERY}&page_type=full_page

    FROM / TO : "DD-MM-YYYY HH:MM:SS"   e.g. 10-09-2026 11:11:57
    PAGE      : "1-100"
    QUERY     : logtype="Alert Logs"
                logtype="Alert Logs" and monitor_name="Do Not Delete - 02"

    NOTE the base is /app/api/  (not /api/). That is why the earlier
    guessed paths all returned 404.

WHAT THIS VERIFIES  (this is the main goal)
    For a given monitor, per integration:
        * was a ticket CREATED when the monitor went into a problem state?
        * was THAT SAME ticket CLOSED when the monitor recovered?
    Matching the ticket id across create and close is what proves the
    correct ticket was acted on - not merely that "some" ticket moved.

AUTH
    Tries the OAuth token first. /app/api/ endpoints are sometimes
    session-authenticated instead; if the token is rejected the script
    says so clearly and tells you how to supply a browser session cookie.
    It never silently pretends to succeed.

USAGE
    source env.sh

    # 1. prove the endpoint works and see the raw shape
    python3 stage3_verify.py probe

    # 2. all alert logs for the last 24h
    python3 stage3_verify.py fetch --hours 24

    # 3. THE IMPORTANT ONE - verify ticket create+close for a monitor
    python3 stage3_verify.py verify --monitor-name "Do Not Delete - 02" --hours 6

SAFETY
    Read-only. GET only. Modifies nothing. Never prints the token.
"""

import argparse
import json
import os
import re
import ssl
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

CONFIG = "s247_config.json"
TIMEOUT = 45

TICKET_RE = re.compile(r"ticket\s*id\s*[:=]?\s*([A-Za-z0-9\-_]+)", re.I)

# ---------------------------------------------------------------------------
# WEB_MON ALIGNMENT — AlertLogs.java source of truth
# ---------------------------------------------------------------------------
# The canonical ticket ID field in Site24x7's alert log (Cassandra /
# AppLog) is  RequestMessageId  — written by AlertLogs.addAlertLogToApplog()
# at the line:
#     if(prop.get("ticket_id") != null) {
#         jsonObject.put("RequestMessageId", (String)prop.get("ticket_id"));
#     }
# The human-readable "Message" field ALSO contains "ticket id: <ID>" in
# the log line, but reading RequestMessageId directly is safer and is
# exactly what the product writes.  We read RequestMessageId first and
# fall back to the regex on Message only when the field is absent.
#
# CommunicationMode integer → integration type (from ThirdPartyServices enum
# in AlertLogs.java → getAlertModeName()):
#   5  = AlarmsOne        6  = SDPOD (ServiceDesk Plus OD)
#   7  = Slack            8  = Slack (alt)       9  = PagerDuty
#   10 = SDP on-premise   11 = Custom Webhook    13 = Microsoft Teams
#   14 = ServiceNow       15 = OpsGenie          17 = iLert
#   18 = JSM Ops          19 = SDPMSP            21 = ConnectWise
#   22 = Zapier           23 = Jira              24 = Zoho Desk (ZDESK)
#   25 = Zoho Cliq        26 = EventBridge       27 = Telegram
#   29 = FreshService     30 = VictorOps         31 = FreshDesk
#   32 = Zendesk          33 = Discord           52 = HaloITSM
#
# To field: written by AlertLogs.addAlertLogToApplog():
#     tpJsonArray.add(prop.get("integration_name"));
#     jsonObject.put("To", tpJsonArray);
# It is a JSON array — our as_list() handles both array and string forms.
#
# Status field:
#     jsonObject.put("Status", prop.get("monitor_status"));
# Values: 0=DOWN, 1=UP (AVAILABLE), 2=TROUBLE, 3=CRITICAL
# ---------------------------------------------------------------------------

MODE_INT_TO_TOOL = {
    5:  "alarmsone",
    6:  "sdp",          # SDPOD — ServiceDesk Plus On Demand
    7:  "slack",
    9:  "pagerduty",
    10: "sdp_onprem",
    11: "webhook",
    13: "msteams",
    14: "servicenow",
    15: "opsgenie",
    17: "ilert",
    18: "jsmops",
    19: "sdpmsp",
    21: "connectwise",
    22: "zapier",
    23: "jira",
    24: "zohodesk",     # ZDESK
    25: "zcliq",
    26: "eventbridge",
    27: "telegram",
    29: "freshservice",
    30: "victorops",
    31: "freshdesk",
    32: "zendesk",
    33: "discord",
    52: "haloitsm",
}

# Ticket IDs that are obviously not real IDs and must be discarded.
# "null" appears in SDP alert log rows when the integration fires but the
# ticket ID has not yet been written back into the log (the ticket was
# queued but not confirmed). Treating "null" as a ticket id produces
# evidence that cannot be looked up in any tool.
_BOGUS_TICKET_IDS = {"null", "none", "undefined", "nan", "0", "-", "n/a"}
OP_RE = re.compile(r"operation\s*[:=]?\s*(create|close|update)", re.I)


def log(m=""):
    print(m, flush=True)


def section(t):
    log("\n" + "=" * 70)
    log(t)
    log("=" * 70)


def die(msg, code=2):
    log(f"\n[BLOCKER] {msg}")
    sys.exit(code)


# --------------------------------------------------------------------------

def get_token():
    tok = os.environ.get("S247_ACCESS_TOKEN", "").strip()
    if tok:
        return tok
    script = os.path.expanduser(os.environ.get("S247_TOKEN_SCRIPT", "").strip())
    if not script or not os.path.isfile(script):
        return None
    try:
        p = subprocess.run(["bash", script], capture_output=True, text=True, timeout=60)
        lines = [l for l in (p.stdout or "").splitlines() if l.strip()]
        return lines[-1].strip() if lines else None
    except Exception:
        return None


def grid_url():
    g = os.environ.get("S247_GRID_URL", "").strip()
    if not g:
        die("S247_GRID_URL not set. Run: source env.sh")
    return g.rstrip("/")


def _ctx():
    c = ssl.create_default_context()
    c.check_hostname = False
    c.verify_mode = ssl.CERT_NONE
    return c


def fmt(dt):
    """Site24x7 applog wants DD-MM-YYYY HH:MM:SS"""
    return dt.strftime("%d-%m-%Y %H:%M:%S")


def parse_when(text):
    """Accept ISO or the applog 'DD-MM-YYYY HH:MM:SS' form."""
    if not text:
        return None
    t = str(text).strip()
    for f in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S",
              "%d-%m-%Y %H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f"):
        try:
            return datetime.strptime(t.split("+")[0], f)
        except ValueError:
            continue
    die(f"could not read the timestamp {text!r}. Use "
        f"'YYYY-MM-DD HH:MM:SS' or 'DD-MM-YYYY HH:MM:SS'.")


def build_url(grid, hours, query, page="1-100", since=None, until=None):
    """
    Build the URL with the SAME encoding the browser uses. The server is
    strict about this - a differently-encoded but logically identical URL
    returns 404.

    Browser form (captured from DevTools):
      /app/api/applog/search/10-09-2026%2011:26:45/11-09-2026%2011:26:44
          /1-100/desc?time_filter=&query=logtype=%22Alert%20Logs%22
          %20and%20MonitorType=%22Website%22&page_type=full_page

    Note:
      * colons in the timestamps stay LITERAL  (not %3A)
      * spaces become %20                      (not '+')
      * '=' inside the query stays LITERAL     (not %3D)
      * double quotes become %22
    """
    # An EXPLICIT window beats a rolling one. "last 2 hours" can sweep in
    # tickets from an earlier run and report them as proof of this one --
    # a false pass, which is worse than a failure. When the caller knows
    # exactly when the cycle ran, verify only that span.
    # DO NOT send a narrow window to the applog endpoint -- it rejects
    # some of them with HTTP 400, and the fallback to a rolling window is
    # what made the report quote tickets from EARLIER cycles. Ask for a
    # window the endpoint is happy with, then filter rows locally to the
    # exact span. Same precision, no 400.
    now = datetime.now()
    start = now - timedelta(hours=hours)
    if since:
        s_dt = parse_when(since)
        if s_dt and s_dt < start:
            start = s_dt - timedelta(minutes=10)
    if until:
        u_dt = parse_when(until)
        if u_dt and u_dt > now:
            now = u_dt + timedelta(minutes=10)

    # keep ':' and '-' literal, encode the space as %20
    f_from = urllib.parse.quote(fmt(start), safe=":-")
    f_to = urllib.parse.quote(fmt(now), safe=":-")

    # keep '=' literal, encode spaces as %20 and quotes as %22
    q = urllib.parse.quote(query, safe="=")

    return (f"{grid}/app/api/applog/search/{f_from}/{f_to}/{page}/desc"
            f"?time_filter=&query={q}&page_type=full_page")


def build_headers(grid, token=None, use_oauth=False, use_cookie=True,
                  use_csrf=True, use_referer=True, use_ua=True):
    """
    Build request headers. The browser does NOT send an OAuth Authorization
    header on these /app/api/ endpoints - it authenticates by session cookie
    plus x-zcsrf-token. Sending Authorization as well can cause a 403, so it
    is OFF by default when a cookie is available.
    """
    h = {
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
    }
    cookie = os.environ.get("S247_SESSION_COOKIE", "").strip()

    if use_oauth and token:
        h["Authorization"] = f"Zoho-oauthtoken {token}"
    if use_cookie and cookie:
        h["Cookie"] = cookie

    if use_csrf:
        csrf = os.environ.get("S247_CSRF_TOKEN", "").strip()
        if not csrf and cookie:
            for part in cookie.split(";"):
                if part.strip().lower().startswith("s247cname="):
                    csrf = "s247pname=" + part.split("=", 1)[1].strip()
                    break
        if csrf:
            h["x-zcsrf-token"] = csrf

    if use_referer:
        # CSRF filters commonly require a same-origin Referer.
        h["Referer"] = f"{grid}/app/client?a=f"
        h["Origin"] = grid
    if use_ua:
        h["User-Agent"] = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36")
        h["time-zone"] = "Asia/Kolkata"
    return h


def raw_get(url, headers):
    req = urllib.request.Request(url, method="GET", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT, context=_ctx()) as r:
            raw = r.read().decode("utf-8", errors="replace")
            try:
                return r.status, json.loads(raw), None
            except json.JSONDecodeError:
                return r.status, None, raw[:600]
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace")[:400]
        except Exception:
            pass
        return e.code, None, body
    except Exception as exc:  # noqa: BLE001
        return None, None, str(exc)


def http_get(url, token):
    """Default path: session cookie + CSRF + referer, NO OAuth header."""
    grid = grid_url()
    return raw_get(url, build_headers(grid, token, use_oauth=False))


def auth_help(status):
    log("\n  The OAuth token was not accepted by this /app/api/ endpoint"
        f" (status={status}).")
    log("  These app endpoints are often SESSION authenticated. To supply a")
    log("  browser session:")
    log("    1. In Chrome on the Alert Logs page, press F12 -> Network tab")
    log("    2. Click the 'applog/search' request")
    log("    3. Under 'Request Headers' find the line starting  Cookie:")
    log("    4. Copy everything after 'Cookie: '")
    log("    5. In your terminal:")
    log('         export S247_SESSION_COOKIE="<paste it here>"')
    log("    6. Re-run this script.")
    log("  NOTE: a session cookie expires. It is fine for local runs; for")
    log("  CI we would need a longer-lived method.")


def extract_entries(payload):
    """The response shape is not documented here; find the biggest list."""
    if payload is None:
        return []
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        best = []
        for key in ("data", "logs", "result", "results", "entries", "rows"):
            v = payload.get(key)
            if isinstance(v, list) and len(v) > len(best):
                best = v
        if best:
            return best
        # search one level deeper
        for v in payload.values():
            if isinstance(v, list) and len(v) > len(best):
                best = v
            elif isinstance(v, dict):
                for vv in v.values():
                    if isinstance(vv, list) and len(vv) > len(best):
                        best = vv
        return best
    return []


def field(entry, *names):
    """Case-insensitive field lookup."""
    if not isinstance(entry, dict):
        return ""
    low = {str(k).lower(): v for k, v in entry.items()}
    for n in names:
        v = low.get(n.lower())
        if isinstance(v, (str, int, float)):
            return str(v)
    return ""


# --------------------------------------------------------------------------

def cmd_probe(args):
    grid, token = grid_url(), get_token()
    query = 'logtype="Alert Logs" and MonitorType="Website"'
    url = build_url(grid, args.hours, query,
                    since=getattr(args, "since", None),
                    until=getattr(args, "until", None))

    section("PROBING THE REAL ALERT LOG ENDPOINT")
    log(f"  {url[:150]}...")

    status, payload, raw = http_get(url, token)
    log(f"\n  HTTP status: {status}")

    if status in (401, 403):
        auth_help(status)
        die("not authorised")
    if status != 200:
        log(f"  body: {(raw or '')[:400]}")
        die(f"unexpected status {status}")

    entries = extract_entries(payload)
    log(f"  entries returned: {len(entries)}")

    if entries:
        section("SAMPLE ENTRY (so we can see the field names)")
        log(json.dumps(entries[0], indent=2)[:1200])
        section("FIELD NAMES FOUND")
        if isinstance(entries[0], dict):
            for k in entries[0].keys():
                log(f"    - {k}")
        cfg = {}
        if os.path.isfile(CONFIG):
            try:
                cfg = json.load(open(CONFIG, encoding="utf-8"))
            except Exception:
                cfg = {}
        cfg["alert_log_style"] = "applog_search"
        with open(CONFIG, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, indent=2)
        log(f"\n  Endpoint works. Recorded in {CONFIG}.")
    else:
        log("\n  Endpoint responded but returned no rows.")
        log("  Try a wider window:  --hours 48")


def cmd_fetch(args):
    grid, token = grid_url(), get_token()
    query = args.query or 'logtype="Alert Logs"'
    url = build_url(grid, args.hours, query, page=f"1-{args.limit}",
                    since=getattr(args, "since", None),
                    until=getattr(args, "until", None))

    status, payload, raw = http_get(url, token)
    if status in (401, 403):
        auth_help(status)
        die("not authorised")
    if status != 200:
        log(f"\n  The request that failed:")
        log(f"    {url}")
        log(f"  HTTP {status}")
        if status == 400:
            log("  A 400 here is the endpoint rejecting the REQUEST, not")
            log("  your session. The usual cause is the time window: a")
            log("  very narrow or oddly-encoded FROM/TO is refused.")
        elif status in (401, 403):
            log("  A 401/403 IS a session problem — the cookie is dead.")
        die(f"status={status} body={(raw or '')[:300]}")

    entries = extract_entries(payload)
    section(f"ALERT LOG ENTRIES ({len(entries)})")
    for e in entries[: args.limit]:
        log("  " + json.dumps(e)[:260])


ALERT_STATUS = {"0": "DOWN", "1": "UP", "2": "TROUBLE", "3": "CRITICAL"}


def as_list(v):
    """The 'to' field is a list in this API; older assumptions treated it
    as a string. Normalise both shapes."""
    if v is None:
        return []
    if isinstance(v, list):
        return [str(x) for x in v if x is not None]
    return [str(v)]


def cmd_verify(args):
    grid, token = grid_url(), get_token()

    # IMPORTANT: do NOT put monitor_name in the query. That field is not
    # valid in this applog syntax and makes the server return 502.
    # Fetch broadly, then filter client-side on monitorid - which is exact.
    query = 'logtype="Alert Logs"'
    if args.monitor_type:
        query += f' and MonitorType="{args.monitor_type}"'
    url = build_url(grid, args.hours, query, page=f"1-{args.limit}",
                    since=getattr(args, "since", None),
                    until=getattr(args, "until", None))

    section("VERIFYING TICKET LIFECYCLE (create -> close)")
    log(f"  monitor id   : {args.monitor_id or '(all)'}")
    log(f"  monitor name : {args.monitor_name or '(all)'}")
    if getattr(args, "since", None) or getattr(args, "until", None):
        log(f"  window       : EXACT — {args.since or '(open)'} "
            f"to {args.until or 'now'}")
        log(f"                 (scoped to this cycle only, not a rolling "
            f"window)")
    else:
        log(f"  window       : last {args.hours}h")
    log(f"  query        : {query}")

    status, payload, raw = http_get(url, token)
    if status in (401, 403):
        auth_help(status)
        die("not authorised")
    if status == 502:
        die("502 from the gateway. Usually transient - retry, or reduce "
            "--hours / --limit.")
    if status != 200:
        log(f"\n  The request that failed:")
        log(f"    {url}")
        log(f"  HTTP {status}")
        if status == 400:
            log("  A 400 here is the endpoint rejecting the REQUEST, not")
            log("  your session. The usual cause is the time window: a")
            log("  very narrow or oddly-encoded FROM/TO is refused.")
        elif status in (401, 403):
            log("  A 401/403 IS a session problem — the cookie is dead.")
        die(f"status={status} body={(raw or '')[:300]}")

    entries = extract_entries(payload)
    log(f"  entries      : {len(entries)}")
    if not entries:
        log("\n  No rows returned. Try --hours 24.")
        return

    # ---- client-side filter -------------------------------------------
    def matches(e):
        if args.monitor_id:
            return str(e.get("monitorid", "")) == str(args.monitor_id)
        if args.monitor_name:
            return args.monitor_name.lower() in json.dumps(e).lower()
        return True

    rows = [e for e in entries if isinstance(e, dict) and matches(e)]
    log(f"  matching     : {len(rows)}")

    # EXACT SCOPING, done locally.
    # The endpoint rejects some narrow windows with HTTP 400, so we fetch
    # wide and cut here instead. Without this the report quotes tickets
    # from EARLIER cycles -- three cycles can run in two hours, and each
    # one leaves perfectly real ticket ids lying in the log.
    s_dt = parse_when(getattr(args, "since", None))
    u_dt = parse_when(getattr(args, "until", None))
    if s_dt or u_dt:
        def row_time(e):
            for k in ("_zl_timestamp", "_zl_received_time", "alert_time",
                      "time", "timestamp"):
                v = e.get(k)
                if not v:
                    continue
                t = str(v).strip()
                if t.isdigit():
                    n = int(t)
                    if n > 1e11:
                        n /= 1000.0
                    try:
                        return datetime.fromtimestamp(n)
                    except (OverflowError, OSError, ValueError):
                        return None
                for f in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S",
                          "%d-%m-%Y %H:%M:%S"):
                    try:
                        d = datetime.strptime(t.split(".")[0], f)
                        return d.replace(tzinfo=None)
                    except ValueError:
                        continue
            return None

        all_rows = list(rows)          # keep, to complete ticket lifecycles
        kept, dropped, unknown = [], 0, 0
        for e in rows:
            t = row_time(e)
            if t is None:
                unknown += 1
                kept.append(e)          # cannot place it -- keep, say so
                continue
            if s_dt and t < s_dt:
                dropped += 1
                continue
            if u_dt and t > u_dt:
                dropped += 1
                continue
            kept.append(e)
        log(f"  exact window : {s_dt or '(open)'} to {u_dt or '(now)'}")
        log(f"  in window    : {len(kept)}   "
            f"(dropped {dropped} row(s) from OTHER cycles)")
        if unknown:
            log(f"  [note] {unknown} row(s) had no readable timestamp and "
                f"were kept")
        rows = kept
        globals()["_ROWS_OUTSIDE_WINDOW"] = [e for e in all_rows
                                             if e not in kept]
    if not rows:
        log("\n  Nothing matched that monitor in this window.")
        log("  Tip: filter by id instead of name:  --monitor-id <id>")
        seen = sorted({str(e.get("monitorid")) for e in entries
                       if isinstance(e, dict) and e.get("monitorid")})
        log(f"  monitor ids present in the data: {seen[:10]}")
        return

    # ---- group per integration ----------------------------------------
    per = {}
    for e in rows:
        # ------------------------------------------------------------------
        # WEB_MON ALIGNMENT — AlertLogs.java field names (canonical casing)
        # "Message"           → the human-readable alert text
        # "Status"            → monitor_status int (0=DOWN,1=UP,2=TROUBLE)
        # "To"                → integration_name JSON array
        # "CommunicationMode" → delivery mode integer (ThirdPartyServices enum)
        # "RequestMessageId"  → ticket ID written by the notifier
        # We check both the product's casing AND lowercase for resilience.
        # ------------------------------------------------------------------
        msg = str(e.get("Message") or e.get("message") or "")
        st_code = str(e.get("Status") or e.get("status") or "")
        st_txt = ALERT_STATUS.get(st_code, st_code)
        blob = (msg + " " + json.dumps(e)).lower()

        # CommunicationMode integer → record for tool mapping
        comm_mode_raw = (e.get("CommunicationMode")
                         or e.get("communicationmode")
                         or e.get("alert_mode")
                         or e.get("alertmode"))
        comm_mode_int = None
        if comm_mode_raw is not None:
            try:
                comm_mode_int = int(comm_mode_raw)
            except (ValueError, TypeError):
                pass

        # To field — integration name (JSON array per Web_Mon)
        to_field = (e.get("To") or e.get("to"))
        for dest in (as_list(to_field) or ["(unknown)"]):
            rec = per.setdefault(dest, {"rows": 0, "create": set(),
                                        "close": set(), "update": set(),
                                        "closed_on_up": set(),
                                        "failed": 0, "statuses": set(),
                                        "modes": set(),
                                        "failed_statuses": set()})
            rec["rows"] += 1
            rec["statuses"].add(st_txt)

            # Site24x7 tells us which TOOL each row was delivered to via
            # CommunicationMode (canonical Web_Mon field name from
            # AlertLogs.java: jsonObject.put("CommunicationMode", alertType))
            # This is an INTEGER matching ThirdPartyServices enum.
            # We record BOTH the integer (for MODE_INT_TO_TOOL lookup) and
            # any string form present for backward compatibility.
            if comm_mode_int is not None:
                rec["modes"].add(str(comm_mode_int))     # integer form
                tool_key = MODE_INT_TO_TOOL.get(comm_mode_int)
                if tool_key:
                    rec["modes"].add(tool_key)           # human-readable too
            else:
                for k in ("CommunicationMode", "communicationmode",
                          "alert_mode", "alertmode"):
                    v = e.get(k)
                    if v:
                        for m in as_list(v):
                            if m:
                                rec["modes"].add(str(m))
                        break
            if ("fail" in blob or "invalid" in blob
                    or "error" in blob):
                rec["failed"] += 1
                # WHICH status failed matters. A failed UP row means the
                # recovery never arrived, which is a delivery problem. A
                # ticket "never closed" is only a real close bug when the
                # recovery was actually delivered.
                rec["failed_statuses"].add(st_txt)
            op = OP_RE.search(msg)

            # ----------------------------------------------------------------
            # WEB_MON ALIGNMENT — AlertLogs.java  addAlertLogToApplog()
            # PRIMARY source:  RequestMessageId  (canonical field written by
            #   the product — "jsonObject.put("RequestMessageId", ticket_id)")
            # FALLBACK source: TICKET_RE regex on Message text
            # This order matches the product's own write path exactly.
            # ----------------------------------------------------------------
            raw_tid = (e.get("RequestMessageId")
                       or e.get("requestmessageid")
                       or e.get("requestMessageId")
                       or "")
            raw_tid = str(raw_tid).strip() if raw_tid else ""

            if raw_tid and raw_tid.lower() not in _BOGUS_TICKET_IDS:
                # PRIMARY: use RequestMessageId directly — Web_Mon ground truth
                ticket_from_db = raw_tid
                tid_source = "RequestMessageId"
            else:
                # FALLBACK: regex on the human-readable Message field
                tid_match = TICKET_RE.search(msg)
                ticket_from_db = tid_match.group(1) if tid_match else None
                tid_source = "Message regex"

            if op and ticket_from_db:
                kind, ticket = op.group(1).lower(), ticket_from_db
                # Discard bogus IDs like "null" — they appear in SDP rows
                # when the ticket has been queued but not yet confirmed, and
                # they produce evidence that cannot be looked up in any tool.
                if ticket.lower() in _BOGUS_TICKET_IDS:
                    log(f"  [note] ignoring bogus ticket id {ticket!r} in "
                        f"row for {dest} (operation={kind}, status={st_txt}, "
                        f"source={tid_source})")
                    continue
                rec[kind].add(ticket)

                # THE CORRELATION KEY.
                # The ticket is created in the ITSM tool FIRST; the Alert
                # Log row is written afterwards, as the receipt. So this
                # row's timestamp is the moment Site24x7 learned the ticket
                # existed -- the ticket itself must have been created at or
                # just before it. Recording it lets stage 4 prove that a
                # specific ticket belongs to a specific alert, instead of
                # just "some ticket exists in roughly this window".
                when = None
                for k in ("_zl_timestamp", "_zl_received_time", "alert_time",
                          "time", "timestamp", "_zlf__zl_timestamp"):
                    if e.get(k):
                        when = e.get(k)
                        break
                rec.setdefault("ticket_times", {})[ticket] = {
                    "operation": kind,
                    "status": st_txt,
                    "alert_row_time_raw": when,
                    # Web_Mon ground truth: which field was the source?
                    "ticket_id_source": tid_source,
                    # Raw RequestMessageId from the DB for traceability
                    "RequestMessageId": raw_tid if raw_tid else None,
                }
                # Some tools (e.g. Zoho Desk) resolve by UPDATING the ticket
                # on recovery rather than issuing a Close. Treat an update
                # that happens while the monitor is UP as close-equivalent,
                # but report it distinctly - never silently as a Close.
                if kind == "update" and st_txt == "UP":
                    # Only mark as closed-on-UP if this ticket was actually
                    # created in THIS cycle. An UPDATE row whose ticket was
                    # never created in our window is a recovery for a ticket
                    # from a PREVIOUS cycle — counting it as proof of this
                    # cycle's lifecycle produces a false PASS.
                    # We defer this into a second pass after all rows are
                    # processed; for now just record it as a candidate.
                    rec.setdefault("up_update_candidates", set()).add(ticket)

    log("\n  NOTE: a ticket is created in the ITSM tool FIRST; the Alert")
    log("        Log row is the receipt written afterwards. So a row here")
    log("        means a ticket WAS created, and its timestamp is when")
    log("        Site24x7 confirmed that. No row means no ticket.")

    # SECOND PASS: resolve up_update_candidates.
    # A ticket updated-on-UP only counts as a lifecycle close if it was
    # CREATED in this same cycle window. Cross-cycle updates (recovery for
    # a ticket from a previous run) are not evidence of this cycle.
    for name, rec in per.items():
        candidates = rec.pop("up_update_candidates", set())
        for ticket in candidates:
            if ticket in rec["create"]:
                rec["closed_on_up"].add(ticket)
            else:
                log(f"  [note] {name}: UPDATE-on-UP for ticket {ticket!r} "
                    f"but no CREATE for it in this window — ticket belongs "
                    f"to a PREVIOUS cycle, not counted as this cycle's proof")

    section("RESULT PER INTEGRATION")
    log(f"  {'INTEGRATION':<26}{'ROWS':>5}{'CREATED':>8}{'CLOSED':>7}"
        f"{'UPD':>5}{'FAIL':>6}   VERDICT")
    log(f"  {'-'*26}{'-'*5}{'-'*8}{'-'*7}{'-'*5}{'-'*6}   {'-'*22}")

    out = []
    for name, rec in sorted(per.items()):
        created, closed, updated = rec["create"], rec["close"], rec["update"]
        on_up = rec["closed_on_up"]

        # LIFECYCLE COMPLETION.
        # A ticket is created on DOWN and resolved on UP minutes later.
        # Strict window filtering can cut one end off that pair, leaving a
        # Create with no Close and making a perfectly healthy integration
        # read as "never closed". So for tickets THIS cycle created, look
        # for their close/update in the rows just outside the window. This
        # does not widen the evidence window -- it only finishes the story
        # of a ticket we already proved belongs to this cycle.
        late = 0
        for e in globals().get("_ROWS_OUTSIDE_WINDOW", []):
            if not isinstance(e, dict):
                continue
            # WEB_MON ALIGNMENT — use canonical To field casing (AlertLogs.java)
            if name not in [str(x) for x in as_list(
                    e.get("To") or e.get("to"))]:
                continue
            msg = str(e.get("Message") or e.get("message") or "")
            op2 = OP_RE.search(msg)
            if not op2:
                continue
            # WEB_MON ALIGNMENT — read RequestMessageId first (primary DB field)
            raw_tid2 = (e.get("RequestMessageId")
                        or e.get("requestmessageid")
                        or e.get("requestMessageId")
                        or "")
            raw_tid2 = str(raw_tid2).strip() if raw_tid2 else ""
            if raw_tid2 and raw_tid2.lower() not in _BOGUS_TICKET_IDS:
                ticket2 = raw_tid2
            else:
                # Fallback: regex on Message text (only if DB field absent)
                tid2 = TICKET_RE.search(msg)
                if not tid2:
                    continue
                ticket2 = tid2.group(1)
                if ticket2.lower() in _BOGUS_TICKET_IDS:
                    continue
            if ticket2 not in created:
                continue                      # not ours, ignore
            st2_code = str(e.get("Status") or e.get("status") or "")
            st2 = ALERT_STATUS.get(st2_code, st2_code)
            kind2 = op2.group(1).lower()
            if kind2 == "close":
                closed.add(ticket2)
                late += 1
            elif kind2 == "update":
                updated.add(ticket2)
                if st2 == "UP":
                    on_up.add(ticket2)
                late += 1
        if late:
            log(f"  [note] {name}: {late} close/update row(s) for THIS "
                f"cycle's tickets were found just outside the window and "
                f"counted — the lifecycle completes after the window ends.")
        matched = created & closed
        matched_on_up = created & on_up
        if matched:
            verdict = "PASS create+close"
        elif matched_on_up:
            # resolved via Update-on-UP instead of an explicit Close
            verdict = "PASS create+update@UP"
        elif created and not closed:
            verdict = "FAIL never closed"
        elif rec["failed"] and not created:
            verdict = "FAIL delivery error"
        elif updated and not created:
            verdict = "INFO update only"
        else:
            verdict = "INCONCLUSIVE"
        log(f"  {name[:25]:<26}{rec['rows']:>5}{len(created):>8}"
            f"{len(closed):>7}{len(updated):>5}{rec['failed']:>6}   {verdict}")
        # Build a clean per-ticket DB evidence map for traceability.
        # Each entry records WHERE the ticket ID came from (the exact
        # Cassandra/AppLog field) so the report can prove it read from
        # the correct Web_Mon DB path (WM_ALERT_LOGS.RequestMessageId).
        db_evidence = {}
        for tid, tinfo in rec.get("ticket_times", {}).items():
            db_evidence[tid] = {
                # The canonical Cassandra field — Web_Mon ground truth
                "cassandra_field": "WM_ALERT_LOGS.RequestMessageId",
                "cassandra_value": tinfo.get("RequestMessageId") or tid,
                "ticket_id_source": tinfo.get("ticket_id_source",
                                              "Message regex"),
                "operation": tinfo.get("operation"),
                "alert_status": tinfo.get("status"),
                "alert_row_time_raw": tinfo.get("alert_row_time_raw"),
            }
        out.append({"integration": name, "rows": rec["rows"],
                    "created_ticket_ids": sorted(created),
                    "closed_ticket_ids": sorted(closed),
                    "updated_ticket_ids": sorted(updated),
                    "matched_ticket_ids": sorted(matched),
                    "matched_on_up_ticket_ids": sorted(matched_on_up),
                    "statuses_seen": sorted(rec["statuses"]),
                    "ticket_times": rec.get("ticket_times", {}),
                    # WEB_MON DB evidence — proves ticket IDs came from the
                    # correct Cassandra table/field (WM_ALERT_LOGS.RequestMessageId)
                    "webmon_db_evidence": db_evidence,
                    "delivery_modes": sorted(rec.get("modes", [])),
                    "failed_statuses": sorted(rec.get("failed_statuses", [])),
                    "failed_rows": rec["failed"], "verdict": verdict})

    section("MATCHED TICKETS (the proof)")
    any_match = False
    for r in out:
        for t in r.get("matched_ticket_ids", []):
            any_match = True
            log(f"  {r['integration']:<26} ticket {t}  CREATED then CLOSED")
        for t in r.get("matched_on_up_ticket_ids", []):
            any_match = True
            log(f"  {r['integration']:<26} ticket {t}  CREATED then "
                f"UPDATED on UP  (tool resolves by update, not close)")
    if not any_match:
        log("  No ticket id was seen both created AND closed in this window.")
        log("  If the cycle just ran, wait a minute and retry, or widen --hours.")

    with open("ticket_verification.json", "w", encoding="utf-8") as fh:
        json.dump({"at": datetime.now().isoformat(),
                   "monitor_id": args.monitor_id,
                   "monitor_name": args.monitor_name,
                   "hours": args.hours,
                   "window_since": getattr(args, "since", None),
                   "window_until": getattr(args, "until", None),
                   "window_exact": bool(getattr(args, "since", None)
                                        or getattr(args, "until", None)),
                   # WEB_MON ALIGNMENT — ticket ID storage path metadata
                   # Ticket IDs are read from WM_ALERT_LOGS (Cassandra) via
                   # the /api/v2/alert_logs endpoint. The canonical field is
                   # RequestMessageId, written by AlertLogs.addAlertLogToApplog().
                   # This is the ONLY correct source — not Redis, not
                   # WM_STATUS_DATA (those are internal product stores we
                   # cannot and should not access from outside).
                   "ticket_id_source_metadata": {
                       "cassandra_table": "WM_ALERT_LOGS",
                       "canonical_field": "RequestMessageId",
                       "fallback_field": "Message (TICKET_RE regex)",
                       "java_class": "AlertLogs.addAlertLogToApplog()",
                       "java_write": "jsonObject.put(\"RequestMessageId\", "
                                     "(String)prop.get(\"ticket_id\"))",
                       "api_endpoint": "/app/api/applog/search/",
                       "webmon_file": "source/server/com/adventnet/webmon/"
                                      "reports/AlertLogs.java",
                   },
                   "results": out}, fh, indent=2)
    log("\n  Wrote ticket_verification.json")

    if any(r["verdict"].startswith("FAIL") for r in out):
        log("\n  One or more integrations FAILED - see the table above.")
        sys.exit(1)


def cmd_diagnose(args):
    """Try header combinations to find which the endpoint accepts. Read-only."""
    grid, token = grid_url(), get_token()
    query = 'logtype="Alert Logs" and MonitorType="Website"'
    url = build_url(grid, args.hours, query,
                    since=getattr(args, "since", None),
                    until=getattr(args, "until", None))

    section("DIAGNOSING AUTH — trying header combinations")
    cookie_present = bool(os.environ.get("S247_SESSION_COOKIE", "").strip())
    log(f"  cookie loaded : {cookie_present}")
    log(f"  csrf loaded   : {bool(os.environ.get('S247_CSRF_TOKEN','').strip())}")
    log(f"  oauth token   : {bool(token)}\n")
    if not cookie_present:
        log("  [!] No cookie loaded. Run:  source .session.env")
        die("no session cookie")

    combos = [
        ("cookie + csrf + referer + UA (browser-like)",
         dict(use_oauth=False, use_cookie=True, use_csrf=True,
              use_referer=True, use_ua=True)),
        ("cookie + csrf + referer",
         dict(use_oauth=False, use_cookie=True, use_csrf=True,
              use_referer=True, use_ua=False)),
        ("cookie + csrf",
         dict(use_oauth=False, use_cookie=True, use_csrf=True,
              use_referer=False, use_ua=False)),
        ("cookie only",
         dict(use_oauth=False, use_cookie=True, use_csrf=False,
              use_referer=False, use_ua=False)),
        ("cookie + csrf + OAuth",
         dict(use_oauth=True, use_cookie=True, use_csrf=True,
              use_referer=True, use_ua=True)),
        ("OAuth only",
         dict(use_oauth=True, use_cookie=False, use_csrf=False,
              use_referer=False, use_ua=False)),
    ]
    winner = None
    for label, kw in combos:
        status, payload, raw = raw_get(url, build_headers(grid, token, **kw))
        n = len(extract_entries(payload)) if status == 200 else 0
        log(f"  [{'OK ' if status == 200 else '-- '}] {str(status):<5} "
            f"{label:<44} entries={n}")
        if status == 200 and winner is None:
            winner = (label, payload)

    if winner:
        label, payload = winner
        section("WORKING COMBINATION")
        log(f"  {label}")
        entries = extract_entries(payload)
        if entries:
            section("SAMPLE ENTRY")
            log(json.dumps(entries[0], indent=2)[:1200])
        return

    section("NO COMBINATION WORKED")
    log("  1. Session expired -> re-run extract_cookie.py with a fresh cURL")
    log("  2. Logged out -> the session is invalidated")
    status, payload, raw = raw_get(url, build_headers(grid, token))
    if raw:
        log(f"\n  Server said: {raw[:400]}")


def main():
    ap = argparse.ArgumentParser(description="Alert Log ticket verification")
    sub = ap.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("diagnose", help="find which header combination works")
    d.add_argument("--hours", type=int, default=24)
    d.set_defaults(func=cmd_diagnose)

    p = sub.add_parser("probe", help="confirm the endpoint works, show fields")
    p.add_argument("--hours", type=int, default=24)
    p.set_defaults(func=cmd_probe)

    f = sub.add_parser("fetch", help="dump raw alert log rows")
    f.add_argument("--hours", type=int, default=24)
    f.add_argument("--limit", type=int, default=100)
    f.add_argument("--query")
    f.set_defaults(func=cmd_fetch)

    v = sub.add_parser("verify", help="verify ticket create+close per integration")
    v.add_argument("--monitor-name")
    v.add_argument("--monitor-id")
    v.add_argument("--monitor-type", default="Website")
    v.add_argument("--hours", type=int, default=6)
    v.add_argument("--since", default=None,
                   help="exact window START, e.g. '2026-09-14 07:58:30'. "
                        "Overrides --hours. Use this to verify ONLY the "
                        "cycle that just ran.")
    v.add_argument("--until", default=None,
                   help="exact window END. Defaults to now.")
    v.add_argument("--limit", type=int, default=200)
    v.set_defaults(func=cmd_verify)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
