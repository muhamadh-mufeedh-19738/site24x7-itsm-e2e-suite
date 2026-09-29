#!/usr/bin/env python3
"""
Site24x7 ITSM Automation — PREFLIGHT
====================================

Run this FIRST whenever you point the suite at a different account, and any
time a run behaves oddly. It answers, in about five seconds, the questions
that otherwise cost you a twenty-minute run to discover:

    1. Is the token valid, and WHICH account does it belong to?
    2. How many monitors are in this account?  (the blast-radius question)
    3. Is there an allowlist, and does every entry in it match something?
    4. Are the ITSM integrations configured in THIS account?
    5. Are the pollers actually keeping up?   <-- the 20-minute killer
    6. Is the session cookie alive for Alert Logs?

It changes NOTHING. No PUTs, no monitor edits, no writes except an
allowlist file you explicitly ask for with --write-allowlist.

USAGE
    source start.sh
    python3 preflight.py

    # build a starter allowlist from a name pattern (case-insensitive)
    python3 preflight.py --write-allowlist "AUTOMATION*"

EXIT CODES
    0  ready to run
    2  not ready - something above must be fixed first
"""

import argparse
import fnmatch
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

CONFIG_FILE = "test_config.json"
ALLOWLIST_FILE = "allowlist.json"

# An account with more monitors than this MUST have an allowlist before any
# script is allowed to modify anything. 2000 monitors and a stray default
# would be a very bad afternoon.
UNGUARDED_MAX = 10

# A monitor whose last poll is older than this is not going to respond to a
# keyword change in any reasonable time.
STALE_POLL_MINUTES = 5

STATUS = {0: "DOWN", 1: "UP", 2: "TROUBLE", 3: "CRITICAL", 5: "SUSPENDED",
          7: "MAINTENANCE", 9: "DISCOVERING", 10: "CONFIG ERROR"}

problems = []
warnings_ = []


def log(m=""):
    print(m, flush=True)


def section(t):
    log("\n" + "=" * 70)
    log(t)
    log("=" * 70)


def ok(name, detail=""):
    log(f"  [OK ] {name}" + (f"  — {detail}" if detail else ""))


def bad(name, detail="", fatal=True):
    log(f"  [{'!!' if fatal else 'WARN'}] {name}" + (f"  — {detail}" if detail else ""))
    (problems if fatal else warnings_).append(f"{name}: {detail}")


def get_token():
    script = os.environ.get("S247_TOKEN_SCRIPT", "").strip()
    if not script or not os.path.isfile(script):
        return None, f"S247_TOKEN_SCRIPT not set or missing ({script!r})"

    env_file = os.environ.get("TOKEN_ENV_FILE")
    if env_file:
        log(f"  token env file : {env_file}")
        if not os.path.isfile(env_file):
            return None, (f"TOKEN_ENV_FILE points at {env_file} which does "
                          f"not exist. Wrong account file, or a typo in "
                          f"env.big.sh.")
    else:
        log("  token env file : (not set - the token script will use its "
            "own default, which is the OLD account)")

    started = datetime.now()
    try:
        # stdin closed: a script that ever waits for input fails fast instead
        # of hanging forever. Longer timeout, because a slow network is not
        # the same thing as a broken script.
        out = subprocess.run(["bash", script], capture_output=True, text=True,
                             timeout=90, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return None, (
            "the token script HUNG for 90s and was killed. It did not fail, "
            "it never answered. That is almost always the network: the script "
            "calls curl with no time limit, so if the accounts server is "
            "unreachable curl waits forever.\n"
            "         Check it directly:\n"
            "           curl -s -o /dev/null -w 'HTTP %{http_code} in "
            "%{time_total}s\\n' --max-time 15 https://accounts.localzoho.com\n"
            "         If that hangs too, reconnect your VPN and retry.")
    except Exception as exc:  # noqa: BLE001
        return None, f"token script failed to start: {exc}"

    secs = (datetime.now() - started).total_seconds()
    if secs > 10:
        log(f"  [WARN] the token script took {secs:.0f}s. Healthy is under "
            f"3s — the network is slow, expect sluggish runs.")

    if out.returncode != 0:
        detail = (out.stderr or out.stdout or "").strip()
        return None, (f"token script exited with code {out.returncode}. "
                      f"It said: {detail[:400] or '(nothing)'}")
    tok = ""
    for line in (out.stdout or "").splitlines():
        line = line.strip()
        if line.startswith("export "):
            line = line.split("=", 1)[-1].strip().strip('"').strip("'")
        if len(line) > 20 and " " not in line:
            tok = line
    if not tok:
        return None, f"no token in script output: {(out.stdout or out.stderr)[:200]}"
    return tok, None


def api(grid, path, token, timeout=30):
    req = urllib.request.Request(
        grid.rstrip("/") + path,
        headers={"Authorization": f"Zoho-oauthtoken {token}",
                 "Accept": "application/json; version=2.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8") or "{}")
        except Exception:  # noqa: BLE001
            return e.code, None
    except Exception as exc:  # noqa: BLE001
        return None, {"_error": str(exc)}


def parse_when(value):
    """Site24x7 hands back timestamps in several shapes. Try them all, and
    say so honestly when none fit rather than pretending it is fresh."""
    if value in (None, "", "-"):
        return None
    s = str(value).strip()
    if s.isdigit():                       # epoch millis or seconds
        n = int(s)
        if n > 1e11:
            n /= 1000.0
        try:
            return datetime.fromtimestamp(n, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%dT%H:%M:%S", "%B %d, %Y %I:%M %p",
                "%d-%m-%Y %H:%M:%S"):
        try:
            dt = datetime.strptime(s, fmt)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def find_poll_field(mon):
    """Return (field_name, value) for whatever this build calls 'last polled'."""
    for key in ("last_polled_time", "lastpolledtime", "last_polled",
                "polled_time", "last_checked_time"):
        if key in mon:
            return key, mon[key]
    for key in mon:
        if "poll" in key.lower():
            return key, mon[key]
    return None, None


def flatten_monitors(payload):
    blob = (payload or {}).get("data", {}) or {}
    out = list(blob.get("monitors") or [])
    for grp in (blob.get("monitor_groups") or []):
        out.extend(grp.get("monitors") or [])
    return out


def load_allowlist():
    if not os.path.isfile(ALLOWLIST_FILE):
        return None
    try:
        with open(ALLOWLIST_FILE, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception as exc:  # noqa: BLE001
        bad(f"{ALLOWLIST_FILE} is not valid JSON", str(exc))
        return {}


def resolve_allowlist(allow, monitors):
    """Return (allowed_monitors, unmatched_entries). Names support wildcards."""
    ids = {str(i) for i in (allow.get("monitor_ids") or [])}
    pats = [str(p) for p in (allow.get("monitor_names") or [])]
    allowed, matched_pats, matched_ids = [], set(), set()
    for m in monitors:
        mid = str(m.get("monitor_id"))
        name = str(m.get("display_name") or m.get("name") or "")
        if mid in ids:
            allowed.append(m)
            matched_ids.add(mid)
            continue
        for p in pats:
            if fnmatch.fnmatch(name.lower(), p.lower()):
                allowed.append(m)
                matched_pats.add(p)
                break
    unmatched = ([i for i in ids if i not in matched_ids]
                 + [p for p in pats if p not in matched_pats])
    return allowed, unmatched


def main():
    ap = argparse.ArgumentParser(
        description="Check an account is ready before running the suite")
    ap.add_argument("--write-allowlist", metavar="PATTERN", default=None,
                    help="create allowlist.json from a monitor-name pattern, "
                         "e.g. 'AUTOMATION*'. Shows what it matched and asks "
                         "before writing.")
    ap.add_argument("--stale-minutes", type=int, default=STALE_POLL_MINUTES)
    args = ap.parse_args()

    section("PREFLIGHT — IS THIS ACCOUNT READY?")

    grid = os.environ.get("S247_GRID_URL", "").strip()
    if not grid:
        bad("S247_GRID_URL not set", "run: source start.sh")
        log("\n  Nothing else can be checked without it.")
        sys.exit(2)
    ok("grid", grid)

    token, err = get_token()
    if not token:
        bad("OAuth token", err)
        log("\n  Fix the token first; every other check needs it.")
        sys.exit(2)
    ok("OAuth token", f"obtained, {len(token)} chars")
    if not os.environ.get("TOKEN_ENV_FILE"):
        bad("TOKEN_ENV_FILE is not set",
            "you are on the OLD account. For the new account run: "
            "source env.big.sh   (never start.sh)")

    # ---- 1. account identity + blast radius ------------------------------
    section("1. ACCOUNT AND BLAST RADIUS")
    code, payload = api(grid, "/api/monitors", token)
    if code != 200:
        bad("could not list monitors", f"status={code} {str(payload)[:200]}")
        sys.exit(2)
    all_mons = (payload or {}).get("data") or []
    log(f"  monitors visible to this token : {len(all_mons)}")

    by_type = {}
    for m in all_mons:
        by_type[m.get("type", "?")] = by_type.get(m.get("type", "?"), 0) + 1
    for t, n in sorted(by_type.items(), key=lambda kv: -kv[1]):
        log(f"      {t:<14} {n}")

    if len(all_mons) > UNGUARDED_MAX:
        log(f"\n  This account has more than {UNGUARDED_MAX} monitors, so an "
            f"allowlist is MANDATORY.")

    # ---- 2. allowlist ----------------------------------------------------
    section("2. ALLOWLIST — WHAT MAY BE TOUCHED")
    allow = load_allowlist()

    if args.write_allowlist:
        pat = args.write_allowlist
        hits = [m for m in all_mons
                if fnmatch.fnmatch(str(m.get("display_name", "")).lower(),
                                   pat.lower())]
        log(f"  pattern {pat!r} matches {len(hits)} monitor(s):")
        for m in hits:
            log(f"      {m.get('display_name')}  id={m.get('monitor_id')}  "
                f"type={m.get('type')}")
        if not hits:
            bad("pattern matched nothing", "nothing written")
            sys.exit(2)
        log("")
        if input("  Write these to allowlist.json? type 'yes': ").strip().lower() == "yes":
            doc = {"_comment": "ONLY these monitors may ever be modified by "
                               "the automation. Names support * wildcards.",
                   "monitor_names": [pat],
                   "monitor_ids": [str(m.get("monitor_id")) for m in hits]}
            with open(ALLOWLIST_FILE, "w", encoding="utf-8") as fh:
                json.dump(doc, fh, indent=2)
            ok(f"wrote {ALLOWLIST_FILE}", f"{len(hits)} monitor(s)")
            allow = doc
        else:
            log("  Not written.")

    if allow is None:
        if len(all_mons) > UNGUARDED_MAX:
            bad(f"no {ALLOWLIST_FILE}",
                f"this account has {len(all_mons)} monitors and NOTHING is "
                f"protecting them. Create one with: "
                f"python3 preflight.py --write-allowlist 'AUTOMATION*'")
            allowed = []
        else:
            bad(f"no {ALLOWLIST_FILE}",
                f"small account ({len(all_mons)} monitors), so this is "
                f"allowed — but an allowlist is still the safer habit",
                fatal=False)
            allowed = all_mons
    else:
        allowed, unmatched = resolve_allowlist(allow, all_mons)
        if unmatched:
            bad("allowlist entries that match NOTHING", ", ".join(map(str, unmatched)))
            log("        An entry matching nothing usually means a typo or a "
                "monitor that was renamed.")
        if not allowed:
            bad("allowlist matches no monitors at all", "nothing can run")
        else:
            ok(f"allowlist resolves to {len(allowed)} monitor(s)")
            for m in allowed:
                log(f"      {m.get('display_name')}  id={m.get('monitor_id')}"
                    f"  type={m.get('type')}")

    # ---- 3. integrations in THIS account ---------------------------------
    section("3. ITSM INTEGRATIONS IN THIS ACCOUNT")
    integrations = []
    if os.path.isfile(CONFIG_FILE):
        try:
            cfg = json.load(open(CONFIG_FILE, encoding="utf-8"))
            integrations = (cfg.get("integrations")
                            or cfg.get("third_party_integrations") or [])
        except Exception as exc:  # noqa: BLE001
            bad(f"could not read {CONFIG_FILE}", str(exc), fatal=False)
    if not integrations:
        # NOT a blocker, and NOT a statement about the account. This reads a
        # local cache file. Site24x7 has no working API endpoint for the
        # integration list on this build, so stage1 cannot populate it. The
        # suite does not need it: stage3 reads integration names straight
        # out of the Alert Logs, and stage4 logs into the tools directly.
        log("  Could not read an integration list from the local "
            f"{CONFIG_FILE} cache.")
        log("  This says NOTHING about your account — the list simply is not")
        log("  available through the API on this build, so nothing filled it "
            "in.")
        log("")
        log("  It is not needed. Integration names are discovered from the")
        log("  Alert Logs at verification time, and stage 4 logs into each")
        log("  ITSM tool directly using .itsm.env.")
        log("")
        ok("integrations", "not required for the run — continuing")
    else:
        for i in integrations:
            nm = i.get("name") or i.get("integration_name") or str(i)
            log(f"      {nm}")
        ok(f"{len(integrations)} integration(s) configured")
        log("\n  Reminder: the monitors you drive must be attached to a "
            "notification")
        log("  profile that routes to these integrations, or they will alert "
            "into")
        log("  the void and the report will look like a defect.")

    # ---- 4. poller freshness --------------------------------------------
    section("4. POLLER FRESHNESS  (the thing that made runs take 20 minutes)")
    code, payload = api(grid, "/api/current_status", token)
    live = flatten_monitors(payload) if code == 200 else []
    if not live:
        bad("could not read /api/current_status", f"status={code}", fatal=False)
    else:
        check_ids = ({str(m.get("monitor_id")) for m in allowed}
                     if allowed else None)
        shown = 0
        unparsed = 0
        for m in live:
            mid = str(m.get("monitor_id"))
            if check_ids is not None and mid not in check_ids:
                continue
            shown += 1
            field, raw = find_poll_field(m)
            when = parse_when(raw)
            st = STATUS.get(m.get("status"), m.get("status"))
            name = m.get("display_name") or m.get("name") or mid
            if when is None:
                unparsed += 1
                log(f"      {name:<28} status={st:<10} "
                    f"last poll: UNKNOWN (field={field}, raw={raw!r})")
                continue
            age = (datetime.now(timezone.utc) - when).total_seconds() / 60.0
            flag = "OK " if age <= args.stale_minutes else "!! "
            log(f"      [{flag}] {name:<24} status={st:<10} "
                f"last polled {age:.0f} min ago")
            if age > args.stale_minutes:
                bad(f"{name} poller is {age:.0f} min behind",
                    f"a keyword change will not take effect for roughly that "
                    f"long. Driving state changes here will be painfully slow.",
                    fatal=False)
        if shown == 0:
            bad("none of the allowlisted monitors appear in current_status",
                "wrong account, or the ids are stale", fatal=False)
        if unparsed and unparsed == shown:
            bad("could not read a last-polled time for ANY monitor",
                "this build names the field differently — tell me the field "
                "name from the raw output above and I will add it",
                fatal=False)

    # ---- 5. session cookie ----------------------------------------------
    section("5. SESSION COOKIE (Alert Logs)")
    if os.environ.get("S247_COOKIE") or os.environ.get("S247_SESSION_COOKIE"):
        ok("session cookie present in the environment")
        log("      Cookies are per-account. If you just switched accounts, "
            "refresh it:")
        log("        python3 extract_cookie.py   (while logged into THIS "
            "account)")
    else:
        bad("no session cookie in the environment",
            "stage 3 (Alert Logs) will fail. Run: python3 extract_cookie.py "
            "then source .session.env", fatal=False)
    log("\n  For the full credential picture: python3 check_session.py")

    # ---- verdict ---------------------------------------------------------
    section("PREFLIGHT VERDICT")
    if problems:
        log(f"  NOT READY — {len(problems)} blocker(s):")
        for p in problems:
            log(f"    - {p}")
        if warnings_:
            log(f"\n  Plus {len(warnings_)} warning(s):")
            for w in warnings_:
                log(f"    - {w}")
        log("\n  Fix the blockers, then run preflight again.")
        sys.exit(2)

    if warnings_:
        log(f"  READY, with {len(warnings_)} warning(s):")
        for w in warnings_:
            log(f"    - {w}")
        log("\n  You can run, but read those first — especially anything "
            "about")
        log("  poller lag, which shows up as very long waits.")
    else:
        log("  READY — account, allowlist, integrations and pollers all look "
            "sane.")
    log("\n  Next:  python3 stage2b_cycle.py --dry-run")
    sys.exit(0)


if __name__ == "__main__":
    main()
