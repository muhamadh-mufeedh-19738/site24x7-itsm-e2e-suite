#!/usr/bin/env python3
"""
save_itoc.py - save ONE iTOC360 web login into several accounts' .itsm.env.

Usage:
    python3 save_itoc.py                       # automation, tpt, tpt1
    python3 save_itoc.py --accounts automation # only some accounts

iTOC360 does not hand its incident number back to Site24x7, and its source
token can only SEND events. To confirm the alert inside iTOC360, the suite
logs in to the iTOC360 web app (https://itoc360.app) and reads the Alerts
list, so it needs a normal user login.

The web address and username are pre-filled (press Enter to keep them);
only the password is typed, hidden, and never printed.
Only the ITOC_* lines change; every other line in .itsm.env is kept.
Files are chmod 600 and must be git-ignored, or nothing is written.
"""
import argparse
import getpass
import os
import re
import shutil

from save_snow import (HERE, LINE_RE, SAMPLE, ask, fail, is_ignored, quote,
                       read_env)

DEFAULT_URL = "https://itoc360.app"
DEFAULT_USER = "muhamadh.mufeedh+tpt1@zohotest.com"
DEFAULT_ACCOUNTS = "automation,tpt,tpt1"
KEYS = ("ITOC_URL", "ITOC_USER", "ITOC_PASSWORD")


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
        out += ["", "# iTOC360 (written by save_itoc.py)"]
        out += [f"export {k}={values[k]}" for k in missing]
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write("\n".join(out) + "\n")
    os.replace(tmp, path)
    os.chmod(path, 0o600)


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

    print("iTOC360 - one web login for these accounts:", ", ".join(names))
    print("Press Enter to keep the value in [brackets].\n")
    url = ask("iTOC360 web address", DEFAULT_URL).rstrip("/")
    if not re.match(r"^https://[\w.-]+$", url):
        fail("address should look like https://itoc360.app")
    user = ask("Login email", DEFAULT_USER)

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

    values = {"ITOC_URL": quote(url, "the address"),
              "ITOC_USER": quote(user, "the email"),
              "ITOC_PASSWORD": quote(pwd, "the password")}
    for n, p in paths.items():
        if not os.path.isfile(p):
            if os.path.isfile(SAMPLE):
                shutil.copyfile(SAMPLE, p)
            else:
                open(p, "w").close()
            os.chmod(p, 0o600)
        write_keys(p, values)
        back = read_env(p)
        same = (back.get("ITOC_URL") == url and back.get("ITOC_USER") == user
                and back.get("ITOC_PASSWORD") == pwd)
        print(f"[{' OK ' if same else 'FAIL'}] saved for '{n}' "
              f"(password {len(pwd)} chars, permissions 600)")
        if not same:
            fail(f"read-back mismatch in {p}", 3)
    print("\n[ OK ] SAVED - iTOC360 login stored for: " + ", ".join(names))
    print("Then upload it to Jenkins:  python3 jenkins_secrets.py")


if __name__ == "__main__":
    main()
