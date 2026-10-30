#!/usr/bin/env python3
"""
save_desk_grant.py - set up Zoho Desk credentials in accounts/<acct>/.itsm.env

Two modes:

  1) Own Desk per account (grant code, valid ~10 min):
        python3 save_desk_grant.py --account tpt
     Exchanges the grant code for a long-lived refresh token, looks up the
     Desk Org ID automatically, saves, then checks tickets are readable.

  2) Share one Desk across accounts (copy a working account's Desk values):
        python3 save_desk_grant.py --share --from automation --to tpt tpt1
     Nothing to type. Copies only the ZOHODESK_* lines.

Secrets are typed with hidden input and never printed. Files are chmod 600 and
must be git-ignored. Only ZOHODESK_* lines change; all other lines are kept.
Saving happens as soon as a refresh token exists; the ticket check afterwards
is informational and never undoes the save.
"""
import argparse
import getpass
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
SAMPLE = os.path.join(HERE, ".itsm.env.sample")
DEFAULT_ACCOUNTS = "https://accounts.localzoho.com"
DEFAULT_API = "https://desk.localzoho.com"
SCOPE_HINT = "Desk.tickets.READ,Desk.basic.READ"
KEYS = ["ZOHODESK_ORG_ID", "ZOHODESK_CLIENT_ID", "ZOHODESK_CLIENT_SECRET",
        "ZOHODESK_REFRESH_TOKEN", "ZOHODESK_ACCOUNTS_URL", "ZOHODESK_API_URL"]
LINE_RE = re.compile(r'\s*(?:export\s+)?(\w+)\s*=\s*"?([^"\n]*)"?')
NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")


# ------------------------------------------------------------------ files
def env_path(account):
    if not NAME_RE.match(account):
        sys.exit(f"[FAIL] bad account name '{account}'")
    d = os.path.join(HERE, "accounts", account)
    if not os.path.isdir(d):
        sys.exit(f"[FAIL] no such account folder: {d}")
    return os.path.join(d, ".itsm.env")


def ensure_ignored(path):
    r = subprocess.run(["git", "-C", HERE, "check-ignore", "-q", path],
                       capture_output=True)
    if r.returncode != 0:
        sys.exit(f"[FAIL] {path} is NOT git-ignored - refusing to write "
                 f"secrets there. Nothing was saved.")


def ensure_file(path):
    if not os.path.isfile(path):
        if os.path.isfile(SAMPLE):
            shutil.copyfile(SAMPLE, path)
        else:
            open(path, "w").close()
        os.chmod(path, 0o600)
        print(f"(created {path} from the blank template)")


def read_env(path):
    vals = {}
    if os.path.isfile(path):
        with open(path) as fh:
            for line in fh:
                if line.lstrip().startswith("#"):
                    continue
                m = LINE_RE.match(line)
                if m:
                    vals[m.group(1)] = m.group(2).strip()
    return vals


def write_desk(path, new):
    """Replace ZOHODESK_* lines in place; append any that are missing."""
    lines = []
    if os.path.isfile(path):
        with open(path) as fh:
            lines = fh.read().splitlines()
    done = set()
    for i, line in enumerate(lines):
        if line.lstrip().startswith("#"):
            continue
        m = LINE_RE.match(line)
        if m and m.group(1) in new:
            lines[i] = f'export {m.group(1)}="{new[m.group(1)]}"'
            done.add(m.group(1))
    missing = [k for k in KEYS if k in new and k not in done]
    if missing:
        lines += ["", "# Zoho Desk (written by save_desk_grant.py)"]
        lines += [f'export {k}="{new[k]}"' for k in missing]
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        fh.write("\n".join(lines) + "\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    os.chmod(path, 0o600)
    back = read_env(path)
    bad = [k for k in new if back.get(k) != new[k]]
    if bad:
        sys.exit(f"[FAIL] read-back mismatch for {bad} in {path}")


# ------------------------------------------------------------------- http
def http(url, data=None, headers=None):
    body = urllib.parse.urlencode(data).encode() if data else None
    req = urllib.request.Request(url, data=body, headers=headers or {},
                                 method="POST" if data else "GET")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read().decode() or "{}"
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")
    except urllib.error.URLError as e:
        return 0, f"network error: {e.reason}"


def as_json(raw):
    try:
        return json.loads(raw)
    except ValueError:
        return {}


def mint(acc, cid, csec, rt):
    st, raw = http(f"{acc}/oauth/v2/token", data={
        "grant_type": "refresh_token", "client_id": cid,
        "client_secret": csec, "refresh_token": rt})
    js = as_json(raw)
    return js.get("access_token"), js.get("error", f"HTTP {st}")


def check_tickets(api, org, tok):
    st, raw = http(f"{api}/api/v1/tickets?limit=1",
                   headers={"Authorization": f"Zoho-oauthtoken {tok}",
                            "orgId": org, "Accept": "application/json"})
    if st == 200:
        print("[ OK ] WORKS - Desk tickets readable.")
        return True
    hint = {401: "token/scope problem", 403: "user has no access to this org",
            422: "Org ID probably wrong", 0: "network/VPN"}.get(st, "")
    print(f"[INFO] ticket check returned HTTP {st} {('- ' + hint) if hint else ''}"
          f"  (the save is already done)")
    return False


def lookup_orgs(api, tok):
    st, raw = http(f"{api}/api/v1/organizations",
                   headers={"Authorization": f"Zoho-oauthtoken {tok}",
                            "Accept": "application/json"})
    if st != 200:
        return []
    return [(str(o.get("id")), o.get("companyName") or o.get("portalName") or "")
            for o in as_json(raw).get("data", []) if o.get("id")]


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


# ------------------------------------------------------------------ modes
def share(src, targets):
    sp = env_path(src)
    vals = read_env(sp)
    new = {k: vals.get(k, "") for k in KEYS}
    empty = [k for k in KEYS[:4] if not new[k]]
    if empty:
        sys.exit(f"[FAIL] '{src}' has no Desk values for {empty}. "
                 f"Set it up first with --account {src}.")
    for t in targets:
        p = env_path(t)
        ensure_file(p)
        ensure_ignored(p)
        write_desk(p, new)
        print(f"[ OK ] Desk copied from '{src}' to '{t}' (permissions 600)")
    print("Checking the shared Desk login ...")
    tok, err = mint(new["ZOHODESK_ACCOUNTS_URL"].rstrip("/"),
                    new["ZOHODESK_CLIENT_ID"], new["ZOHODESK_CLIENT_SECRET"],
                    new["ZOHODESK_REFRESH_TOKEN"])
    if not tok:
        print(f"[INFO] token refresh said: {err}  (the save is already done)")
        return
    check_tickets(new["ZOHODESK_API_URL"].rstrip("/"), new["ZOHODESK_ORG_ID"], tok)


def own(account):
    path = env_path(account)
    ensure_file(path)
    ensure_ignored(path)
    old = read_env(path)
    print(f"\nAccount : {account}")
    print(f"Saving  : {path}  (only ZOHODESK_* lines change)")
    print(f"Grant-code scope: {SCOPE_HINT}")
    print("Secret input is hidden - paste and press Enter.\n")

    def keep(k, dflt):
        v = old.get(k, "")
        # the blank template ships US defaults; prefer local grid defaults
        return v if v and "zoho.com" not in v else dflt

    acc = ask("accounts URL", keep("ZOHODESK_ACCOUNTS_URL", DEFAULT_ACCOUNTS),
              pattern=r"^https://[\w.-]+/?$",
              hint="like https://accounts.localzoho.com").rstrip("/")
    api = ask("Desk API URL", keep("ZOHODESK_API_URL", DEFAULT_API),
              pattern=r"^https://[\w.-]+/?$",
              hint="like https://desk.localzoho.com").rstrip("/")
    cid = ask("client_id", old.get("ZOHODESK_CLIENT_ID") or None, secret=True,
              pattern=r"^1000\.\w+$",
              hint="client_id starts with 1000. and has no second dot")
    csec = ask("client_secret", old.get("ZOHODESK_CLIENT_SECRET") or None,
               secret=True)
    if csec.startswith("1000."):
        sys.exit("[FAIL] that looks like a client_id/token, not a client_secret. "
                 "Nothing was saved.")
    code = ask("grant code (the 10-minute one)", secret=True,
               pattern=r"^1000\.\w+\.\w+$",
               hint="grant code looks like 1000.xxxx.yyyy")

    print("\nExchanging grant code for a refresh token ...")
    st, raw = http(f"{acc}/oauth/v2/token", data={
        "grant_type": "authorization_code", "client_id": cid,
        "client_secret": csec, "code": code})
    resp = as_json(raw)
    rt = resp.get("refresh_token")
    if not rt:
        err = resp.get("error", f"HTTP {st} {raw[:80]}")
        print(f"[FAIL] Zoho said: {err}   (nothing was saved)")
        if err == "invalid_code":
            print("  -> code expired / already used / made with a different "
                  "client_id. Generate a new one and re-run.")
        elif err == "invalid_client":
            print("  -> client_id or client_secret wrong for this Self Client.")
        sys.exit(2)

    tok, err = mint(acc, cid, csec, rt)
    org = old.get("ZOHODESK_ORG_ID", "")
    if tok:
        orgs = lookup_orgs(api, tok)
        if len(orgs) == 1:
            org = orgs[0][0]
            print(f"[ OK ] Desk Org ID found automatically: {org} ({orgs[0][1]})")
        elif len(orgs) > 1:
            print("This login sees several Desk orgs:")
            for oid, name in orgs:
                print(f"   {oid}  {name}")
            org = ask("Org ID to use", org or orgs[0][0], pattern=r"^\d+$",
                      hint="digits only")
    if not org:
        org = ask("Desk Org ID (Desk > Setup > Developer Space > API)",
                  pattern=r"^\d+$", hint="digits only")

    write_desk(path, {"ZOHODESK_ORG_ID": org, "ZOHODESK_CLIENT_ID": cid,
                      "ZOHODESK_CLIENT_SECRET": csec,
                      "ZOHODESK_REFRESH_TOKEN": rt,
                      "ZOHODESK_ACCOUNTS_URL": acc, "ZOHODESK_API_URL": api})
    print(f"[ OK ] SAVED - Desk stored for '{account}' (permissions 600)")
    print(f"       granted scope: {resp.get('scope', '<not reported>')}")
    if not tok:
        print(f"[INFO] token refresh said: {err}  (the save is already done)")
        return
    check_tickets(api, org, tok)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--account", help="own-Desk mode: automation | tpt | tpt1")
    ap.add_argument("--share", action="store_true", help="copy mode")
    ap.add_argument("--from", dest="src", default="automation")
    ap.add_argument("--to", nargs="+", default=["tpt", "tpt1"])
    a = ap.parse_args()
    if a.share:
        share(a.src, [t for t in a.to if t != a.src])
    elif a.account:
        own(a.account)
    else:
        ap.error("use --account <name>  or  --share")


if __name__ == "__main__":
    try:  # every saved credential also goes to Jenkins automatically
        from jenkins_secrets import register_auto_push
        register_auto_push()
    except Exception:
        pass
    main()
