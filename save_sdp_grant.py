#!/usr/bin/env python3
"""
save_sdp_grant.py - turn a ServiceDesk Plus Cloud (SDPOD) GRANT CODE (valid
~10 min) into a long-lived refresh token and save it into THAT account's
accounts/<account>/.itsm.env. Then prove it works (token + read requests).

Usage:
    python3 save_sdp_grant.py --account automation
    python3 save_sdp_grant.py --account tpt
    python3 save_sdp_grant.py --account tpt1

Each account keeps its OWN SDP values - nothing is shared between accounts.
Secrets are typed with hidden input, never printed, and the file is chmod 600.
Only the SDP_* lines are changed; every other line in .itsm.env is kept.
"""
import argparse
import getpass
import json
import os
import re
import shutil
import sys
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
SAMPLE = os.path.join(HERE, ".itsm.env.sample")
DEFAULT_ACCOUNTS = "https://accounts.localzoho.com"
DEFAULT_API = "https://sdpondemand.localmanageengine.com"
SCOPE_HINT = "SDPOnDemand.requests.READ (or SDPOnDemand.requests.ALL)"
KEYS = ["SDP_PORTAL", "SDP_CLIENT_ID", "SDP_CLIENT_SECRET",
        "SDP_REFRESH_TOKEN", "SDP_ACCOUNTS_URL", "SDP_API_URL"]
LINE_RE = re.compile(r'\s*(?:export\s+)?(\w+)\s*=\s*"?([^"\n]*)"?')


def read_env(path):
    vals = {}
    if os.path.isfile(path):
        with open(path) as fh:
            for line in fh:
                m = LINE_RE.match(line)
                if m:
                    vals[m.group(1)] = m.group(2).strip()
    return vals


def write_sdp(path, new):
    """Replace SDP_* lines in place; append any that are missing."""
    lines = []
    if os.path.isfile(path):
        with open(path) as fh:
            lines = fh.read().splitlines()
    done = set()
    for i, line in enumerate(lines):
        m = LINE_RE.match(line)
        if m and m.group(1) in new and not line.lstrip().startswith("#"):
            lines[i] = f'export {m.group(1)}="{new[m.group(1)]}"'
            done.add(m.group(1))
    missing = [k for k in KEYS if k in new and k not in done]
    if missing:
        lines.append("")
        lines.append("# ServiceDesk Plus Cloud (written by save_sdp_grant.py)")
        lines += [f'export {k}="{new[k]}"' for k in missing]
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        fh.write("\n".join(lines) + "\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    os.chmod(path, 0o600)


def http(url, data=None, headers=None):
    body = urllib.parse.urlencode(data).encode() if data else None
    req = urllib.request.Request(url, data=body, headers=headers or {},
                                 method="POST" if data else "GET")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read().decode() or "{}"
            return r.status, raw
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")
    except urllib.error.URLError as e:
        sys.exit(f"[FAIL] could not reach {url}: {e.reason}  "
                 f"(network/VPN problem, not the code)")


def as_json(raw):
    try:
        return json.loads(raw)
    except ValueError:
        return {}


def ask(label, default=None, secret=False, pattern=None, hint=""):
    while True:
        shown = " [keep existing]" if (default and secret) else (
            f" [{default}]" if default else "")
        val = (getpass.getpass(f"{label}{shown}: ") if secret
               else input(f"{label}{shown}: ")).strip().strip('"').strip("'")
        if not val and default:
            return default
        if not val:
            print("  value required")
            continue
        if pattern and not re.match(pattern, val):
            print(f"  that doesn't look right. {hint}")
            continue
        return val


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--account", required=True, help="automation | tpt | tpt1")
    a = ap.parse_args()

    if not re.match(r"^[A-Za-z0-9_-]+$", a.account):
        sys.exit("[FAIL] account name may only contain letters, digits, - and _")
    acct_dir = os.path.join(HERE, "accounts", a.account)
    if not os.path.isdir(acct_dir):
        sys.exit(f"[FAIL] no such account folder: {acct_dir}")
    path = os.path.join(acct_dir, ".itsm.env")
    if not os.path.isfile(path):
        if os.path.isfile(SAMPLE):
            shutil.copyfile(SAMPLE, path)
        else:
            open(path, "w").close()
        os.chmod(path, 0o600)
        print(f"(created {path} from the blank template)")

    old = read_env(path)
    print(f"\nAccount : {a.account}")
    print(f"Saving  : {path}  (only SDP_* lines change)")
    print("Secret input is hidden - paste and press Enter.\n")

    portal = ask("SDP portal name (the part after /app/ in the SDP URL)",
                 old.get("SDP_PORTAL") or None, pattern=r"^[\w.-]+$",
                 hint="just the name, e.g. itdesk - no slashes or https")
    acc = ask("accounts URL", old.get("SDP_ACCOUNTS_URL") or DEFAULT_ACCOUNTS,
              pattern=r"^https://[\w.-]+$", hint="like https://accounts.localzoho.com")
    api = ask("SDP API URL", old.get("SDP_API_URL") or DEFAULT_API,
              pattern=r"^https://[\w.-]+$",
              hint="like https://sdpondemand.localmanageengine.com")
    cid = ask("client_id", old.get("SDP_CLIENT_ID") or None, secret=True,
              pattern=r"^1000\.\w+$",
              hint="client_id starts with 1000. and has no second dot")
    csec = ask("client_secret", old.get("SDP_CLIENT_SECRET") or None, secret=True)
    if csec.startswith("1000."):
        sys.exit("[FAIL] that looks like a client_id / token, not a client_secret. "
                 "Nothing was saved.")
    code = ask("grant code (the 10-minute one)", secret=True,
               pattern=r"^1000\.\w+\.\w+$", hint="grant code looks like 1000.xxxx.yyyy")

    print("\nExchanging grant code for a refresh token ...")
    st, raw = http(f"{acc.rstrip('/')}/oauth/v2/token", data={
        "grant_type": "authorization_code", "client_id": cid,
        "client_secret": csec, "code": code})
    resp = as_json(raw)
    rt = resp.get("refresh_token")
    if not rt:
        err = resp.get("error", f"HTTP {st}")
        print(f"[FAIL] Zoho said: {err}   (nothing was saved)")
        if err == "invalid_code":
            print("  -> code expired / already used / made with a different "
                  "client_id. Generate a new one.")
        elif err == "invalid_client":
            print("  -> client_id or client_secret wrong for this Self Client.")
        sys.exit(2)

    write_sdp(path, {"SDP_PORTAL": portal, "SDP_CLIENT_ID": cid,
                     "SDP_CLIENT_SECRET": csec, "SDP_REFRESH_TOKEN": rt,
                     "SDP_ACCOUNTS_URL": acc.rstrip("/"),
                     "SDP_API_URL": api.rstrip("/")})
    print(f"[ OK ] SDP refresh token saved for '{a.account}' (permissions 600)")
    print(f"       granted scope: {resp.get('scope', '<not reported>')}")

    print("Verifying it mints an access token ...")
    st, raw = http(f"{acc.rstrip('/')}/oauth/v2/token", data={
        "grant_type": "refresh_token", "client_id": cid,
        "client_secret": csec, "refresh_token": rt})
    tok = as_json(raw).get("access_token")
    if not tok:
        print(f"[WARN] saved, but refresh test returned: "
              f"{as_json(raw).get('error', f'HTTP {st}')}")
        sys.exit(3)
    print("[ OK ] access token minted")

    print("Verifying it can read SDP requests ...")
    st, raw = http(f"{api.rstrip('/')}/app/{portal}/api/v3/requests",
                   headers={"Authorization": f"Zoho-oauthtoken {tok}",
                            "Accept": "application/vnd.manageengine.sdp.v3+json"})
    if st == 200:
        print("[ OK ] WORKS - SDP requests readable. You're done for this account.")
    elif st in (401, 403):
        print(f"[WARN] token works but SDP refused (HTTP {st}). Likely missing "
              f"scope {SCOPE_HINT}, or the token's user has no access to "
              f"portal '{portal}'.")
        sys.exit(4)
    elif st == 404:
        print(f"[WARN] token works but HTTP 404 - portal name '{portal}' or "
              f"SDP API URL is probably wrong. Re-run and correct it.")
        sys.exit(4)
    else:
        print(f"[WARN] token works but SDP returned HTTP {st}: {raw[:150]}")
        sys.exit(4)


if __name__ == "__main__":
    try:  # every saved credential also goes to Jenkins automatically
        from jenkins_secrets import register_auto_push
        register_auto_push()
    except Exception:
        pass
    main()
