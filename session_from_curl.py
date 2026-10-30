#!/usr/bin/env python3
"""
session_from_curl.py — turn a browser "Copy as cURL" into a working session
============================================================================

WHY THIS EXISTS
    Stage 3 (Alert Logs) needs a browser SESSION COOKIE. Normally
    s247_login.js gets it for you. When that fails, this is the manual
    fallback: copy one request out of Chrome, paste it here, done.

    It writes the cookie to the SAME file itsm.py reads, so the next run
    picks it up with no extra steps. Then it tests the cookie for real
    against Alert Logs and tells you in plain English whether it works --
    and, if it does not, whether the cause is the COOKIE or the NETWORK.

HOW TO GET THE cURL (about 1 minute)
    1. Chrome -> your grid -> log in -> Operations -> Alert Logs
    2. F12 -> Network tab -> tick "Fetch/XHR" -> press Ctrl+R
    3. Click any request whose name starts with  desc?time_filter=
    4. Right-click it -> Copy -> "Copy as cURL (bash)"

USAGE
    python3 session_from_curl.py --account automation --grid integrations-qa
        <paste the cURL>   then press Enter, then Ctrl+D

    # or from a file you saved the cURL into:
    python3 session_from_curl.py --account automation --grid integrations-qa \
        --file /tmp/curl.txt

    # only test the cookie that is already saved, do not write anything:
    python3 session_from_curl.py --account automation --grid integrations-qa \
        --check-only

    If the grid demands a client certificate, point at it (PEM files):
        export S247_CLIENT_CERT=~/certs/me.crt
        export S247_CLIENT_KEY=~/certs/me.key

AFTER IT SAYS OK
    python3 itsm.py --account automation --grid integrations-qa --no-login
    (--no-login stops itsm.py from overwriting the cookie you just pasted)

SAFETY
    Never prints a cookie value. The file is written with permissions 600 and
    its name matches .gitignore, so it can never be pushed to GitHub.
"""

import argparse
import json
import os
import re
import shlex
import ssl
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from urllib.parse import urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
TIMEOUT = 20


def log(m=""):
    print(m, flush=True)


def die(msg, code=2):
    log(f"\n[BLOCKER] {msg}")
    sys.exit(code)


# --------------------------------------------------------------------------
# 1. parse the pasted cURL
# --------------------------------------------------------------------------
def parse_curl(text):
    """Return (url, cookie, csrf) from a Chrome/Firefox 'Copy as cURL'.

    Chrome now puts cookies in  -b '...'  ; older Chrome and Firefox use
    -H 'Cookie: ...'. Both are handled."""
    # Join bash line continuations, then tokenise like a shell would.
    flat = re.sub(r"\\\r?\n", " ", text.strip())
    try:
        parts = shlex.split(flat)
    except ValueError as exc:
        die(f"could not read the pasted text as a cURL command ({exc}).\n"
            "          Use Copy -> 'Copy as cURL (bash)', not '(cmd)'.")
    if not parts or parts[0] != "curl":
        die("the pasted text does not start with 'curl'.")

    url = cookie = csrf = None
    i = 1
    while i < len(parts):
        p = parts[i]
        nxt = parts[i + 1] if i + 1 < len(parts) else ""
        if p in ("-H", "--header"):
            name, _, value = nxt.partition(":")
            key = name.strip().lower()
            if key == "cookie":
                cookie = value.strip()
            elif key == "x-zcsrf-token":
                csrf = value.strip()
            i += 2
            continue
        if p in ("-b", "--cookie"):
            cookie = nxt.strip()
            i += 2
            continue
        if p.startswith("http://") or p.startswith("https://"):
            url = p
        elif p in ("--url",):
            url = nxt
            i += 2
            continue
        i += 1

    if not cookie:
        die("no cookie found in the pasted cURL.\n"
            "          Pick a request to your grid (desc?time_filter=...),\n"
            "          not one to a CDN or image.")
    if not csrf:
        # The CSRF token is just the s247cname cookie value, prefixed.
        m = re.search(r"(?:^|;\s*)s247cname=([^;]+)", cookie)
        if m:
            csrf = "s247pname=" + m.group(1).strip()
    return url, cookie, csrf


# --------------------------------------------------------------------------
# 2. where the session goes (identical to itsm.py's build_env)
# --------------------------------------------------------------------------
def load_grids():
    path = os.path.join(HERE, "grids.json")
    try:
        return (json.load(open(path, encoding="utf-8")).get("grids") or {})
    except (OSError, ValueError):
        return {}


def session_path(account, grid_name):
    if account:
        d = os.path.join(HERE, "accounts", account)
        if not os.path.isdir(d):
            die(f"accounts/{account} does not exist.")
        return os.path.join(d, f".session.{grid_name}.env")
    return os.path.join(HERE, ".session.env")


def read_session(path):
    env = {}
    if not os.path.isfile(path):
        return env
    for raw in open(path, encoding="utf-8"):
        line = raw.strip()
        if line.startswith("export "):
            line = line[7:]
        if "=" not in line or line.startswith("#"):
            continue
        k, v = line.split("=", 1)
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
            v = v[1:-1]
        env[k.strip()] = v
    return env


def write_session(path, cookie, csrf):
    if "'" in cookie or "'" in (csrf or ""):
        die("the cookie contains a single quote, which cannot be stored "
            "safely. Copy the request again.")
    body = ("# Session credentials for /app/api/ endpoints.\n"
            "# WRITTEN BY session_from_curl.py — do not hand-edit.\n"
            f"# Generated: {datetime.now().isoformat(timespec='seconds')}\n"
            "# NEVER commit this file.\n"
            f"export S247_SESSION_COOKIE='{cookie}'\n"
            f"export S247_CSRF_TOKEN='{csrf or ''}'\n")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(body)
    os.chmod(path, 0o600)


# --------------------------------------------------------------------------
# 3. test it for real, and explain failures properly
# --------------------------------------------------------------------------
def ssl_context():
    c = ssl.create_default_context()
    c.check_hostname = False
    c.verify_mode = ssl.CERT_NONE
    cert = os.path.expanduser(os.environ.get("S247_CLIENT_CERT", "").strip())
    key = os.path.expanduser(os.environ.get("S247_CLIENT_KEY", "").strip())
    if cert:
        if not os.path.isfile(cert):
            die(f"S247_CLIENT_CERT points at a missing file: {cert}")
        c.load_cert_chain(cert, key or None)
    return c


def explain_network_error(err):
    """Turn a raw SSL/socket error into the actual cause."""
    s = str(err)
    if "CERTIFICATE_REQUIRED" in s or "BAD_SSL_CLIENT_AUTH" in s:
        return ("The SERVER asked for a CLIENT CERTIFICATE and none was sent.\n"
                "      This is a NETWORK / certificate problem, NOT your cookie.\n"
                "      A fresh cookie will NOT fix it. Check, in order:\n"
                "        1. Are you on the office network / VPN?\n"
                "        2. Does Chrome on this machine open the grid without\n"
                "           asking you to pick a certificate?\n"
                "        3. If you have the cert as PEM files, export\n"
                "           S247_CLIENT_CERT / S247_CLIENT_KEY and re-run.")
    if "alternative certificate subject name" in s or "hostname" in s.lower():
        return ("The server's certificate does not match its name. Usually a\n"
                "      proxy/VPN is intercepting traffic or DNS points at the\n"
                "      wrong box. NOT a cookie problem.")
    if "timed out" in s.lower():
        return "Timed out reaching the grid. VPN down or grid unreachable."
    if "Name or service not known" in s or "nodename nor servname" in s:
        return "DNS cannot resolve the grid. VPN / hosts-file problem."
    if "Connection refused" in s:
        return "Connection refused. The grid (or local tunnel) is not running."
    return "Network-level failure (not a cookie verdict)."


def test_session(grid, cookie, csrf):
    now = datetime.now() - timedelta(minutes=1)
    start = now - timedelta(hours=1)
    f = lambda d: d.strftime("%d-%m-%Y %H:%M:%S").replace(" ", "%20")
    url = (f"{grid}/app/api/applog/search/{f(start)}/{f(now)}/1-5/desc"
           "?time_filter=&query=logtype=%22Alert%20Logs%22"
           "&page_type=full_page")
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Cookie": cookie,
        "Referer": f"{grid}/app/client",
        "Origin": grid,
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36",
    }
    if csrf:
        headers["x-zcsrf-token"] = csrf
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT,
                                    context=ssl_context()) as r:
            return r.status, r.read(400).decode("utf-8", "replace"), None
    except urllib.error.HTTPError as e:
        return e.code, e.read(400).decode("utf-8", "replace"), None
    except Exception as exc:  # noqa: BLE001
        return None, "", exc


def report(grid, code, body, err):
    log("\n" + "=" * 66)
    log("VERDICT")
    log("=" * 66)
    if err is not None:
        log("  [FAIL] could not even reach the grid")
        log(f"      raw error: {str(err)[:160]}")
        log("      " + explain_network_error(err))
        return 3
    if code == 200:
        log("  [ OK ] cookie WORKS — Alert Logs answered 200")
        return 0
    if code in (401, 403):
        log(f"  [FAIL] grid rejected the cookie (HTTP {code})")
        log("      The session expired or you logged out. Copy a fresh cURL.")
        return 1
    if code in (301, 302):
        log(f"  [FAIL] redirected to the login page (HTTP {code})")
        log("      The cookie is dead. Log in again in Chrome, copy a fresh cURL.")
        return 1
    log(f"  [WARN] unexpected HTTP {code}")
    if body:
        log(f"      server said: {body[:200]}")
    return 1


def main():
    ap = argparse.ArgumentParser(
        description="Save and verify a Site24x7 session from 'Copy as cURL'")
    ap.add_argument("--account", "-a", help="account folder, e.g. automation")
    ap.add_argument("--grid", "-g", help="grid name from grids.json, "
                    "e.g. integrations-qa (required with --account)")
    ap.add_argument("--file", help="read the cURL from this file, not stdin")
    ap.add_argument("--check-only", action="store_true",
                    help="only test the cookie already saved; write nothing")
    args = ap.parse_args()

    grids = load_grids()
    if args.account and not args.grid:
        die(f"--grid is required with --account. Known: {', '.join(grids)}")
    if args.grid and args.grid not in grids:
        die(f"no such grid {args.grid!r}. Known: {', '.join(grids)}")
    out = session_path(args.account, args.grid)
    grid_url = grids[args.grid]["url"].rstrip("/") if args.grid else None

    if args.check_only:
        s = read_session(out)
        cookie, csrf = s.get("S247_SESSION_COOKIE"), s.get("S247_CSRF_TOKEN")
        if not cookie:
            die(f"no saved cookie in {os.path.relpath(out, HERE)}")
        if not grid_url:
            die("--check-only needs --grid so it knows where to test.")
    else:
        if args.file:
            text = open(os.path.expanduser(args.file), encoding="utf-8").read()
        else:
            if sys.stdin.isatty():
                log("Paste the 'Copy as cURL (bash)' text, then press "
                    "Enter and Ctrl+D:\n")
            text = sys.stdin.read()
        url, cookie, csrf = parse_curl(text)

        curl_host = (urlparse(url).hostname or "").lower() if url else ""
        if grid_url:
            want = (urlparse(grid_url).hostname or "").lower()
            if curl_host and curl_host != want:
                die(f"the cURL is for {curl_host}, but --grid {args.grid} is "
                    f"{want}.\n          A cookie only works on the grid it "
                    "came from. Copy it from the right grid.")
        elif curl_host:
            grid_url = f"https://{curl_host}"
        else:
            die("could not tell which grid this is. Pass --grid.")

        write_session(out, cookie, csrf)
        log(f"  [ OK ] saved {os.path.relpath(out, HERE)}  (permissions 600)")
        log(f"         cookie: {len(cookie)} chars, "
            f"csrf: {'yes' if csrf else 'NO (s247cname missing)'}")

    log(f"  testing against {grid_url} ...")
    rc = report(grid_url, *test_session(grid_url, cookie, csrf))

    if rc == 0 and args.account:
        log("\n  NEXT — run the suite WITHOUT re-login (so this cookie is kept):")
        log(f"    python3 itsm.py --account {args.account} --grid {args.grid} "
            "--no-login")
    if args.account:
        log("\n  Re-test the saved cookie any time (writes nothing):")
        log(f"    python3 session_from_curl.py --account {args.account} "
            f"--grid {args.grid} --check-only")
    sys.exit(rc)


if __name__ == "__main__":
    main()
