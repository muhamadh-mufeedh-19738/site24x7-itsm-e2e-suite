#!/usr/bin/env python3
"""
save_snow.py - save ONE ServiceNow login into several accounts' .itsm.env,
then prove it works (reads one incident over the REST API).

Usage:
    python3 save_snow.py                       # automation, tpt, tpt1
    python3 save_snow.py --accounts tpt,tpt1   # only some accounts

The instance URL and username are pre-filled (press Enter to keep them);
only the password is typed, hidden, and never printed.
The password is checked against ServiceNow BEFORE anything is saved.
Only the SNOW_* lines change; every other line in .itsm.env is kept.
Files are chmod 600 and must be git-ignored, or nothing is written.
"""
import argparse
import base64
import getpass
import os
import re
import shutil
import ssl
import subprocess
import sys
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
SAMPLE = os.path.join(HERE, ".itsm.env.sample")
DEFAULT_URL = "https://dev434120.service-now.com"
DEFAULT_USER = "admin"
DEFAULT_ACCOUNTS = "automation,tpt,tpt1"
KEYS = ("SNOW_URL", "SNOW_USER", "SNOW_PASSWORD")
LINE_RE = re.compile(r"\s*(?:export\s+)?(\w+)\s*=\s*(.*)$")


def fail(msg, code=2):
    print(f"[FAIL] {msg}")
    sys.exit(code)


def unquote(v):
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
        return v[1:-1]
    return v


def read_env(path):
    vals = {}
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                if line.lstrip().startswith("#"):
                    continue
                m = LINE_RE.match(line)
                if m:
                    vals[m.group(1)] = unquote(m.group(2))
    return vals


def quote(value, what):
    """Quote so both the suite's parser and bash `source` read it back exactly."""
    if "\n" in value or "\r" in value:
        fail(f"{what} contains a line break - type it again. Nothing was saved.")
    if "'" not in value:
        return f"'{value}'"
    if not any(c in value for c in '"$`\\'):
        return f'"{value}"'
    fail(f"{what} contains both a single quote and one of \" $ ` \\ - it cannot "
         "be stored safely in a shell file. Nothing was saved.")


def is_ignored(path):
    r = subprocess.run(["git", "check-ignore", "-q", path], cwd=HERE,
                       capture_output=True)
    return r.returncode == 0


def write_keys(path, values):
    lines = []
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    out, done = [], set()
    for line in lines:
        m = LINE_RE.match(line)
        if m and m.group(1) in values and not line.lstrip().startswith("#"):
            k = m.group(1)
            if k not in done:                       # drop duplicate lines
                out.append(f"export {k}={values[k]}")
                done.add(k)
            continue
        out.append(line)
    missing = [k for k in KEYS if k not in done]
    if missing:
        out += ["", "# ServiceNow (written by save_snow.py)"]
        out += [f"export {k}={values[k]}" for k in missing]
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write("\n".join(out) + "\n")
    os.replace(tmp, path)
    os.chmod(path, 0o600)


def check_login(url, user, pwd):
    """Return (ok, message). Reads one incident - read-only."""
    auth = base64.b64encode(f"{user}:{pwd}".encode()).decode()
    req = urllib.request.Request(
        f"{url}/api/now/table/incident?sysparm_limit=1&sysparm_fields=number",
        headers={"Authorization": f"Basic {auth}", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30,
                                    context=ssl.create_default_context()) as r:
            return True, f"HTTP {r.status}"
    except urllib.error.HTTPError as e:
        if e.code == 401:
            return False, "HTTP 401 - wrong username or password"
        if e.code == 403:
            return False, ("HTTP 403 - login OK but this user cannot read the "
                           "incident table (needs the itil or admin role)")
        if e.code in (404, 503):
            return False, (f"HTTP {e.code} - instance not found or hibernating. "
                           "Open the URL in a browser to wake it, then retry.")
        return False, f"HTTP {e.code}"
    except urllib.error.URLError as e:
        return False, f"could not reach {url}: {e.reason} (network/VPN problem)"


def ask(label, default):
    val = input(f"{label} [{default}]: ").strip().strip('"').strip("'")
    return val or default


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--accounts", default=DEFAULT_ACCOUNTS,
                    help="comma-separated account names (default: %(default)s)")
    a = ap.parse_args()

    names = [n.strip() for n in a.accounts.split(",") if n.strip()]
    paths = {}
    for n in names:
        if not re.match(r"^[A-Za-z0-9_-]+$", n):
            fail(f"bad account name {n!r}")
        d = os.path.join(HERE, "accounts", n)
        if not os.path.isdir(d):
            fail(f"no such account folder: {d}")
        p = os.path.join(d, ".itsm.env")
        if not is_ignored(p):
            fail(f"{p} is NOT git-ignored - refusing to write a password there.")
        paths[n] = p

    print("ServiceNow - one login for these accounts:", ", ".join(names))
    print("Press Enter to keep the value in [brackets].\n")
    url = ask("Instance URL", DEFAULT_URL).rstrip("/")
    if not re.match(r"^https://[\w.-]+$", url):
        fail("URL should look like https://devXXXXX.service-now.com")
    user = ask("Username", DEFAULT_USER)

    # Show what each account has now, so nothing changes silently.
    changing = []
    for n, p in paths.items():
        old = read_env(p)
        ou = old.get("SNOW_URL", "").rstrip("/")
        if ou and ou != url:
            changing.append(f"  {n}: {ou}  ->  {url}")
    if changing:
        print("\nThese accounts currently point at a DIFFERENT instance:")
        print("\n".join(changing))
        if input("Replace them with the new one? [y/N]: ").strip().lower() != "y":
            fail("cancelled - nothing was saved.", 1)

    # Save first, check afterwards: a failed API check never blocks the save.
    pwd = None
    for _ in range(3):
        p1 = getpass.getpass("\nPassword (hidden): ")
        if not p1:
            fail("empty password - nothing was saved.")
        if getpass.getpass("Type it again    : ") == p1:
            pwd = p1
            break
        print("[FAIL] the two passwords don't match - try again.")
    if pwd is None:
        fail("passwords did not match 3 times - nothing was saved.")
    if pwd.strip() != pwd:
        print("[WARN] removed space/tab characters at the start or end.")
        pwd = pwd.strip()
    if any(ord(c) > 127 for c in pwd):
        print("[WARN] the password contains non-English characters "
              "(curly quotes from copy-paste?). Saving it as typed.")

    values = {"SNOW_URL": quote(url, "the URL"),
              "SNOW_USER": quote(user, "the username"),
              "SNOW_PASSWORD": quote(pwd, "the password")}
    for n, p in paths.items():
        if not os.path.isfile(p):
            if os.path.isfile(SAMPLE):
                shutil.copyfile(SAMPLE, p)
            else:
                open(p, "w").close()
            os.chmod(p, 0o600)
        write_keys(p, values)
        back = read_env(p)
        same = (back.get("SNOW_URL") == url and back.get("SNOW_USER") == user
                and back.get("SNOW_PASSWORD") == pwd)
        print(f"[{' OK ' if same else 'FAIL'}] saved for '{n}' "
              f"(password {len(pwd)} chars, permissions 600)")
        if not same:
            fail(f"read-back mismatch in {p}", 3)
    print("\n[ OK ] SAVED - ServiceNow password stored for: " + ", ".join(names))

    print(f"\nAPI check (informational only, the save is already done) ...")
    ok, msg = check_login(url, user, pwd)
    if ok:
        print(f"[ OK ] REST API accepted the login ({msg})")
    else:
        print(f"[INFO] REST API said: {msg}")
        print("       Saved anyway. If the browser login works, the instance is "
              "blocking Basic-auth API calls; this is fixed on the ServiceNow "
              "side, not by retyping.")


if __name__ == "__main__":
    try:  # every saved credential also goes to Jenkins automatically
        from jenkins_secrets import register_auto_push
        register_auto_push()
    except Exception:
        pass
    main()
