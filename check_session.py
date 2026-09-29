#!/usr/bin/env python3
"""
check_session.py — "IS MY AUTH STILL GOOD?" DOCTOR
===================================================

WHY THIS EXISTS
    There are THREE separate credentials in this project, and they fail in
    different ways at different times. When something stops working it is
    rarely obvious which one died. This script checks all of them and tells
    you exactly what to do.

        1. OAuth token      - from get_token.sh. Lives ~1 hour, but it is
                              re-minted automatically every run, so it
                              rarely causes trouble.
        2. Session cookie   - from your browser. EXPIRES (hours). This is
                              the one that usually breaks.
        3. CSRF token       - travels with the cookie. Dies with it.

    RUN THIS FIRST whenever anything returns 401 / 403 / 404 unexpectedly.

USAGE
    cd ~/itsm-automation
    source env.sh
    source .session.env      # only if the file exists
    python3 check_session.py

WHAT IT DOES
    Checks each credential, tries a real request with it, and prints a
    plain-English verdict plus the exact command to fix anything broken.

SAFETY
    Read-only. Never prints a secret - only whether it is present and
    whether it works.
"""

import json
import os
import ssl
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta

TIMEOUT = 20
OK, BAD, WARN = "[ OK ]", "[FAIL]", "[WARN]"


def log(m=""):
    print(m, flush=True)


def section(t):
    log("\n" + "=" * 68)
    log(t)
    log("=" * 68)


def _ctx():
    c = ssl.create_default_context()
    c.check_hostname = False
    c.verify_mode = ssl.CERT_NONE
    return c


def get(url, headers):
    req = urllib.request.Request(url, method="GET", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT, context=_ctx()) as r:
            return r.status, r.read(3000).decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read(600).decode("utf-8", errors="replace")
        except Exception:
            pass
        return e.code, body
    except Exception as exc:  # noqa: BLE001
        return None, str(exc)


problems = []


def fix(msg):
    problems.append(msg)


def main():
    log("check_session.py — checking every credential this project uses")
    log("Read-only. No secret values are ever printed.")

    # ---------------- 1. environment ----------------
    section("1. ENVIRONMENT VARIABLES")
    grid = os.environ.get("S247_GRID_URL", "").strip()
    tok_script = os.environ.get("S247_TOKEN_SCRIPT", "").strip()
    cookie = os.environ.get("S247_SESSION_COOKIE", "").strip()
    csrf = os.environ.get("S247_CSRF_TOKEN", "").strip()

    log(f"  {OK if grid else BAD} S247_GRID_URL        "
        f"{grid or 'NOT SET'}")
    if not grid:
        fix("Run:  source env.sh")

    log(f"  {OK if tok_script else WARN} S247_TOKEN_SCRIPT    "
        f"{'set' if tok_script else 'NOT SET'}")
    if not tok_script:
        fix("Run:  source env.sh")

    log(f"  {OK if cookie else BAD} S247_SESSION_COOKIE  "
        f"{f'set ({len(cookie)} chars)' if cookie else 'NOT SET'}")
    if not cookie:
        fix("Run:  source .session.env      "
            "(or regenerate it - see step 4 below)")

    log(f"  {OK if csrf else WARN} S247_CSRF_TOKEN      "
        f"{f'set ({len(csrf)} chars)' if csrf else 'NOT SET'}")

    if not grid:
        summary()
        return

    # ---------------- 2. oauth token ----------------
    section("2. OAUTH TOKEN (get_token.sh)")
    token = None
    if tok_script and os.path.isfile(os.path.expanduser(tok_script)):
        try:
            p = subprocess.run(["bash", os.path.expanduser(tok_script)],
                               capture_output=True, text=True, timeout=60)
            lines = [l for l in (p.stdout or "").splitlines() if l.strip()]
            if p.returncode == 0 and lines:
                token = lines[-1].strip()
                log(f"  {OK} fresh token obtained ({len(token)} chars)")
            else:
                log(f"  {BAD} get_token.sh failed")
                log(f"        {(p.stderr or '')[:200]}")
                fix("Check ~/Documents/qg/.token.env has client_id, "
                    "client_secret and refresh_token")
        except Exception as exc:  # noqa: BLE001
            log(f"  {BAD} could not run get_token.sh: {exc}")
            fix("Check the path in env.sh points at get_token.sh")
    else:
        log(f"  {BAD} token script not found")
        fix("Run:  source env.sh")

    if token:
        code, _ = get(f"{grid}/api/monitors",
                      {"Authorization": f"Zoho-oauthtoken {token}",
                       "Accept": "application/json; version=2.1"})
        if code == 200:
            log(f"  {OK} token WORKS against /api/monitors")
        else:
            log(f"  {BAD} token rejected (status={code})")
            fix("The refresh_token may be for a different account/grid.")

    # ---------------- 3. session cookie ----------------
    section("3. SESSION COOKIE (the one that expires)")
    if not cookie:
        log(f"  {BAD} no cookie loaded - cannot test")
    else:
        now = datetime.now()
        start = now - timedelta(hours=1)
        f = lambda d: d.strftime("%d-%m-%Y %H:%M:%S").replace(" ", "%20")
        url = (f"{grid}/app/api/applog/search/{f(start)}/{f(now)}/1-5/desc"
               f"?time_filter=&query=logtype=%22Alert%20Logs%22"
               f"&page_type=full_page")
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "Cookie": cookie,
            "Referer": f"{grid}/app/client?a=f",
            "Origin": grid,
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36",
        }
        use_csrf = csrf
        if not use_csrf:
            for part in cookie.split(";"):
                if part.strip().lower().startswith("s247cname="):
                    use_csrf = "s247pname=" + part.split("=", 1)[1].strip()
                    break
        if use_csrf:
            headers["x-zcsrf-token"] = use_csrf

        code, body = get(url, headers)
        if code == 200:
            log(f"  {OK} session cookie WORKS - Alert Logs are reachable")
        elif code in (401, 403):
            log(f"  {BAD} session REJECTED (status={code})")
            log("        Your browser session has expired or you logged out.")
            fix("Regenerate the session - see step 4 below.")
        elif code == 404:
            log(f"  {WARN} status 404 - auth is fine, but the URL was rejected")
            log("        (usually an encoding problem, not a credential one)")
        else:
            log(f"  {WARN} unexpected status={code}")
            if body:
                log(f"        server said: {body[:200]}")

    # ---------------- 4. how to regenerate ----------------
    section("4. HOW TO REGENERATE THE SESSION (when it expires)")
    log("  This takes about 2 minutes:")
    log("")
    log("   1. Chrome -> your Site24x7 grid -> any monitor -> Alert Logs tab")
    log("   2. Press F12 -> Network tab -> tick 'Fetch/XHR' -> press Ctrl+R")
    log("   3. Click the request named  desc?time_filter=...")
    log("   4. Right-click it -> Copy -> 'Copy as cURL'")
    log("   5. In this terminal:")
    log("        python3 extract_cookie.py")
    log("        <paste>  then Enter, then Ctrl+D")
    log("   6. source .session.env")
    log("   7. python3 check_session.py     (confirm it says OK)")

    summary()


def summary():
    section("SUMMARY")
    if not problems:
        log(f"  {OK} Everything checks out. You are good to run the scripts.")
        return
    log(f"  {len(problems)} thing(s) need attention:\n")
    for i, p in enumerate(problems, 1):
        log(f"   {i}. {p}")
    log("\n  Fix those, then run this script again.")
    sys.exit(1)


if __name__ == "__main__":
    main()
