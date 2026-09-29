#!/usr/bin/env python3
"""
itsm.py — THE ONE COMMAND. Pick an account, run the suite.
==========================================================

WHY THIS EXISTS
    Two Site24x7 accounts, two sets of ITSM credentials, two logins, two
    sets of alert logs. Keeping those straight by hand is how a run ends
    up pointed at the wrong grid and nobody notices. So: one launcher
    that ASKS which account, then isolates everything that account owns.

    It does NOT replace anything. Every existing script still works
    exactly as it does today when run by hand. This only wires them
    together with the right environment.

LAYOUT IT CREATES

    ~/itsm-automation/            <- the CODE (shared, one copy)
        itsm.py  run_all.py  stage2b_cycle.py  stage3_verify.py ...

        accounts/
            qa/                   <- one folder per account. DATA only.
                account.env       grid + which token/profile to use
                .itsm.env         that account's ITSM tool credentials
                .session.env      that account's cookie (auto-written)
                allowlist.json    which monitors may be touched
                test_config.json  that account's inventory
                reports/          that account's reports
            big/
                ... the same, completely separate ...

    Nothing leaks between accounts. Different grid, different token,
    different cookie, different Chrome profile, different allowlist,
    different reports.

USAGE
    python3 itsm.py                     ask which account, then run
    python3 itsm.py --account big       skip the question
    python3 itsm.py --list              show configured accounts
    python3 itsm.py --check             preflight only, no run
    python3 itsm.py --login-only        just refresh the cookie
    python3 itsm.py --init big          create a new account folder

    Anything else is passed straight through to run_all.py:
    python3 itsm.py --account big --skip-cycle --hours 8

SETTING UP AN ACCOUNT
    python3 itsm.py --init myaccount
    then edit  accounts/myaccount/account.env  and  .itsm.env  with nano.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ACCOUNTS = os.path.join(HERE, "accounts")
# All scripts live in the same folder as itsm.py.
LOGIN_JS = os.environ.get(
    "S247_LOGIN_JS",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "s247_login.js"))

# Files that belong to an ACCOUNT, not to the code.
ACCOUNT_FILES = [".itsm.env", ".session.env", "allowlist.json",
                 "test_config.json", "known_issues.json"]

TEMPLATE = """# Account settings for '{name}'.
# Paths may use ~ and are resolved from your home folder.
# Single quotes around anything containing $ ! # or a backtick.

# The Site24x7 grid this account lives on
export S247_GRID_URL="https://integrations-qa.localsite24x7.com"

# The script that mints an OAuth token, and the secrets file it reads
export S247_TOKEN_SCRIPT="$HOME/Documents/qg/get_token.sh"
export TOKEN_ENV_FILE="$HOME/Documents/qg/.token.{name}.env"

# The Site24x7 login used by the headless Chrome profile.
# Each account gets its OWN profile, so the two logins never collide.
export S247_LOGIN_USER=''
export S247_LOGIN_PASS=''
"""


def log(m=""):
    print(m, flush=True)


def section(t):
    log("\n" + "=" * 70)
    log(t)
    log("=" * 70)


def die(msg, code=2):
    log(f"\n[BLOCKER] {msg}")
    sys.exit(code)


def list_accounts():
    if not os.path.isdir(ACCOUNTS):
        return []
    out = []
    for n in sorted(os.listdir(ACCOUNTS)):
        if os.path.isfile(os.path.join(ACCOUNTS, n, "account.env")):
            out.append(n)
    return out


def read_env_file(path):
    """Parse KEY=VALUE / export KEY=VALUE. Quotes stripped, ~ and $HOME
    expanded. Deliberately simple -- it never executes the file."""
    env = {}
    if not os.path.isfile(path):
        return env
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[7:].strip()
            if "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                v = v[1:-1]
            v = os.path.expandvars(os.path.expanduser(v))
            env[k] = v
    return env


def cmd_init(name):
    d = os.path.join(ACCOUNTS, name)
    if os.path.isdir(d):
        die(f"accounts/{name} already exists. Nothing was changed.")
    os.makedirs(os.path.join(d, "reports"), exist_ok=True)

    with open(os.path.join(d, "account.env"), "w", encoding="utf-8") as fh:
        fh.write(TEMPLATE.format(name=name))
    os.chmod(os.path.join(d, "account.env"), 0o600)

    # Offer to seed the account with whatever is in the project root today,
    # so an existing working setup can be adopted without retyping it.
    seeded = []
    for f in ACCOUNT_FILES:
        src = os.path.join(HERE, f)
        if os.path.isfile(src):
            shutil.copy2(src, os.path.join(d, f))
            try:
                os.chmod(os.path.join(d, f), 0o600)
            except OSError:
                pass
            seeded.append(f)

    section(f"CREATED accounts/{name}")
    log(f"  folder : {d}")
    if seeded:
        log(f"  copied from the project root (originals untouched):")
        for f in seeded:
            log(f"      {f}")
    log("\n  NEXT:")
    log(f"    nano {os.path.join(d, 'account.env')}")
    log("      - set S247_GRID_URL")
    log("      - set TOKEN_ENV_FILE to this account's secrets file")
    log("      - set S247_LOGIN_USER / S247_LOGIN_PASS")
    log(f"    nano {os.path.join(d, '.itsm.env')}")
    log("      - this account's ServiceNow / Desk / SDP / HALO credentials")
    log(f"\n  Then:  python3 itsm.py --account {name} --check")


def choose_account(accounts):
    section("WHICH ACCOUNT?")
    for i, a in enumerate(accounts, 1):
        cfg = read_env_file(os.path.join(ACCOUNTS, a, "account.env"))
        log(f"  {i}. {a:<12} {cfg.get('S247_GRID_URL', '(no grid set)')}")
    log("")
    while True:
        try:
            pick = input("  Enter a number (or the name): ").strip()
        except (EOFError, KeyboardInterrupt):
            die("no account chosen.")
        if pick in accounts:
            return pick
        if pick.isdigit() and 1 <= int(pick) <= len(accounts):
            return accounts[int(pick) - 1]
        log("  Not one of the options. Try again.")


def build_env(name):
    d = os.path.join(ACCOUNTS, name)
    cfg = read_env_file(os.path.join(d, "account.env"))
    if not cfg.get("S247_GRID_URL"):
        die(f"accounts/{name}/account.env has no S247_GRID_URL.")

    env = dict(os.environ)
    env.update(cfg)
    env.update(read_env_file(os.path.join(d, ".itsm.env")))

    # Everything account-scoped, so two accounts can never overwrite
    # each other's cookie, Chrome profile or reports.
    env["S247_ACCOUNT"] = name
    env["S247_SESSION_FILE"] = os.path.join(d, ".session.env")
    env["S247_PROFILE_DIR"] = os.path.join(
        os.path.expanduser("~"), f".s247-profile-{name}")
    env.update(read_env_file(os.path.join(d, ".session.env")))
    return d, env


def run(argv, env, cwd, label):
    log(f"\n  $ {' '.join(str(a) for a in argv)}")
    try:
        return subprocess.run(argv, env=env, cwd=cwd).returncode
    except FileNotFoundError as exc:
        die(f"{label}: {exc}")
    except KeyboardInterrupt:
        log("\n  [ABORTED] Ctrl+C")
        raise


def main():
    ap = argparse.ArgumentParser(
        description="Run the ITSM suite against a chosen account",
        epilog="Unrecognised options are passed through to run_all.py")
    ap.add_argument("--account", "-a", default=None)
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--init", metavar="NAME", default=None)
    ap.add_argument("--check", action="store_true",
                    help="run preflight only, change nothing")
    ap.add_argument("--tools", action="store_true",
                    help="test this account's ITSM tool credentials "
                         "(ServiceNow / Desk / SDP / HALO). Reads no "
                         "tickets, changes nothing.")
    ap.add_argument("--no-integrations", action="store_true",
                    help="skip the automatic integration-list capture at the "
                         "start of a run")
    ap.add_argument("--logreport", metavar="MONITOR_ID", nargs="?",
                    const="ALL", default=None,
                    help="capture Log Reports and print the EXACT times each "
                         "monitor changed status — the anchor a ticket must "
                         "be matched against. With no id, does EVERY monitor "
                         "this account is allowed to touch.")
    ap.add_argument("--integrations", action="store_true",
                    help="capture the LIVE third-party integration list for "
                         "this account, so deleted integrations stop "
                         "appearing in reports")
    ap.add_argument("--inventory", action="store_true",
                    help="rebuild this account's monitor inventory "
                         "(test_config.json). Read-only against Site24x7.")
    ap.add_argument("--login-only", action="store_true",
                    help="refresh this account's cookie and stop")
    ap.add_argument("--no-login", action="store_true",
                    help="skip the cookie refresh (use the saved one)")
    args, passthrough = ap.parse_known_args()

    if args.init:
        cmd_init(args.init)
        return

    accounts = list_accounts()
    if args.list or not accounts:
        section("ACCOUNTS")
        if not accounts:
            log("  none configured yet.")
            log("\n  Create one:  python3 itsm.py --init big")
            sys.exit(0 if args.list else 2)
        for a in accounts:
            cfg = read_env_file(os.path.join(ACCOUNTS, a, "account.env"))
            d = os.path.join(ACCOUNTS, a)
            log(f"\n  {a}")
            log(f"     grid      : {cfg.get('S247_GRID_URL', '(not set)')}")
            log(f"     token file: {cfg.get('TOKEN_ENV_FILE', '(not set)')}")
            for f in (".itsm.env", "allowlist.json", ".session.env"):
                mark = "yes" if os.path.isfile(os.path.join(d, f)) else "NO"
                log(f"     {f:<14}: {mark}")
        return

    name = args.account or (accounts[0] if len(accounts) == 1
                            else choose_account(accounts))
    if name not in accounts:
        die(f"no such account {name!r}. Known: {', '.join(accounts)}")

    d, env = build_env(name)

    section(f"ACCOUNT: {name}")
    log(f"  grid     : {env.get('S247_GRID_URL')}")
    log(f"  data dir : {d}")
    log(f"  profile  : {env.get('S247_PROFILE_DIR')}")
    log(f"  token    : {env.get('TOKEN_ENV_FILE', '(default)')}")

    if args.logreport:
        js = os.path.join(os.path.dirname(LOGIN_JS), "s247_logreport.js")
        if not os.path.isfile(js):
            die(f"{js} not found.")

        ids = []
        if args.logreport != "ALL":
            ids = [str(args.logreport)]
        else:
            # every monitor this account may touch: the allowlist if there
            # is one, otherwise the whole inventory
            allow = os.path.join(d, "allowlist.json")
            cfg = os.path.join(d, "test_config.json")
            if os.path.isfile(allow):
                data = json.load(open(allow, encoding="utf-8"))
                ids = [str(i) for i in (data.get("monitor_ids") or [])]
                if ids:
                    log(f"  using the {len(ids)} monitor(s) in allowlist.json")
            if not ids and os.path.isfile(cfg):
                data = json.load(open(cfg, encoding="utf-8"))
                ids = [str(m.get("monitor_id"))
                       for m in (data.get("monitors") or [])
                       if m.get("monitor_id")]
                log(f"  no allowlist — using all {len(ids)} monitor(s) in "
                    f"test_config.json")
            if not ids:
                die("no monitors found. Run --inventory first, or give an id.")

        env["S247_LOGREPORT_DIR"] = d
        env.pop("S247_LOGREPORT_FILE", None)
        rc = run(["node", js] + ids, env, d, "s247_logreport.js")
        sys.exit(rc)

    if args.integrations:
        js = os.path.join(os.path.dirname(LOGIN_JS), "s247_integrations.js")
        if not os.path.isfile(js):
            die(f"{js} not found.")
        env["S247_INTEGRATIONS_FILE"] = os.path.join(d, "integrations.json")
        rc = run(["node", js], env, d, "s247_integrations.js")
        if rc != 0:
            log("\n  Could not capture the list. Is this account logged in?")
            log(f"    python3 itsm.py -a {name} --login-only")
        sys.exit(rc)

    if args.inventory:
        rc = run([sys.executable, os.path.join(HERE, "stage1_inventory.py")],
                 env, d, "stage1_inventory.py")
        sys.exit(rc)

    if args.tools:
        section(f"ITSM TOOL CREDENTIALS — {name}")
        itsm_file = os.path.join(d, ".itsm.env")
        if not os.path.isfile(itsm_file):
            log(f"  [!! ] {itsm_file} does not exist.")
            log("        This account has NO ITSM credentials, so stage 4")
            log("        can never verify a ticket inside a tool.")
            sys.exit(2)
        log(f"  using {itsm_file}")
        rc = run([sys.executable, os.path.join(HERE, "stage4_tickets.py"),
                  "check"], env, d, "stage4_tickets.py")
        sys.exit(rc)

    if args.check:
        rc = run([sys.executable, os.path.join(HERE, "preflight.py")],
                 env, d, "preflight.py")
        sys.exit(rc)

    # ---- cookie ---------------------------------------------------------
    # --setup must run FIRST. Trying the silent login before it is pointless
    # on a brand-new profile: it always fails, and it used to exit before
    # the setup ever got a chance to run.
    want_setup = "--setup" in passthrough
    if want_setup:
        if not os.path.isfile(LOGIN_JS):
            die(f"{LOGIN_JS} not found.")
        section(f"ONE-TIME LOGIN SETUP — {name}")
        log("  A Chrome window will open using THIS ACCOUNT'S OWN profile:")
        log(f"    {env.get('S247_PROFILE_DIR')}")
        log("  Log in to the account you expect, and leave it on any")
        log("  Site24x7 page. Each account has a separate profile, so the")
        log("  two logins never overwrite each other.")
        rc = run(["node", LOGIN_JS, "--setup"], env, d, "s247_login.js")
        sys.exit(rc)

    if not args.no_login:
        section(f"SESSION COOKIE — {name}")
        if not os.path.isfile(LOGIN_JS):
            log(f"  [skip] {LOGIN_JS} not found; using the saved cookie.")
        else:
            rc = run(["node", LOGIN_JS], env, d, "s247_login.js")
            if rc != 0:
                # Headless can fail where a visible window succeeds -- a
                # client-certificate prompt, for one. Retry visibly before
                # asking the human for anything. The window opens and
                # closes itself; there is still nothing to type.
                log("")
                log("  Silent sign-in did not complete. Retrying with a")
                log("  visible window (it closes itself)...")
                rc = run(["node", LOGIN_JS, "--headed"], env, d,
                         "s247_login.js")
            if rc != 0:
                log("")
                log("  The headless login failed for this account.")
                log("  First time on this account? Run the one-time setup:")
                log(f"    python3 itsm.py --account {name} --login-only "
                    f"--setup")
                log("  (a Chrome window opens; log in once and it is "
                    "remembered)")
                sys.exit(2)
            env.update(read_env_file(env["S247_SESSION_FILE"]))

    if args.login_only:
        log("\n  Cookie refreshed. Stopping (--login-only).")
        return

    # ---- live integration list, BEFORE the suite ------------------------
    # Alert Logs are history, so without a current list the report can show
    # integrations that were deleted weeks ago as though they were live.
    # Capture it every run; it takes seconds and it is what makes the
    # report's integration section trustworthy.
    if not args.no_integrations:
        js = os.path.join(os.path.dirname(LOGIN_JS), "s247_integrations.js")
        target = os.path.join(d, "integrations.json")
        if not os.path.isfile(js):
            log(f"\n  [WARN] {js} not found — skipping the integration list.")
            log("         Deleted integrations may appear in the report.")
        else:
            section(f"THIRD-PARTY INTEGRATIONS — {name}")
            env["S247_INTEGRATIONS_FILE"] = target
            rc = run(["node", js], env, d, "s247_integrations.js")
            if rc != 0:
                log("\n  [WARN] could not capture the live list.")
                if os.path.isfile(target):
                    log(f"         Using the previously captured "
                        f"{target}.")
                else:
                    log("         The report will say the filter was NOT")
                    log("         applied, so you know deleted integrations")
                    log("         may appear.")

    # ---- the suite ------------------------------------------------------
    section(f"RUNNING THE SUITE — {name}")
    rc = run([sys.executable, os.path.join(HERE, "run_all.py")] + passthrough,
             env, d, "run_all.py")

    section(f"DONE — {name}")
    rep = os.path.join(d, "reports")
    log(f"  reports in: {rep}")
    try:
        latest = max((os.path.join(rep, f) for f in os.listdir(rep)
                      if f.endswith(".html")), key=os.path.getmtime)
        latest_abs = os.path.abspath(latest)
        latest_uri = "file://" + latest_abs
        log(f"\n  ┌─ LATEST REPORT ──────────────────────────────────────────┐")
        log(f"  │  Ctrl+Click to open in browser:                           │")
        log(f"  │  {latest_uri}")
        log(f"  └───────────────────────────────────────────────────────────┘")
        # Auto-open the report in the default browser
        try:
            import subprocess as _sp
            _sp.Popen(["xdg-open", latest_abs],
                      stdout=_sp.DEVNULL, stderr=_sp.DEVNULL)
            log(f"\n  ✅  Report auto-opened in your browser.")
        except Exception:
            log(f"\n  (Could not auto-open — Ctrl+Click the link above.)")
    except (OSError, ValueError):
        pass
    log(f"  exit code {rc}  (0 = build OK, 1 = defect, 2 = blocked)")
    sys.exit(rc)


if __name__ == "__main__":
    main()
