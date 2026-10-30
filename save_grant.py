#!/usr/bin/env python3
"""
save_grant.py - turn a Zoho Self-Client GRANT CODE (valid ~10 min) into a
long-lived refresh_token and save it to .token.<account>.env. Then prove it
works by minting an access token.

Usage:
    python3 save_grant.py --account tpt
    python3 save_grant.py --account tpt1

Secrets are typed with hidden input, never printed, and the file is chmod 600.
"""
import argparse
import getpass
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_ACCOUNTS = "https://accounts.localzoho.com"


def read_existing(path):
    vals = {}
    if os.path.isfile(path):
        with open(path) as fh:
            for line in fh:
                m = re.match(r'\s*(?:export\s+)?(\w+)\s*=\s*"?([^"\n]*)"?', line)
                if m:
                    vals[m.group(1)] = m.group(2).strip()
    return vals


def post(url, data):
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=body, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode() or "{}")
        except ValueError:
            return {"error": f"HTTP {e.code}"}
    except urllib.error.URLError as e:
        sys.exit(f"[FAIL] could not reach {url}: {e.reason}  (network/VPN problem, not the code)")


def ask(label, default=None, secret=False, pattern=None, hint=""):
    while True:
        shown = " [keep existing]" if default else ""
        prompt = f"{label}{shown}: "
        val = (getpass.getpass(prompt) if secret else input(prompt)).strip().strip('"').strip("'")
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

    path = os.path.join(HERE, f".token.{a.account}.env")
    old = read_existing(path)
    print(f"\nSaving to: {path}\n(input for secrets is hidden - paste and press Enter)\n")

    cid = ask("client_id", old.get("client_id"), pattern=r"^1000\.\w+$",
              hint="client_id starts with 1000. and has no second dot")
    csec = ask("client_secret", old.get("client_secret"), secret=True)
    code = ask("grant code (the 10-minute one)", secret=True, pattern=r"^1000\.\w+\.\w+$",
               hint="grant code looks like 1000.xxxx.yyyy")
    acc = old.get("accounts_url") or DEFAULT_ACCOUNTS

    print("\nExchanging grant code for a refresh token ...")
    resp = post(f"{acc}/oauth/v2/token", {
        "grant_type": "authorization_code",
        "client_id": cid, "client_secret": csec, "code": code})
    rt = resp.get("refresh_token")
    if not rt:
        err = resp.get("error", resp)
        print(f"[FAIL] Zoho said: {err}")
        if err == "invalid_code":
            print("  -> code expired / already used / made with a different client_id. Generate a new one.")
        elif err == "invalid_client":
            print("  -> client_id or client_secret wrong for this Self Client.")
        sys.exit(2)

    with open(path, "w") as fh:
        fh.write(f'export client_id="{cid}"\n'
                 f'export client_secret="{csec}"\n'
                 f'export refresh_token="{rt}"\n'
                 f'export accounts_url="{acc}"\n')
    os.chmod(path, 0o600)
    print(f"[ OK ] refresh token saved to {os.path.basename(path)} (permissions 600)")
    print(f"       granted scope: {resp.get('scope', '<not reported>')}")

    print("Verifying it mints an access token ...")
    v = post(f"{acc}/oauth/v2/token", {
        "grant_type": "refresh_token", "client_id": cid,
        "client_secret": csec, "refresh_token": rt})
    if v.get("access_token"):
        print("[ OK ] WORKS - access token minted. You're done for this account.")
    else:
        print(f"[WARN] saved, but refresh test returned: {v.get('error', v)}")
        sys.exit(3)


if __name__ == "__main__":
    main()
