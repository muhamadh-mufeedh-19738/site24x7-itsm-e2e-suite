#!/usr/bin/env python3
"""
Site24x7 ITSM Automation — STAGE 0.5 : API DISCOVERY PROBE
===========================================================

WHY THIS EXISTS
    The first probe reported BROWSER_LOGIN_REQUIRED, but that was only because
    it had no token to present. Your get_token.sh proves the grid has a working
    OAuth API. This probe uses that token to find out exactly which API
    endpoints work, so the framework can be API-first (fast + stable) instead
    of browser-led (slow + brittle).

WHAT IT DOES
    1. Obtains an access token (three ways, tried in order):
         a) $S247_ACCESS_TOKEN if you already exported one
         b) runs your get_token.sh if you point at it
         c) tells you clearly what to do if neither is available
    2. Probes a list of CANDIDATE read-only API endpoints.
       Nothing is assumed - it reports which ones actually answer.
    3. Writes api_discovery_report.json

USAGE (simplest first)

    # Option A - let this script call your get_token.sh for you:
    export S247_GRID_URL="https://integrations-qa.localsite24x7.com"
    export S247_TOKEN_SCRIPT="$HOME/Documents/qg/get_token.sh"
    python3 api_probe.py

    # Option B - get the token yourself first, then run:
    export S247_GRID_URL="https://integrations-qa.localsite24x7.com"
    export S247_ACCESS_TOKEN="$(~/Documents/qg/get_token.sh)"
    python3 api_probe.py

SAFETY
    - READ-ONLY. Sends GET requests only. Never creates, edits or deletes
      monitors, integrations or tickets.
    - Your token is NEVER printed, logged, or written to the report file.
      Only its presence and length are recorded.
"""

import json
import os
import ssl
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

REPORT_PATH = "api_discovery_report.json"
TIMEOUT = 20

report = {
    "probe_version": "0.5",
    "generated_at": datetime.now(timezone.utc).isoformat(),
    "grid_url": None,
    "token": {"obtained": False, "source": None, "length": None},
    "endpoints": {},
    "verdict": None,
    "next_steps": [],
    "blockers": [],
    "warnings": [],
}


def log(msg):
    print(msg, flush=True)


def section(title):
    log("\n" + "=" * 70)
    log(title)
    log("=" * 70)


def blocker(msg):
    report["blockers"].append(msg)
    log(f"  [BLOCKER] {msg}")


def warn(msg):
    report["warnings"].append(msg)
    log(f"  [WARN]    {msg}")


# ---------------------------------------------------------------------------
# 1. get the access token
# ---------------------------------------------------------------------------

def get_token():
    section("1. OBTAINING ACCESS TOKEN")

    # (a) already exported?
    token = os.environ.get("S247_ACCESS_TOKEN", "").strip()
    if token:
        log("  [OK ] Using token from $S247_ACCESS_TOKEN")
        report["token"] = {"obtained": True, "source": "env:S247_ACCESS_TOKEN",
                           "length": len(token)}
        return token

    # (b) run the user's get_token.sh
    script = os.environ.get("S247_TOKEN_SCRIPT", "").strip()
    if script:
        script = os.path.expanduser(script)
        if not os.path.isfile(script):
            blocker(f"S247_TOKEN_SCRIPT points to '{script}' but no such file exists.")
            return None
        if not os.access(script, os.X_OK):
            warn(f"'{script}' is not executable. Fix with: chmod +x {script}")
        log(f"  ...  Running {script}")
        try:
            proc = subprocess.run(["bash", script], capture_output=True,
                                  text=True, timeout=60)
            out = (proc.stdout or "").strip()
            err = (proc.stderr or "").strip()
            if proc.returncode != 0 or not out:
                blocker("get_token.sh did not return a token.")
                # stderr may contain a Zoho error message - useful, and not secret
                if err:
                    log(f"           script said: {err[:400]}")
                return None
            # token is the last non-empty line
            token = [l for l in out.splitlines() if l.strip()][-1].strip()
            log(f"  [OK ] Token obtained from get_token.sh (length {len(token)})")
            report["token"] = {"obtained": True, "source": "get_token.sh",
                               "length": len(token)}
            return token
        except subprocess.TimeoutExpired:
            blocker("get_token.sh timed out after 60s.")
            return None
        except Exception as exc:  # noqa: BLE001
            blocker(f"Could not run get_token.sh: {exc}")
            return None

    blocker("No token available. Set S247_ACCESS_TOKEN or S247_TOKEN_SCRIPT.")
    report["next_steps"].append(
        'export S247_TOKEN_SCRIPT="$HOME/Documents/qg/get_token.sh"'
    )
    return None


# ---------------------------------------------------------------------------
# 2. probe endpoints
# ---------------------------------------------------------------------------

def http_get(url, token):
    """Read-only GET with the OAuth header. Never raises."""
    result = {"status": None, "ok": False, "error": None,
              "content_type": None, "looks_like_json": False,
              "top_level_keys": None, "item_count": None,
              "body_snippet": None}

    headers = {
        # NOTE: 'Zoho-oauthtoken' is the correct modern scheme,
        # as used by your own get_token.sh --header output.
        "Authorization": f"Zoho-oauthtoken {token}",
        "Accept": "application/json; version=2.1",
    }
    req = urllib.request.Request(url, headers=headers, method="GET")

    ctx = None
    if url.lower().startswith("https"):
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE  # local grids often use self-signed certs

    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT, context=ctx) as resp:
            raw = resp.read(20000).decode("utf-8", errors="replace")
            result["status"] = resp.status
            result["ok"] = 200 <= resp.status < 300
            result["content_type"] = resp.headers.get("Content-Type")
            try:
                parsed = json.loads(raw)
                result["looks_like_json"] = True
                if isinstance(parsed, dict):
                    result["top_level_keys"] = list(parsed.keys())[:12]
                    data = parsed.get("data")
                    if isinstance(data, list):
                        result["item_count"] = len(data)
                elif isinstance(parsed, list):
                    result["item_count"] = len(parsed)
            except Exception:
                result["body_snippet"] = raw[:300]
    except urllib.error.HTTPError as exc:
        result["status"] = exc.code
        result["error"] = f"HTTP {exc.code} {exc.reason}"
        try:
            result["body_snippet"] = exc.read(500).decode("utf-8", errors="replace")[:300]
        except Exception:
            pass
    except urllib.error.URLError as exc:
        result["error"] = f"URLError: {exc.reason}"
    except Exception as exc:  # noqa: BLE001
        result["error"] = f"error: {exc}"
    return result


def probe_endpoints(grid, token):
    section("2. PROBING CANDIDATE API ENDPOINTS (read-only)")
    log("  These are CANDIDATES. Nothing is assumed - we report what answers.\n")

    # /api/current_status is confirmed by your own get_token.sh usage example.
    # The rest are standard Site24x7 API candidates; unknown ones simply 404
    # and are reported as unavailable. No infrastructure is invented.
    candidates = {
        "current_status":       "/api/current_status",
        "monitors":             "/api/monitors",
        "monitor_groups":       "/api/monitor_groups",
        "third_party_services": "/api/integration/third_party_services",
        "notification_profiles": "/api/notification_profiles",
        "threshold_profiles":   "/api/threshold_profiles",
        "tags":                 "/api/tags",
        "user_alert_groups":    "/api/user_groups",
    }

    working, failing = [], []
    for name, path in candidates.items():
        url = grid.rstrip("/") + path
        res = http_get(url, token)
        report["endpoints"][name] = {"path": path, **res}

        if res["ok"]:
            working.append(name)
            extra = ""
            if res["item_count"] is not None:
                extra = f"  ({res['item_count']} items)"
            log(f"  [OK ] {name:22} {path}{extra}")
        else:
            failing.append(name)
            log(f"  [-- ] {name:22} {path}   status={res['status']} {res['error'] or ''}")

    report["working_endpoints"] = working
    report["failing_endpoints"] = failing
    return working


# ---------------------------------------------------------------------------
# 3. verdict
# ---------------------------------------------------------------------------

def summarise(working):
    section("3. VERDICT & WHAT IT MEANS")

    if not working:
        report["verdict"] = "API_NOT_USABLE"
        log("  ==> API_NOT_USABLE")
        log("      The token did not unlock any endpoint.")
        log("      Most likely causes:")
        log("        - the refresh_token belongs to a DIFFERENT grid")
        log("          (your get_token.sh mentions automation.localsite24x7.com,")
        log("           but you are probing integrations-qa.localsite24x7.com)")
        log("        - the token expired (they last ~1 hour - re-run get_token.sh)")
        log("        - the OAuth scope does not cover these endpoints")
        report["next_steps"] += [
            "Confirm which grid the refresh_token was issued for.",
            "If it is a different grid, generate a refresh_token for this one.",
            "Fallback: browser-led Playwright login (still fully workable).",
        ]
        return

    report["verdict"] = "API_FIRST_VIABLE"
    log(f"  ==> API_FIRST_VIABLE  ({len(working)} endpoints working)")
    log("      This is the BEST outcome. It means the framework can read")
    log("      monitors, integrations and status over the API - fast and stable -")
    log("      and use the browser ONLY where UI behaviour is itself under test")
    log("      (Save / Save and Test / Trigger buttons).")

    if "monitors" in working:
        log("\n      'monitors' works -> we can list monitors and their IDs")
        log("      automatically instead of hard-coding names.")
    if "third_party_services" in working:
        log("      'third_party_services' works -> we can read the 5 integrations")
        log("      and their configuration directly.")
    if "current_status" in working:
        log("      'current_status' works -> we can poll monitor state properly")
        log("      instead of using fixed sleeps.")

    report["next_steps"] += [
        "Proceed to Stage 1: build the framework skeleton (API-first).",
        "Install the Python Playwright binding for the UI-under-test parts.",
    ]


def main():
    log("Site24x7 ITSM Automation - Stage 0.5 API Discovery Probe")
    log("READ-ONLY. Your token is never printed or saved.")

    grid = os.environ.get("S247_GRID_URL", "").strip()
    if not grid:
        blocker('S247_GRID_URL is not set. Example:\n'
                '  export S247_GRID_URL="https://integrations-qa.localsite24x7.com"')
        finish()
        return
    report["grid_url"] = grid
    log(f"\n  Grid: {grid}")

    token = get_token()
    if not token:
        finish()
        return

    working = probe_endpoints(grid, token)
    summarise(working)
    finish()


def finish():
    section("SUMMARY")
    log(f"  BLOCKERS: {len(report['blockers'])}")
    for b in report["blockers"]:
        log(f"    ! {b}")
    log(f"  WARNINGS: {len(report['warnings'])}")
    for w in report["warnings"]:
        log(f"    ~ {w}")
    if report["next_steps"]:
        log("\n  NEXT STEPS:")
        for s in report["next_steps"]:
            log(f"    -> {s}")

    try:
        with open(REPORT_PATH, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2)
        log(f"\n  Wrote {REPORT_PATH} - safe to share (contains NO token).")
    except Exception as exc:  # noqa: BLE001
        log(f"\n  [BLOCKER] Could not write {REPORT_PATH}: {exc}")

    sys.exit(2 if report["blockers"] else 0)


if __name__ == "__main__":
    main()
