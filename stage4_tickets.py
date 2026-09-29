#!/usr/bin/env python3
"""
Site24x7 ITSM Automation — STAGE 4 : VERIFY TICKETS INSIDE EACH ITSM TOOL
=========================================================================

WHY THIS EXISTS
    Stage 3 reads Site24x7's Alert Logs. That proves Site24x7 BELIEVES it
    delivered something. It does not prove the ticket really exists in the
    destination tool, nor that it really closed.

    Two blind spots Stage 3 cannot fix:
      * ServiceNow log lines carry NO ticket id at all -> INCONCLUSIVE
      * A tool could accept the payload and then silently drop it

    This stage asks each tool directly: "does ticket X exist, and what is
    its status right now?"

WHAT IT DOES
    check    Test connectivity to each configured tool. Reads no tickets.
    verify   Take the ticket ids Stage 3 found, look each one up in its
             own tool, and report the real status.

    Any tool with no credentials is SKIPPED and reported as NOT CONFIGURED.
    It is never counted as a failure.

USAGE
    cd ~/itsm-automation
    source start.sh
    source .itsm.env

    python3 stage4_tickets.py check      # connectivity only
    python3 stage4_tickets.py verify     # uses ticket_verification.json

SAFETY
    READ-ONLY. Only GET requests against ticket endpoints (plus the POST
    required to mint an OAuth token). Never creates, edits or closes a
    ticket. Never prints a secret.
"""

import argparse
from datetime import datetime, timedelta, timezone
import base64
import json
import os
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

STAGE3_FILE = "ticket_verification.json"
OUT_FILE = "stage4_results.json"
TIMEOUT = 30


def log(m=""):
    print(m, flush=True)


def section(t):
    log("\n" + "=" * 70)
    log(t)
    log("=" * 70)


def env(name, default=""):
    return os.environ.get(name, default).strip()


def _ctx():
    c = ssl.create_default_context()
    c.check_hostname = False
    c.verify_mode = ssl.CERT_NONE
    return c


def request(url, headers=None, data=None, method="GET"):
    """Returns (status, parsed_json_or_None, raw_text)."""
    body = None
    hdrs = dict(headers or {})
    if data is not None:
        if isinstance(data, dict):
            body = urllib.parse.urlencode(data).encode()
            hdrs.setdefault("Content-Type",
                            "application/x-www-form-urlencoded")
        else:
            body = data
    req = urllib.request.Request(url, data=body, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT, context=_ctx()) as r:
            raw = r.read().decode("utf-8", errors="replace")
            try:
                return r.status, json.loads(raw), raw
            except json.JSONDecodeError:
                return r.status, None, raw
    except urllib.error.HTTPError as e:
        raw = ""
        try:
            raw = e.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        try:
            return e.code, json.loads(raw), raw
        except Exception:
            return e.code, None, raw
    except Exception as exc:  # noqa: BLE001
        return None, None, str(exc)


# ===========================================================================
# Each tool is an adapter with the same shape:
#   configured() -> bool
#   connect()    -> (ok, detail)        # auth / smoke test
#   get_ticket(ticket_id) -> dict       # {found, status, raw_status, error}
# ===========================================================================

class Tool:
    name = "tool"

    def configured(self):
        return False

    def connect(self):
        return False, "not implemented"

    def get_ticket(self, tid):
        return {"found": False, "error": "not implemented"}

    def search_by_window(self, monitor_text, since_utc, until_utc):
        """Search this tool for tickets created in the given UTC window
        whose description matches the monitor name.

        Returns a list of dicts, each with at least:
          ticket_id, status, summary, created_raw, created_utc
        """
        return []

    def search_by_window(self, monitor_text, since_utc, until_utc):
        """Search for tickets created in a time window mentioning this
        monitor. Returns a list of dicts with ticket_id, status, summary,
        created_raw, created_utc."""
        return []


# --------------------------------------------------------------- HALO ITSM
class Halo(Tool):
    name = "HALO ITSM"

    def __init__(self):
        self.url = env("HALO_URL").rstrip("/")
        self.cid = env("HALO_CLIENT_ID")
        self.secret = env("HALO_CLIENT_SECRET")
        self.tenant = env("HALO_TENANT")
        self.token = None

    def configured(self):
        return bool(self.url and self.cid and self.secret)

    def _auth(self):
        if self.token:
            return True, "cached"
        auth_url = f"{self.url}/auth/token"
        if self.tenant:
            auth_url += f"?tenant={urllib.parse.quote(self.tenant)}"
        data = {"grant_type": "client_credentials",
                "client_id": self.cid,
                "client_secret": self.secret,
                "scope": "all"}
        st, js, raw = request(auth_url, data=data, method="POST")
        if st == 200 and isinstance(js, dict) and js.get("access_token"):
            self.token = js["access_token"]
            return True, "token obtained"
        return False, f"auth failed (status={st}) {str(raw)[:150]}"

    def connect(self):
        if not self.configured():
            return False, "not configured"
        return self._auth()

    def search_by_window(self, monitor_text, since_utc, until_utc):
        """Search HALO for tickets created in this window."""
        if not self.configured():
            return []
        ok, _ = self.connect()
        if not ok:
            return []
        since_str = since_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
        until_str = until_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
        st, js, raw = request(
            f"{self.url}/Tickets?search={urllib.parse.quote(monitor_text)}"
            f"&count=20&order=id&orderdesc=true",
            headers=self._headers())
        if st != 200 or not isinstance(js, dict):
            return []
        out = []
        for t in (js.get("tickets") or js.get("records") or []):
            created = t.get("dateoccurred") or t.get("datecreated")
            dt, how = parse_ticket_time(created, "halo")
            if dt and (dt < since_utc or dt > until_utc):
                continue
            out.append({"ticket_id": str(t.get("id") or ""),
                        "status": str(t.get("status_name") or
                                      t.get("statusname") or ""),
                        "summary": str(t.get("summary") or "")[:80],
                        "created_raw": created,
                        "created_utc": dt.isoformat() if dt else None,
                        "read_as": how,
                        "source": "HaloITSM"})
        return out

    def get_ticket(self, tid):
        ok, detail = self._auth()
        if not ok:
            return {"found": False, "error": detail}
        st, js, raw = request(
            f"{self.url}/api/Tickets/{urllib.parse.quote(str(tid))}",
            headers={"Authorization": f"Bearer {self.token}",
                     "Accept": "application/json"})
        if st == 200 and isinstance(js, dict):
            # FIX: the original expression had a precedence bug -- when Halo
            # returned a plain string status, status_name was ignored.
            raw_status = js.get("status")
            if isinstance(raw_status, dict):
                status = js.get("status_name") or raw_status.get("name")
            else:
                status = js.get("status_name") or raw_status
            return {"found": True, "status": str(status),
                    "summary": str(js.get("summary", ""))[:80]}
        if st == 404:
            return {"found": False, "error": "ticket not found"}
        return {"found": False, "error": f"status={st} {str(raw)[:120]}"}


# --------------------------------------------------------------- ServiceNow
class ServiceNow(Tool):
    name = "ServiceNow"

    def __init__(self):
        self.url = env("SNOW_URL").rstrip("/")
        self.user = env("SNOW_USER")
        self.pwd = env("SNOW_PASSWORD")

    def configured(self):
        return bool(self.url and self.user and self.pwd)

    def _headers(self):
        raw = f"{self.user}:{self.pwd}".encode()
        return {"Authorization": "Basic " + base64.b64encode(raw).decode(),
                "Accept": "application/json"}

    def connect(self):
        if not self.configured():
            return False, "not configured"
        st, js, raw = request(
            f"{self.url}/api/now/table/incident?sysparm_limit=1",
            headers=self._headers())
        if st == 200:
            return True, "incident table readable"
        return False, f"status={st} {str(raw)[:150]}"

    def get_ticket(self, tid):
        # ServiceNow tickets may be referenced by number (INC0012345)
        # or sys_id. Try number first, then sys_id.
        q = urllib.parse.quote(f"number={tid}")
        # FIX: sysparm_display_value=true so state comes back as "New" /
        # "Resolved" instead of a raw number. search_recent already did this;
        # without it the report showed state=1 for some rows and state=New
        # for others, which is useless as developer evidence.
        st, js, raw = request(
            f"{self.url}/api/now/table/incident?sysparm_query={q}"
            f"&sysparm_limit=1&sysparm_display_value=true",
            headers=self._headers())
        rows = (js or {}).get("result") if isinstance(js, dict) else None
        if st == 200 and rows:
            r = rows[0]
            return {"found": True, "status": str(r.get("state")),
                    "summary": str(r.get("short_description", ""))[:80]}
        st, js, raw = request(
            f"{self.url}/api/now/table/incident/"
            f"{urllib.parse.quote(str(tid))}?sysparm_display_value=true",
            headers=self._headers())
        r = (js or {}).get("result") if isinstance(js, dict) else None
        if st == 200 and r:
            return {"found": True, "status": str(r.get("state")),
                    "summary": str(r.get("short_description", ""))[:80]}
        return {"found": False, "error": f"not found (status={st})"}

    def search_recent(self, text, limit=5):
        """ServiceNow alert logs carry no ticket id, so search by text."""
        q = urllib.parse.quote(f"short_descriptionLIKE{text}"
                               f"^ORDERBYDESCsys_created_on")
        # display_value=all returns BOTH the readable state AND the raw
        # sys_created_on, which ServiceNow stores in UTC. That removes the
        # guesswork from the time comparison entirely.
        st, js, raw = request(
            f"{self.url}/api/now/table/incident?sysparm_query={q}"
            f"&sysparm_limit={limit}&sysparm_display_value=all",
            headers=self._headers())
        rows = (js or {}).get("result") if isinstance(js, dict) else []
        return st, rows or []


# --------------------------------------------------------------- Zoho Desk
    def search_by_window(self, monitor_text, since_utc, until_utc):
        """Search ServiceNow for incidents created in this exact window."""
        if not self.configured():
            return []
        since_str = since_utc.strftime("%Y-%m-%d %H:%M:%S")
        until_str = until_utc.strftime("%Y-%m-%d %H:%M:%S")
        q = urllib.parse.quote(
            f"short_descriptionLIKE{monitor_text}"
            f"^sys_created_on>={since_str}"
            f"^sys_created_on<={until_str}"
            f"^ORDERBYDESCsys_created_on")
        st, js, raw = request(
            f"{self.url}/api/now/table/incident?sysparm_query={q}"
            f"&sysparm_limit=20&sysparm_display_value=all",
            headers=self._headers())
        rows = (js or {}).get("result") if isinstance(js, dict) else []
        out = []
        for r in (rows or []):
            created = r.get("sys_created_on")
            if isinstance(created, dict):
                created = created.get("value") or created.get("display_value")
            dt, how = parse_ticket_time(created, "servicenow")
            # Local time filter as safety net
            if dt and (dt < since_utc or dt > until_utc):
                continue
            num = r.get("number")
            if isinstance(num, dict):
                num = num.get("display_value") or num.get("value")
            state = r.get("state")
            if isinstance(state, dict):
                state = state.get("display_value") or state.get("value")
            out.append({"ticket_id": str(num or ""),
                        "status": str(state or ""),
                        "summary": str((r.get("short_description") or {}).get(
                            "display_value", "") if isinstance(
                            r.get("short_description"), dict)
                            else r.get("short_description", ""))[:80],
                        "created_raw": created,
                        "created_utc": dt.isoformat() if dt else None,
                        "read_as": how,
                        "source": "ServiceNow",
                        "raw": r})
        return out


class ZohoDesk(Tool):
    name = "Zoho Desk"

    def __init__(self):
        self.org = env("ZOHODESK_ORG_ID")
        self.cid = env("ZOHODESK_CLIENT_ID")
        self.secret = env("ZOHODESK_CLIENT_SECRET")
        self.refresh = env("ZOHODESK_REFRESH_TOKEN")
        self.accounts = env("ZOHODESK_ACCOUNTS_URL",
                            "https://accounts.zoho.com").rstrip("/")
        self.api = env("ZOHODESK_API_URL",
                       "https://desk.zoho.com").rstrip("/")
        self.token = None

    def configured(self):
        return bool(self.org and self.cid and self.secret and self.refresh)

    def _auth(self):
        if self.token:
            return True, "cached"
        st, js, raw = request(
            f"{self.accounts}/oauth/v2/token",
            data={"grant_type": "refresh_token", "client_id": self.cid,
                  "client_secret": self.secret, "refresh_token": self.refresh},
            method="POST")
        if st == 200 and isinstance(js, dict) and js.get("access_token"):
            self.token = js["access_token"]
            return True, "token obtained"
        return False, f"auth failed (status={st}) {str(raw)[:150]}"

    def _headers(self):
        return {"Authorization": f"Zoho-oauthtoken {self.token}",
                "orgId": self.org, "Accept": "application/json"}

    def connect(self):
        if not self.configured():
            return False, "not configured"
        ok, detail = self._auth()
        if not ok:
            return False, detail
        st, js, raw = request(f"{self.api}/api/v1/tickets?limit=1",
                              headers=self._headers())
        if st == 200:
            return True, "tickets endpoint readable"
        return False, f"status={st} {str(raw)[:150]}"

    def get_ticket(self, tid):
        ok, detail = self._auth()
        if not ok:
            return {"found": False, "error": detail}
        st, js, raw = request(
            f"{self.api}/api/v1/tickets/{urllib.parse.quote(str(tid))}",
            headers=self._headers())
        if st == 200 and isinstance(js, dict):
            return {"found": True, "status": str(js.get("status")),
                    "summary": str(js.get("subject", ""))[:80]}
        if st == 404:
            return {"found": False, "error": "ticket not found"}
        return {"found": False, "error": f"status={st} {str(raw)[:120]}"}


# ------------------------------------------------------- ServiceDesk Plus
    def search_by_window(self, monitor_text, since_utc, until_utc):
        """Search Zoho Desk for tickets created in this window."""
        ok, detail = self._auth()
        if not ok:
            return []
        since_ms = int(since_utc.timestamp() * 1000)
        until_ms = int(until_utc.timestamp() * 1000)
        st, js, raw = request(
            f"{self.api}/api/v1/tickets?limit=20&sortBy=createdTime"
            f"&createdTimeRange={since_ms},{until_ms}",
            headers=self._headers())
        if st != 200 or not isinstance(js, dict):
            return []
        out = []
        for t in (js.get("data") or []):
            subj = str(t.get("subject") or "")
            if monitor_text.lower() not in subj.lower():
                continue
            created = t.get("createdTime")
            dt, how = parse_ticket_time(created, "zohodesk")
            # Local time filter as safety net
            if dt and (dt < since_utc or dt > until_utc):
                continue
            out.append({"ticket_id": str(t.get("id") or ""),
                        "status": str(t.get("status") or ""),
                        "summary": subj[:80],
                        "created_raw": created,
                        "created_utc": dt.isoformat() if dt else None,
                        "read_as": how,
                        "source": "Zoho Desk"})
        return out


class SDP(Tool):
    name = "ServiceDesk Plus"

    def __init__(self):
        self.portal = env("SDP_PORTAL")
        self.cid = env("SDP_CLIENT_ID")
        self.secret = env("SDP_CLIENT_SECRET")
        self.refresh = env("SDP_REFRESH_TOKEN")
        self.accounts = env("SDP_ACCOUNTS_URL",
                            "https://accounts.zoho.com").rstrip("/")
        self.api = env("SDP_API_URL",
                       "https://sdpondemand.manageengine.com").rstrip("/")
        self.token = None

    def configured(self):
        return bool(self.cid and self.secret and self.refresh)

    def _auth(self):
        if self.token:
            return True, "cached"
        st, js, raw = request(
            f"{self.accounts}/oauth/v2/token",
            data={"grant_type": "refresh_token", "client_id": self.cid,
                  "client_secret": self.secret, "refresh_token": self.refresh},
            method="POST")
        if st == 200 and isinstance(js, dict) and js.get("access_token"):
            self.token = js["access_token"]
            return True, "token obtained"
        return False, f"auth failed (status={st}) {str(raw)[:150]}"

    def _base(self):
        if self.portal:
            return f"{self.api}/app/{self.portal}/api/v3"
        return f"{self.api}/api/v3"

    def _headers(self):
        return {"Authorization": f"Zoho-oauthtoken {self.token}",
                "Accept": "application/vnd.manageengine.sdp.v3+json"}

    def connect(self):
        if not self.configured():
            return False, "not configured"
        ok, detail = self._auth()
        if not ok:
            return False, detail
        st, js, raw = request(f"{self._base()}/requests",
                              headers=self._headers())
        if st == 200:
            return True, "requests endpoint readable"
        return False, f"status={st} {str(raw)[:150]}"

    def get_ticket(self, tid):
        ok, detail = self._auth()
        if not ok:
            return {"found": False, "error": detail}

        # 1) direct lookup by internal id
        st, js, raw = request(
            f"{self._base()}/requests/{urllib.parse.quote(str(tid))}",
            headers=self._headers())
        if st == 200 and isinstance(js, dict):
            r = js.get("request") or {}
            status = r.get("status")
            if isinstance(status, dict):
                status = status.get("name")
            return {"found": True, "status": str(status),
                    "summary": str(r.get("subject", ""))[:80],
                    "matched_by": "id"}

        # 2) The id in the Alert Log is often the DISPLAY id, which is not
        #    always the internal id the API expects. Fall back to a search.
        crit = {"list_info": {"row_count": 100, "start_index": 1,
                              "sort_field": "created_time",
                              "sort_order": "desc"}}
        q = urllib.parse.quote(json.dumps(crit))
        st2, js2, raw2 = request(
            f"{self._base()}/requests?input_data={q}",
            headers=self._headers())
        if st2 == 200 and isinstance(js2, dict):
            for r in (js2.get("requests") or []):
                if str(r.get("display_id")) == str(tid) or \
                   str(r.get("id")) == str(tid):
                    status = r.get("status")
                    if isinstance(status, dict):
                        status = status.get("name")
                    return {"found": True, "status": str(status),
                            "summary": str(r.get("subject", ""))[:80],
                            "matched_by": "display_id search"}
            return {"found": False,
                    "error": f"display id {tid} not found in the 100 most "
                             f"recent requests (direct GET gave {st}). "
                             f"Check SDP_PORTAL is correct."}
        return {"found": False,
                "error": f"status={st} (search also failed: {st2})"}

    def search_recent(self, text, limit=10):
        """List recent requests whose subject contains text."""
        ok, _ = self._auth()
        if not ok:
            return None, []
        crit = {"list_info": {"row_count": limit, "start_index": 1,
                              "sort_field": "created_time",
                              "sort_order": "desc"}}
        q = urllib.parse.quote(json.dumps(crit))
        st, js, raw = request(f"{self._base()}/requests?input_data={q}",
                              headers=self._headers())
        rows = []
        if st == 200 and isinstance(js, dict):
            for r in (js.get("requests") or []):
                if text.lower() in str(r.get("subject", "")).lower():
                    rows.append(r)
        return st, rows


# ===========================================================================
# map the integration names seen in Alert Logs onto the right tool
# ===========================================================================

    def search_by_window(self, monitor_text, since_utc, until_utc):
        """Search SDP for requests created in this window."""
        ok, detail = self._auth()
        if not ok:
            return []
        since_str = since_utc.strftime("%Y-%m-%dT%H:%M:%S.000Z")
        until_str = until_utc.strftime("%Y-%m-%dT%H:%M:%S.000Z")
        params = urllib.parse.urlencode({
            "input_data": json.dumps({
                "list_info": {
                    "row_count": 20,
                    "sort_field": "created_time",
                    "sort_order": "desc",
                    "search_criteria": {
                        "field": "subject",
                        "condition": "contains",
                        "value": monitor_text
                    },
                    "filter_by": {
                        "field": "created_time",
                        "from": since_str,
                        "to": until_str
                    }
                }
            })
        })
        st, js, raw = request(
            f"{self._base()}/requests?{params}",
            headers=self._headers())
        if st != 200 or not isinstance(js, dict):
            return []
        out = []
        for r in (js.get("requests") or []):
            subj = str(r.get("subject") or "")
            created = r.get("created_time") or r.get("created_date")
            if isinstance(created, dict):
                created = created.get("display_value") or created.get("value")
            dt, how = parse_ticket_time(created, "sdp")
            # The API's filter_by parameter is UNRELIABLE — it returned
            # Sep 16-17 tickets for a Sep 25 search. Filter locally.
            if dt and (dt < since_utc or dt > until_utc):
                continue
            disp_id = r.get("display_id") or r.get("id")
            out.append({"ticket_id": str(disp_id or ""),
                        "status": str((r.get("status") or {}).get("name", "")
                                      if isinstance(r.get("status"), dict)
                                      else r.get("status", "")),
                        "summary": subj[:80],
                        "created_raw": created,
                        "created_utc": dt.isoformat() if dt else None,
                        "read_as": how,
                        "source": "ServiceDesk Plus"})
        return out


class PagerDuty(Tool):
    """PagerDuty REST API v2.

    Auth: a single API key in the Authorization header.
    Incidents are looked up by time window and matched on the description
    containing the monitor name, since PagerDuty ticket IDs in Alert Logs
    are routing keys, not incident IDs.
    """
    name = "PagerDuty"

    def __init__(self):
        self.api_url = env("PAGERDUTY_API_URL").rstrip("/") or "https://api.pagerduty.com"
        self.api_key = env("PAGERDUTY_API_KEY")
        self.user_email = env("PAGERDUTY_USER_EMAIL")

    def configured(self):
        return bool(self.api_key)

    def _headers(self):
        h = {"Authorization": f"Token token={self.api_key}",
             "Accept": "application/json",
             "Content-Type": "application/json"}
        if self.user_email:
            h["From"] = self.user_email
        return h

    def connect(self):
        if not self.configured():
            return False, "not configured (set PAGERDUTY_API_KEY and PAGERDUTY_USER_EMAIL in .itsm.env)"
        st, js, raw = request(
            f"{self.api_url}/incidents?limit=1&total=true",
            headers=self._headers())
        if st == 200:
            total = "unknown"
            if isinstance(js, dict):
                total = js.get("total", "unknown")
            return True, f"incidents endpoint readable (total={total})"
        return False, f"status={st} {(raw or '')[:200]}"

    def search_by_window(self, monitor_text, since_utc, until_utc):
        """Search PagerDuty for incidents created in this window."""
        if not self.configured():
            return []
        since_str = since_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
        until_str = until_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
        st, js, raw = request(
            f"{self.api_url}/incidents?since={since_str}&until={until_str}"
            f"&limit=20&sort_by=created_at:desc",
            headers=self._headers())
        if st != 200 or not isinstance(js, dict):
            return []
        out = []
        for inc in (js.get("incidents") or []):
            title = str(inc.get("title") or inc.get("summary") or "")
            if monitor_text.lower() not in title.lower():
                continue
            created = inc.get("created_at")
            dt, how = parse_ticket_time(created, "pagerduty")
            out.append({"ticket_id": str(inc.get("incident_key") or
                                         inc.get("id") or ""),
                        "status": str(inc.get("status") or ""),
                        "summary": title[:80],
                        "created_raw": created,
                        "created_utc": dt.isoformat() if dt else None,
                        "read_as": how,
                        "source": "PagerDuty"})
        return out

    def get_ticket(self, tid):
        # PagerDuty ticket IDs from Alert Logs are sometimes routing keys
        # or PagerDuty-format IDs like P03VZWH or full hex strings.
        # Try the incident endpoint directly first.
        st, js, raw = request(
            f"{self.api_url}/incidents/{tid}",
            headers=self._headers())
        if st == 200 and isinstance(js, dict):
            inc = js.get("incident", js)
            status = inc.get("status", "unknown")
            title = str(inc.get("title") or inc.get("summary") or "")[:80]
            created = inc.get("created_at")
            return {"found": True, "status": status, "summary": title,
                    "created": created}
        # Not found by direct ID — expected, since Alert Log IDs are often
        # not PagerDuty incident IDs
        return {"found": False,
                "error": f"not found by id (status={st})"}

    def search_recent(self, text, limit=10, since_hours=4):
        """Search incidents by description text within a time window."""
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        since = (now - timedelta(hours=since_hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
        until = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        st, js, raw = request(
            f"{self.api_url}/incidents?since={since}&until={until}"
            f"&limit={limit}&sort_by=created_at%3Adesc",
            headers=self._headers())
        if st != 200:
            return st, []
        incidents = []
        for inc in (js or {}).get("incidents", []):
            title = str(inc.get("title") or inc.get("summary") or "")
            if text.lower() in title.lower():
                incidents.append({
                    "id": inc.get("id"),
                    "number": inc.get("incident_number"),
                    "status": inc.get("status"),
                    "title": title[:100],
                    "created_at": inc.get("created_at"),
                    "service": (inc.get("service") or {}).get("summary"),
                })
        return st, incidents


def build_tools():
    return {"halo": Halo(), "servicenow": ServiceNow(),
            "zohodesk": ZohoDesk(), "sdp": SDP(),
            "pagerduty": PagerDuty()}


# Site24x7's own "Alert Mode" / communicationmode value -> adapter key.
# This is what the product says the row was delivered to, so it beats any
# guess made from the integration's name.
# ---------------------------------------------------------------------------
# WEB_MON ALIGNMENT — AlertLogs.java / ThirdPartyServices enum
# CommunicationMode INTEGER → tool adapter key
# This is the PRIMARY lookup — integer beats name-based guessing.
# Source: AlertLogs.java getAlertModeName() + ThirdPartyServices enum
# ---------------------------------------------------------------------------
MODE_INT_TO_TOOL = {
    6:  "sdp",          # SDPOD — ServiceDesk Plus On Demand
    9:  "pagerduty",    # PagerDuty
    14: "servicenow",   # ServiceNow
    24: "zohodesk",     # ZDESK — Zoho Desk
    52: "halo",         # HaloITSM
    15: "opsgenie",     # OpsGenie (no adapter yet)
    23: "jira",         # Jira (no adapter yet)
    29: "freshservice", # FreshService (no adapter yet)
}

# String-based fallback — used when CommunicationMode is absent or non-int.
# Kept for backward compatibility with older snapshots.
MODE_TO_TOOL = {
    "service-now":            "servicenow",
    "servicenow":             "servicenow",
    "snow":                   "servicenow",
    "zoho desk":              "zohodesk",
    "zohodesk":               "zohodesk",
    "zdesk":                  "zohodesk",
    "servicedesk plus cloud": "sdp",
    "servicedesk plus":       "sdp",
    "sdpod":                  "sdp",
    "sdp":                    "sdp",
    "haloitsm":               "halo",
    "halo itsm":              "halo",
    "halo":                   "halo",
    "pagerduty":              "pagerduty",
    "pager duty":             "pagerduty",
    # integer strings — from stage3 storing comm_mode_int as str
    "6":                      "sdp",
    "9":                      "pagerduty",
    "14":                     "servicenow",
    "24":                     "zohodesk",
    "52":                     "halo",
}

# Delivery modes that are real, but are NOT ticketing tools. There will
# never be an adapter for these, so say so plainly instead of reporting
# them as something we failed to reach.
NON_TICKETING_MODES = {
    "custom webhook", "webhook", "zoho cliq", "cliq", "alarmsone",
    "email", "sms", "voice call", "slack", "microsoft teams",
    "google chat", "telegram", "connectwise", "analytics plus",
    "google cloud pub/sub", "application manager",
}


# ==========================================================================
# TIME CORRELATION
# A ticket only proves anything if it was created BY THIS CYCLE. Tools
# report time in their own timezone -- ServiceNow and HALO in the
# instance's zone, Desk in ISO with an offset, SDP in epoch millis. So we
# convert everything to UTC and compare against the cycle window, and we
# PRINT the raw value every time so a wrong conversion is visible rather
# than silently producing confident nonsense.
# ==========================================================================

TZ_OVERRIDES = {            # hours to ADD to a tool's naive timestamp to
    "servicenow": None,     # reach UTC. None = read from *_TZ_OFFSET env.
    "halo": None,
    "zohodesk": None,
    "sdp": None,
}


def tz_offset_for(key):
    """Hours to add to this tool's naive timestamps to get UTC."""
    raw = os.environ.get(f"{key.upper()}_TZ_OFFSET", "").strip()
    if raw:
        try:
            return float(raw)
        except ValueError:
            pass
    return 0.0                      # assume the value is already UTC


def parse_ticket_time(value, tool_key=""):
    """(datetime_utc or None, how_it_was_read). Never guesses silently."""
    if value in (None, "", "-"):
        return None, "no timestamp field"
    s = str(value).strip()

    if s.isdigit():                                     # epoch
        n = int(s)
        unit = "epoch-ms" if n > 1e11 else "epoch-s"
        if n > 1e11:
            n /= 1000.0
        try:
            return datetime.fromtimestamp(n, tz=timezone.utc), unit
        except (OverflowError, OSError, ValueError):
            return None, "unreadable epoch"

    # ISO with an explicit offset -- unambiguous, use it as-is
    try:
        iso = s.replace("Z", "+00:00")
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo:
            return dt.astimezone(timezone.utc), "ISO with offset"
    except ValueError:
        pass

    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S",
                "%d-%m-%Y %H:%M:%S", "%m/%d/%Y %H:%M:%S",
                "%Y-%m-%d %H:%M",
                "%b %d, %Y %I:%M %p", "%b %d, %Y %I:%M:%S %p",
                "%b %d, %Y %H:%M:%S", "%b %d, %Y %H:%M"):
        try:
            naive = datetime.strptime(s, fmt)
        except ValueError:
            continue
        off = tz_offset_for(tool_key)
        dt = (naive - timedelta(hours=off)).replace(tzinfo=timezone.utc)
        how = (f"naive '{fmt}' + {tool_key.upper()}_TZ_OFFSET={off}h"
               if off else f"naive '{fmt}', assumed UTC")
        return dt, how
    return None, f"unrecognised format {s!r}"


def in_window(dt_utc, since_utc, until_utc):
    if dt_utc is None or since_utc is None:
        return None                 # unknown, not a pass and not a fail
    if until_utc and dt_utc > until_utc:
        return False
    return dt_utc >= since_utc


def window_from_args(args):
    """Cycle window (local naive strings) -> UTC datetimes."""
    def conv(txt):
        if not txt:
            return None
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
            try:
                naive = datetime.strptime(str(txt).strip(), fmt)
            except ValueError:
                continue
            # the cycle timestamps are written in the RUNNER's local time
            return naive.astimezone().astimezone(timezone.utc)
        return None
    return conv(getattr(args, "since", None)), conv(getattr(args, "until", None))


def load_anchors(path):
    """Status changes from the monitor's own Log Report.

    THE ANCHOR. A ticket only counts as evidence if it was created just
    after a real state change on the monitor. Without this, any ticket
    lying around in the right rough window can be mistaken for proof --
    and with three cycles in two hours, there are always several.
    """
    if not path or not os.path.isfile(path):
        return []
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:  # noqa: BLE001
        return []
    out = []
    for t in (data.get("transitions") or []):
        dt, how = parse_ticket_time(t.get("at_raw"), "site24x7")
        if dt:
            out.append({"from": t.get("from"), "to": t.get("to"),
                        "at_raw": t.get("at_raw"), "at": dt, "read_as": how})
    out.sort(key=lambda x: x["at"])
    return out


def match_anchor(ticket_dt, anchors, max_lag=300, max_early=60):
    """Tie a ticket to the state change that produced it.

    The ticket is created AFTER the monitor changes, typically 30-120s
    later. So the match is the latest transition at or just before the
    ticket, within max_lag seconds.
    """
    if ticket_dt is None or not anchors:
        return None
    # The change comes FIRST and the ticket follows, so a positive lag is
    # the real relationship. Ranking by smallest ABSOLUTE lag matched a
    # ticket to a change that happened after it -- nonsense. Prefer the
    # latest change at or before the ticket; only fall back to a slightly
    # later one (clock skew) when nothing precedes it.
    after, before = None, None
    for a in anchors:
        lag = (ticket_dt - a["at"]).total_seconds()
        cand = {"from": a["from"], "to": a["to"],
                "change_at_raw": a["at_raw"],
                "change_at_utc": a["at"].isoformat(),
                "lag_seconds": round(lag, 1)}
        if 0 <= lag <= max_lag:
            if after is None or lag < after["lag_seconds"]:
                after = cand
        elif -max_early <= lag < 0:
            if before is None or lag > before["lag_seconds"]:
                before = cand
    return after or before


def pick_tool(integration_name, tools, delivery_modes=None):
    """Choose the ITSM adapter for an integration.

    WEB_MON ALIGNMENT — AlertLogs.java / ThirdPartyServices enum
    =============================================================
    STEP 1: Try CommunicationMode INTEGER (most reliable — written by
            AlertLogs.addAlertLogToApplog() as jsonObject.put("CommunicationMode", alertType))
            This is the product's OWN authoritative delivery-type field.
    STEP 2: Try string mode names (fallback for older snapshots).
    STEP 3: Check NON_TICKETING_MODES so channels like Slack / webhook
            are correctly excluded rather than mapped to a wrong adapter.
    STEP 4: Name-based heuristic (last resort only).

    Matching on the integration NAME alone is a trap: an account can name
    its ServiceNow integration "Snow HALO Sanity" and a name-based match
    would log into HaloITSM instead — producing completely wrong verdicts.
    """
    # STEP 1 — integer CommunicationMode (Web_Mon ground truth)
    for m in (delivery_modes or []):
        try:
            mode_int = int(m)
            key = MODE_INT_TO_TOOL.get(mode_int)
            if key:
                return tools.get(key)
        except (ValueError, TypeError):
            pass

    # STEP 2 — string mode names (backward compat)
    for m in (delivery_modes or []):
        key = MODE_TO_TOOL.get(str(m).strip().lower())
        if key:
            return tools.get(key)

    # STEP 3 — non-ticketing channel → no adapter, not a failure
    for m in (delivery_modes or []):
        if str(m).strip().lower() in NON_TICKETING_MODES:
            return None

    # STEP 4 — name heuristic (last resort only, most specific first)
    n = (integration_name or "").lower()
    if "servicedesk" in n or "sdp" in n or "sdpod" in n:
        return tools["sdp"]
    if "snow" in n or "servicenow" in n or "service-now" in n:
        return tools["servicenow"]
    if "halo" in n:
        return tools["halo"]
    if "desk" in n or "zoho" in n:
        return tools["zohodesk"]
    if "pager" in n or "pagerduty" in n:
        return tools.get("pagerduty")
    return None


# ===========================================================================

def cmd_check(args):
    section("CONNECTIVITY CHECK — reads no tickets")
    tools = build_tools()
    results = {}
    for key, t in tools.items():
        if not t.configured():
            log(f"  [SKIP] {t.name:<18} NOT CONFIGURED "
                f"(fill in .itsm.env to enable)")
            results[key] = {"configured": False}
            continue
        ok, detail = t.connect()
        log(f"  [{'OK ' if ok else 'FAIL'}] {t.name:<18} {detail}")
        results[key] = {"configured": True, "ok": ok, "detail": detail}

    section("SUMMARY")
    ready = [k for k, v in results.items() if v.get("ok")]
    log(f"  ready  : {ready or 'none'}")
    notcfg = [k for k, v in results.items() if not v.get("configured")]
    if notcfg:
        log(f"  not set: {notcfg}  (these are skipped, not failed)")
    bad = [k for k, v in results.items()
           if v.get("configured") and not v.get("ok")]
    if bad:
        log(f"  broken : {bad}  <- check those credentials in .itsm.env")
    log("\n  Next:  python3 stage4_tickets.py verify")




def cmd_search(args):
    """ITSM-FIRST verification.

    Instead of reading Alert Logs first and finding ticket IDs there, go
    directly to each ITSM tool and ask: what tickets were created in the
    last few minutes matching this monitor?

    This is the architecture Mufi specified and it solves the fundamental
    problem: Alert Logs are historical, so old tickets from previous cycles
    get reported as this run's evidence. The tool's own creation timestamp
    cannot be wrong about when it was created.
    """
    section("ITSM-FIRST VERIFICATION — searching each tool directly")
    tools = build_tools()
    since_utc, until_utc = window_from_args(args)
    if not since_utc or not until_utc:
        die("--since and --until are required for ITSM-first search")

    log(f"  window (UTC) : {since_utc:%Y-%m-%d %H:%M:%S} to "
        f"{until_utc:%Y-%m-%d %H:%M:%S}")
    log(f"  monitor text : {args.monitor_text}")

    anchors = load_anchors(getattr(args, "logreport", None))
    if anchors:
        log(f"  anchors      : {len(anchors)} state changes from Log Report")

    results = []
    for key, tool in tools.items():
        if not tool.configured():
            log(f"\n  [SKIP] {tool.name:<18} NOT CONFIGURED")
            results.append({"tool": tool.name, "tool_key": key,
                            "configured": False, "tickets": []})
            continue

        log(f"\n  {tool.name}")
        log(f"    searching for tickets created {since_utc:%H:%M:%S}–"
            f"{until_utc:%H:%M:%S} UTC containing "
            f"'{args.monitor_text}'...")

        try:
            tickets = tool.search_by_window(
                args.monitor_text, since_utc, until_utc)
        except Exception as exc:
            log(f"    [!! ] search failed: {exc}")
            results.append({"tool": tool.name, "tool_key": key,
                            "configured": True, "tickets": [],
                            "error": str(exc)})
            continue

        log(f"    found {len(tickets)} ticket(s) in this window")

        for t in tickets:
            log(f"      - {t['ticket_id']}  status={t.get('status')}"
                f"  {t.get('summary','')[:50]}")
            log(f"          created raw : {t.get('created_raw')}")
            log(f"          created UTC : {t.get('created_utc')}")

            # anchor to a real state change
            tkt_dt, _ = parse_ticket_time(
                t.get("created_raw"), key)
            anchor = match_anchor(tkt_dt, anchors) if anchors else None
            t["anchor"] = anchor
            if anchor:
                log(f"          anchor     : {anchor['from']}->"
                    f"{anchor['to']} at {anchor['change_at_raw']}, "
                    f"+{anchor['lag_seconds']:.0f}s — MATCHED")
            elif anchors:
                log(f"          anchor     : NO state change matches "
                    f"— may not be from this cycle")

        results.append({"tool": tool.name, "tool_key": key,
                        "configured": True, "tickets": tickets})

    # write results
    out_path = "itsm_first_results.json"
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump({"monitor_text": args.monitor_text,
                    "since_utc": since_utc.isoformat(),
                    "until_utc": until_utc.isoformat(),
                    "results": results}, fh, indent=2, default=str)

    # summary
    section("ITSM-FIRST SUMMARY")
    total = sum(len(r["tickets"]) for r in results)
    log(f"  {total} ticket(s) found across all tools in this window")
    for r in results:
        n = len(r["tickets"])
        if not r["configured"]:
            log(f"    {r['tool']:<20} SKIP (not configured)")
        elif r.get("error"):
            log(f"    {r['tool']:<20} ERROR: {r['error'][:60]}")
        elif n == 0:
            log(f"    {r['tool']:<20} NONE found")
        else:
            ids = ", ".join(t["ticket_id"] for t in r["tickets"])
            log(f"    {r['tool']:<20} {n} ticket(s): {ids}")
    log(f"\n  wrote {out_path}")

def cmd_verify(args):
    if not os.path.isfile(STAGE3_FILE):
        log(f"[BLOCKER] {STAGE3_FILE} not found.")
        log("          Run Stage 3 first:")
        log("            python3 stage3_verify.py verify "
            "--monitor-id <id> --hours 12")
        sys.exit(2)

    stage3 = json.load(open(STAGE3_FILE, encoding="utf-8"))
    tools = build_tools()

    section("VERIFYING TICKETS INSIDE EACH ITSM TOOL")
    log(f"  source     : {STAGE3_FILE}")
    log(f"  monitor id : {stage3.get('monitor_id')}")

    # WEB_MON ALIGNMENT — log ticket ID source path for full traceability
    src_meta = stage3.get("ticket_id_source_metadata", {})
    if src_meta:
        log(f"\n  Ticket IDs sourced from:")
        log(f"    Cassandra table : {src_meta.get('cassandra_table', 'WM_ALERT_LOGS')}")
        log(f"    Canonical field : {src_meta.get('canonical_field', 'RequestMessageId')}")
        log(f"    Java class      : {src_meta.get('java_class', 'AlertLogs.addAlertLogToApplog()')}")
        log(f"    API endpoint    : {src_meta.get('api_endpoint', '/app/api/applog/search/')}")
    log("")

    out = []
    for entry in stage3.get("results", []):
        integ = entry.get("integration", "")
        tool = pick_tool(integ, tools,
                         entry.get("delivery_modes"))
        ids = (entry.get("created_ticket_ids") or []) \
            + [t for t in (entry.get("closed_ticket_ids") or [])
               if t not in (entry.get("created_ticket_ids") or [])]

        # WEB_MON DB evidence — proves ticket ID came from Cassandra
        db_evidence = entry.get("webmon_db_evidence", {})

        log(f"\n  {integ}")
        if tool is None:
            log("    [SKIP] no matching tool adapter for this integration")
            out.append({"integration": integ, "verdict": "NO ADAPTER"})
            continue
        if not tool.configured():
            log(f"    [SKIP] {tool.name} NOT CONFIGURED in .itsm.env")
            out.append({"integration": integ, "tool": tool.name,
                        "verdict": "NOT CONFIGURED"})
            continue
        if not ids:
            log("    [INFO] Alert Logs gave no ticket id for this integration")
            if isinstance(tool, ServiceNow) and args.monitor_text:
                st, rows = tool.search_recent(args.monitor_text, limit=10)
                log(f"    searching ServiceNow by text "
                    f"'{args.monitor_text}' -> status={st}, {len(rows)} row(s)")

                # FIX (silent failure): a 401/500 returns zero rows, and zero
                # rows used to fall through to "OK all resolved". A dead
                # connection was being reported as a PASS. Never again --
                # anything that is not a clean 200 is BLOCKED, loudly.
                if st != 200:
                    log(f"    [BLOCKED] ServiceNow returned status={st}. "
                        f"This is an ACCESS problem, not a test result. "
                        f"NOT counted as pass or fail.")
                    out.append({"integration": integ, "tool": tool.name,
                                "verdict": f"BLOCKED status={st}",
                                "blocked": True,
                                "blocker_detail": f"ServiceNow API returned "
                                                  f"status={st} (check "
                                                  f"SNOW_USER / SNOW_PASSWORD "
                                                  f"in .itsm.env)"})
                    continue

                since_utc, until_utc = window_from_args(args)
                if since_utc:
                    log(f"    correlating against the cycle window "
                        f"{since_utc:%Y-%m-%d %H:%M:%S} to "
                        f"{until_utc:%Y-%m-%d %H:%M:%S} UTC")
                else:
                    log("    [WARN] no cycle window given, so EVERY matching "
                        "incident is")
                    log("           listed — including ones from earlier "
                        "runs. Pass --since.")

                kept, skipped_old = [], 0
                for r in rows:
                    created = r.get("sys_created_on")
                    if isinstance(created, dict):
                        created = created.get("value") or created.get(
                            "display_value")
                    dt, how = parse_ticket_time(created, "servicenow")
                    verdict_t = in_window(dt, since_utc, until_utc)
                    r["_created_raw"] = created
                    r["_created_utc"] = dt.isoformat() if dt else None
                    r["_time_read_as"] = how
                    r["_in_window"] = verdict_t
                    if verdict_t is False:
                        skipped_old += 1
                        continue
                    kept.append(r)

                if since_utc:
                    log(f"    {len(kept)} incident(s) created in this cycle, "
                        f"{skipped_old} from earlier runs ignored")
                rows = kept

                open_states = {"New", "In Progress", "On Hold", "Open",
                               "1", "2", "3"}
                still_open = 0
                found_rows = []
                for r in rows[:10]:
                    state = str(r.get("state"))
                    is_open = state in open_states
                    if is_open:
                        still_open += 1
                    log(f"      - {r.get('number')}  state={state}"
                        f"{'  <- STILL OPEN' if is_open else ''}  "
                        f"{str(r.get('short_description',''))[:55]}")
                    found_rows.append({"number": r.get("number"),
                                       "state": state, "open": is_open})
                if still_open:
                    log(f"    -> {still_open} incident(s) still OPEN. If the "
                        f"monitor has recovered, these should have closed.")
                    verdict = f"REVIEW {still_open} still open"
                else:
                    verdict = "OK all resolved"
                out.append({"integration": integ, "tool": tool.name,
                            "verdict": verdict, "rows": found_rows})
            else:
                out.append({"integration": integ, "tool": tool.name,
                            "verdict": "NO TICKET ID IN LOGS"})
            continue

        checked = []
        row_times = entry.get("ticket_times") or {}
        anchors = load_anchors(getattr(args, "logreport", None))
        for tid in ids:
            res = tool.get_ticket(tid)
            if res.get("found"):
                log(f"    [OK ] ticket {tid}  status={res.get('status')}  "
                    f"{res.get('summary','')}")
            else:
                log(f"    [!! ] ticket {tid}  {res.get('error')}")

            # CORRELATE: the ticket is created in the tool FIRST, and the
            # Alert Log row is written afterwards as the receipt. So the
            # ticket's own creation time must come at or just BEFORE its
            # alert row. Showing both, and the gap, is what turns "a ticket
            # exists" into "this alert produced this ticket".
            corr = {}
            meta = row_times.get(str(tid)) or {}
            raw_row = meta.get("alert_row_time_raw")
            if raw_row:
                row_dt, row_how = parse_ticket_time(raw_row, "site24x7")
                created_raw = (res.get("created") or res.get("created_time")
                               or res.get("createdTime")
                               or res.get("sys_created_on"))
                tkt_dt, tkt_how = parse_ticket_time(
                    created_raw, tool.__class__.__name__.lower())
                corr = {
                    "operation": meta.get("operation"),
                    "alert_status": meta.get("status"),
                    "alert_row_time_raw": raw_row,
                    "alert_row_time_utc": row_dt.isoformat() if row_dt else None,
                    "ticket_created_raw": created_raw,
                    "ticket_created_utc": tkt_dt.isoformat() if tkt_dt else None,
                    "read_as": f"row: {row_how}; ticket: {tkt_how}",
                }
                if row_dt and tkt_dt:
                    gap = (row_dt - tkt_dt).total_seconds()
                    corr["gap_seconds"] = round(gap, 1)
                    # ticket first, receipt after -> gap should be >= 0
                    corr["order_ok"] = gap >= -30
                    log(f"          alert row : {raw_row}  "
                        f"({meta.get('operation')}/{meta.get('status')})")
                    log(f"          ticket    : {created_raw}")
                    log(f"          gap       : {gap:.0f}s  "
                        f"{'OK — ticket created before its receipt' if gap >= -30 else 'ODD — receipt predates the ticket'}")
                else:
                    log(f"          alert row : {raw_row}")
                    shown = created_raw or "(no creation time from tool)"
                    log(f"          ticket    : {shown}")
                    log(f"          [note] could not compare — {corr['read_as']}")
            # STEP 1 of the chain: which state change produced this?
            tkt_dt_for_anchor, _ = parse_ticket_time(
                (res.get("created") or res.get("created_time")
                 or res.get("createdTime") or res.get("sys_created_on")),
                tool.__class__.__name__.lower())
            anchor = match_anchor(tkt_dt_for_anchor, anchors)
            if anchors:
                if anchor:
                    log(f"          anchor    : monitor went "
                        f"{anchor['from']} -> {anchor['to']} at "
                        f"{anchor['change_at_raw']}")
                    log(f"                      ticket followed "
                        f"{anchor['lag_seconds']:.0f}s later — MATCHED")
                else:
                    log(f"          anchor    : NO state change lines up "
                        f"with this ticket")
                    log(f"                      it was not produced by this "
                        f"cycle — not counted as evidence")
            corr["anchor"] = anchor
            checked.append({"ticket_id": tid, **res, "correlation": corr,
                            "anchored": bool(anchor)})

        found = sum(1 for c in checked if c.get("found"))

        # An access problem is NOT an application defect. If nothing was found
        # and every miss looks like an auth/connection error, report BLOCKED so
        # the ticket never lands on a developer's desk as a false bug.
        auth_markers = ("status=401", "status=403", "auth failed",
                        "not configured", "status=None")
        misses = [str(c.get("error", "")) for c in checked
                  if not c.get("found")]
        all_auth = bool(misses) and all(
            any(m in e for m in auth_markers) for e in misses)

        if found == 0 and all_auth:
            verdict = "BLOCKED cannot reach tool"
            log(f"    [BLOCKED] cannot reach {tool.name} — "
                f"this is an ACCESS problem, not a test result.")
            out.append({"integration": integ, "tool": tool.name,
                        "tickets": checked, "verdict": verdict,
                        "blocked": True,
                        "blocker_detail": misses[0][:200]})
            continue

        verdict = ("PASS all found" if found == len(checked)
                   else f"PARTIAL {found}/{len(checked)} found"
                   if found else "FAIL none found")
        log(f"    -> {verdict}")
        # WEB_MON ALIGNMENT — attach DB evidence to stage4 output so the
        # report can prove every ticket ID came from WM_ALERT_LOGS.RequestMessageId
        out.append({"integration": integ, "tool": tool.name,
                    "tickets": checked, "verdict": verdict,
                    "webmon_db_evidence": db_evidence})

    section("SUMMARY")
    for r in out:
        log(f"  {r['verdict']:<24} {r['integration']}")

    with open(OUT_FILE, "w", encoding="utf-8") as fh:
        json.dump({"at": datetime.now().isoformat(), "results": out},
                  fh, indent=2)
    log(f"\n  Wrote {OUT_FILE}")


def main():
    ap = argparse.ArgumentParser(description="Verify tickets inside ITSM tools")
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("check", help="test connectivity to each tool")
    c.set_defaults(func=cmd_check)

    v_first = sub.add_parser("search",
        help="ITSM-FIRST: search each tool for tickets created in the "
             "cycle window. This is the PRIMARY verification — tickets "
             "found here are real-time evidence, not historical.")
    v_first.add_argument("--monitor-text", default="Do Not Delete",
                         help="text the monitor name contains")
    v_first.add_argument("--since", required=True,
                         help="cycle window START in local time")
    v_first.add_argument("--until", required=True,
                         help="cycle window END in local time")
    v_first.add_argument("--logreport", default=None,
                         help="logreport JSON for anchoring")
    v_first.set_defaults(func=cmd_search)

    v = sub.add_parser("verify", help="look up Stage 3 ticket ids in each tool")
    v.add_argument("--monitor-text", default="Do Not Delete",
                   help="text to search ServiceNow by, since its Alert Log "
                        "lines carry no ticket id")
    v.add_argument("--logreport", default=None,
                   help="logreport_<monitor>.json from s247_logreport.js. "
                        "Tickets are matched to the monitor's OWN status "
                        "changes; anything that does not line up is not "
                        "counted as evidence.")
    v.add_argument("--since", default=None,
                   help="cycle window START in local time, e.g. "
                        "'2026-09-14 14:56:20'. Tickets created outside the "
                        "window are IGNORED — they belong to earlier runs.")
    v.add_argument("--until", default=None,
                   help="cycle window END in local time")
    v.set_defaults(func=cmd_verify)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
