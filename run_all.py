#!/usr/bin/env python3
"""
Site24x7 ITSM Automation — STAGE 5 : ONE COMMAND, ONE REPORT
============================================================

WHAT THIS DOES
    Runs the whole proven flow end to end and turns it into a report you
    can hand to a developer without editing anything by hand.

        stage2b_cycle.py    drive a real alert cycle  (UP -> problem -> UP)
        stage3_verify.py    verify from Site24x7 Alert Logs
        stage4_tickets.py   verify inside each ITSM tool

    Then writes three files into ./reports/ :

        report_<timestamp>.html   for humans / developers
        report_<timestamp>.json   for machines
        junit_<timestamp>.xml     for Jenkins

THE IMPORTANT PART — PASS vs FAIL vs BLOCKED
    A test suite that reports its own broken credentials as an application
    bug is worse than no suite at all. Every integration lands in exactly
    one bucket:

        PASS         ticket created and closed. Working as designed.
        DEFECT       the product misbehaved. A developer must act.
        BLOCKED      WE could not test it (expired cookie, bad ITSM
                     credentials, tool unreachable). NOT a product bug.
        KNOWN        a real problem already triaged — see known_issues.json
        INCONCLUSIVE nothing conclusive in the window. Re-run or widen.

    In the JUnit XML, DEFECT becomes a <failure>. BLOCKED and KNOWN become
    <skipped>. That way an expired session cookie can never turn a Jenkins
    build red and send somebody chasing a bug that does not exist.

USAGE
    cd ~/itsm-automation
    source start.sh

    python3 run_all.py                     # full run, all three stages
    python3 run_all.py --skip-cycle        # reuse the last alert cycle
    python3 run_all.py --report-only       # just rebuild the report
    python3 run_all.py --dry-run           # show the plan, change nothing

OPTIONAL: known_issues.json
    Put this next to the script to stop already-triaged problems from
    shouting at you every run. Integration names are matched loosely.

        {
          "PagerDuty Automation": "Invalid routing key - config, out of scope"
        }

SAFETY
    This script changes nothing by itself. All monitor changes happen
    inside stage2b_cycle.py, which keeps its own backup-and-restore.
    Reads no secrets, prints no secrets, writes no secrets.
"""

import argparse
import html
import json
import os
import subprocess
import sys
import time
import xml.sax.saxutils as sx
from datetime import datetime, timedelta

# Where the CODE lives. The working directory may be a per-account data
# folder, so sibling scripts must be found relative to this file, not cwd.
HERE = os.path.dirname(os.path.abspath(__file__))


def script(name):
    p = os.path.join(HERE, name)
    return p if os.path.isfile(p) else name


STAGE2_RESULT = "stage2b_result.json"
STAGE3_RESULT = "ticket_verification.json"
STAGE4_RESULT = "stage4_results.json"
KNOWN_ISSUES = "known_issues.json"
REPORT_DIR = "reports"

PASS = "PASS"
DEFECT = "DEFECT"
BLOCKED = "BLOCKED"
KNOWN = "KNOWN"
INCONCLUSIVE = "INCONCLUSIVE"


def log(m=""):
    print(m, flush=True)


def section(t):
    log("\n" + "=" * 70)
    log(t)
    log("=" * 70)


# ==========================================================================
# running the stages
# ==========================================================================

def run_stage(label, argv, dry_run=False):
    """Run one stage, streaming its output live. Returns a result dict."""
    section(label)
    log("  $ " + " ".join(argv))
    if dry_run:
        log("  [DRY RUN] not executed")
        return {"label": label, "cmd": argv, "skipped": True,
                "returncode": None}

    started = datetime.now()
    try:
        proc = subprocess.run(argv, check=False)
        rc = proc.returncode
    except FileNotFoundError:
        log(f"  [BLOCKER] {argv[1]} not found in this folder.")
        return {"label": label, "cmd": argv, "returncode": None,
                "error": f"{argv[1]} not found",
                "seconds": 0, "blocked": True}
    except KeyboardInterrupt:
        log("\n  [ABORTED] you pressed Ctrl+C")
        raise

    secs = (datetime.now() - started).total_seconds()
    log(f"\n  -> exit code {rc}   ({secs:.0f}s)")
    return {"label": label, "cmd": argv, "returncode": rc, "seconds": secs}


LOGIN_JS = os.environ.get(
    "S247_LOGIN_JS",
    os.path.join(os.path.expanduser("~"), "Documents", "qg",
                 "s247_login.js"))


def reload_session():
    """Mint a fresh cookie and load it into this process.

    Two people share each account. When the other person logs out, the
    server kills the session and our cookie dies MID-RUN -- often after
    the 15-minute cycle has already happened. Losing that run to someone
    else's logout is unacceptable, so recover instead of reporting it.
    """
    if not os.path.isfile(LOGIN_JS):
        log(f"  [!! ] cannot auto-recover: {LOGIN_JS} not found")
        return False
    log("  Re-minting the session cookie...")
    try:
        r = subprocess.run(["node", LOGIN_JS], capture_output=True, text=True,
                           timeout=300)
    except Exception as exc:  # noqa: BLE001
        log(f"  [!! ] login script failed: {exc}")
        return False
    if r.returncode != 0:
        log("  [!! ] could not log back in automatically.")
        log("        Somebody may have logged the account out. Run:")
        log(f"          node {LOGIN_JS} --setup")
        return False

    path = os.environ.get("S247_SESSION_FILE", ".session.env")
    if not os.path.isfile(path):
        return False
    n = 0
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line.startswith("export "):
                line = line[7:]
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                v = v.strip()
                if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                    v = v[1:-1]
                os.environ[k.strip()] = v
                n += 1
    log(f"  [OK ] new session loaded ({n} value(s)) — retrying")
    return n > 0


def load_json(path):
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception as exc:  # noqa: BLE001
        log(f"  [WARN] could not read {path}: {exc}")
        return None


def file_age_minutes(path):
    if not os.path.isfile(path):
        return None
    delta = datetime.now().timestamp() - os.path.getmtime(path)
    return delta / 60.0


INTEGRATIONS_FILE = "integrations.json"


# Which third-party apps this suite can log into and verify a ticket in.
# Everything else still gets DELIVERY verified from the Alert Logs -- we
# just cannot open the destination and read the ticket back.
ITSM_VERIFIABLE = ("servicenow", "service-now", "snow", "zoho desk",
                   "zohodesk", "servicedesk plus", "sdp", "haloitsm",
                   "halo itsm", "halo", "pagerduty")


# Site24x7 returns the third-party app as a NUMERIC id and its own UI
# only translates the ones it knows (it shows a bare "43" for HALO). These
# four were read off the account's own Integrations page, matched against
# the ids the API returned for the same rows -- not guessed. Extend it by
# dropping a third_party_apps.json next to the account data:
#     { "43": "HaloITSM", "11": "ServiceNow" }
APP_IDS = {
    "4": "ServiceDesk Plus Cloud",
    "11": "ServiceNow",
    "20": "Zoho Desk",
    "43": "HaloITSM",
    "1": "PagerDuty",
}


def app_ids():
    extra = load_json("third_party_apps.json")
    if isinstance(extra, dict):
        merged = dict(APP_IDS)
        merged.update({str(k): str(v) for k, v in extra.items()})
        return merged
    return APP_IDS


def app_label(entry):
    """What to show in an APP column. Never a bare id."""
    t = str(entry.get("type") or "").strip()
    if t and not t.isdigit():
        return t
    ident = str(entry.get("type_id") or t or "").strip()
    known = app_ids().get(ident)
    if known:
        return known
    if ident:
        return f"app id {ident} (name unknown)"
    return "(app type not reported)"


def is_itsm(entry):
    """Classify by the APP TYPE, never by the integration's name.

    "WebHook Sanity HALO" is a webhook, not HALO. Matching on names is how
    a ServiceNow integration called "Snow HALO Sanity" ended up being sent
    to the HALO adapter earlier. The type is what Site24x7 itself says the
    integration is, so use that.
    """
    t = str(entry.get("type") or "").lower().strip()
    if t and not t.isdigit():
        return any(k in t for k in ITSM_VERIFIABLE)
    # numeric id -> resolve through the app map before falling back
    ident = str(entry.get("type_id") or t or "").strip()
    known = app_ids().get(ident)
    if known:
        return any(k in known.lower() for k in ITSM_VERIFIABLE)
    # No usable type -- fall back to the NAME. Exclusions first, because
    # "WebHook Sanity HALO" is a webhook however much HALO is in its name.
    n = str(entry.get("name") or "").lower()
    for bad in ("webhook", "cliq", "alarmsone", "slack",
                "teams", "analytics", "pub/sub", "application manager"):
        if bad in n:
            return False
    NAME_HINTS = ("servicenow", "service-now", "snow", "desk", "sdp",
                  "servicedesk", "halo", "pagerduty", "pager duty")
    return any(k in n for k in NAME_HINTS)


def load_integration_records():
    data = load_json(INTEGRATIONS_FILE)
    if not isinstance(data, dict):
        return None
    recs = [i for i in (data.get("integrations") or []) if i.get("name")]
    return recs or None


def load_live_integrations():
    """Names of integrations that CURRENTLY exist in this account.

    Alert Logs are history. An integration deleted last week still has
    rows there, so without this the report lists it as live -- which is
    misleading and makes the whole report suspect. Returns None when the
    list has not been captured, in which case nothing is filtered and the
    report says so.
    """
    data = load_json(INTEGRATIONS_FILE)
    if not isinstance(data, dict):
        return None
    names = [str(i.get("name")) for i in (data.get("integrations") or [])
             if i.get("name")]
    return set(names) if names else None


def integration_status(name, recs):
    """Return the integration's status string from integrations.json.

    Possible returns:
        'active'     — live and enabled
        'suspended'  — exists but suspended in Site24x7
        'deleted'    — was in alert logs but NOT in integrations.json (removed)
        'unknown'    — integrations.json not captured, can't tell
    """
    if recs is None:
        return "unknown"
    for r in recs:
        if str(r.get("name") or "") == name:
            st = str(r.get("status") or "").lower().strip()
            if st in ("inactive", "suspended", "disabled", "0"):
                return "suspended"
            # service_status from raw: 0=Active, 1=Suspended (Site24x7)
            raw = r.get("raw") or {}
            svc_st = str(raw.get("service_status") or "").strip()
            if svc_st == "1":
                return "suspended"
            return "active"
    # Not found in integrations.json at all — must have been deleted
    return "deleted"


def load_known_issues():
    data = load_json(KNOWN_ISSUES)
    if not isinstance(data, dict):
        return {}
    return {str(k).lower(): str(v) for k, v in data.items()}


def known_note(integration, known):
    n = (integration or "").lower()
    for key, note in known.items():
        if key in n or n in key:
            return note
    return None


# ==========================================================================
# classification — the heart of the report
# ==========================================================================

def classify(stage3_entry, stage4_entry, known, integ_status="active"):
    """Return (bucket, headline, detail). Never guesses in the product's
    favour, and never blames the product for our own access problems.

    integ_status: 'active' | 'suspended' | 'deleted' | 'unknown'
        Passed in from integrations.json so the report can immediately
        say "this integration was DELETED" or "this integration is SUSPENDED"
        rather than showing confusing INCONCLUSIVE or DEFECT results for
        something that no longer exists.
    """
    integ = stage3_entry.get("integration", "unknown")
    v3 = str(stage3_entry.get("verdict", ""))
    v4 = str((stage4_entry or {}).get("verdict", ""))
    blocked4 = bool((stage4_entry or {}).get("blocked"))

    # ── DELETED ─────────────────────────────────────────────────────────────
    # The integration existed when the alert cycle ran (so it produced rows)
    # but has since been REMOVED from the account. We still show whatever
    # data we have, but the headline says it was deleted so the tester is
    # not confused about why it no longer appears in the UI.
    if integ_status == "deleted":
        note = known_note(integ, known)
        bucket = KNOWN if note else INCONCLUSIVE
        return (bucket,
                "⚠️  Integration was DELETED from this account",
                (f"'{integ}' no longer exists in the Site24x7 account. "
                 f"The findings below are from the alert log rows captured "
                 f"BEFORE it was deleted — they are historical, not current. "
                 f"To test this integration again, re-add it in Site24x7 and "
                 f"re-run the suite."
                 + (f" KNOWN: {note}" if note else "")))

    # ── SUSPENDED ────────────────────────────────────────────────────────────
    # The integration exists but is suspended (service_status=1 in S247).
    # A suspended integration receives no alerts — so zero rows and no
    # tickets is EXPECTED, not a defect. Report it clearly.
    if integ_status == "suspended":
        note = known_note(integ, known)
        bucket = KNOWN if note else BLOCKED
        return (bucket,
                "⏸️  Integration is SUSPENDED in Site24x7",
                (f"'{integ}' is currently SUSPENDED. While suspended it "
                 f"receives no alert deliveries, creates no tickets, and "
                 f"produces no alert log rows. This is expected — re-activate "
                 f"the integration in Site24x7 (Third-Party Integrations → "
                 f"Edit → Activate) and re-run the suite to test it."
                 + (f" KNOWN: {note}" if note else "")))

    note = known_note(integ, known)
    tool_blocked = blocked4 or v4.startswith("BLOCKED")
    no_adapter = v4 in ("NOT CONFIGURED", "NO ADAPTER")
    tool_unavailable = tool_blocked or no_adapter

    caveat = ""
    if tool_unavailable:
        why = ((stage4_entry or {}).get("blocker_detail")
               or v4 or "tool not reachable")
        caveat = (f" NOTE: the destination tool could not be checked ({why}), "
                  f"so the ticket's CURRENT state was not independently "
                  f"confirmed. Re-run once access is restored.")

    # --- 1. product-side evidence outranks any tool-side gap --------------
    # Site24x7's OWN alert logs are independent evidence. If they show the
    # product misbehaved, that finding stands even when we cannot log into
    # the destination tool to confirm it. Downgrading a proven defect to
    # BLOCKED because our ITSM password expired would hide a real bug.
    if v3.startswith("FAIL never closed"):
        bucket = KNOWN if note else DEFECT
        created = stage3_entry.get("created_ticket_ids") or []
        # stage3 writes statuses_seen as TEXT ("UP", "DOWN", "TROUBLE") via
        # its ALERT_STATUS map, not as the numeric codes from the raw log.
        # Accept both forms so this can never silently miss a recovery row.
        seen = {str(s).strip().upper()
                for s in (stage3_entry.get("statuses_seen") or [])}
        failed_st = {str(x).strip().upper()
                     for x in (stage3_entry.get("failed_statuses") or [])}
        # An UP row that FAILED is not a delivered recovery. Reporting a
        # failed delivery as "the tool ignored the recovery" sends a
        # developer to the wrong system entirely.
        delivered = bool(seen & {"UP", "1"}) and not (failed_st & {"UP", "1"})
        recovery_failed = bool(failed_st & {"UP", "1"})
        problem_states = sorted(seen & {"DOWN", "TROUBLE", "CRITICAL",
                                        "0", "2", "3"})
        state_note = (f" Problem states delivered in this cycle: "
                      f"{', '.join(problem_states)}." if problem_states else "")
        # A successful close/update outranks failed rows. Zoho Desk resolves
        # by UPDATE on UP, and one 404 among several rows must not turn a
        # working integration into a failure -- the ticket demonstrably
        # closed.
        resolved = (stage3_entry.get("matched_ticket_ids")
                    or stage3_entry.get("matched_on_up_ticket_ids"))
        if resolved:
            ids = ", ".join(map(str, resolved))
            return (PASS, "Ticket created and resolved",
                    f"Ticket(s) {ids} were created on the problem alert and "
                    f"resolved on recovery. "
                    + (f"{stage3_entry.get('failed_rows')} row(s) in this "
                       f"cycle did fail, but the lifecycle completed, so the "
                       f"integration works. "
                       if stage3_entry.get("failed_rows") else "")
                    + caveat)

        if recovery_failed:
            headline = ("Recovery DELIVERY FAILED — ticket left open")
            evidence = (
                f"A ticket was created on the problem alert "
                f"({', '.join(map(str, created)) or 'id not in logs'}), but "
                f"the recovery alert FAILED to reach this integration "
                f"({stage3_entry.get('failed_rows', '?')} failed row(s) in "
                f"this cycle). The ticket is open because the close never "
                f"arrived — this is a DELIVERY problem, not the tool "
                f"ignoring a recovery. Fix delivery first; only then can "
                f"the close behaviour be judged.")
            return (bucket, headline,
                    (note + " " if note else "") + evidence + state_note
                    + caveat)

        if delivered:
            headline = ("Recovery alert WAS delivered, but the ticket "
                        "was still not closed")
            evidence = (
                f"Site24x7 created a ticket on the problem alert "
                f"({', '.join(map(str, created)) or 'id not in logs'}) and "
                f"then delivered the recovery: an UP row for this integration "
                f"appears in the alert logs for the same cycle. The tool "
                f"accepted that recovery but issued no Close and no Update "
                f"against the ticket. Other integrations in the same cycle "
                f"logged 'Operation : Close' or 'Operation : Update' for the "
                f"identical recovery alert, so the fault is on the "
                f"integration's side, not in Site24x7's delivery."
                + state_note)
        else:
            headline = "Ticket created but NEVER closed on recovery"
            evidence = (
                f"Site24x7 delivered the problem alert and a ticket was "
                f"created ({', '.join(map(str, created)) or 'id not in logs'}), "
                f"but no recovery row for this integration appears in the "
                f"alert logs, and the ticket was never closed or resolved."
                + state_note)
        return (bucket, headline,
                (note + " " if note else "") + evidence + caveat)

    if v3.startswith("FAIL delivery error"):
        bucket = KNOWN if note else DEFECT
        return (bucket,
                "Delivery to the third-party tool failed",
                (note + " " if note else "") +
                f"Site24x7 recorded a failed delivery. No ticket was ever "
                f"created. Failed rows in window: "
                f"{stage3_entry.get('failed_rows', '?')}.")

    # --- 2. our own access problems, where nothing else is proven ---------
    if tool_blocked:
        detail = (stage4_entry or {}).get("blocker_detail", v4)
        return (BLOCKED,
                "Could not verify inside the tool",
                f"Site24x7 side says: {v3 or 'no data'}. "
                f"Tool-side check was blocked: {detail}. "
                f"This is a TEST HARNESS access problem, not a product "
                f"defect. Fix the credentials and re-run to get a verdict.")

    if no_adapter:
        if note:
            return (KNOWN, "Triaged issue, no tool-side check available",
                    f"{note} Site24x7 side says: {v3 or 'no data'}.")
        return (BLOCKED,
                "No tool-side verification configured",
                f"Site24x7 side says: {v3 or 'no data'}. "
                f"Tool-side check reported '{v4}', so the ticket was never "
                f"confirmed inside the destination tool.")

    # --- 3. everything else ----------------------------------------------
    if v3.startswith("PASS create+close"):
        created = stage3_entry.get("created_ticket_ids") or []
        matched = stage3_entry.get("matched_ticket_ids") or []
        extra = [t for t in created if t not in matched]
        extra_note = ""
        if extra:
            statuses = sorted({str(s) for s in
                               (stage3_entry.get("statuses_seen") or [])})
            extra_note = (
                f" NOTE: {len(created)} ticket(s) were created in total "
                f"({', '.join(map(str, created))}). Site24x7 creates one "
                f"ticket per alert event — e.g. a DOWN and a subsequent "
                f"TROUBLE each open a separate ticket. Alert statuses seen "
                f"in this cycle: {', '.join(statuses) or 'unknown'}. "
                f"Only the ticket matched to a recovery row "
                f"({', '.join(map(str, matched))}) is counted as the "
                f"lifecycle proof. The other ticket(s) "
                f"({', '.join(map(str, extra))}) belong to the earlier "
                f"alert event(s) in the same cycle — this is expected "
                f"behaviour, NOT a duplicate ticket defect."
            )
        return (PASS, "Ticket created and closed",
                f"Full lifecycle confirmed. Tool-side check: "
                f"{v4 or 'not run'}." + extra_note)

    if v3.startswith("PASS create+update@UP"):
        created = stage3_entry.get("created_ticket_ids") or []
        matched_up = stage3_entry.get("matched_on_up_ticket_ids") or []
        extra = [t for t in created if t not in matched_up]
        extra_note = ""
        if extra:
            statuses = sorted({str(s) for s in
                               (stage3_entry.get("statuses_seen") or [])})
            extra_note = (
                f" NOTE: {len(created)} ticket(s) were created in total "
                f"({', '.join(map(str, created))}). Site24x7 opens one "
                f"ticket per alert event — e.g. DOWN and TROUBLE are "
                f"separate events and each one creates its own ticket. "
                f"Alert statuses seen in this cycle: "
                f"{', '.join(statuses) or 'unknown'}. "
                f"The ticket matched to the UP recovery row is "
                f"{', '.join(map(str, matched_up))} — that is the one "
                f"proven to have been created AND resolved in this cycle. "
                f"The other ticket(s) ({', '.join(map(str, extra))}) were "
                f"created on earlier alert events within the same cycle. "
                f"This is expected Site24x7 behaviour — NOT a duplicate "
                f"ticket defect."
            )
        return (PASS, "Ticket created, then resolved by update on UP",
                f"This tool resolves by updating the ticket when the monitor "
                f"recovers rather than issuing an explicit Close. That is "
                f"correct behaviour for this integration, not a miss. "
                f"Tool-side check: {v4 or 'not run'}." + extra_note)

    if v3.startswith("INFO update only"):
        # The window caught an UPDATE row but not the CREATE. This means
        # a ticket WAS created (proven by the update) but in a prior alert
        # cycle. The ticket ID is real and useful — show it so the tester
        # can look it up. Never show "none recorded" when we have the id.
        updated_ids = stage3_entry.get("updated_ticket_ids") or []
        real_ids = [t for t in updated_ids
                    if str(t).lower() not in ("null", "none", "undefined",
                                              "nan", "0", "-", "n/a")]
        id_note = (f" Ticket ID(s) seen in update row(s): "
                   f"{', '.join(real_ids)}." if real_ids else "")
        return (INCONCLUSIVE, "Updates seen, no creation in this window",
                "The window caught an UPDATE (recovery) row for this "
                "integration but no CREATE row. The ticket was created in "
                "a prior alert cycle — the window probably starts after it. "
                "Re-run with a wider --hours to capture the full lifecycle."
                + id_note)

    return (INCONCLUSIVE,
            f"No conclusive result ({v3 or 'no Site24x7 data'})",
            f"Tool-side check: {v4 or 'not run'}. Re-run the cycle, or "
            f"widen the Alert Logs window with --hours.")


def build_findings(stage3, stage4, known, live_recs=None):
    s4_by_integ = {}
    for e in (stage4 or {}).get("results", []):
        s4_by_integ[str(e.get("integration", "")).lower()] = e

    findings = []
    seen_integrations = set()
    for e in (stage3 or {}).get("results", []):
        integ = e.get("integration", "unknown")
        seen_integrations.add(integ)
        s4 = s4_by_integ.get(str(integ).lower())
        # Determine live status: deleted/suspended/active/unknown
        integ_st = integration_status(integ, live_recs)
        bucket, headline, detail = classify(e, s4, known, integ_status=integ_st)
        findings.append({
            "integration": integ,
            "bucket": bucket,
            "headline": headline,
            "detail": detail,
            "site24x7_verdict": e.get("verdict"),
            "tool_verdict": (s4 or {}).get("verdict"),
            "tool": (s4 or {}).get("tool"),
            "created_ticket_ids": e.get("created_ticket_ids") or [],
            "closed_ticket_ids": e.get("closed_ticket_ids") or [],
            "updated_ticket_ids": e.get("updated_ticket_ids") or [],
            "matched_ticket_ids": e.get("matched_ticket_ids") or [],
            "matched_on_up_ticket_ids": e.get("matched_on_up_ticket_ids") or [],
            "alert_log_rows": e.get("rows"),
            "failed_rows": e.get("failed_rows"),
            "tool_tickets": (s4 or {}).get("tickets") or [],
        })

    # ALWAYS-PRESENT RULE: every integration that is live in the account
    # MUST appear in the report, even if it fired zero alert log rows.
    # Without this, integrations like SDP and ServiceNow silently vanish
    # when they don't fire for a particular monitor — which is itself a
    # finding (NOT TESTED / no alert rows) that must be visible, not hidden.
    #
    # DELETED / SUSPENDED integrations that appear in alert log history
    # (seen_integrations) are already handled by classify() above via
    # integ_status. This loop only needs to surface integrations from
    # integrations.json that produced ZERO alert log rows at all.
    if live_recs:
        for rec in live_recs:
            integ = str(rec.get("name") or "")
            if not integ or integ in seen_integrations:
                continue
            note = known_note(integ, known)
            integ_st = integration_status(integ, live_recs)

            # Suspended — receives no alerts, so zero rows is expected
            if integ_st == "suspended":
                bucket = KNOWN if note else BLOCKED
                headline = "⏸️  Integration is SUSPENDED in Site24x7"
                detail = (
                    f"'{integ}' is currently SUSPENDED in Site24x7. "
                    f"While suspended it receives no alert deliveries, "
                    f"creates no tickets, and produces no alert log rows. "
                    f"Re-activate it in Site24x7 → Third-Party Integrations "
                    f"→ Edit → Activate, then re-run the suite."
                    + (f" KNOWN: {note}" if note else "")
                )
            else:
                # Active but no alert log rows — a real gap to investigate
                bucket = KNOWN if note else INCONCLUSIVE
                headline = "No alert log rows for this integration in this cycle's window"
                detail = (
                    f"'{integ}' is configured and active in this account "
                    f"(app type: {app_label(rec)}), but no alert log rows "
                    f"for it appeared in the window for this monitor. "
                    f"Possible causes: (1) this integration is not attached "
                    f"to this specific monitor, (2) the integration did not "
                    f"fire during the cycle window, or (3) the alert was "
                    f"delivered but not yet logged. "
                    f"Ticket IDs cannot be shown when no alert rows exist. "
                    f"Check the integration's monitor assignment in the "
                    f"Site24x7 UI and re-run."
                    + (f" KNOWN: {note}" if note else "")
                )

            findings.append({
                "integration": integ,
                "bucket": bucket,
                "headline": headline,
                "detail": detail,
                "site24x7_verdict": "NO ALERT LOG ROWS",
                "tool_verdict": None,
                "tool": None,
                "created_ticket_ids": [],
                "closed_ticket_ids": [],
                "updated_ticket_ids": [],
                "matched_ticket_ids": [],
                "matched_on_up_ticket_ids": [],
                "alert_log_rows": 0,
                "failed_rows": 0,
                "tool_tickets": [],
            })

    findings.sort(key=lambda f: {DEFECT: 0, BLOCKED: 1, INCONCLUSIVE: 2,
                                 KNOWN: 3, PASS: 4}.get(f["bucket"], 9))
    return findings


# ==========================================================================
# report writers
# ==========================================================================

CSS = """
*{box-sizing:border-box}
body{margin:0;padding:32px;background:#f4f5f7;color:#1b1f23;
 font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
.wrap{max-width:1080px;margin:0 auto}
h1{font-size:26px;margin:0 0 4px}
h2{font-size:18px;margin:34px 0 12px;padding-bottom:7px;border-bottom:2px solid #e1e4e8}
.sub{color:#57606a;margin:0 0 24px}
.card{background:#fff;border:1px solid #d8dee4;border-radius:8px;padding:18px 20px;margin-bottom:14px}
.tiles{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:8px}
.tile{flex:1;min-width:130px;background:#fff;border:1px solid #d8dee4;border-radius:8px;
 padding:14px 16px;text-align:center}
.tile .n{font-size:30px;font-weight:700;line-height:1.1}
.tile .l{font-size:11px;letter-spacing:.09em;text-transform:uppercase;color:#57606a;margin-top:3px}
.pill{display:inline-block;padding:3px 11px;border-radius:20px;font-size:11px;font-weight:700;
 letter-spacing:.07em;text-transform:uppercase;color:#fff}
.PASS{background:#1a7f37}.DEFECT{background:#cf222e}.BLOCKED{background:#9a6700}
.KNOWN{background:#6e7781}.INCONCLUSIVE{background:#0969da}
.DELETED{background:#6f42c1}.SUSPENDED{background:#e36209}
.b-PASS{border-left:5px solid #1a7f37}.b-DEFECT{border-left:5px solid #cf222e}
.b-BLOCKED{border-left:5px solid #9a6700}.b-KNOWN{border-left:5px solid #6e7781}
.b-INCONCLUSIVE{border-left:5px solid #0969da}
.b-DELETED{border-left:5px solid #6f42c1}.b-SUSPENDED{border-left:5px solid #e36209}
table{border-collapse:collapse;width:100%;font-size:14px}
th,td{text-align:left;padding:8px 10px;border-bottom:1px solid #e1e4e8;vertical-align:top}
th{font-size:11px;letter-spacing:.07em;text-transform:uppercase;color:#57606a}
code,.mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:13px}
.tid{display:inline-block;background:#eef1f4;border:1px solid #d8dee4;border-radius:4px;
 padding:1px 7px;margin:2px 4px 2px 0;font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12.5px}
.hd{font-weight:600;font-size:16px;margin:8px 0 6px}
.det{color:#3d444d}
.note{background:#fff8e5;border:1px solid #e8d9a8;border-radius:8px;padding:14px 16px;margin-bottom:14px}
.foot{color:#57606a;font-size:12.5px;margin-top:34px;border-top:1px solid #d8dee4;padding-top:14px}
.ok{color:#1a7f37;font-weight:600}.bad{color:#cf222e;font-weight:600}
"""


def esc(x):
    return html.escape(str(x if x is not None else ""))


def tickets_html(ids, warn_multiple=False):
    if not ids:
        return '<span style="color:#8b949e">none recorded</span>'
    rendered = "".join(f'<span class="tid">{esc(t)}</span>' for t in ids)
    if warn_multiple and len(ids) > 1:
        tip = (
            "Multiple tickets were created in this cycle — one per alert event "
            "(e.g. DOWN and TROUBLE each open a new ticket). "
            "Only the ticket matched to a recovery row appears in Created AND Closed."
        )
        rendered += (
            f' <span style="color:#9a6700;font-size:11px;font-weight:600;" '
            f'title="{tip}">'
            f'&#9888;&nbsp;{len(ids)} tickets — see &quot;Created AND Closed&quot; for the matched one'
            f'</span>'
        )
    return rendered


def write_html(path, ctx):
    f = ctx["findings"]
    counts = ctx["counts"]
    cyc = ctx["cycle"] or {}
    env = ctx["env"]

    rows = ""
    for x in f:
        rows += f"""<tr><td class="mono">{esc(x.get('monitor'))}</td>
<td><strong>{esc(x['integration'])}</strong></td>
<td><span class="pill {x['bucket']}">{x['bucket']}</span></td>
<td>{esc(x['headline'])}</td>
<td class="mono">{esc(x['site24x7_verdict'])}</td>
<td class="mono">{esc(x['tool_verdict'])}</td></tr>"""

    blocks = ""
    for x in f:
        tool_rows = ""
        for t in x["tool_tickets"]:
            state = ("found" if t.get("found") else "NOT FOUND")
            tool_rows += (f'<tr><td class="mono">{esc(t.get("ticket_id"))}</td>'
                          f'<td>{esc(state)}</td>'
                          f'<td>{esc(t.get("status") or t.get("error"))}</td></tr>')
        tool_tbl = ("<table><tr><th>ticket</th><th>lookup</th>"
                    "<th>status in tool</th></tr>" + tool_rows + "</table>"
                    if tool_rows else "")
        blocks += f"""<div class="card b-{x['bucket']}">
<span class="pill {x['bucket']}">{x['bucket']}</span>
<div class="hd">{esc(x.get('monitor'))} &nbsp;/&nbsp; {esc(x['integration'])} &mdash; {esc(x['headline'])}</div>
<p class="det">{esc(x['detail'])}</p>
<table>
<tr><th style="width:190px">Created ticket ids</th><td>{tickets_html(x['created_ticket_ids'], warn_multiple=len(x['created_ticket_ids']) > 1)}</td></tr>
<tr><th>Closed ticket ids</th><td>{tickets_html(x['closed_ticket_ids'])}</td></tr>
<tr><th>Updated ticket ids</th><td>{tickets_html(x['updated_ticket_ids'])}</td></tr>
<tr><th>Created AND closed</th><td>{tickets_html(x['matched_ticket_ids'] + x['matched_on_up_ticket_ids'])}</td></tr>
<tr><th>Alert log rows</th><td class="mono">{esc(x['alert_log_rows'])} (failed deliveries: {esc(x['failed_rows'])})</td></tr>
<tr><th>Verified in tool</th><td>{esc(x['tool'] or 'not checked')}</td></tr>
</table>{tool_tbl}</div>"""

    fresh_cycle = ctx.get("cycle_this_run")
    restored = cyc.get("restored")
    verified = cyc.get("restore_verified")
    clean = bool(restored and verified)
    if cyc:
        per_mon = ""
        for mid, d in (cyc.get("restore_detail") or {}).items():
            okk = d.get("restored") and d.get("verified")
            per_mon += (f'<tr><th>{esc(d.get("name") or mid)}</th>'
                        f'<td class="mono"><span class="{"ok" if okk else "bad"}">'
                        f'{"restored and re-read from server" if okk else "NOT CONFIRMED"}'
                        f'</span> &mdash; keyword now {esc(d.get("keyword_now"))} '
                        f'(original {esc(d.get("keyword_original"))}), '
                        f'backup {esc(d.get("backup"))}</td></tr>')
        cleanup = (f'<p><span class="{"ok" if clean else "bad"}">'
                   f'{"ENVIRONMENT RESTORED AND VERIFIED" if clean else "RESTORATION NOT CONFIRMED - CHECK THE MONITORS BY HAND"}'
                   f'</span></p><table>'
                   f'<tr><th style="width:230px">Cycle ran in this session</th>'
                   f'<td class="mono">{"yes" if fresh_cycle else "no &mdash; values below are from the most recent cycle, not this run"}</td></tr>'
                   f'<tr><th>Cycle verdict</th><td class="mono">{esc(cyc.get("verdict"))}</td></tr>'
                   f'</table>'
                   f'<table>{per_mon}</table>')
    else:
        cleanup = ('<p>The alert cycle was not run in this session '
                   '(--skip-cycle or --report-only). Nothing was modified, '
                   'so nothing needed restoring.</p>')

    recs = ctx["env"].get("integration_records") or []
    if recs:
        rows_i = ""
        for r in sorted(recs, key=lambda x: str(x.get("name"))):
            kind = ("ITSM — ticket read back inside the tool"
                    if is_itsm(r) else
                    "delivery checked in Alert Logs only")
            st = str(r.get("status") or "Active")
            st_low = st.lower().strip()
            if st_low in ("inactive", "suspended", "disabled", "0"):
                cls = "bad"
                st_label = "⏸️ Suspended"
            else:
                cls = "ok"
                st_label = "✅ Active"
            rows_i += (f'<tr><td><strong>{esc(r.get("name"))}</strong></td>'
                       f'<td class="mono">{esc(app_label(r))}</td>'
                       f'<td class="mono"><span class="{cls}">{st_label}</span></td>'
                       f'<td>{kind}</td></tr>')
        integrations_html = (
            f'<div class="card"><p>Captured live from the account at run '
            f'time, so integrations that have been deleted do not appear.</p>'
            f'<table><tr><th>Integration</th><th>App</th><th>Status</th>'
            f'<th>How it is verified</th></tr>{rows_i}</table></div>')
    else:
        integrations_html = (
            '<div class="note"><strong>The live integration list was not '
            'captured for this run.</strong><p>Integration names below come '
            'from the Alert Logs, which are historical — any integration '
            'deleted from the account may still appear as though it were '
            'live. Capture the list with '
            '<code>python3 itsm.py -a &lt;account&gt; --integrations</code>.'
            '</p></div>')

    itsm_first_data = ctx["env"].get("itsm_first") or {}
    if itsm_first_data:
        rows_if = ""
        for mid, data in itsm_first_data.items():
            for tool_result in (data.get("results") or []):
                tool_name = tool_result.get("tool", "")
                tickets = tool_result.get("tickets") or []
                if not tool_result.get("configured"):
                    rows_if += (f'<tr><td>{esc(tool_name)}</td>'
                                f'<td class="mono">not configured</td>'
                                f'<td>-</td><td>-</td></tr>')
                    continue
                if not tickets:
                    rows_if += (f'<tr><td>{esc(tool_name)}</td>'
                                f'<td class="mono">0 tickets</td>'
                                f'<td>no tickets created in this window</td>'
                                f'<td>-</td></tr>')
                    continue
                for t in tickets:
                    anc = t.get("anchor")
                    anc_text = (f'{anc["from"]}&rarr;{anc["to"]} at '
                                f'{esc(anc["change_at_raw"])}, '
                                f'+{anc["lag_seconds"]:.0f}s'
                                if anc else "no anchor")
                    rows_if += (f'<tr><td>{esc(tool_name)}</td>'
                                f'<td class="tid">{esc(t["ticket_id"])}</td>'
                                f'<td>{esc(t.get("status",""))}</td>'
                                f'<td>{anc_text}</td></tr>')
        itsm_first_html = (
            f'<table><tr><th>Tool</th><th>Ticket</th><th>Status</th>'
            f'<th>Anchored to state change</th></tr>{rows_if}</table>')
    else:
        itsm_first_html = ('<p class="note">ITSM-first verification did not '
                           'run for this report.</p>')

    defects = [x for x in f if x["bucket"] == DEFECT]
    if defects:
        dhtml = "".join(
            f'<div class="card b-DEFECT"><div class="hd">DEFECT &mdash; '
            f'{esc(d["integration"])} on {esc(d.get("monitor"))}: '
            f'{esc(d["headline"])}</div>'
            f'<p class="det">{esc(d["detail"])}</p>'
            f'<p><strong>Evidence:</strong> {tickets_html(d["created_ticket_ids"])}</p>'
            f'<p><strong>Reproduce:</strong> <code>python3 run_all.py '
            f'--monitor-id {esc(d.get("monitor_id"))}</code> on '
            f'<code>{esc(env.get("grid"))}</code></p></div>'
            for d in defects)
    else:
        dhtml = ('<div class="card"><p>No application defects detected in '
                 'this run.</p></div>')

    sc = (cyc or {}).get("server_coverage") or {}
    server_banner = ""
    if sc.get("attempted") and sc.get("skipped"):
        rows_ = "".join(
            f'<tr><th>{esc(v.get("name"))}</th>'
            f'<td class="mono">{esc(v.get("status"))}</td></tr>'
            for v in (sc.get("monitors") or {}).values())
        server_banner = (
            f'<div class="note"><strong>SERVER MONITOR COVERAGE WAS '
            f'SKIPPED.</strong>'
            f'<p>All server monitors were unreachable and could not be '
            f'connected. They were given {esc(sc.get("waited_seconds"))} '
            f'seconds to recover; at least {esc(sc.get("min_required"))} had '
            f'to be UP or TROUBLE and none qualified.</p>'
            f'<p><strong>Only the website monitors were tested in this run. '
            f'Server integrations are UNTESTED — not passed, not failed.'
            f'</strong></p><table>{rows_}</table></div>')

    blockers = [x for x in f if x["bucket"] == BLOCKED]
    bnote = ""
    if blockers:
        items = "".join(f'<li><strong>{esc(b["integration"])}</strong>: '
                        f'{esc(b["headline"])}</li>' for b in blockers)
        bnote = (f'<div class="note"><strong>Read this before acting on the '
                 f'results.</strong><p>{len(blockers)} integration(s) could '
                 f'not be verified because the test harness could not reach '
                 f'the tool. These are <em>not</em> product defects and must '
                 f'not be raised as bugs. They are untested.</p>'
                 f'<ul>{items}</ul></div>')

    doc = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<title>Site24x7 ITSM End-to-End Report {esc(ctx['started'])}</title>
<style>{CSS}</style></head><body><div class="wrap">
<h1>Site24x7 ITSM End-to-End Test Report</h1>
<p class="sub">Build deployed in: {esc(env.get('grid'))}<br>
Account: <strong>{esc(os.environ.get('S247_ACCOUNT', '(not set)'))}</strong>
&nbsp;&middot;&nbsp; User: {esc(os.environ.get('S247_LOGIN_USER', '(not set)'))}<br>
Run started {esc(ctx['started'])} &nbsp;&middot;&nbsp; Run ended {esc(ctx['finished'])} &nbsp;&middot;&nbsp; Duration {esc(ctx['duration'])}</p>

<h2>Executive summary</h2>
<div class="tiles">
<div class="tile"><div class="n" style="color:#1a7f37">{counts.get(PASS,0)}</div><div class="l">Pass</div></div>
<div class="tile"><div class="n" style="color:#cf222e">{counts.get(DEFECT,0)}</div><div class="l">Defect</div></div>
<div class="tile"><div class="n" style="color:#9a6700">{counts.get(BLOCKED,0)}</div><div class="l">Blocked</div></div>
<div class="tile"><div class="n" style="color:#6e7781">{counts.get(KNOWN,0)}</div><div class="l">Known</div></div>
<div class="tile"><div class="n" style="color:#0969da">{counts.get(INCONCLUSIVE,0)}</div><div class="l">Inconclusive</div></div>
</div>
{server_banner}
<div class="card"><p><strong>Build health: <span class="pill {ctx['health']}">{ctx['health']}</span></strong></p>
<p class="det">{esc(ctx['health_reason'])}</p></div>
{bnote}

<h2>Environment</h2>
<div class="card"><table>
<tr><th style="width:230px">Grid</th><td class="mono">{esc(env.get('grid'))}</td></tr>
<tr><th>Monitors under test</th><td class="mono">{esc(env.get('monitor_name') or env.get('monitor_id'))}</td></tr>
<tr><th>Integrations filter</th><td class="mono">{("only the " + str(len(env.get("live_integrations") or [])) + " integration(s) currently configured in this account are reported" + ((" &mdash; excluded as deleted: " + esc(", ".join(env.get("excluded_integrations") or []))) if env.get("excluded_integrations") else "")) if env.get("live_integrations") else "NOT APPLIED &mdash; integrations.json missing, so deleted integrations may appear as live"}</td></tr>
<tr><th>Alert log window</th><td class="mono">{esc(env.get('hours'))} hour(s)</td></tr>
<tr><th>Host</th><td class="mono">{esc(env.get('host'))}</td></tr>
<tr><th>Report generated</th><td class="mono">{esc(ctx['finished'])}</td></tr>
</table></div>

<h2>Third-party integrations configured in this account</h2>
{integrations_html}

<h2>ITSM-First Verification (Primary Evidence)</h2>
<div class="card">
<p>Tickets found by searching each ITSM tool directly for this cycle's window. These are real-time results — not derived from Alert Logs.</p>
{itsm_first_html}
</div>

<h2>Results by integration</h2>
<div class="card"><table>
<tr><th>Monitor</th><th>Integration</th><th>Result</th><th>Summary</th><th>Site24x7 alert logs</th><th>Inside the tool</th></tr>
{rows}</table></div>

<h2>Evidence detail</h2>
{blocks}

<h2>Defects for development</h2>
{dhtml}

<h2>Cleanup and restoration</h2>
<div class="card">{cleanup}</div>

<p class="foot"><strong>How to read this report</strong><br>
<span style="color:#1a7f37">&#9632; PASS</span> &mdash; the integration worked correctly: a ticket was created when the monitor went down and closed when it recovered.<br>
<span style="color:#cf222e">&#9632; DEFECT</span> &mdash; something went wrong in the product. A developer needs to investigate and fix this.<br>
<span style="color:#9a6700">&#9632; BLOCKED</span> &mdash; our test setup could not connect to the tool (expired credentials, network issue, etc). This is NOT a product bug &mdash; the integration was simply not tested. Do not file a bug for it.<br>
<span style="color:#6e7781">&#9632; KNOWN</span> &mdash; a real issue that has already been identified and is being tracked separately.<br>
<span style="color:#0969da">&#9632; INCONCLUSIVE</span> &mdash; not enough information to judge. Re-run with a wider time window or check the alert logs manually.<br><br>
This report contains no passwords, tokens or credentials. Safe to share with developers.</p>
</div></body></html>"""

    with open(path, "w", encoding="utf-8") as fh:
        fh.write(doc)


def write_junit(path, ctx):
    cases = []
    for x in ctx["findings"]:
        # Include the monitor: with two monitors the same integration
        # appears twice, and Jenkins silently merges identically-named
        # cases -- one monitor's result would vanish from the trend.
        name = sx.quoteattr(
            f"{x['integration']} ticket lifecycle "
            f"[{x.get('monitor') or x.get('monitor_id') or 'monitor'}]")
        body = f"{x['headline']}. {x['detail']}"
        if x["bucket"] == DEFECT:
            inner = (f"<failure message={sx.quoteattr(x['headline'])} "
                     f"type=\"ProductDefect\">{sx.escape(body)}</failure>")
        elif x["bucket"] in (BLOCKED, KNOWN):
            # deliberately NOT a failure: harness problems and triaged issues
            # must never turn the build red.
            inner = (f"<skipped message={sx.quoteattr(x['bucket'] + ': ' + x['headline'])}/>")
        elif x["bucket"] == INCONCLUSIVE:
            inner = f"<skipped message={sx.quoteattr(x['headline'])}/>"
        else:
            inner = ""
        cases.append(f'  <testcase classname="itsm.integration" name={name} '
                     f'time="0">{inner}</testcase>')

    counts = ctx["counts"]
    total = len(ctx["findings"])
    fails = counts.get(DEFECT, 0)
    skips = counts.get(BLOCKED, 0) + counts.get(KNOWN, 0) + \
        counts.get(INCONCLUSIVE, 0)
    xml = (f'<?xml version="1.0" encoding="UTF-8"?>\n'
           f'<testsuites><testsuite name="Site24x7 ITSM end-to-end" '
           f'tests="{total}" failures="{fails}" skipped="{skips}" '
           f'errors="0" timestamp={sx.quoteattr(ctx["started"])}>\n'
           + "\n".join(cases) + "\n</testsuite></testsuites>\n")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(xml)


# ==========================================================================

def main():
    ap = argparse.ArgumentParser(
        description="Stage 5 - run the whole ITSM suite and build a report")
    ap.add_argument("--monitor-ids", default=None,
                    help="comma-separated monitor ids to drive and verify, "
                         "e.g. 73970000000036006,73970000000110005")
    ap.add_argument("--monitor-id", default=None,
                    help="monitor to drive. Default: whatever stage2b picks, "
                         "read back from its result file.")
    ap.add_argument("--hours", type=int, default=2,
                    help="Alert Logs window for stage 3 (default 2)")
    ap.add_argument("--skip-cycle", action="store_true",
                    help="do not drive a new alert cycle, reuse the last one")
    ap.add_argument("--skip-tools", action="store_true",
                    help="skip stage 4 (tool-side verification)")
    ap.add_argument("--report-only", action="store_true",
                    help="run nothing, just rebuild the report from the "
                         "existing json files")
    ap.add_argument("--no-logreport", action="store_true",
                    help="skip the Log Report capture. Tickets then cannot "
                         "be anchored to real status changes, and the report "
                         "says the anchor was not applied.")
    ap.add_argument("--settle", type=int, default=120,
                    help="seconds to wait after the cycle before verifying, "
                         "so slow ticket creation is not mistaken for a "
                         "missing ticket (default 120)")
    ap.add_argument("--no-relogin", action="store_true",
                    help="do not try to re-authenticate if the session dies "
                         "mid-run (default is to retry once)")
    ap.add_argument("--wide-window", action="store_true",
                    help="ignore the cycle's exact timestamps and use the "
                         "rolling --hours window instead. Less precise; can "
                         "pick up tickets from earlier runs.")
    ap.add_argument("--window-pad", type=int, default=3,
                    help="minutes of margin either side of the cycle window "
                         "(default 3) to allow for delivery lag")
    ap.add_argument("--server-wait", type=int, default=None,
                    help="passed to stage2b: how long to give DOWN server "
                         "monitors to recover before skipping server "
                         "coverage (seconds)")
    ap.add_argument("--server-poll", type=int, default=None,
                    help="passed to stage2b: how often to re-check them")
    ap.add_argument("--routes", default=None,
                    help="passed to stage2b: state route per monitor, e.g. "
                         "'trouble,down;down,trouble'")
    ap.add_argument("--monitor-text", default="Do Not Delete",
                    help="text used to find ServiceNow incidents, which "
                         "carry no ticket id in the alert logs")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the plan, change nothing")
    ap.add_argument("--out-dir", default=REPORT_DIR)
    args = ap.parse_args()

    started_dt = datetime.now()
    started = started_dt.strftime("%Y-%m-%d %H:%M:%S")
    stamp = started_dt.strftime("%Y%m%d_%H%M%S")
    grid = os.environ.get("S247_GRID_URL", "(S247_GRID_URL not set)")

    section("SITE24X7 ITSM END-TO-END SUITE — STAGE 5")
    log(f"  started : {started}")
    log(f"  grid    : {grid}")
    log(f"  plan    : "
        + ("report only"
           if args.report_only else
           ("stage3 + stage4" if args.skip_cycle else "stage2b + stage3 + stage4")))
    if grid.startswith("("):
        log("\n  [BLOCKER] S247_GRID_URL is not set in this terminal.")
        log("            Run:  source start.sh")
        sys.exit(2)

    stages = []

    # ---- stage 2b -------------------------------------------------------
    if not args.report_only and not args.skip_cycle:
        argv = [sys.executable, script("stage2b_cycle.py"), "--yes"]
        if args.monitor_ids:
            argv += ["--monitor-ids", args.monitor_ids]
        elif args.monitor_id:
            argv += ["--monitor-id", args.monitor_id]
        # forward the stage2b-specific flags rather than swallowing them
        if args.server_wait is not None:
            argv += ["--server-wait", str(args.server_wait)]
        if args.server_poll is not None:
            argv += ["--server-poll", str(args.server_poll)]
        if args.routes:
            argv += ["--routes", args.routes]
        if args.dry_run:
            argv += ["--dry-run"]
        stages.append(run_stage("STAGE 2B — DRIVE A REAL ALERT CYCLE",
                                argv, args.dry_run))
    else:
        log("\n  [skip] stage 2b — reusing the previous alert cycle")

    cycle = load_json(STAGE2_RESULT)

    # Flag a bad cycle at its source, not three stages later.
    if cycle and not args.report_only and not args.skip_cycle:
        cv = str(cycle.get("verdict", ""))
        if cv != "PASS":
            section(f"WARNING — ALERT CYCLE VERDICT: {cv}")
            phases = cycle.get("phases", {})
            if not (phases.get("B_force_problem") or {}).get("ok", True):
                log("  Phase B never reached a problem state, so NO alert")
                log("  fired and NO ticket was created. Whatever stages 3 and")
                log("  4 find next will be left over from an earlier run.")
                log("")
                log("  The keyword used was: "
                    f"{cycle.get('derived_keyword')!r}")
                log("  If that text is not in the page body Site24x7 actually")
                log("  fetches, the monitor can never go down. Check with:")
                log(f"    curl -s <monitor url> | grep -c -i "
                    f"{cycle.get('derived_keyword')!r}")
            if cv == "FAIL_UNSAFE":
                log("  !! The monitor may NOT be restored. Check it by hand.")

    # Which monitors to verify -- read back from stage2b, never hard-coded.
    monitors = []
    if args.monitor_ids:
        monitors = [{"monitor_id": m.strip(), "name": None}
                    for m in args.monitor_ids.split(",") if m.strip()]
    elif args.monitor_id:
        monitors = [{"monitor_id": str(args.monitor_id), "name": None}]
    elif cycle:
        for m in (cycle.get("monitors") or []):
            mid = m.get("monitor_id") or m.get("resource_id") or m.get("id")
            if mid:
                monitors.append({"monitor_id": str(mid),
                                 "name": m.get("name") or m.get("display_name"),
                                 "route": m.get("route")})
        if not monitors:                       # older single-monitor result
            mon = cycle.get("monitor")
            if isinstance(mon, dict):
                mid = (mon.get("monitor_id") or mon.get("resource_id")
                       or mon.get("id"))
                if mid:
                    monitors.append({"monitor_id": str(mid),
                                     "name": mon.get("name")
                                     or mon.get("display_name")})
            elif isinstance(mon, str):
                monitors.append({"monitor_id": mon, "name": None})

    if not monitors and not args.report_only:
        log(f"\n  [BLOCKER] no monitor ids. {STAGE2_RESULT} recorded none and")
        log("            --monitor-id was not given. Pass one explicitly:")
        log("              python3 run_all.py --monitor-id <id>")
        sys.exit(2)

    if monitors:
        log("\n  Monitors to verify:")
        for m in monitors:
            route = " -> ".join(["UP"] + [r.upper() for r in (m.get("route") or [])]
                                + ["UP"]) if m.get("route") else "(route unknown)"
            log(f"    {m['monitor_id']}  {m.get('name') or ''}   {route}")

    # ---- the exact cycle window -----------------------------------------
    cycle_since = (cycle or {}).get("cycle_started_at")
    cycle_until = (cycle or {}).get("cycle_ended_at")
    if cycle_since and not args.wide_window:
        # a small margin each side: delivery to the ITSM tool lags the
        # state change by seconds, and clocks drift
        try:
            pad = timedelta(minutes=args.window_pad)
            # An Alert Log row is written only AFTER the ticket has been
            # created in the destination tool, so its timestamp already
            # carries the 30-120s creation lag. The window must therefore
            # extend PAST the end of the cycle by at least that lag, or the
            # rows for the final recovery fall outside it and the run looks
            # like nothing happened.
            tail = pad + timedelta(seconds=max(args.settle, 120))
            cycle_since = (datetime.strptime(cycle_since, "%Y-%m-%d %H:%M:%S")
                           - pad).strftime("%Y-%m-%d %H:%M:%S")
            if cycle_until:
                cycle_until = (datetime.strptime(cycle_until,
                                                 "%Y-%m-%d %H:%M:%S")
                               + tail).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            pass
        section("VERIFICATION WINDOW")
        log(f"  EXACT window: {cycle_since}  ->  {cycle_until or 'now'}")
        log(f"  Start: cycle start minus {args.window_pad} min.")
        log(f"  End:   cycle end PLUS {args.window_pad} min plus the "
            f"{max(args.settle, 120)}s ticket-creation lag —")
        log(f"         an Alert Log row is only written once the ticket has")
        log(f"         actually been created in the tool, so those rows")
        log(f"         arrive AFTER the alert itself.")
        log("  Only alerts raised by THIS cycle are counted. A ticket from")
        log("  an earlier run cannot be mistaken for proof of this one.")
    elif args.wide_window:
        section("VERIFICATION WINDOW")
        log(f"  WIDE window: last {args.hours}h (--wide-window given).")
        log("  [WARN] this can sweep in tickets from EARLIER runs.")
    else:
        section("VERIFICATION WINDOW")
        log(f"  rolling window: last {args.hours}h")
        log("  [note] no cycle timestamps recorded (old stage2b result, or")
        log("         --skip-cycle with a result predating this feature).")

    # ---- let delivery settle --------------------------------------------
    # Ticket creation lags the alert by 30-120 seconds. Verifying the
    # instant the cycle ends finds nothing and reports a false failure.
    if (not args.report_only and not args.dry_run and not args.skip_cycle
            and args.settle > 0):
        section("WAITING FOR TICKET DELIVERY TO SETTLE")
        log(f"  Ticket creation lags the alert by roughly 30-120s in these")
        log(f"  tools. Waiting {args.settle}s before verifying, so a slow")
        log(f"  delivery is not reported as a missing ticket.")
        for left in range(args.settle, 0, -15):
            time.sleep(min(15, left))
            log(f"    {left - min(15, left)}s remaining")
        log("  done waiting.")

    # ---- STEP 1: the monitor's OWN record of when it changed -------------
    # Captured AFTER the cycle so it covers the transitions that just
    # happened. This is the anchor: a ticket only counts as evidence if it
    # was created just after a real state change. Three cycles can run in
    # two hours, and each leaves perfectly real ticket ids in the log.
    LOGREPORT_JS = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "s247_logreport.js")

    if (not args.report_only and not args.dry_run
            and not args.no_logreport and monitors):
        if os.path.isfile(LOGREPORT_JS):
            section("STEP 1 — LOG REPORT: WHEN DID EACH MONITOR CHANGE?")
            env2 = dict(os.environ)
            env2["S247_LOGREPORT_DIR"] = os.getcwd()
            env2.pop("S247_LOGREPORT_FILE", None)
            ids = [m["monitor_id"] for m in monitors]
            try:
                r = subprocess.run(["node", LOGREPORT_JS] + ids, env=env2,
                                   timeout=900)
                if r.returncode != 0:
                    log("\n  [WARN] the Log Report capture failed. Tickets")
                    log("         cannot be anchored to real state changes,")
                    log("         and the report will say so.")
            except Exception as exc:  # noqa: BLE001
                log(f"\n  [WARN] could not run the Log Report capture: {exc}")
        else:
            log(f"\n  [WARN] {LOGREPORT_JS} not found — tickets will not be")
            log("         anchored to state changes.")

    # ==== ITSM-FIRST VERIFICATION ========================================
    # THE ARCHITECTURE CHANGE: go to each ITSM tool FIRST and ask what
    # tickets were created in the cycle window. Alert Logs are the receipt,
    # not the starting point. A ticket in the tool is real-time evidence;
    # an alert log row is a historical receipt that can contain entries
    # from runs a week ago.
    itsm_first_results = {}
    if not args.report_only and not args.dry_run and monitors:
        section("ITSM-FIRST — SEARCHING EACH TOOL DIRECTLY")
        log("  Going to each ITSM tool and asking: what tickets were")
        log("  created in the last few minutes for these monitors?")
        log("  This is the PRIMARY evidence. Alert Logs follow as a")
        log("  cross-check.")

        for mon in monitors:
            mid = mon["monitor_id"]
            label = mon.get("name") or mid
            # Use THIS MONITOR'S actual name, not a static default.
            # "Do Not Delete" finds nothing when the monitor is called
            # "New Monitor".
            search_text = mon.get("name") or args.monitor_text

            argv = [sys.executable, script("stage4_tickets.py"), "search",
                    "--monitor-text", search_text]
            if cycle_since and not args.wide_window:
                argv += ["--since", cycle_since]
                if cycle_until:
                    argv += ["--until", cycle_until]
            else:
                now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                two_h = (datetime.now() - timedelta(hours=2)).strftime(
                    "%Y-%m-%d %H:%M:%S")
                argv += ["--since", two_h, "--until", now_str]
            anchor_file = f"logreport_{mid}.json"
            if os.path.isfile(anchor_file):
                argv += ["--logreport", anchor_file]
            st_itsm = run_stage(
                f"ITSM-FIRST — {label}", argv, args.dry_run)
            stages.append(st_itsm)

            itsm_file = "itsm_first_results.json"
            if os.path.isfile(itsm_file):
                itsm_first_results[mid] = load_json(itsm_file)


    # ---- session cookie liveness, BEFORE any verification ---------------
    # Stage 3 reads Alert Logs with a browser session cookie. A dead cookie
    # returns zero rows, which is indistinguishable from "nothing happened"
    # unless we check first. Reporting an auth failure as an empty result is
    # how a perfectly good run gets written up as untested.
    if not args.report_only and not args.dry_run:
        section("SESSION COOKIE — IS IT STILL ALIVE?")
        probe = subprocess.run(
            [sys.executable, script("stage3_verify.py"), "probe"],
            capture_output=True, text=True)
        out = (probe.stdout or "") + (probe.stderr or "")
        # Use the EXIT CODE, not a substring search. Searching the output
        # for "401"/"403" was a false-positive machine: the probe prints
        # timestamps and field lists, so those digits appear by chance and
        # a perfectly good session gets declared dead.
        dead = probe.returncode != 0
        if not dead and "endpoint works" not in out.lower():
            # exit 0 but no success marker -- trust the exit code, say so
            log("  [note] probe exited 0 without its usual success line; "
                "continuing")
        if dead:
            log("  [BLOCKER] the Alert Logs session cookie is DEAD.")
            log("")
            log("            Every verification would come back empty, and")
            log("            the report would claim your monitors produced no")
            log("            alerts. They probably did — we just cannot read")
            log("            them. NO REPORT WAS WRITTEN.")
            log("")
            log("            Usual causes: you logged out of Site24x7, the")
            log("            cookie aged out (a few hours), or the cookie")
            log("            belongs to a DIFFERENT account.")
            log("")
            log("            Refresh it, then re-run WITHOUT repeating the")
            log("            cycle:")
            log("              python3 extract_cookie.py")
            log("              source .session.env")
            log("              python3 run_all.py --skip-cycle")
            log("")
            log("            The alert cycle you already ran still counts —")
            log("            --skip-cycle reuses it instead of driving the")
            log("            monitors again.")
            log("")
            log("  probe said: " + out.strip()[:300])
            sys.exit(2)
        ok_line = out.strip().splitlines()[-1] if out.strip() else "reachable"
        log(f"  [OK ] Alert Logs readable — {ok_line[:120]}")

    # ---- stage 3 + stage 4, ONCE PER MONITOR ----------------------------
    # stage3 and stage4 each handle a single monitor and write to a fixed
    # filename, so they are run per monitor and their output snapshotted
    # immediately. Nothing about them had to change.
    per_monitor = []
    window_note = None
    for mon in monitors:
        mid = mon["monitor_id"]
        name = mon.get("name") or mid
        label = f"{name} ({mid})"

        s3_before = os.path.getmtime(STAGE3_RESULT) \
            if os.path.isfile(STAGE3_RESULT) else None

        if not args.report_only:
            argv = [sys.executable, script("stage3_verify.py"), "verify",
                    "--monitor-id", str(mid), "--hours", str(args.hours)]
            # Scope verification to EXACTLY when the cycle ran. Anything
            # wider risks crediting an older run's ticket to this one.
            if cycle_since and not args.wide_window:
                argv += ["--since", cycle_since]
                if cycle_until:
                    argv += ["--until", cycle_until]
            st3 = run_stage(f"STAGE 3 — ALERT LOGS — {label}", argv,
                            args.dry_run)
            stages.append(st3)

            # A non-zero exit means stage 3 FAILED, not that the window was
            # empty. Reporting an access failure as "no alerts" is how a
            # good run gets written up as untested.
            # stage3 exit codes: 0 = all good, 1 = ran fine but some
            # integrations FAILED (a real result we must keep), 2+ = it
            # could not read the logs at all. Only 2+ is an access failure.
            rc3 = st3.get("returncode")

            # An access failure here usually means the cookie died while we
            # were running -- most often because the colleague sharing this
            # account logged out. Re-authenticate and try once more before
            # writing the run off.
            # A narrow exact window can be rejected by the applog endpoint
            # (HTTP 400). That is a REQUEST problem, not a session problem,
            # and it must not cost you the whole report. Retry once with the
            # rolling window and say plainly that a wider window was used.
            # stage3 now fetches a WIDE window and filters locally, so a
            # narrow-window 400 can no longer happen. If it still fails, it
            # is a genuine problem -- but keep the fallback so a run is
            # never lost to it.
            if (not args.dry_run and rc3 is not None and rc3 >= 2
                    and cycle_since and not args.wide_window):
                log(f"\n  [!! ] the exact-window request failed (exit {rc3}).")
                log("        Retrying with the rolling window so you still")
                log("        get a report. Results may include rows from")
                log("        just outside this cycle — the report will say so.")
                argv_wide = [a for a in argv]
                for flag in ("--since", "--until"):
                    if flag in argv_wide:
                        i = argv_wide.index(flag)
                        del argv_wide[i:i + 2]
                st3 = run_stage(
                    f"STAGE 3 — ALERT LOGS — {label} (wider window)",
                    argv_wide, args.dry_run)
                stages.append(st3)
                rc3 = st3.get("returncode")
                if rc3 == 0 or rc3 == 1:
                    window_note = ("the exact cycle window was REJECTED by "
                                   "the endpoint, so a rolling window was "
                                   "used for this monitor")
                    log(f"  [OK ] recovered using the rolling window.")

            if (not args.dry_run and rc3 is not None and rc3 >= 2
                    and not args.no_relogin):
                log(f"\n  [!! ] stage 3 could not read Alert Logs "
                    f"(exit {rc3}).")
                log("        Most likely the session was ended by someone "
                    "else.")
                if reload_session():
                    st3 = run_stage(f"STAGE 3 — ALERT LOGS — {label} (retry)",
                                    argv, args.dry_run)
                    stages.append(st3)
                    rc3 = st3.get("returncode")

            if not args.dry_run and rc3 is not None and rc3 >= 2:
                log(f"\n  [BLOCKED] stage 3 could not read Alert Logs for "
                    f"{label} (exit {rc3}).")
                log("            This is an ACCESS failure, not an empty "
                    "result.")
                log("            A 400/401/403 here means the Alert Logs "
                    "session cookie is dead.")
                log("            Refresh it:  node "
                    "~/Documents/qg/s247_login.js")
                log("                         source .session.env")
                per_monitor.append({"monitor_id": mid, "name": name,
                                    "blocked": True,
                                    "reason": f"stage 3 failed with exit "
                                              f"{rc3} — "
                                              f"Alert Logs could not be read "
                                              f"(dead session cookie). This "
                                              f"monitor was NOT tested; it "
                                              f"says nothing about whether "
                                              f"alerts fired"})
                continue

            s3_after = os.path.getmtime(STAGE3_RESULT) \
                if os.path.isfile(STAGE3_RESULT) else None
            if not args.dry_run and s3_after == s3_before:
                log(f"\n  [BLOCKED] stage 3 wrote no new results for {label}.")
                log(f"            No alert log rows matched this monitor in "
                    f"the last {args.hours}h, so it is UNTESTED in this run.")
                per_monitor.append({"monitor_id": mid, "name": name,
                                    "blocked": True,
                                    "reason": f"stage 3 returned no rows for "
                                              f"this monitor in the "
                                              f"{args.hours}h window. Either "
                                              f"the cycle produced no alerts, "
                                              f"or Alert Logs could not be "
                                              f"read (dead session cookie / "
                                              f"wrong account)"})
                continue

        # For a live run, ticket_verification.json was JUST written by stage3
        # for THIS monitor. Read it directly — do NOT read the snapshot first,
        # because the snapshot may be from a previous run (e.g. Sep 17).
        # HOWEVER: ticket_verification.json is overwritten by EACH monitor's
        # stage3 run sequentially, so after all monitors have run it only
        # contains the LAST monitor's data. The snapshot is therefore written
        # immediately after each stage3 run (below) so that:
        #   monitor A runs -> fresh data read from ticket_verification.json
        #                  -> immediately snapshotted to stage3_<A>.json
        #   monitor B runs -> fresh data read from ticket_verification.json
        #                  -> immediately snapshotted to stage3_<B>.json
        # For --report-only, the per-monitor snapshots are the only source
        # (no fresh stage3 ran), but we also validate the snapshot timestamp
        # against the stage2b cycle window so that a stale snapshot triggers
        # a clear warning rather than silently producing old ticket IDs.
        if args.report_only:
            s3 = load_json(f"stage3_{mid}.json") or load_json(STAGE3_RESULT)
            # Warn loudly if the snapshot is from a different cycle
            if s3 and cycle:
                snap_window = s3.get("window_since") or s3.get("at", "")
                cycle_start = (cycle or {}).get("cycle_started_at", "")
                if snap_window and cycle_start:
                    snap_date = snap_window[:10]
                    cycle_date = cycle_start[:10]
                    if snap_date != cycle_date:
                        log(f"\n  [WARN] stage3 snapshot for {mid} is from "
                            f"{snap_date} but the cycle ran on {cycle_date}.")
                        log(f"         The report will show tickets from "
                            f"{snap_date}. Re-run a live cycle to get "
                            f"today's data.")
        else:
            # Live run: read ticket_verification.json first (freshly written
            # by stage3 for this specific monitor), fall back to snapshot only
            # if that file is missing (shouldn't happen in a normal run).
            fresh = load_json(STAGE3_RESULT)
            # Validate that the fresh file is actually for THIS monitor.
            # If stage3 somehow failed silently and left the previous
            # monitor's data in place, the ids would be wrong.
            fresh_mid = str((fresh or {}).get("monitor_id") or "")
            if fresh and fresh_mid == str(mid):
                s3 = fresh
                log(f"  [s3 ] read fresh ticket_verification.json for {mid}")
            else:
                # Fresh file is for a different monitor — fall back to snapshot
                s3 = load_json(f"stage3_{mid}.json")
                if s3:
                    log(f"  [s3 ] ticket_verification.json was for monitor "
                        f"{fresh_mid!r}, not {mid!r}. Using snapshot instead.")
                else:
                    log(f"  [s3 ] no data found for {mid} in either "
                        f"ticket_verification.json or stage3_{mid}.json")
        if s3 is None:
            per_monitor.append({"monitor_id": mid, "name": name,
                                "blocked": True,
                                "reason": f"{STAGE3_RESULT} missing"})
            continue

        s4 = None
        if not args.report_only and not args.skip_tools:
            argv = [sys.executable, script("stage4_tickets.py"), "verify",
                    "--monitor-text", args.monitor_text]
            if cycle_since and not args.wide_window:
                argv += ["--since", cycle_since]
                if cycle_until:
                    argv += ["--until", cycle_until]
            # the anchor: this monitor's own status changes
            anchor_file = f"logreport_{mid}.json"
            if os.path.isfile(anchor_file):
                argv += ["--logreport", anchor_file]
            else:
                log(f"  [note] no {anchor_file} — tickets for {label} "
                    f"cannot be anchored to real status changes")
            stages.append(run_stage(
                f"STAGE 4 — INSIDE EACH ITSM TOOL — {label}",
                argv, args.dry_run))
        # Same principle as s3: for a live run always read the fresh output
        # from stage4; only use the per-monitor snapshot for --report-only.
        if args.report_only:
            s4 = load_json(f"stage4_{mid}.json") or load_json(STAGE4_RESULT)
        else:
            s4 = load_json(STAGE4_RESULT) or load_json(f"stage4_{mid}.json")

        # snapshot so the next monitor's run cannot overwrite this one.
        # IMPORTANT: only snapshot s3 if it actually belongs to THIS monitor.
        # On a live run, ticket_verification.json is overwritten by EACH
        # monitor's stage3 call. If we snapshot it blindly, the 2nd monitor's
        # snapshot gets the 2nd monitor's data, but the 1st monitor's snapshot
        # already captured the correct data for monitor 1. The guard below
        # ensures a stale snapshot from a previous run (different monitor_id
        # or old timestamp) never replaces a fresh one from this run.
        snap3 = f"stage3_{mid}.json"
        snap4 = f"stage4_{mid}.json"
        if not args.dry_run:
            try:
                # Only write the snapshot when the data is genuinely for this
                # monitor (monitor_id matches) OR when there is no existing
                # snapshot yet. This prevents a Sep-17 ticket_verification.json
                # from overwriting a fresh Sep-28 snapshot.
                s3_mid = str((s3 or {}).get("monitor_id") or "")
                snap_exists = os.path.isfile(snap3)
                snap_age = file_age_minutes(snap3) if snap_exists else None
                # Write if: data is for this monitor, OR no snapshot yet, OR
                # the data is fresher than the snapshot (within this run).
                should_write_s3 = (
                    not snap_exists
                    or s3_mid == str(mid)
                    or (snap_age is not None and snap_age > 30)
                )
                if should_write_s3:
                    with open(snap3, "w", encoding="utf-8") as fh:
                        json.dump(s3, fh, indent=2)
                    log(f"  [snap] wrote {snap3}"
                        + (" (monitor_id matched)" if s3_mid == str(mid)
                           else " (no existing snapshot)" if not snap_exists
                           else " (stale snapshot replaced)"))
                else:
                    log(f"  [snap] SKIPPED {snap3} — data monitor_id "
                        f"({s3_mid!r}) != {mid!r} and snapshot is fresh "
                        f"({snap_age:.0f} min old). Keeping existing snapshot.")
                if s4:
                    with open(snap4, "w", encoding="utf-8") as fh:
                        json.dump(s4, fh, indent=2)
            except Exception as exc:  # noqa: BLE001
                log(f"  [WARN] could not snapshot results for {mid}: {exc}")

        per_monitor.append({"monitor_id": mid, "name": name,
                            "blocked": False, "stage3": s3, "stage4": s4})

    if args.dry_run:
        section("DRY RUN COMPLETE")
        log("  Nothing ran, nothing changed, no report written.")
        return

    # ---- freshness guard: never report stale files as this run's result --
    section("INPUT FILES")
    stale = []
    for path in ([STAGE3_RESULT, STAGE4_RESULT]
                 + [f"stage3_{m['monitor_id']}.json" for m in monitors]
                 + [f"stage4_{m['monitor_id']}.json" for m in monitors]):
        if not os.path.isfile(path) and path.startswith("stage"):
            continue
        age = file_age_minutes(path)
        if age is None:
            log(f"  [MISSING] {path}")
            stale.append(path)
        else:
            tag = "fresh" if age < 60 else f"STALE ({age:.0f} min old)"
            log(f"  [{'OK ' if age < 60 else '!! '}] {path:<28} {tag}")
            if age >= 60 and not args.report_only:
                stale.append(path)

    known = load_known_issues()
    if not known:
        log("")
        log(f"  [NOTE] no {KNOWN_ISSUES} found (or it is empty), so NOTHING")
        log("         is suppressed. Already-triaged problems will be")
        log("         reported as fresh defects.")
    else:
        log(f"\n  [OK ] {KNOWN_ISSUES} loaded — {len(known)} triaged issue(s) "
            f"will report as KNOWN, not DEFECT")

    live = load_live_integrations()
    recs = load_integration_records()
    section("THIRD-PARTY INTEGRATIONS CONFIGURED IN THIS ACCOUNT")
    if live is None:
        log(f"  [WARN] no {INTEGRATIONS_FILE} — cannot tell which")
        log("         integrations still exist. Alert Logs are history, so")
        log("         integrations you have DELETED may appear below as")
        log("         though they were live. Capture the list with:")
        log("           python3 itsm.py -a <account> --integrations")
    else:
        itsm_recs = [r for r in (recs or []) if is_itsm(r)]
        other = [r for r in (recs or []) if not is_itsm(r)]
        log(f"  {len(live)} integration(s) live in this account.")
        log("")
        log(f"  ITSM — ticket created AND read back inside the tool "
            f"({len(itsm_recs)}):")
        for r in sorted(itsm_recs, key=lambda x: str(x.get("name"))):
            log(f"      {str(r.get('name')):<32} "
                f"{app_label(r):<26} "
                f"{r.get('status') or ''}")
        log("")
        log(f"  OTHER — delivery checked in the Alert Logs only, no ticket "
            f"to read ({len(other)}):")
        for r in sorted(other, key=lambda x: str(x.get("name"))):
            log(f"      {str(r.get('name')):<32} "
                f"{app_label(r):<26} "
                f"{r.get('status') or ''}")
        inactive = [r for r in (recs or [])
                    if str(r.get("status") or "").lower().startswith("inact")]
        if inactive:
            log("")
            log(f"  [note] {len(inactive)} integration(s) are INACTIVE and "
                f"will receive")
            log("         nothing, so they produce no alert log rows. That is")
            log("         configuration, not a defect:")
            for r in inactive:
                log(f"      {r.get('name')}")

    findings = []
    blocked_monitors = []
    excluded = set()
    for pm in per_monitor:
        label = pm.get("name") or pm["monitor_id"]
        if pm.get("blocked"):
            blocked_monitors.append(pm)
            findings.append({
                "monitor": label, "monitor_id": pm["monitor_id"],
                "integration": "(all integrations)", "bucket": BLOCKED,
                "headline": f"Monitor not verified — {pm.get('reason')}",
                "detail": f"No verification ran for {label}. This monitor is "
                          f"UNTESTED in this run; it is not a pass and not a "
                          f"failure.",
                "site24x7_verdict": None, "tool_verdict": None, "tool": None,
                "created_ticket_ids": [], "closed_ticket_ids": [],
                "updated_ticket_ids": [], "matched_ticket_ids": [],
                "matched_on_up_ticket_ids": [], "alert_log_rows": 0,
                "failed_rows": 0, "tool_tickets": []})
            continue
        for f in build_findings(pm.get("stage3"), pm.get("stage4"), known,
                                  live_recs=recs):
            # DELETED integrations: they appear in alert log history but
            # no longer exist in integrations.json. Do NOT silently drop
            # them — show them with a clear DELETED headline so the tester
            # knows exactly why it's there.
            if live is not None and f["integration"] not in live:
                st = integration_status(f["integration"], recs)
                if st == "deleted":
                    # Reclassify this finding as DELETED
                    f["headline"] = "⚠️  Integration was DELETED from this account"
                    f["detail"] = (
                        f"'{f['integration']}' no longer exists in the "
                        f"Site24x7 account. The data below is from the alert "
                        f"log rows captured BEFORE it was deleted — historical "
                        f"only, not a current result. To test this integration "
                        f"again, re-add it in Site24x7 and re-run the suite."
                    )
                    f["bucket"] = INCONCLUSIVE
                else:
                    # Truly unknown — skip (old behaviour)
                    excluded.add(f["integration"])
                    continue
            f["monitor"] = label
            f["monitor_id"] = pm["monitor_id"]
            findings.append(f)

    # MANDATORY: if server coverage was skipped, it must appear in the
    # report. Silence would let a website-only run read as full coverage.
    sc = (cycle or {}).get("server_coverage") or {}
    if sc.get("attempted") and sc.get("skipped"):
        detail = sc.get("reason") or ("Server monitors were unreachable.")
        seen = sc.get("monitors") or {}
        states = "; ".join(f"{v.get('name')}={v.get('status')}"
                           for v in seen.values())
        findings.append({
            "monitor": "server monitors", "monitor_id": "",
            "integration": "(server coverage)", "bucket": BLOCKED,
            "headline": "ALL SERVER MONITORS UNREACHABLE — coverage skipped",
            "detail": (f"{detail} They were given "
                       f"{sc.get('waited_seconds')}s to recover and at least "
                       f"{sc.get('min_required')} had to be UP or TROUBLE. "
                       f"Status at the end of that window: {states}. "
                       f"Only the website monitors were tested in this run."),
            "site24x7_verdict": "not run", "tool_verdict": "not run",
            "tool": None, "created_ticket_ids": [], "closed_ticket_ids": [],
            "updated_ticket_ids": [], "matched_ticket_ids": [],
            "matched_on_up_ticket_ids": [], "alert_log_rows": 0,
            "failed_rows": 0, "tool_tickets": []})

    if not findings:
        section("CANNOT BUILD REPORT — NO RESULTS AT ALL")
        log("  [BLOCKER] nothing was verified for any monitor, so no report")
        log("            was written. Check the stage 2b and stage 3 output")
        log(f"            above. Widen the window with --hours if the cycle")
        log("            ran a while ago.")
        sys.exit(2)

    if excluded:
        log("")
        log(f"  [EXCLUDED] {len(excluded)} name(s) appear in the Alert Logs "
            f"but are")
        log("             NOT configured in this account any more — almost")
        log("             certainly deleted. They are left out of the report")
        log("             rather than reported as live:")
        for n in sorted(excluded):
            log(f"      {n}")

    findings.sort(key=lambda f: ({DEFECT: 0, BLOCKED: 1, INCONCLUSIVE: 2,
                                  KNOWN: 3, PASS: 4}.get(f["bucket"], 9),
                                 str(f.get("monitor"))))

    counts = {}
    for x in findings:
        counts[x["bucket"]] = counts.get(x["bucket"], 0) + 1

    if counts.get(DEFECT):
        health, code = "DEFECT", 1
        reason = (f"{counts[DEFECT]} application defect(s) found. The build "
                  f"has a real problem that a developer must look at.")
    elif counts.get(BLOCKED) and not counts.get(PASS):
        health, code = "BLOCKED", 2
        reason = ("Nothing could be verified. Every result was blocked by a "
                  "test harness access problem or a monitor that produced no "
                  "alert rows. This says nothing about the build.")
    elif counts.get(BLOCKED):
        health, code = "PARTIAL", 0
        reason = (f"{counts.get(PASS,0)} check(s) passed. "
                  f"{counts[BLOCKED]} could not be tested and remain "
                  f"UNVERIFIED — not failures.")
    else:
        health, code = "PASS", 0
        reason = (f"All {counts.get(PASS,0)} verified check(s) created and "
                  f"closed tickets correctly.")

    finished_dt = datetime.now()
    ctx = {
        "started": started,
        "finished": finished_dt.strftime("%Y-%m-%d %H:%M:%S"),
        "duration": (lambda t: f"{int(t)//60}m {int(t)%60}s")(
            (finished_dt - started_dt).total_seconds()),
        "env": {"grid": grid,
                "monitor_id": ", ".join(m["monitor_id"] for m in monitors),
                "monitor_name": ", ".join(
                    f"{m.get('name') or m['monitor_id']}"
                    + (" [" + " -> ".join(["UP"]
                       + [r.upper() for r in (m.get('route') or [])]
                       + ["UP"]) + "]" if m.get("route") else "")
                    for m in monitors),
                "monitors": monitors,
                "live_integrations": sorted(live) if live else None,
                "excluded_integrations": sorted(excluded),
                "integration_records": recs or [],
                "hours": args.hours,
                "host": os.uname().nodename if hasattr(os, "uname") else ""},
        "cycle": cycle,
        "cycle_this_run": not args.skip_cycle and not args.report_only,
        "findings": findings,
        "counts": counts,
        "health": health,
        "health_reason": reason,
        "stages": stages,
        "stale_inputs": stale,
        "itsm_first": itsm_first_results,
        "window_note": window_note,
        "live_integrations": sorted(live) if live else None,
        "excluded_integrations": sorted(excluded),
    }

    os.makedirs(args.out_dir, exist_ok=True)
    html_path = os.path.join(args.out_dir, f"report_{stamp}.html")
    json_path = os.path.join(args.out_dir, f"report_{stamp}.json")
    xml_path = os.path.join(args.out_dir, f"junit_{stamp}.xml")

    write_html(html_path, ctx)
    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump(ctx, fh, indent=2, default=str)
    write_junit(xml_path, ctx)

    # ---- console summary ------------------------------------------------
    section("RESULTS")
    for x in findings:
        log(f"  {x['bucket']:<13} {str(x.get('monitor'))[:22]:<24}"
            f"{x['integration']:<26} {x['headline']}")

    if counts.get(BLOCKED):
        section("NOT A PRODUCT BUG — READ THIS")
        for x in findings:
            if x["bucket"] == BLOCKED:
                log(f"  {x['integration']}: {x['headline']}")
        log("\n  These integrations were NOT tested. Do not raise bugs for")
        log("  them. Fix the access problem and re-run.")

    section(f"BUILD HEALTH: {health}")
    log(f"  {reason}")
    # ABSOLUTE paths printed as file:// URIs so the terminal makes them
    # Ctrl+Click-able links. Works in VS Code, Zoho Code IDE, GNOME Terminal,
    # iTerm2, and most modern terminal emulators.
    html_abs  = os.path.abspath(html_path)
    json_abs  = os.path.abspath(json_path)
    xml_abs   = os.path.abspath(xml_path)
    html_uri  = "file://" + html_abs
    log(f"\n  ┌─ REPORT FILES ──────────────────────────────────────────┐")
    log(f"  │  HTML  (Ctrl+Click to open):                            │")
    log(f"  │  {html_uri}")
    log(f"  │                                                          │")
    log(f"  │  JSON  : {json_abs}")
    log(f"  │  JUnit : {xml_abs}")
    log(f"  └──────────────────────────────────────────────────────────┘")
    log(f"\n  Or open from terminal:")
    log(f"    xdg-open '{html_abs}'")
    # Also auto-open the report so you never have to click at all
    try:
        import subprocess as _sp
        _sp.Popen(["xdg-open", html_abs],
                  stdout=_sp.DEVNULL, stderr=_sp.DEVNULL)
        log(f"\n  ✅  Report auto-opened in your browser.")
    except Exception:
        log(f"\n  (Could not auto-open — use Ctrl+Click on the link above.)")
    log(f"\n  exit code {code}  (0 = build OK, 1 = defect, 2 = blocked)")
    sys.exit(code)


if __name__ == "__main__":
    main()
