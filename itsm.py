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
    python3 itsm.py                     ask which GRID, then which account, then run
    python3 itsm.py --grid eum-dev      skip the grid question
    python3 itsm.py --account big       skip the account question
                                        (its grid must match the chosen grid)
    python3 itsm.py --list              show configured accounts
    python3 itsm.py --check             preflight only, no run
    python3 itsm.py --login-only        just refresh the cookie
    python3 itsm.py --init big --grid eum-dev
                                        create a new account folder on a grid

GRIDS
    The grids you can test live in grids.json (next to this file). Add a
    grid there and it appears in the question automatically -- nothing in
    the code needs to change. An account belongs to the grid its
    account.env S247_GRID_URL points at.

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
import time
import sys
from urllib.parse import urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
ACCOUNTS = os.path.join(HERE, "accounts")
# All scripts live in the same folder as itsm.py.
LOGIN_JS = os.environ.get(
    "S247_LOGIN_JS",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "s247_login.js"))

# Files that belong to an ACCOUNT, not to the code.
ACCOUNT_FILES = [".itsm.env", ".session.env", "allowlist.json",
                 "test_config.json", "known_issues.json", "scope.json"]

TEMPLATE = """# Account settings for '{name}'.
# Paths may use ~ and are resolved from your home folder.
# Single quotes around anything containing $ ! # or a backtick.

# The Site24x7 grid this account lives on. itsm.py always replaces this
# with the grid you pick, so it only matters for scripts run on their own.
export S247_GRID_URL="{grid_url}"

# The script that mints an OAuth token, and the secrets file it reads.
# Both live in the suite folder (the same layout Jenkins uses).
export S247_TOKEN_SCRIPT="{token_script}"
export TOKEN_ENV_FILE="{token_file}"

# Which browser the headless login uses. 'chromium' is the browser that
# Playwright downloads itself (the same one Jenkins uses). Change it to
# 'chrome' only if you need your installed Google Chrome instead.
export S247_BROWSER_CHANNEL="chromium"

# The Site24x7 login used by the headless browser profile.
# Each account gets its OWN profile, so the logins never collide.
export S247_LOGIN_USER=''
export S247_LOGIN_PASS=''
"""

# Blank templates copied in by --init when a secrets file is missing.
# A file that already exists is NEVER overwritten.
ITSM_SAMPLE = os.path.join(HERE, ".itsm.env.sample")
TOKEN_SAMPLE = os.path.join(HERE, ".token.env.sample")


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
            literal = False
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                # Single quotes mean "take it literally", exactly as in a
                # shell. Without this a password containing $ was silently
                # mangled by expandvars.
                literal = v[0] == "'"
                v = v[1:-1]
            if not literal:
                v = os.path.expandvars(os.path.expanduser(v))
            env[k] = v
    return env


def cmd_init(name, grid_url):
    d = os.path.join(ACCOUNTS, name)
    if os.path.isdir(d):
        die(f"accounts/{name} already exists. Nothing was changed.")
    os.makedirs(os.path.join(d, "reports"), exist_ok=True)

    with open(os.path.join(d, "account.env"), "w", encoding="utf-8") as fh:
        fh.write(TEMPLATE.format(
            name=name, grid_url=grid_url,
            token_script=os.path.join(HERE, "get_token.sh"),
            token_file=os.path.join(HERE, f".token.{name}.env")))
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
    log(f"\n  Then:  python3 itsm.py --grid <grid> --account {name} --check")


def _copy_if_missing(src, dst, made, kept, missing_src):
    """Copy a blank template to dst ONLY when dst does not exist yet.
    Never overwrites a file you have already filled in. chmod 600."""
    if os.path.exists(dst):
        kept.append(dst)
        return
    if not os.path.isfile(src):
        missing_src.append(src)
        return
    shutil.copyfile(src, dst)
    os.chmod(dst, 0o600)
    made.append(dst)


def cmd_local_setup(name, grid_url):
    """Prepare THIS machine to run an account that already exists in the
    repo (accounts/<name>/ with allowlist.json etc.). Creates the three
    secret files from blank templates if they are missing. Never overwrites,
    never asks for or prints a secret. The files are git-ignored."""
    d = os.path.join(ACCOUNTS, name)
    if not os.path.isdir(d):
        die(f"accounts/{name} does not exist. Known accounts: "
            f"{', '.join(sorted(n for n in os.listdir(ACCOUNTS) if os.path.isdir(os.path.join(ACCOUNTS, n)))) or 'none'}. "
            f"For a brand-new account use:  python3 itsm.py --init {name}")
    os.makedirs(os.path.join(d, "reports"), exist_ok=True)

    token_file = os.path.join(HERE, f".token.{name}.env")
    acct_env = os.path.join(d, "account.env")
    itsm_env = os.path.join(d, ".itsm.env")
    made, kept, missing_src = [], [], []

    if os.path.exists(acct_env):
        kept.append(acct_env)
    else:
        with open(acct_env, "w", encoding="utf-8") as fh:
            fh.write(TEMPLATE.format(
                name=name, grid_url=grid_url,
                token_script=os.path.join(HERE, "get_token.sh"),
                token_file=token_file))
        os.chmod(acct_env, 0o600)
        made.append(acct_env)

    _copy_if_missing(ITSM_SAMPLE, itsm_env, made, kept, missing_src)
    _copy_if_missing(TOKEN_SAMPLE, token_file, made, kept, missing_src)

    section(f"LOCAL SETUP — account '{name}'")
    for p in made:
        log(f"  [new ] {os.path.relpath(p, HERE)}   (blank template, chmod 600)")
    for p in kept:
        log(f"  [kept] {os.path.relpath(p, HERE)}   (already existed — NOT touched)")
    for p in missing_src:
        log(f"  [!!  ] template {os.path.relpath(p, HERE)} is missing — "
            f"could not create its file")

    # Loud check: every secret file must be git-ignored, or we stop.
    leaked = []
    for p in made + kept:
        r = subprocess.run(["git", "check-ignore", "-q", p], cwd=HERE,
                           capture_output=True)
        if r.returncode == 1:
            leaked.append(p)
    if leaked:
        for p in leaked:
            log(f"  [!!  ] {os.path.relpath(p, HERE)} is NOT git-ignored!")
        die("a secrets file could be committed to GitHub. Fix .gitignore "
            "before filling anything in.")
    log("  [ok  ] all three files are git-ignored (they can never be pushed)")

    log("\n  NEXT — fill them in with nano (one at a time):")
    log(f"    nano {os.path.relpath(acct_env, HERE)}      "
        f"-> S247_LOGIN_USER / S247_LOGIN_PASS")
    log(f"    nano {os.path.relpath(itsm_env, HERE)}       "
        f"-> ServiceNow / Desk / SDP / HALO / PagerDuty / TOPdesk")
    log(f"    nano {os.path.relpath(token_file, HERE)}  "
        f"-> client_id / client_secret / refresh_token")
    if missing_src:
        sys.exit(2)


GRIDS_FILE = os.path.join(HERE, "grids.json")


def host_of(url):
    """'https://eum-dev.localsite24x7.com/app' -> 'eum-dev.localsite24x7.com'"""
    u = (url or "").strip()
    if "://" not in u:
        u = "https://" + u
    return (urlparse(u).hostname or "").lower()


def load_grids():
    """Read grids.json. Fails LOUDLY if it is missing or broken -- a run
    must never guess which grid it is pointed at."""
    if not os.path.isfile(GRIDS_FILE):
        die(f"{GRIDS_FILE} not found. It lists the grids you can test.")
    try:
        data = json.load(open(GRIDS_FILE, encoding="utf-8"))
    except ValueError as exc:
        die(f"grids.json is not valid JSON: {exc}")
    grids = data.get("grids") or {}
    if not grids:
        die("grids.json has no grids in it.")
    for name, g in grids.items():
        if not g.get("url"):
            die(f"grids.json: grid {name!r} has no \"url\".")
    return grids


def account_grids(name):
    """Which grids an account may run on. By default an account works on
    EVERY grid in grids.json (the same login and monitors exist on all of
    them). To restrict one, add to its account.env:
        export S247_GRIDS="integrations-qa,eum-dev"
    """
    cfg = read_env_file(os.path.join(ACCOUNTS, name, "account.env"))
    raw = cfg.get("S247_GRIDS", "").strip()
    return [g.strip() for g in raw.split(",") if g.strip()] if raw else None


def accounts_on_grid(accounts, gname):
    out = []
    for a in accounts:
        allowed = account_grids(a)
        if allowed is None or gname in allowed:
            out.append(a)
    return out


def choose_grid(grids, accounts):
    section("WHICH GRID?")
    names = list(grids)
    for i, n in enumerate(names, 1):
        cnt = len(accounts_on_grid(accounts, n))
        note = f"{cnt} account(s)" if cnt else "no account yet"
        log(f"  {i}. {n:<18} {grids[n]['url']:<45} {note}")
    log("")
    while True:
        try:
            pick = input("  Enter a number (or the name): ").strip()
        except (EOFError, KeyboardInterrupt):
            die("no grid chosen. When nobody can answer (Jenkins, cron), "
                "pass it:  --grid <name>")
        if pick in grids:
            return pick
        if pick.isdigit() and 1 <= int(pick) <= len(names):
            return names[int(pick) - 1]
        log("  Not one of the options. Try again.")


def choose_account(accounts):
    section("WHICH ACCOUNT?")
    for i, a in enumerate(accounts, 1):
        cfg = read_env_file(os.path.join(ACCOUNTS, a, "account.env"))
        log(f"  {i}. {a:<12} {cfg.get('S247_LOGIN_USER', '')}")
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


def newest(folder, prefix, suffix):
    try:
        files = [os.path.join(folder, f) for f in os.listdir(folder)
                 if f.startswith(prefix) and f.endswith(suffix)]
        return max(files, key=os.path.getmtime) if files else None
    except OSError:
        return None


def read_json_file(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def build_env(name, gname, grid):
    d = os.path.join(ACCOUNTS, name)
    cfg = read_env_file(os.path.join(d, "account.env"))

    env = dict(os.environ)
    env.update(cfg)
    env.update(read_env_file(os.path.join(d, ".itsm.env")))

    # The CHOSEN grid always wins over any URL written in the account files,
    # so one account can be tested on any grid without editing anything.
    env["S247_GRID_URL"] = grid["url"].rstrip("/")
    env["S247_GRID_NAME"] = gname

    # Everything account-scoped, so two accounts can never overwrite
    # each other's cookie, Chrome profile or reports. The cookie is also
    # per GRID, because a cookie for one grid does not work on another.
    env["S247_ACCOUNT"] = name
    env["S247_SESSION_FILE"] = os.path.join(d, f".session.{gname}.env")
    env["S247_PROFILE_DIR"] = os.path.join(
        os.path.expanduser("~"), f".s247-profile-{name}")
    env.update(read_env_file(env["S247_SESSION_FILE"]))
    env["S247_GRID_URL"] = grid["url"].rstrip("/")
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


def custom_plan():
    """CUSTOM TEST settings (the Jenkins job S247-Integrations-Custom-Test
    sets them; they can also be set by hand before a local run). None set =
    the normal full sanity run, exactly as before.

      S247_TEST_INTEGRATIONS     "Slack Sanity,ServiceNow*"  (empty = all)
      S247_TEST_ALERTS           both | consolidated | events
      S247_TEST_CONS_ROUTES      "trouble,down;down,trouble"  one per monitor
                                 (states: down, trouble)
      S247_TEST_EVENT_ROUTES     "down,trouble,critical;down,critical,trouble"
                                 one per event monitor (down, trouble, critical)
      S247_TEST_ADD_INTEGRATIONS yes | no
      S247_TEST_NOTES            free text shown at the top of the report
    """
    g = lambda k: os.environ.get(k, "").strip()
    p = {"integrations": [n.strip() for n in g("S247_TEST_INTEGRATIONS").split(",") if n.strip()],
         "alerts": (g("S247_TEST_ALERTS") or "both").lower(),
         "cons_routes": g("S247_TEST_CONS_ROUTES").lower().replace(" ", ""),
         "event_routes": g("S247_TEST_EVENT_ROUTES").lower().replace(" ", ""),
         "add": (g("S247_TEST_ADD_INTEGRATIONS") or "yes").lower() not in ("no", "false", "0"),
         "notes": g("S247_TEST_NOTES")}
    p["custom"] = bool(p["integrations"] or p["alerts"] != "both" or p["cons_routes"]
                       or p["event_routes"] or not p["add"] or p["notes"])
    if p["alerts"] not in ("both", "consolidated", "events"):
        die(f"ALERTS must be both, consolidated or events (got {p['alerts']!r})")
    label = {"cons_routes": "CONSOLIDATED_ROUTE", "event_routes": "EVENT_ROUTES"}
    for key, allowed in (("cons_routes", {"down", "trouble"}),
                         ("event_routes", {"down", "trouble", "critical"})):
        for route in filter(None, p[key].split(";")):
            states = route.split(",")
            bad = [s for s in states if s not in allowed]
            if bad or not states:
                die(f"{label[key]}: {route!r} — allowed states are "
                    f"{', '.join(sorted(allowed))} (UP is added at the end "
                    f"automatically)")
            if any(a == b for a, b in zip(states, states[1:])):
                die(f"{label[key]}: {route!r} repeats a state back to back")
    return p


def main():
    ap = argparse.ArgumentParser(
        description="Run the ITSM suite against a chosen account",
        epilog="Unrecognised options are passed through to run_all.py")
    ap.add_argument("--account", "-a", default=None)
    ap.add_argument("--grid", "-g", default=None,
                    help="which grid to test (a name from grids.json). "
                         "Asked interactively when not given.")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--init", metavar="NAME", default=None)
    ap.add_argument("--local-setup", metavar="NAME", default=None,
                    help="prepare THIS machine for an existing account: "
                         "creates blank account.env / .itsm.env / "
                         ".token.<name>.env if missing (never overwrites)")
    ap.add_argument("--check", action="store_true",
                    help="run preflight only, change nothing")
    ap.add_argument("--tools", action="store_true",
                    help="test this account's ITSM tool credentials "
                         "(ServiceNow / Desk / SDP / HALO). Reads no "
                         "tickets, changes nothing.")
    ap.add_argument("--no-add-integrations", dest="add_integrations_off",
                    action="store_true",
                    help="full run WITHOUT deleting and re-adding the "
                         "integrations (debugging only)")
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
    ap.add_argument("--full", action="store_true",
                    help="(default) consolidated + event-based cycles + ONE "
                         "summary report; the event monitor/profile come from "
                         "accounts/<acct>/events.json")
    ap.add_argument("--consolidated-only", action="store_true",
                    help="old behaviour: keyword cycle only, no event-based "
                         "cycle and no summary report")
    ap.add_argument("--itoc360-jenkins-login", action="store_true",
                    help="sign in to iTOC360 once more, as a SEPARATE session "
                         "for Jenkins; saved to accounts/<acct>/"
                         ".itoc360_jenkins_state.json (git-ignored), then "
                         "upload with: python3 jenkins_secrets.py")
    ap.add_argument("--itoc360-login", action="store_true",
                    help="sign in to iTOC360 once in a browser window, so "
                         "the suite can read iTOC360 alerts")
    ap.add_argument("--inventory", action="store_true",
                    help="rebuild this account's monitor inventory "
                         "(test_config.json). Read-only against Site24x7.")
    ap.add_argument("--login-only", action="store_true",
                    help="refresh this account's cookie and stop")
    ap.add_argument("--no-login", action="store_true",
                    help="skip the cookie refresh (use the saved one)")
    args, passthrough = ap.parse_known_args()
    # The full run is the DEFAULT. A consolidated-only run produced the old
    # report for a production sanity on 4 Nov (Jenkins #80), so it now has to
    # be asked for explicitly. An explicit event-only/report-only run through
    # run_all options is left alone.
    if not args.consolidated_only and not any(
            a in passthrough for a in ("--events-monitor", "--report-only",
                                       "--dry-run")):
        args.full = True
    plan = custom_plan()
    if plan["custom"]:
        if plan["cons_routes"] and "--routes" not in passthrough:
            passthrough += ["--routes", plan["cons_routes"]]
        if not plan["add"]:
            args.add_integrations_off = True

    if args.local_setup:
        grids = load_grids()
        gname = args.grid or next(iter(grids))
        if gname not in grids:
            die(f"no such grid {gname!r}. Known: {', '.join(grids)}")
        cmd_local_setup(args.local_setup, grids[gname]["url"])
        return

    if args.init:
        grids = load_grids()
        gname = args.grid or choose_grid(grids, list_accounts())
        if gname not in grids:
            die(f"no such grid {gname!r}. Known: {', '.join(grids)}")
        cmd_init(args.init, grids[gname]["url"])
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
            ag = account_grids(a)
            log(f"     grids     : {', '.join(ag) if ag else 'all grids in grids.json'}")
            log(f"     token file: {cfg.get('TOKEN_ENV_FILE', '(not set)')}")
            for f in (".itsm.env", "allowlist.json"):
                mark = "yes" if os.path.isfile(os.path.join(d, f)) else "NO"
                log(f"     {f:<14}: {mark}")
        return

    # ---- 1) GRID: always decided first, asked when not given -------------
    grids = load_grids()
    gname = args.grid or choose_grid(grids, accounts)
    if gname not in grids:
        die(f"no such grid {gname!r}. Known: {', '.join(grids)}")
    grid = grids[gname]
    on_grid = accounts_on_grid(accounts, gname)

    # ---- 2) ACCOUNT: only accounts that live on that grid -----------------
    if args.account:
        name = args.account
        if name not in accounts:
            die(f"no such account {name!r}. Known: {', '.join(accounts)}")
        if name not in on_grid:
            die(f"account {name!r} is limited to grid(s) "
                f"{', '.join(account_grids(name))} (S247_GRIDS in its account.env), "
                f"NOT {gname}. Nothing was run. Accounts allowed on {gname}: "
                f"{', '.join(on_grid) or 'none'}")
    else:
        if not on_grid:
            die(f"no account is allowed on grid {gname}. Check S247_GRIDS in the "
                f"account.env files, or create one: "
                f"python3 itsm.py --init <name> --grid {gname}")
        if len(on_grid) == 1:
            name = on_grid[0]
            log(f"\n  Only one account on {gname}: using '{name}'.")
        else:
            name = choose_account(on_grid)

    d, env = build_env(name, gname, grid)

    section(f"GRID: {gname}   ACCOUNT: {name}")
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

    if args.itoc360_jenkins_login:
        # Its own browser profile: Jenkins and this Mac must never share one
        # iTOC360 session (each refresh would sign the other one out).
        env["ITOC_PROFILE_DIR"] = os.path.join(
            os.path.expanduser("~"), f".s247-itoc360-profile-{name}-jenkins")
        state = os.path.join(d, ".itoc360_jenkins_state.json")
        rc = run(["node", os.path.join(HERE, "itoc360_login.js"), "--setup",
                  "--export-state", state], env, d, "itoc360_login.js")
        if rc == 0:
            log("\n  Next: upload it to Jenkins:  python3 jenkins_secrets.py")
        sys.exit(rc)

    if args.itoc360_login:
        js = os.path.join(HERE, "itoc360_login.js")
        rc = run(["node", js, "--setup"], env, d, "itoc360_login.js")
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

    # ---- ADD INTEGRATIONS (mandatory in a full run) ----------------------
    # Every covered integration is deleted and ADDED again (DOWN/TROUBLE/
    # CRITICAL, All Monitors, hook URL / key, credential profile or Zoho
    # connection), then read back and compared. The validation below then
    # runs on these freshly added integrations. Result: add_integrations.json
    # (shown in the report). A failed add never stops the run; it is
    # reported. Accounts without integration_templates.json are skipped.
    # ---- HEALTH CHECKS (every full run, ~10 s, never stops the run) -----
    # Can monitor edits be saved on this build? Is ServiceNow awake? Shown at
    # the top of the report (9 Nov: a build that silently dropped monitor
    # edits was reported as "usually login, cookie or credentials").
    hc_file = os.path.join(d, "health_checks.json")
    if os.path.isfile(hc_file):
        os.remove(hc_file)
    if args.full:
        section(f"HEALTH CHECKS — {name}")
        run([sys.executable, os.path.join(HERE, "health_checks.py")], env, d,
            "health_checks.py")

    add_res = os.path.join(d, "add_integrations.json")
    if os.path.isfile(add_res):
        os.remove(add_res)          # never show an older run's result
    plan_file = os.path.join(d, "test_plan.json")
    if os.path.isfile(plan_file):
        os.remove(plan_file)
    if plan["custom"]:
        rec = {k: v for k, v in plan.items() if k != "custom"}
        rec["job"] = os.environ.get("JOB_NAME") or "local run"
        rec["build"] = os.environ.get("BUILD_NUMBER") or ""
        with open(plan_file, "w", encoding="utf-8") as fh:
            json.dump(rec, fh, indent=2)
        section(f"CUSTOM TEST — {name}")
        log(f"  integrations : {', '.join(plan['integrations']) or 'all covered'}")
        log(f"  alerts       : {plan['alerts']}")
        log(f"  consolidated : {plan['cons_routes'] or 'default routes'}")
        log(f"  event-based  : {plan['event_routes'] or 'default routes'}")
        log(f"  add first    : {'yes' if plan['add'] else 'no'}")
        if plan["notes"]:
            log(f"  notes        : {plan['notes']}")
    if args.full and not args.add_integrations_off:
        setup = os.path.join(HERE, "integration_setup.py")
        if os.path.isfile(os.path.join(d, "integration_templates.json")):
            section(f"ADD INTEGRATIONS — {name}")
            rc = run([sys.executable, setup, "-a", name,
                      "--grid", env["S247_GRID_NAME"]], env, HERE,
                     "integration_setup.py")
            if rc != 0:
                log("\n  [WARN] not every integration was added and verified;"
                    " the report shows which one and why.")

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
    # Hand run_all.py the EXACT paths it needs, so it never has to guess:
    #   S247_LOGIN_JS          -> mid-run re-login after someone logs out
    #   S247_INTEGRATIONS_FILE -> THIS account's list for the Stage 0 test
    #   S247_NO_AUTO_OPEN      -> itsm.py opens the report once, at the end
    env["S247_LOGIN_JS"] = os.path.abspath(LOGIN_JS)
    env["S247_INTEGRATIONS_FILE"] = os.path.join(d, "integrations.json")
    env["S247_NO_AUTO_OPEN"] = "1"
    # --full: the event-based cycle drives a DIFFERENT monitor, so it runs
    # AT THE SAME TIME as the consolidated run (cut ~45 min to ~20 min).
    # Only the cycle runs in parallel; its verification runs afterwards, so
    # the two never share the working files.
    ev, ev_proc, ev_log = None, None, None
    if args.full:
        ev_file = os.path.join(d, "events.json")
        ev = read_json_file(ev_file)
        # events.json: {"monitors": [{monitor_id, profile_id, route}, ...]}
        # (one route per monitor, run in parallel); the older single-monitor
        # form {monitor_id, profile_id} still works (routes one after another).
        if ev and not ev.get("monitors") and ev.get("monitor_id"):
            ev["monitors"] = [{"monitor_id": ev["monitor_id"], "profile_id": ev.get("profile_id"),
                               "monitor_name": ev.get("monitor_name"), "route": None}]
        if ev and any(not m.get("monitor_id") or not m.get("profile_id")
                      for m in ev.get("monitors") or []):
            ev = None
        # Custom test: overrides first, then the normal start below.
        # (FIX 6 Nov: these were in the same if/elif chain as the start, so
        # giving EVENT_ROUTES skipped starting the event cycle entirely.)
        if ev and plan["alerts"] == "consolidated":
            log("\n  [CUSTOM] consolidated alerts only — event-based cycle not run.")
            ev = None
        if ev and ev.get("monitors") and plan["event_routes"]:
            routes = [r for r in plan["event_routes"].split(";") if r]
            if len(routes) > len(ev["monitors"]):
                die(f"{len(routes)} event route(s) given but this account has only "
                    f"{len(ev['monitors'])} event monitor(s)")
            ev["monitors"] = [dict(m, route=r) for m, r in zip(ev["monitors"], routes)]
        if plan["alerts"] == "consolidated":
            pass                    # already said so above
        elif not ev or not ev.get("monitors"):
            log(f"\n  [WARN] {ev_file} missing or incomplete — the event-based "
                f"cycle is SKIPPED and the summary will say so.")
            ev = None
        else:
            os.makedirs(os.path.join(d, "reports"), exist_ok=True)
            ev_log = os.path.join(d, "reports", "events_cycle_"
                                  + time.strftime("%Y%m%d_%H%M%S") + ".log")
            section(f"EVENT-BASED CYCLE STARTED IN PARALLEL — {name}")
            mons = ev["monitors"]
            for m in mons:
                log(f"  monitor {m.get('monitor_name') or m['monitor_id']} · "
                    f"route {m.get('route') or 'down,trouble,critical then down,critical,trouble'}")
            log(f"  log: {ev_log}")
            if all(m.get("route") for m in mons):
                cyc = [a for m in mons for a in
                       ("--pair", f"{m['monitor_id']}:{m['profile_id']}:{m['route']}")]
            else:
                cyc = ["--monitor-id", str(mons[0]["monitor_id"]),
                       "--profile-id", str(mons[0]["profile_id"])]
            ev_proc = subprocess.Popen(
                [sys.executable, os.path.join(HERE, "stage2c_events.py")] + cyc,
                env=env, cwd=d, stdout=open(ev_log, "w"),
                stderr=subprocess.STDOUT)

    events_only = args.full and plan["alerts"] == "events"
    if events_only:
        log("\n  [CUSTOM] event-based alerts only — consolidated cycle not run.")
        rc, cons_json = 0, None
    else:
        section(f"RUNNING THE SUITE — {name}")
        rc = run([sys.executable, os.path.join(HERE, "run_all.py")] + passthrough,
                 env, d, "run_all.py")
        cons_json = newest(os.path.join(d, "reports"), "report_2", ".json")

    if args.full:
        evts_json = None
        if ev_proc is not None:
            section(f"WAITING FOR THE EVENT-BASED CYCLE — {name}")
            ev_rc = ev_proc.wait()
            with open(ev_log, encoding="utf-8") as fh:
                for line in fh:
                    if "[ OK ]" in line or "[FAIL]" in line or "VERDICT" in line:
                        log(line.rstrip())
            section(f"VERIFYING THE EVENT-BASED CYCLE — {name}")
            rc2 = run([sys.executable, os.path.join(HERE, "run_all.py")]
                      + [a for a in passthrough if a != "--skip-trigger-test"]
                      + ["--events-monitor", str(ev["monitors"][0]["monitor_id"]),
                         "--events-profile", str(ev["monitors"][0]["profile_id"]),
                         "--skip-cycle"]
                      + ([] if events_only else ["--skip-trigger-test"]),
                      env, d, "run_all.py --events")
            rc = max(rc, rc2, 1 if ev_rc else 0)
            evts_json = newest(os.path.join(d, "reports"), "events_report_2", ".json")
        section(f"ONE SUMMARY REPORT — {name}")
        argv = [sys.executable, os.path.join(HERE, "summary_report.py"),
                "--account", name]
        if cons_json:
            argv += ["--consolidated", cons_json]
        if evts_json:
            argv += ["--events", evts_json]
        rc_sum = run(argv, env, d, "summary_report.py")
        # A summary report was written: the build result follows IT.
        #   PASS -> keep rc (0)   not PASS -> 1 (Jenkins UNSTABLE + report)
        # Exit 2 ("could not test", Jenkins FAILURE "usually login, cookie or
        # credentials") is kept only for runs that produced no report. 9 Nov:
        # a build that did not save monitor edits showed that misleading text.
        if rc_sum == 1:
            rc = 1

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
        # Auto-open the report in the default browser. macOS uses 'open',
        # Linux desktops use 'xdg-open'. The Jenkins server has neither a
        # desktop nor a browser, so we say so instead of claiming success.
        opener = "open" if sys.platform == "darwin" else "xdg-open"
        # Only when a person started this run in a terminal. Background and
        # scripted runs popped report tabs nobody asked for (3 Nov).
        if not sys.stdin.isatty() or os.environ.get("S247_NO_AUTO_OPEN"):
            log("\n  (Report not auto-opened: this run was not started "
                "from a terminal. Use the link above.)")
        elif shutil.which(opener):
            try:
                subprocess.Popen([opener, latest_abs],
                                 stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
                log(f"\n  ✅  Report auto-opened in your browser.")
            except OSError as exc:
                log(f"\n  (Could not auto-open: {exc} — open the link above.)")
        else:
            log(f"\n  (No '{opener}' command on this machine — open the "
                f"link above.)")
    except (OSError, ValueError):
        pass
    log(f"  exit code {rc}  (0 = build OK, 1 = defect, 2 = blocked)")
    sys.exit(rc)


if __name__ == "__main__":
    main()
