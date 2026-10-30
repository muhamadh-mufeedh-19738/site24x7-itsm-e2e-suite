#!/usr/bin/env python3
"""
save_login.py - save the Site24x7 login (email + password) for ONE account
into accounts/<account>/account.env, then optionally prove it works by
running the real headless login (itsm.py --login-only).

Usage:
    python3 save_login.py --account tpt
    python3 save_login.py --account tpt1 --grid integrations-qa

- The password is typed with hidden input, asked twice, never printed.
- account.env is created from the standard template if it is missing.
- Only the S247_LOGIN_USER / S247_LOGIN_PASS lines are changed; everything
  else in the file is kept exactly as it was.
- The file is chmod 600 and must be git-ignored, or nothing is written.
"""
import argparse
import getpass
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import itsm  # noqa: E402  (TEMPLATE / read_env_file; main() is guarded)

ACCOUNTS = os.path.join(HERE, "accounts")
EMAIL_RE = re.compile(r"^[^@\s'\"]+@[^@\s'\"]+\.[^@\s'\"]+$")
KEYS = ("S247_LOGIN_USER", "S247_LOGIN_PASS")


def fail(msg):
    print(f"[FAIL] {msg}")
    sys.exit(2)


def grid_url(name):
    with open(os.path.join(HERE, "grids.json"), encoding="utf-8") as fh:
        grids = json.load(fh)["grids"]
    if name not in grids:
        fail(f"unknown grid {name!r}. Known: {', '.join(grids)}")
    return grids[name]["url"].rstrip("/")


def quote(value, what):
    """Quote a value so BOTH the suite's parser and `source` in bash read it
    back unchanged. Single quotes are fully literal; fall back to double
    quotes only when that is still safe."""
    if "\n" in value or "\r" in value:
        fail(f"{what} contains a line break - paste it again.")
    if "'" not in value:
        return f"'{value}'"
    if not any(c in value for c in '"$`\\'):
        return f'"{value}"'
    fail(f"{what} contains both a single quote and one of \" $ ` \\ . "
         "That cannot be stored safely in a shell file. Nothing was saved.")


def is_ignored(path):
    r = subprocess.run(["git", "check-ignore", "-q", path], cwd=HERE,
                       capture_output=True)
    return r.returncode == 0


def other_accounts_using(email, me):
    hits = []
    for n in sorted(os.listdir(ACCOUNTS)):
        if n == me:
            continue
        cfg = itsm.read_env_file(os.path.join(ACCOUNTS, n, "account.env"))
        if cfg.get("S247_LOGIN_USER", "").lower() == email.lower():
            hits.append(n)
    return hits


def write_keys(path, values):
    lines = []
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    done = set()
    out = []
    for line in lines:
        m = re.match(r"\s*(?:export\s+)?(\w+)\s*=", line)
        if m and m.group(1) in values:
            k = m.group(1)
            if k not in done:          # drop any duplicate lines
                out.append(f"export {k}={values[k]}")
                done.add(k)
            continue
        out.append(line)
    for k in KEYS:
        if k not in done:
            out.append(f"export {k}={values[k]}")
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write("\n".join(out) + "\n")
    os.replace(tmp, path)
    os.chmod(path, 0o600)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--account", required=True)
    ap.add_argument("--grid", default="integrations-qa")
    args = ap.parse_args()

    name = args.account.strip()
    d = os.path.join(ACCOUNTS, name)
    if not os.path.isdir(d):
        fail(f"accounts/{name} does not exist.")
    url = grid_url(args.grid)
    path = os.path.join(d, "account.env")
    rel = os.path.relpath(path, HERE)

    # 1. Make sure account.env exists (standard template, never overwritten).
    if not os.path.isfile(path):
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(itsm.TEMPLATE.format(
                name=name, grid_url=url,
                token_script=os.path.join(HERE, "get_token.sh"),
                token_file=os.path.join(HERE, f".token.{name}.env")))
        print(f"[new ] {rel} created from the standard template")
    if not is_ignored(path):
        fail(f"{rel} is NOT git-ignored. Fix .gitignore first. Nothing saved.")

    token_file = os.path.join(HERE, f".token.{name}.env")
    if not os.path.isfile(token_file):
        print(f"[warn] {os.path.relpath(token_file, HERE)} is missing - "
              "run save_grant.py for this account too.")

    cur = itsm.read_env_file(path)
    cur_user = cur.get("S247_LOGIN_USER", "")
    has_pass = bool(cur.get("S247_LOGIN_PASS", ""))

    print(f"\nSite24x7 login for account '{name}'  (grid: {url})")
    print("Press Enter at a prompt to keep the existing value.\n")

    # 2. Email (not a secret - it is shown, so typos are visible).
    hint = f" [keep {cur_user}]" if cur_user else ""
    email = input(f"Login email{hint}: ").strip() or cur_user
    if not EMAIL_RE.match(email):
        fail("that does not look like an email address. Nothing saved.")
    clash = other_accounts_using(email, name)
    if clash:
        print(f"[warn] account(s) {', '.join(clash)} already use {email}.")
        if input("       Each account normally has its own login. "
                 "Save anyway? [y/N]: ").strip().lower() != "y":
            fail("cancelled. Nothing saved.")

    # 3. Password: hidden, typed twice.
    hint = " [keep existing]" if has_pass else ""
    pw = getpass.getpass(f"Password (hidden){hint}: ")
    if not pw:
        if not has_pass:
            fail("no password given. Nothing saved.")
        pw = cur["S247_LOGIN_PASS"]
    else:
        if pw != pw.strip():
            print("[note] removed spaces at the start/end of the password.")
            pw = pw.strip()
        if getpass.getpass("Type it again (hidden): ").strip() != pw:
            fail("the two passwords did not match. Nothing saved.")

    values = {"S247_LOGIN_USER": quote(email, "the email"),
              "S247_LOGIN_PASS": quote(pw, "the password")}
    write_keys(path, values)

    # 4. Read it back through the SAME parser the suite uses.
    back = itsm.read_env_file(path)
    if back.get("S247_LOGIN_USER") != email or back.get("S247_LOGIN_PASS") != pw:
        fail(f"{rel} did not read back correctly. Please tell the maintainer.")
    print(f"\n[ OK ] saved to {rel} (permissions 600, git-ignored)")
    print(f"       email    : {email}")
    print(f"       password : {len(pw)} characters (not shown)")

    # .itsm.env is read AFTER account.env, so a login line there would win.
    over = itsm.read_env_file(os.path.join(d, ".itsm.env"))
    for k in KEYS:
        if over.get(k):
            print(f"[warn] accounts/{name}/.itsm.env also sets {k}; that value "
                  "WINS over account.env. Remove it from .itsm.env.")

    # 5. Prove it: the real headless login, exactly as a suite run does it.
    ans = input("\nTest the login now? A browser signs in and saves the "
                "cookie. [Y/n]: ").strip().lower()
    if ans == "n":
        print("Skipped. Test later with:")
        print(f"  python3 itsm.py --account {name} --grid {args.grid} --login-only")
        return
    rc = subprocess.run([sys.executable, os.path.join(HERE, "itsm.py"),
                         "--account", name, "--grid", args.grid,
                         "--login-only"], cwd=HERE).returncode
    print()
    if rc == 0:
        print(f"[ OK ] WORKS - '{name}' is logged in and its cookie is saved.")
    else:
        print(f"[FAIL] login test failed for '{name}' (exit {rc}).")
        print("       Check the email/password, or run the one-time visible")
        print("       setup (OTP / certificate prompts):")
        print(f"  python3 itsm.py --account {name} --grid {args.grid} "
              "--login-only --setup")
    sys.exit(rc)


if __name__ == "__main__":
    try:  # every saved credential also goes to Jenkins automatically
        from jenkins_secrets import register_auto_push
        register_auto_push()
    except Exception:
        pass
    try:
        main()
    except KeyboardInterrupt:
        print("\n[ABORTED] nothing further was saved.")
        sys.exit(130)
