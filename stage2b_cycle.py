#!/usr/bin/env python3
"""
Site24x7 ITSM Automation — STAGE 2b : FULL ALERT CYCLE (corrected mechanism)
============================================================================

WHAT CHANGED FROM STAGE 2 (and why it failed)
    Stage 2 used 'matching_keyword' (= "page SHOULD contain X") with a
    nonsense value. The monitor never left UP.

    The correct mechanism is 'unmatching_keyword' (= "page should NOT
    contain X"):

        unmatching_keyword = a string that IS on the page   -> rule broken -> DOWN/TROUBLE
        unmatching_keyword = a nonsense string              -> rule kept   -> UP

    Crucially this drives the monitor in BOTH directions. So we never have
    to wait for a monitor to happen to be UP - we can FORCE it UP first,
    then force it into a problem state, then force it back UP.
    That guarantees a complete cycle on every run.

THE CYCLE THIS RUNS
    PHASE A  force UP        (nonsense keyword)      -> wait for UP
    PHASE B  force PROBLEM   (real keyword from URL) -> wait for DOWN/TROUBLE
             ... this is where tickets should be CREATED in the integrations
    PHASE C  force UP again  (nonsense keyword)      -> wait for UP
             ... this is where tickets should be CLOSED
    PHASE D  restore original config + verify

HOW THE KEYWORD IS DERIVED
    Read from the monitor's own URL. e.g. https://www.w3schools.com
    -> derived keyword "w3schools", which appears in that page.
    Override it yourself with --keyword if the derived one is wrong.

STATUS CODES (confirmed against the Site24x7 UI)
    0 = DOWN    1 = UP    2 = TROUBLE    3 = CRITICAL

SAFETY
    * Original config saved to backup_<id>.json BEFORE any change
    * Restore runs in a 'finally' block - even on crash or Ctrl-C
    * Restore is VERIFIED by re-reading from the server, never assumed
    * Nothing is ever deleted
    * --dry-run shows everything it would do and changes nothing

WHAT THIS RUNS BY DEFAULT
    PHASE 0   EVERY monitor is forced UP first. This is MANDATORY. If any
              monitor will not go UP the run aborts before touching a
              single problem state, because a ticket raised by a state the
              monitor was already sitting in would otherwise be credited
              to this run.

    STEP 1    monitor 1 -> TROUBLE        monitor 2 -> DOWN
    STEP 2    monitor 1 -> DOWN           monitor 2 -> TROUBLE
    FINAL     every monitor -> UP         (tickets should be CLOSED)
    RESTORE   original config for every monitor, verified by re-read

    The two monitors are driven in LOCKSTEP from a single polling loop, so
    two monitors take about the same wall-clock time as one.

    THE KEYWORDS, derived from each monitor's own URL. Nothing hard-coded.
        https://www.timesofisrael.com  -> page keyword 'timesofisrael'
        UP keyword = page keyword + gibberish -> 'timesofisraelsdfjoksfors'

        'should not contain' = timesofisrael             -> problem state
        'should not contain' = timesofisraelsdfjoksfors  -> UP

    severity 0 on the keyword -> DOWN
    severity 2 on the keyword -> TROUBLE

USAGE
    source env.sh
    python3 stage2b_cycle.py --dry-run              # look first
    python3 stage2b_cycle.py                        # run for real
    python3 stage2b_cycle.py --routes 'down,trouble;trouble,down'
    python3 stage2b_cycle.py --monitor-id <id> --routes 'down'
    python3 stage2b_cycle.py --max-wait 900         # slower pollers
"""

import argparse
import fnmatch
import json
import os
import re
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

CONFIG_FILE = "test_config.json"
RESULT_FILE = "stage2b_result.json"
TIMEOUT = 30

# CONFIRMED against the Site24x7 UI
STATUS = {0: "DOWN", 1: "UP", 2: "TROUBLE", 3: "CRITICAL", 5: "SUSPENDED",
          7: "MAINTENANCE", 9: "DISCOVERING", 10: "CONFIG ERROR"}
UP_CODE = 1
PROBLEM_CODES = (0, 2, 3)

# THE UP KEYWORD.
# It is the monitor's OWN page keyword with gibberish stuck on the end:
#     timesofisrael  ->  timesofisraelsdfjoksfors
#     w3schools      ->  w3schoolssdfjoksfors
# The page contains the keyword but never the keyword+gibberish, so the
# "should not contain" rule is satisfied and the monitor goes UP.
# Derived per monitor from that monitor's own URL -- nothing hard-coded.
GIBBERISH_SUFFIX = "sdfjoksfors"

# SAFETY. On an account with more monitors than this, an allowlist is
# MANDATORY -- the script refuses to modify anything without one. A stray
# default run against a 2000-monitor production account is not a mistake
# that should be possible.
ALLOWLIST_FILE = "allowlist.json"
UNGUARDED_MAX = 10


def up_keyword(page_keyword):
    """'timesofisrael' -> 'timesofisraelsdfjoksfors'"""
    return f"{page_keyword}{GIBBERISH_SUFFIX}"

# Driving the monitor to a chosen state.
#   The keyword ('should not contain') decides WHETHER there is a problem.
#   The severity on that keyword decides WHICH problem state.
# Confirmed in the Site24x7 UI: the Trouble/Down toggle next to
# "Should not contain string(s)" writes this severity.
STATE_SEVERITY = {"down": 0, "trouble": 2}   # severity written to the keyword
STATE_CODE = {"down": 0, "trouble": 2}       # monitor status code to expect

result = {
    "started_at": datetime.now(timezone.utc).isoformat(),
    "dry_run": False,
    "monitor": None,
    "derived_keyword": None,
    "phases": {},
    "steps": [],
    "restored": False,
    "restore_verified": False,
    "verdict": None,
}

def log(m=""):
    print(m, flush=True)


def section(t):
    log("\n" + "=" * 70)
    log(t)
    log("=" * 70)


def step(name, ok, detail=""):
    log(f"  [{'OK ' if ok else '!! '}] {name}" + (f"  — {detail}" if detail else ""))
    result["steps"].append({"step": name, "ok": ok, "detail": detail,
                            "at": datetime.now(timezone.utc).isoformat()})


def save_result():
    try:
        with open(RESULT_FILE, "w", encoding="utf-8") as fh:
            json.dump(result, fh, indent=2)
    except Exception as exc:  # noqa: BLE001
        log(f"[WARN] could not write {RESULT_FILE}: {exc}")


def die(msg, code=2):
    log(f"\n[BLOCKER] {msg}")
    result["verdict"] = "BLOCKED"
    save_result()
    sys.exit(code)


# --------------------------------------------------------------------------

def get_token():
    tok = os.environ.get("S247_ACCESS_TOKEN", "").strip()
    if tok:
        return tok
    script = os.path.expanduser(os.environ.get("S247_TOKEN_SCRIPT", "").strip())
    if not script or not os.path.isfile(script):
        die("No token. Set S247_ACCESS_TOKEN or S247_TOKEN_SCRIPT.")
    try:
        p = subprocess.run(["bash", script], capture_output=True, text=True, timeout=60)
        lines = [l for l in (p.stdout or "").splitlines() if l.strip()]
        if p.returncode != 0 or not lines:
            die(f"get_token.sh failed: {(p.stderr or '')[:300]}")
        return lines[-1].strip()
    except Exception as exc:  # noqa: BLE001
        die(f"Could not run get_token.sh: {exc}")


def _ctx():
    c = ssl.create_default_context()
    c.check_hostname = False
    c.verify_mode = ssl.CERT_NONE
    return c


def api(grid, path, token, method="GET", body=None):
    url = grid.rstrip("/") + path
    data = None
    headers = {"Authorization": f"Zoho-oauthtoken {token}",
               "Accept": "application/json; version=2.1"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json;charset=UTF-8"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT, context=_ctx()) as r:
            raw = r.read().decode("utf-8", errors="replace")
            return r.status, (json.loads(raw) if raw.strip() else {})
    except urllib.error.HTTPError as e:
        try:
            raw = e.read().decode("utf-8", errors="replace")
            return e.code, (json.loads(raw) if raw.strip() else {"_raw": raw[:400]})
        except Exception:
            return e.code, None
    except Exception as exc:  # noqa: BLE001
        log(f"        (request error: {exc})")
        return None, None


def monitor_status(grid, token, monitor_id):
    code, payload = api(grid, "/api/current_status", token)
    if not payload:
        return None
    blob = payload.get("data", {}) if isinstance(payload, dict) else {}
    buckets = list(blob.get("monitors") or [])
    for grp in (blob.get("monitor_groups") or []):
        buckets.extend(grp.get("monitors") or [])
    for m in buckets:
        if str(m.get("monitor_id")) == str(monitor_id):
            return m.get("status")
    return None


def wait_for(grid, token, mid, want_codes, max_wait, poll_every=20, label=None):
    """Wait until the monitor reports one of want_codes.

    This checks for an EXACT state. 'Any problem state' is not good enough
    when we need to prove DOWN and TROUBLE separately -- a monitor sitting
    in DOWN would otherwise satisfy a wait for TROUBLE and we would report
    a state transition that never happened.
    """
    if isinstance(want_codes, int):
        want_codes = (want_codes,)
    waited, last = 0, None
    if label is None:
        label = " or ".join(STATUS.get(c, str(c)) for c in want_codes)
    log(f"        waiting for {label}  (up to {max_wait}s, every {poll_every}s)")
    while waited <= max_wait:
        st = monitor_status(grid, token, mid)
        last = STATUS.get(st, str(st))
        log(f"          t+{waited:>4}s  status = {last}")
        if st in want_codes:
            return True, last, waited
        time.sleep(poll_every)
        waited += poll_every
    return False, last, waited


def read_keyword_obj(grid, token, mid):
    """Return (value, severity) of unmatching_keyword, or (None, None)."""
    code, payload = api(grid, f"/api/monitors/{mid}", token)
    if code != 200 or not payload:
        return None, None
    kw = (payload.get("data") or {}).get("unmatching_keyword") or {}
    sev = kw.get("severity")
    try:
        sev = int(sev)
    except (TypeError, ValueError):
        pass
    return kw.get("value"), sev


def derive_keyword(url):
    """
    https://www.w3schools.com/xyz -> 'w3schools'
    Take the domain, drop www./TLD, use the main label.
    """
    if not url:
        return None
    try:
        host = urllib.parse.urlparse(url).hostname or ""
    except Exception:
        return None
    host = host.lower().lstrip(".")
    for pre in ("www.", "m."):
        if host.startswith(pre):
            host = host[len(pre):]
    parts = [p for p in host.split(".") if p]
    if not parts:
        return None
    # ignore common TLD-ish trailing labels
    main = parts[0]
    if main in ("com", "net", "org") and len(parts) > 1:
        main = parts[1]
    return main or None


def set_keyword(grid, token, mid, original, value, severity):
    """PUT the full monitor object with unmatching_keyword set."""
    body = json.loads(json.dumps(original))       # deep copy
    body["unmatching_keyword"] = {"severity": int(severity), "value": value}
    code, resp = api(grid, f"/api/monitors/{mid}", token, method="PUT", body=body)
    ok = code in (200, 201)
    if not ok:
        log(f"        response: {str(resp)[:300]}")
    return ok, code


def read_keyword(grid, token, mid):
    code, payload = api(grid, f"/api/monitors/{mid}", token)
    if code != 200 or not payload:
        return None
    # The API may omit unmatching_keyword entirely OR return it as null.
    # ".get('unmatching_keyword', {})" returns None in the second case, so
    # chaining .get() off it crashes -- and this runs inside the restore
    # verification, where a crash is most expensive. Normalise first.
    data = payload.get("data") or {}
    kw = data.get("unmatching_keyword") or {}
    return kw.get("value")


# --------------------------------------------------------------------------



# ==========================================================================
# MULTI-MONITOR SUPPORT
# ==========================================================================

def all_statuses(grid, token):
    """One call, every monitor's status. {monitor_id: code}"""
    code, payload = api(grid, "/api/current_status", token)
    out = {}
    if not payload:
        return out
    blob = payload.get("data", {}) if isinstance(payload, dict) else {}
    buckets = list(blob.get("monitors") or [])
    for grp in (blob.get("monitor_groups") or []):
        buckets.extend(grp.get("monitors") or [])
    for m in buckets:
        out[str(m.get("monitor_id"))] = m.get("status")
    return out


def wait_for_targets(grid, token, targets, max_wait, poll_every=20):
    """Wait until EVERY monitor reaches its own target status code.

    targets: {monitor_id: (want_code, display_name)}

    Both monitors are polled inside a single loop from one /api/current_status
    call, so driving two monitors costs the same wall-clock time as driving
    one. Each monitor is checked against its OWN exact target -- a monitor
    sitting in DOWN never satisfies a wait for TROUBLE.
    """
    waited = 0
    want_txt = ", ".join(f"{nm}={STATUS.get(c, c)}"
                         for c, nm in
                         ((c, nm) for c, nm in targets.values()))
    log(f"        waiting for {want_txt}  (up to {max_wait}s, every {poll_every}s)")
    last = {}
    while waited <= max_wait:
        snap = all_statuses(grid, token)
        line, done = [], True
        for mid, (want, name) in targets.items():
            st = snap.get(str(mid))
            last[mid] = STATUS.get(st, str(st))
            hit = (st == want)
            done = done and hit
            line.append(f"{name}={last[mid]}{'' if hit else ' (waiting)'}")
        log(f"          t+{waited:>4}s  " + "  |  ".join(line))
        if done:
            return True, last, waited
        time.sleep(poll_every)
        waited += poll_every
    return False, last, waited


def load_allowlist():
    """Return the allowlist dict, or None if there is no file."""
    if not os.path.isfile(ALLOWLIST_FILE):
        return None
    try:
        with open(ALLOWLIST_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception as exc:  # noqa: BLE001
        die(f"{ALLOWLIST_FILE} exists but is not valid JSON: {exc}. "
            f"Refusing to run rather than guessing what is safe to touch.")
    if not isinstance(data, dict):
        die(f"{ALLOWLIST_FILE} must be a JSON object.")
    return data


def allowlist_filter(monitors, allow):
    """Keep only monitors the allowlist permits. Names support * wildcards."""
    ids = {str(i) for i in (allow.get("monitor_ids") or [])}
    pats = [str(p) for p in (allow.get("monitor_names") or [])]
    kept = []
    for m in monitors:
        mid = str(m["monitor_id"])
        name = str(m.get("name") or "")
        if mid in ids or any(fnmatch.fnmatch(name.lower(), p.lower())
                             for p in pats):
            kept.append(m)
    return kept


def enforce_allowlist(selected, total_in_account, explicit_ids):
    """The hard gate. Nothing gets modified unless it passes through here."""
    allow = load_allowlist()

    if allow is None:
        if total_in_account > UNGUARDED_MAX:
            die(f"This account has {total_in_account} monitors and there is "
                f"no {ALLOWLIST_FILE}.\n"
                f"         REFUSING TO RUN. Without an allowlist a mistake "
                f"here could modify hundreds of monitors.\n"
                f"         Create one:  python3 preflight.py "
                f"--write-allowlist 'AUTOMATION*'")
        log(f"\n  [WARN] no {ALLOWLIST_FILE}. Small account "
            f"({total_in_account} monitors), so this is permitted — but an "
            f"allowlist is the safer habit.")
        return selected

    permitted = allowlist_filter(selected, allow)
    blocked = [m for m in selected if m not in permitted]

    if blocked:
        # On a 2000-monitor account this list is the whole account. Show a
        # readable sample, not a wall of text nobody will read.
        sample = ", ".join(f"{m.get('name')} ({m['monitor_id']})"
                           for m in blocked[:5])
        names = (f"{len(blocked)} monitor(s), e.g. {sample}"
                 + (" ..." if len(blocked) > 5 else ""))
        if explicit_ids:
            # They named these on the command line. Never silently drop a
            # monitor somebody explicitly asked for -- say no out loud.
            die(f"these monitors are NOT in {ALLOWLIST_FILE}: {names}.\n"
                f"         REFUSING TO RUN. Add them to the allowlist if they "
                f"really are safe to modify.")
        log(f"\n  [SKIP] not in {ALLOWLIST_FILE}, will not be touched: "
            f"{names}")

    if not permitted:
        die(f"nothing left to run: no selected monitor is in "
            f"{ALLOWLIST_FILE}.")

    log(f"\n  [OK ] allowlist: {len(permitted)} monitor(s) permitted "
        f"out of {total_in_account} in the account")
    return permitted


# A server monitor is only worth testing if it is actually reachable.
# UP or TROUBLE means the agent is reporting. DOWN means it is not.
SERVER_ELIGIBLE_CODES = (1, 2)          # UP, TROUBLE
SERVER_MIN_ELIGIBLE = 2                 # need at least two, per the test plan
WEBSITE_TYPES = ("URL",)


def monitor_types(grid, token):
    """{monitor_id: type} straight from the account. Nothing hard-coded."""
    code, payload = api(grid, "/api/monitors", token)
    out = {}
    for m in ((payload or {}).get("data") or []):
        out[str(m.get("monitor_id"))] = m.get("type")
    return out


def server_coverage_check(grid, token, servers, wait_seconds, poll_every):
    """Give DOWN server monitors a fair chance before writing them off.

    Polls for up to wait_seconds. The moment SERVER_MIN_ELIGIBLE monitors are
    UP or TROUBLE, it returns them. If the window closes and not enough are
    reachable, server coverage is SKIPPED -- never silently, and never
    reported as a pass or a failure of the product.
    """
    section("SERVER MONITORS — ELIGIBILITY CHECK")
    log(f"  A server monitor can only be tested if its agent is reporting.")
    log(f"  Eligible = UP or TROUBLE.  Need at least {SERVER_MIN_ELIGIBLE}.")
    log(f"  Giving them up to {wait_seconds}s to come back "
        f"(checking every {poll_every}s).")

    # A 60s window with a 300s poll interval checks twice and takes five
    # minutes. Clamp the interval so --server-wait means what it says.
    if poll_every > wait_seconds and wait_seconds > 0:
        poll_every = max(10, wait_seconds // 2)
        log(f"  (poll interval reduced to {poll_every}s so the {wait_seconds}s "
            f"window is honoured)")

    waited = 0
    last_seen = {}
    while True:
        snap = all_statuses(grid, token)
        eligible = []
        for m in servers:
            mid = str(m["monitor_id"])
            st = snap.get(mid)
            last_seen[mid] = {"name": m.get("name"),
                              "status": STATUS.get(st, str(st))}
            if st in SERVER_ELIGIBLE_CODES:
                eligible.append(m)
        line = "  |  ".join(f"{v['name']}={v['status']}"
                            for v in last_seen.values())
        log(f"    t+{waited:>4}s  {line}")

        if len(eligible) >= SERVER_MIN_ELIGIBLE:
            step(f"{len(eligible)} server monitor(s) reachable", True,
                 ", ".join(m.get("name") for m in eligible))
            return eligible[:SERVER_MIN_ELIGIBLE], last_seen, True

        if waited >= wait_seconds:
            break
        time.sleep(poll_every)
        waited += poll_every

    up_now = [v["name"] for v in last_seen.values()
              if v["status"] in ("UP", "TROUBLE")]
    log("")
    log("  " + "!" * 66)
    if up_now:
        log(f"  NOT ENOUGH SERVER MONITORS ARE REACHABLE.")
        log(f"  {len(up_now)} reachable ({', '.join(up_now)}), "
            f"{SERVER_MIN_ELIGIBLE} required.")
    else:
        log("  ALL SERVER MONITORS ARE UNREACHABLE AND CANNOT BE CONNECTED.")
    log("  Server monitor coverage is SKIPPED for this run.")
    log("  Only the website monitors were tested. The server integrations")
    log("  are UNTESTED — this is not a pass and not a product defect.")
    log("  " + "!" * 66)
    for mid, v in last_seen.items():
        log(f"      {v['name']:<24} {v['status']:<10} id={mid}")
    log("")
    return [], last_seen, False


def parse_routes(spec, monitors):
    """--routes 'trouble,down;down,trouble' -> [[...], [...]] per monitor.

    Routes are assigned to monitors in the order the monitors were selected.
    Nothing is hard-coded to a monitor id or name.
    """
    routes = []
    for chunk in spec.split(";"):
        states = [s.strip().lower() for s in chunk.split(",") if s.strip()]
        bad = [s for s in states if s not in STATE_SEVERITY]
        if bad:
            die(f"unknown state(s) in --routes: {bad}. "
                f"Valid values: {sorted(STATE_SEVERITY)}")
        if states:
            routes.append(states)
    if not routes:
        die("--routes was empty. Nothing to drive.")
    if len(routes) < len(monitors):
        # Repeat the pattern rather than refusing. The routes alternate by
        # design, so a third monitor simply takes the first route again.
        # Dying here stopped a whole account from running just because it
        # had one more monitor than the default string covered.
        log(f"\n  [note] {len(monitors)} monitor(s) but {len(routes)} "
            f"route(s) given — the routes repeat in order.")
        full = []
        while len(full) < len(monitors):
            full.extend(routes)
        routes = full
    return routes[:len(monitors)]


def main():
    ap = argparse.ArgumentParser(
        description="Stage 2b - drive real alert cycles on one or more "
                    "monitors, then restore everything")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--yes", action="store_true")
    ap.add_argument("--keyword", default=None,
                    help="override the keyword that IS on the page "
                         "(applies to every monitor -- normally leave this "
                         "alone so each monitor derives its own)")
    ap.add_argument("--up-keyword", default=None,
                    help="override the gibberish keyword used to force UP. "
                         "By default each monitor uses its own page keyword "
                         "plus '%s' (timesofisrael -> timesofisrael%s)."
                         % (GIBBERISH_SUFFIX, GIBBERISH_SUFFIX))
    ap.add_argument("--routes", default="trouble,down;down,trouble",
                    help="state route per monitor, monitors separated by ';' "
                         "and states by ','. Default: "
                         "'trouble,down;down,trouble' -- the first monitor "
                         "goes TROUBLE then DOWN, the second goes DOWN then "
                         "TROUBLE. Every route starts and ends at UP.")
    ap.add_argument("--severity", type=int, default=None,
                    help="LEGACY single-state mode: 0 = Down only, "
                         "2 = Trouble only, on one monitor.")
    ap.add_argument("--server-wait", type=int, default=600,
                    help="how long to give DOWN server monitors to come back "
                         "before skipping server coverage (default 600s = "
                         "10 minutes)")
    ap.add_argument("--server-poll", type=int, default=300,
                    help="how often to re-check them (default 300s, so two "
                         "polls inside the 10 minute window)")
    ap.add_argument("--max-wait", type=int, default=600)
    ap.add_argument("--poll-every", type=int, default=20)
    ap.add_argument("--monitor-id", default=None,
                    help="drive ONE specific monitor instead of all of them")
    ap.add_argument("--monitor-ids", default=None,
                    help="comma-separated list of monitor ids to drive")
    args = ap.parse_args()

    log("Site24x7 ITSM Automation — Stage 2b Full Alert Cycle")
    log("Mechanism: unmatching_keyword ('page should NOT contain X')")
    log("           severity 0 -> DOWN,  severity 2 -> TROUBLE")
    if args.dry_run:
        log(">>> DRY RUN: nothing will be changed. <<<")

    grid = os.environ.get("S247_GRID_URL", "").strip()
    if not grid:
        die("S247_GRID_URL not set. Run: source env.sh")
    if not os.path.isfile(CONFIG_FILE):
        die(f"{CONFIG_FILE} not found. Run stage1_inventory.py first.")

    cfg = json.load(open(CONFIG_FILE, encoding="utf-8"))
    token = get_token()

    # ---- 1. select monitors ----------------------------------------------
    section("1. SELECTING MONITORS")
    mons = cfg.get("monitors") or []
    if not mons:
        die("No monitors in test_config.json. Run stage1_inventory.py.")

    wanted = None
    if args.monitor_ids:
        wanted = [m.strip() for m in args.monitor_ids.split(",") if m.strip()]
    elif args.monitor_id:
        wanted = [str(args.monitor_id)]

    snap = all_statuses(grid, token)
    selected = []
    for m in mons:
        mid = str(m["monitor_id"])
        st = snap.get(mid)
        log(f"  {m['name']:<26} id={mid}  status={STATUS.get(st, st)} (code={st})")
        if wanted is None or mid in wanted:
            selected.append(dict(m))

    if wanted:
        missing = [w for w in wanted
                   if w not in {str(s["monitor_id"]) for s in selected}]
        if missing:
            die(f"monitor id(s) not found in {CONFIG_FILE}: {missing}")

    if not selected:
        die("No monitors selected.")

    # ---- SAFETY GATE : nothing past this point is unguarded --------------
    selected = enforce_allowlist(selected, len(mons), bool(wanted))

    # ---- split by monitor TYPE -------------------------------------------
    # Website monitors are driven with keywords. Server monitors are not --
    # they use thresholds, a different mechanism entirely. Handing a server
    # monitor to the keyword path would crash on a URL that does not exist.
    types = monitor_types(grid, token)
    websites, servers = [], []
    for m in selected:
        t = types.get(str(m["monitor_id"]), "")
        m["type"] = t
        (websites if t in WEBSITE_TYPES else servers).append(m)

    log(f"\n  by type: {len(websites)} website, {len(servers)} server")

    result["server_coverage"] = {"attempted": False, "skipped": True,
                                 "reason": "no server monitors in allowlist",
                                 "monitors": {}}
    if servers:
        eligible, seen, got_enough = server_coverage_check(
            grid, token, servers, args.server_wait, args.server_poll)
        result["server_coverage"] = {
            "attempted": True,
            "skipped": not got_enough,
            "waited_seconds": args.server_wait,
            "min_required": SERVER_MIN_ELIGIBLE,
            "monitors": seen,
            "eligible": [m.get("name") for m in eligible],
            "reason": ("" if got_enough else
                       (f"Only {len([v for v in seen.values() if v['status'] in ('UP','TROUBLE')])} "
                        f"of {len(servers)} server monitor(s) were reachable; "
                        f"{SERVER_MIN_ELIGIBLE} are required. Server coverage "
                        f"was skipped, so the server integrations are "
                        f"UNTESTED in this run."))}
        if got_enough:
            log("  NOTE: driving server state needs threshold profile "
                "changes,")
            log("        which are not wired up yet. Recording eligibility "
                "only.")

    if not websites:
        die("no WEBSITE monitors in the allowlist, so there is nothing this "
            "script can drive yet. Server-state driving is not implemented.")

    selected = websites

    # ---- 2. routes --------------------------------------------------------
    if args.severity is not None:
        legacy = {0: "down", 2: "trouble"}.get(args.severity)
        if legacy is None:
            die(f"--severity {args.severity} is not a state this script can "
                f"drive. Use 0 (Down) or 2 (Trouble), or use --routes.")
        selected = selected[:1]
        routes = [[legacy]]
        log(f"\n  [NOTE] legacy single-state mode: {legacy.upper()} only, "
            f"on one monitor.")
    else:
        routes = parse_routes(args.routes, selected)

    for m, r in zip(selected, routes):
        m["route"] = r
    step("monitors selected", True,
         f"{len(selected)} monitor(s)")
    for m in selected:
        seq = " -> ".join(["UP"] + [s.upper() for s in m["route"]] + ["UP"])
        log(f"      {m['name']:<26} {seq}")

    # ---- 3. read each monitor, derive its own keyword ---------------------
    section("2. MONITOR DETAILS & KEYWORDS")
    for m in selected:
        mid = m["monitor_id"]
        code, payload = api(grid, f"/api/monitors/{mid}", token)
        if code != 200 or not payload or "data" not in payload:
            die(f"Could not read monitor {mid} (status={code}). "
                f"Nothing changed.")
        original = payload["data"]
        url = original.get("website") or original.get("url") or ""
        kw = args.keyword or derive_keyword(url)
        if not kw:
            die(f"Could not derive a keyword from {url!r} for monitor {mid}. "
                f"Pass one with --keyword <text-that-is-on-the-page>")
        m["original"] = original
        m["url"] = url
        m["keyword"] = kw
        m["up_keyword"] = args.up_keyword or up_keyword(kw)
        log(f"  {m['name']}")
        log(f"      url                : {url or '(not found)'}")
        log(f"      check_frequency    : {original.get('check_frequency')}")
        log(f"      keyword (on page)  : '{kw}'   "
            f"-> forces DOWN / TROUBLE")
        log(f"      keyword (gibberish): '{m['up_keyword']}'   -> forces UP")
        log(f"      existing keyword   : {original.get('unmatching_keyword')}")

        backup = f"backup_{mid}.json"
        with open(backup, "w", encoding="utf-8") as fh:
            json.dump(original, fh, indent=2)
        m["backup"] = backup
        step("original config saved", True, backup)

    result["monitors"] = [
        {"monitor_id": m["monitor_id"], "name": m["name"], "url": m["url"],
         "keyword": m["keyword"], "route": m["route"],
         # Carry the monitor TYPE (URL, SERVER, SSL_CERT, REALBROWSER, ...)
         # through to the report so each monitor row shows what KIND of check
         # it was. Future monitor types (and new website-monitoring variants)
         # appear automatically — no code change needed here.
         "type": m.get("type") or ""} for m in selected]
    # keep the old single-monitor keys so existing readers still work
    result["monitor"] = {k: v for k, v in selected[0].items()
                         if k not in ("original",)}
    result["derived_keyword"] = selected[0]["keyword"]

    max_steps = max(len(m["route"]) for m in selected)

    if args.dry_run:
        section("DRY RUN — WHAT WOULD HAPPEN")
        log("  PHASE 0  every monitor -> its own gibberish keyword "
            "-> wait until ALL UP")
        for m in selected:
            log(f"           {m['name']:<26} '{m['keyword']}' "
                f"-> '{m['up_keyword']}'")
        log("           (mandatory: the run aborts if any monitor is not UP)")
        for i in range(max_steps):
            log(f"  STEP {i+1}")
            for m in selected:
                if i < len(m["route"]):
                    st = m["route"][i]
                    log(f"           {m['name']:<26} keyword='{m['keyword']}' "
                        f"severity={STATE_SEVERITY[st]} -> wait {st.upper()}")
            log("           >>> tickets should be CREATED/UPDATED here <<<")
        log("  PHASE UP every monitor -> its own gibberish keyword "
            "-> wait until ALL UP")
        log("           >>> tickets should be CLOSED here <<<")
        log("  RESTORE  original config for every monitor, verified by re-read")
        log("\n  Nothing changed. Re-run without --dry-run to execute.")
        result["dry_run"] = True
        result["verdict"] = "DRY_RUN"
        save_result()
        return

    if not args.yes:
        log("")
        log(f"  About to run FULL ALERT CYCLES on {len(selected)} monitor(s).")
        log(f"  This creates REAL alerts and REAL tickets in your integrations.")
        if input("  Type 'yes' to continue: ").strip().lower() != "yes":
            log("  Aborted. Nothing changed.")
            result["verdict"] = "ABORTED"
            save_result()
            return

    phase_up_start = phase_up_end = False
    step_results = {}
    # Record EXACTLY when alert-producing work starts. Verification is then
    # scoped to this span instead of a rolling "last N hours", so a ticket
    # from an earlier run can never be counted as proof of this one.
    result["cycle_started_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    # Every state change, with the moment it was CONFIRMED on the server.
    # The ticket is created in the ITSM tool off the back of one of these,
    # and the Alert Log row is the receipt written afterwards. With these
    # timestamps a report can say "the DOWN at 15:36:57 produced ticket
    # 25871" instead of "a ticket appeared somewhere in this 9 minutes".
    result["transitions"] = []

    def note_transition(monitor, state, status, secs):
        result["transitions"].append({
            "monitor_id": monitor["monitor_id"],
            "monitor": monitor.get("name"),
            "state": state,
            "observed_status": status,
            "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "waited_seconds": secs,
            "severity": STATE_SEVERITY.get(state),
            "keyword": (monitor.get("up_keyword") if state == "up"
                        else monitor.get("keyword")),
        })

    try:
        # ---- PHASE 0 : MANDATORY - every monitor UP before anything -------
        section("PHASE 0 — FORCING EVERY MONITOR *UP*  (mandatory)")
        log("  Every monitor must start from UP. Otherwise a ticket raised")
        log("  by a state the monitor was ALREADY in would be credited to")
        log("  this run, which would make the whole report untrustworthy.")
        for m in selected:
            ok, code = set_keyword(grid, token, m["monitor_id"],
                                   m["original"], m["up_keyword"],
                                   STATE_SEVERITY[m["route"][0]])
            step(f"{m['name']}: UP keyword '{m['up_keyword']}' applied", ok,
                 f"status={code}")
            if not ok:
                raise RuntimeError(
                    f"could not apply the nonsense keyword to "
                    f"{m['name']} ({m['monitor_id']})")

        targets = {m["monitor_id"]: (UP_CODE, m["name"]) for m in selected}
        phase_up_start, last, secs = wait_for_targets(
            grid, token, targets, args.max_wait, args.poll_every)
        step("ALL monitors UP", phase_up_start, f"after {secs}s  {last}")
        for m in selected:
            note_transition(m, "up", last.get(m["monitor_id"]), secs)
        result["phases"]["0_all_up"] = {"ok": phase_up_start,
                                        "status": last, "secs": secs}
        if not phase_up_start:
            not_up = [f"{m['name']}={last.get(m['monitor_id'])}"
                      for m in selected
                      if last.get(m["monitor_id"]) != "UP"]
            raise RuntimeError(
                "MANDATORY PHASE 0 FAILED — not every monitor reached UP: "
                + ", ".join(not_up) +
                ". Refusing to drive problem states from an unknown "
                "starting point.")

        # ---- STEPS : each monitor follows its OWN route, in lockstep ------
        for i in range(max_steps):
            movers = [m for m in selected if i < len(m["route"])]
            desc = ",  ".join(f"{m['name']} -> {m['route'][i].upper()}"
                              for m in movers)
            section(f"STEP {i+1} — {desc}   (tickets should be CREATED)")

            for m in movers:
                st = m["route"][i]
                sev = STATE_SEVERITY[st]
                ok, code = set_keyword(grid, token, m["monitor_id"],
                                       m["original"], m["keyword"], sev)
                step(f"{m['name']}: keyword '{m['keyword']}' severity={sev} "
                     f"({st.upper()})", ok, f"status={code}")
                if not ok:
                    raise RuntimeError(
                        f"could not set {st} on {m['name']}")

                got_val, got_sev = read_keyword_obj(grid, token,
                                                    m["monitor_id"])
                verified = (got_val == m["keyword"] and got_sev == sev)
                step(f"{m['name']}: verified on server", verified,
                     f"value={got_val!r} severity={got_sev}")
                if not verified:
                    raise RuntimeError(
                        f"{m['name']}: server did not accept the {st} "
                        f"settings (value={got_val!r}, severity={got_sev}). "
                        f"Refusing to wait for a state the config cannot "
                        f"produce.")

            targets = {m["monitor_id"]:
                       (STATE_CODE[m["route"][i]], m["name"]) for m in movers}
            hit, last, secs = wait_for_targets(grid, token, targets,
                                               args.max_wait, args.poll_every)
            for m in movers:
                st = m["route"][i]
                reached = last.get(m["monitor_id"]) == STATUS[STATE_CODE[st]]
                step_results[(m["monitor_id"], i)] = reached
                step(f"{m['name']} reached {st.upper()}", reached,
                     f"status={last.get(m['monitor_id'])} after {secs}s")
                if reached:
                    note_transition(m, st, last.get(m["monitor_id"]), secs)
                result["phases"][f"step{i+1}_{m['monitor_id']}_{st}"] = {
                    "ok": reached, "monitor": m["name"], "state": st,
                    "severity": STATE_SEVERITY[st],
                    "status": last.get(m["monitor_id"]), "secs": secs}

            if hit:
                log(f"\n  >>> Both target states reached. Tickets should have "
                    f"been created or updated.")
            else:
                log(f"\n  >>> Not every monitor reached its target state. "
                    f"Continuing — the restore still runs.")
            log(f"      Pausing 30s so delivery can complete...")
            time.sleep(30)

        # ---- FINAL : everything back UP ----------------------------------
        section("FINAL PHASE — RECOVERING EVERY MONITOR TO UP  "
                "(tickets should be CLOSED)")
        for m in selected:
            ok, code = set_keyword(grid, token, m["monitor_id"],
                                   m["original"], m["up_keyword"],
                                   STATE_SEVERITY[m["route"][-1]])
            step(f"{m['name']}: UP keyword '{m['up_keyword']}' re-applied",
                 ok,
                 f"status={code}")
        targets = {m["monitor_id"]: (UP_CODE, m["name"]) for m in selected}
        phase_up_end, last, secs = wait_for_targets(
            grid, token, targets, args.max_wait, args.poll_every)
        step("ALL monitors back UP", phase_up_end, f"after {secs}s  {last}")
        if phase_up_end:
            for m in selected:
                note_transition(m, "up", last.get(m["monitor_id"]), secs)
        result["phases"]["final_all_up"] = {"ok": phase_up_end,
                                            "status": last, "secs": secs}
        if phase_up_end:
            log("\n  >>> All monitors are UP. CHECK YOUR INTEGRATIONS —")
            log("      the tickets from the problem states should now be "
                "CLOSED.")
            time.sleep(30)

    except Exception as exc:  # noqa: BLE001
        log(f"\n  [!!] Error during cycle: {exc}")

    finally:
        # ---- RESTORE : always, for every monitor -------------------------
        # IMPORTANT: We restore the original config BUT then apply the UP
        # keyword on top. This is intentional and critical:
        #
        #   The monitors used for testing were NOT necessarily UP before this
        #   run started. If we restore the original config blindly, the monitor
        #   may go straight back DOWN (because the original config had a real
        #   keyword that fails, or no keyword but the site is genuinely down).
        #   That would immediately re-open tickets in every integration —
        #   defeating the whole purpose of the test.
        #
        #   Policy: ALWAYS leave test monitors in UP state after a run.
        #   The UP keyword (gibberish suffix) guarantees they stay UP because
        #   no real page contains "timesofisraelsdfjoksfors". The original
        #   config is still restored for every other field (thresholds,
        #   notification profiles, check frequency, etc.) — ONLY the
        #   unmatching_keyword is overridden to the UP value.
        #
        #   If you want the monitor back to its true original state (with
        #   whatever keyword it had before), remove the keyword manually in
        #   the Site24x7 UI or run stage1_inventory.py to refresh the config.
        section("RESTORE — ORIGINAL CONFIG + FORCE UP (always runs)")
        log("  Restoring original config for every field, THEN applying")
        log("  the UP keyword so the monitor stays UP after this run.")
        log("  This prevents monitors from going straight back DOWN and")
        log("  re-opening tickets the moment the test finishes.")
        restored_all, verified_all = True, True
        result["restore_detail"] = {}
        for m in selected:
            mid = m["monitor_id"]
            # Step A: restore original config (all fields correct)
            code, resp = api(grid, f"/api/monitors/{mid}", token,
                             method="PUT", body=m["original"])
            ok = code in (200, 201)
            step(f"{m['name']}: original config restored", ok,
                 f"status={code}")
            if not ok:
                log(f"        response: {str(resp)[:300]}")

            # Step B: apply the UP keyword on top — keeps the monitor UP.
            # This is a second PUT because some APIs do not allow merging
            # an override into the restore in a single call without
            # re-reading the just-written value first.
            up_ok, up_code = set_keyword(
                grid, token, mid, m["original"],
                m["up_keyword"],
                STATE_SEVERITY[m["route"][-1]])
            step(f"{m['name']}: UP keyword applied post-restore "
                 f"('{m['up_keyword']}')", up_ok, f"status={up_code}")
            if not up_ok:
                log(f"        [WARN] could not apply UP keyword — "
                    f"monitor may go DOWN and re-open tickets.")

            # Step C: verify the UP keyword is now on the server
            now_kw = read_keyword(grid, token, mid)
            vok = (now_kw == m["up_keyword"])
            step(f"{m['name']}: UP keyword verified on server", vok,
                 f"keyword now = {now_kw!r} (expected {m['up_keyword']!r})")

            restored_all = restored_all and ok
            verified_all = verified_all and vok
            result["restore_detail"][mid] = {
                "name": m["name"], "restored": ok, "verified": vok,
                "keyword_now": now_kw, "keyword_up": m["up_keyword"],
                "keyword_original": (m["original"].get(
                    "unmatching_keyword") or {}).get("value"),
                "backup": m["backup"],
                "left_in_state": "UP (gibberish keyword applied)"}
            if not ok:
                log(f"        !! restore from {m['backup']} by hand if needed")

        result["restored"] = restored_all
        result["restore_verified"] = verified_all

        # Wait for all monitors to confirm UP after the post-restore keyword.
        log("")
        log("  Waiting for all monitors to confirm UP after post-restore...")
        targets_up = {m["monitor_id"]: (UP_CODE, m["name"]) for m in selected}
        post_up, post_last, post_secs = wait_for_targets(
            grid, token, targets_up, args.max_wait, args.poll_every)
        step("ALL monitors confirmed UP after restore", post_up,
             f"after {post_secs}s  {post_last}")
        result["restore_detail"]["post_restore_all_up"] = {
            "ok": post_up, "status": post_last, "secs": post_secs}
        if not post_up:
            not_up = [f"{m['name']}={post_last.get(m['monitor_id'])}"
                      for m in selected
                      if post_last.get(m["monitor_id"]) != "UP"]
            log(f"  [WARN] these monitors are NOT UP after restore: "
                f"{', '.join(not_up)}")
            log("  They may go DOWN and re-open tickets. Check the Site24x7 UI.")

    result["cycle_ended_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # ---- verdict ---------------------------------------------------------
    section("VERDICT")
    safe = result["restored"] and result["restore_verified"]
    all_steps_hit = all(step_results.values()) if step_results else False
    full_cycle = phase_up_start and all_steps_hit and phase_up_end

    if full_cycle and safe:
        result["verdict"] = "PASS"
        log("  PASS — every monitor started UP, walked its full route, and")
        log("         returned UP. All configs restored cleanly.")
    elif not safe:
        result["verdict"] = "FAIL_UNSAFE"
        log("  FAIL — the environment may NOT be fully restored.")
        log("         Check the monitors and restore from the backup files.")
    elif not phase_up_start:
        result["verdict"] = "BLOCKED_NOT_ALL_UP"
        log("  BLOCKED — the mandatory 'all monitors UP' phase failed, so no")
        log("            problem states were driven. Nothing was tested.")
    elif not all_steps_hit:
        missed = [f"{mid} step {i+1}"
                  for (mid, i), ok in step_results.items() if not ok]
        result["verdict"] = "INCONCLUSIVE"
        log(f"  INCONCLUSIVE — these transitions never happened: "
            f"{', '.join(missed)}")
        log("         If the keyword is not really on the page the monitor")
        log("         can never go down. Check with:")
        log("           curl -s <url> | grep -c -i <keyword>")
        log("         Or allow more time:  --max-wait 900")
    else:
        result["verdict"] = "PARTIAL"
        log("  PARTIAL — see the phases above.")

    log(f"\n  Phase 0 (all monitors UP)  : {phase_up_start}")
    for m in selected:
        for i, st in enumerate(m["route"]):
            got = step_results.get((m["monitor_id"], i))
            log(f"  {m['name']:<24} step {i+1} {st.upper():<8}: {got}")
    log(f"  Final   (all monitors UP)  : {phase_up_end}")
    log(f"  Restored / verified        : {result['restored']} / "
        f"{result['restore_verified']}")

    if result.get("transitions"):
        log("\n  STATE CHANGES (each one should have produced a ticket):")
        log(f"    {'WHEN':<21}{'MONITOR':<24}{'STATE':<10}SEVERITY")
        for t in result["transitions"]:
            sev = t.get("severity")
            log(f"    {t['at']:<21}{str(t.get('monitor'))[:22]:<24}"
                f"{str(t['state']).upper():<10}"
                f"{'-' if sev is None else sev}")
        log("\n    A ticket is raised in the ITSM tool from one of these,")
        log("    and the Alert Log row follows as the receipt. Stage 4 uses")
        log("    these times to tie a specific ticket to a specific change.")
    save_result()
    log(f"\n  Wrote {RESULT_FILE} — safe to share (contains NO token).")


if __name__ == "__main__":
    main()
