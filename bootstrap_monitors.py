#!/usr/bin/env python3
"""
Site24x7 ITSM Automation — BOOTSTRAP MONITORS
=============================================

WHY THIS EXISTS
    The lifecycle stage (stage2b_cycle.py) drives REAL website monitors
    DOWN -> UP to prove each integration raises and closes a ticket. That
    only works if the account HAS suitable website monitors to drive.

    Your own QA account has hand-made test monitors ("New Monitor",
    "New Monitor 2", ...). A developer who clones this suite and points it
    at THEIR OWN account will NOT have those monitors — so the suite would
    die with "no WEBSITE monitors in the allowlist".

    This script removes that blocker. It CREATES the exact test monitors the
    suite needs, in ANY account, and wires them straight into that account's
    allowlist so the safety gate permits them. Every developer runs this ONCE
    after setup, and they are ready.

WHAT IT CREATES  (website monitors, the only type stage2b can drive)
    By default: two URL monitors on real public pages, each with the
    gibberish "should not contain" keyword already applied so they come up
    UP immediately — exactly the state stage2b expects to start from.

        Site24x7 E2E Test Monitor 1   -> https://www.wikipedia.org
        Site24x7 E2E Test Monitor 2   -> https://www.example.com

    The per-account profile IDs (notification / threshold / location /
    user group) are DISCOVERED from the account — never hard-coded, because
    they differ in every account.

WHAT IT TOUCHES
    * POST /api/monitors                      (creates the test monitors)
    * accounts/<account>/allowlist.json       (adds the new monitor ids)
    * bootstrap_manifest.json                 (records what it created, so
                                               --teardown removes exactly
                                               those and nothing else)

SAFETY
    * Idempotent: re-running will not create duplicates — it detects
      monitors it already created (by name) and reuses them.
    * Reversible: --teardown deletes ONLY the monitors listed in the
      manifest this script wrote. It will never delete a monitor it did
      not create.
    * --dry-run shows everything it would do and changes nothing.

USAGE
    source env.sh                         # S247_GRID_URL + S247_TOKEN_SCRIPT
    python3 bootstrap_monitors.py --dry-run     # look first
    python3 bootstrap_monitors.py               # create the test monitors
    python3 bootstrap_monitors.py --count 3     # create three instead of two
    python3 bootstrap_monitors.py --teardown    # delete what this created

    # multi-account (same convention as the rest of the suite):
    python3 bootstrap_monitors.py --account <name>

AFTER THIS
    python3 stage1_inventory.py           # picks up the new monitors
    python3 run_all.py                    # full end-to-end run
"""

import argparse
import json
import os
import ssl
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

MANIFEST_FILE = "bootstrap_manifest.json"
ALLOWLIST_FILE = "allowlist.json"
CONFIG_FILE = "test_config.json"
TIMEOUT = 30

# The gibberish suffix MUST match stage2b_cycle.py's GIBBERISH_SUFFIX so the
# monitor the bootstrap creates is already in the exact UP state stage2b
# expects to start from. If you change it in one place, change it in both.
GIBBERISH_SUFFIX = "sdfjoksfors"

# Prefix for every monitor this script creates. teardown and idempotency both
# key off this, so it must be stable and unlikely to collide with real
# monitors in a developer's account.
NAME_PREFIX = "Site24x7 E2E Test Monitor"

# Default test targets — real, stable, public pages whose body reliably
# contains the derived keyword (wikipedia -> "wikipedia", example -> "example").
# Developers can override the whole list with --urls.
DEFAULT_TARGETS = [
    "https://www.wikipedia.org",
    "https://www.example.com",
    "https://www.iana.org",
    "https://www.w3.org",
    "https://www.gnu.org",
]

STATUS = {0: "DOWN", 1: "UP", 2: "TROUBLE", 3: "CRITICAL", 5: "SUSPENDED",
          7: "MAINTENANCE", 9: "DISCOVERING", 10: "CONFIG ERROR"}


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------

def log(m=""):
    print(m, flush=True)


def section(t):
    log("\n" + "=" * 70)
    log(t)
    log("=" * 70)


def die(msg, code=2):
    log(f"\n[BLOCKER] {msg}")
    sys.exit(code)


def derive_keyword(url):
    """https://www.wikipedia.org -> 'wikipedia'  (same rule as stage2b)."""
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
    main = parts[0]
    if main in ("com", "net", "org") and len(parts) > 1:
        main = parts[1]
    return main or None


def up_keyword(page_keyword):
    """'wikipedia' -> 'wikipediasdfjoksfors' (page never contains this)."""
    return f"{page_keyword}{GIBBERISH_SUFFIX}"


# ---------------------------------------------------------------------------
# auth + API (same shape as every other stage)
# ---------------------------------------------------------------------------

def get_token():
    tok = os.environ.get("S247_ACCESS_TOKEN", "").strip()
    if tok:
        return tok
    script = os.path.expanduser(os.environ.get("S247_TOKEN_SCRIPT", "").strip())
    if not script or not os.path.isfile(script):
        die("No token. Set S247_ACCESS_TOKEN or S247_TOKEN_SCRIPT "
            "(run: source env.sh).")
    try:
        p = subprocess.run(["bash", script], capture_output=True, text=True,
                           timeout=90, stdin=subprocess.DEVNULL)
        lines = [l.strip() for l in (p.stdout or "").splitlines() if l.strip()]
        if p.returncode != 0 or not lines:
            die(f"get_token.sh failed: {(p.stderr or '')[:300]}")
        # last non-empty token-looking line
        for line in reversed(lines):
            if line.startswith("export "):
                line = line.split("=", 1)[-1].strip().strip('"').strip("'")
            if len(line) > 20 and " " not in line:
                return line
        return lines[-1]
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


def data_of(payload):
    if isinstance(payload, dict):
        d = payload.get("data")
        if isinstance(d, list):
            return d
        if isinstance(d, dict):
            return [d]
    if isinstance(payload, list):
        return payload
    return []


# ---------------------------------------------------------------------------
# account-specific profile discovery (NEVER hard-coded — differs per account)
# ---------------------------------------------------------------------------

def first_id(grid, token, path, id_key, name_key=None):
    """Return the first (id, name) from a Site24x7 list endpoint."""
    code, payload = api(grid, path, token)
    if code != 200:
        return None, None, code
    items = data_of(payload)
    if not items:
        return None, None, code
    it = items[0]
    return it.get(id_key), (it.get(name_key) if name_key else None), code


def discover_profiles(grid, token):
    """
    Gather the per-account profile ids a monitor POST needs. These differ in
    every account, so they are discovered live. Returns a dict and a list of
    anything that could not be found (which blocks creation).
    """
    section("1. DISCOVERING ACCOUNT PROFILES  (read-only)")
    found, missing = {}, []

    # notification profile
    npid, npname, _ = first_id(grid, token, "/api/notification_profiles",
                               "profile_id", "profile_name")
    if npid:
        found["notification_profile_id"] = npid
        log(f"  [OK ] notification profile : {npname}  ({npid})")
    else:
        missing.append("notification profile (/api/notification_profiles)")
        log("  [!! ] notification profile : none found")

    # threshold profile — must be a WEBSITE/URL threshold profile
    code, payload = api(grid, "/api/threshold_profiles", token)
    tpid = tpname = None
    for p in data_of(payload):
        # type 1 / "URL" is the website monitor threshold profile
        if str(p.get("type", "")).upper() in ("URL", "1", "HOMEPAGE"):
            tpid, tpname = p.get("profile_id"), p.get("profile_name")
            break
    if not tpid:  # fall back to the first one of any type
        items = data_of(payload)
        if items:
            tpid, tpname = items[0].get("profile_id"), items[0].get("profile_name")
    if tpid:
        found["threshold_profile_id"] = tpid
        log(f"  [OK ] threshold profile    : {tpname}  ({tpid})")
    else:
        missing.append("threshold profile (/api/threshold_profiles)")
        log("  [!! ] threshold profile    : none found")

    # location profile
    lpid, lpname, _ = first_id(grid, token, "/api/location_profiles",
                               "profile_id", "profile_name")
    if lpid:
        found["location_profile_id"] = lpid
        log(f"  [OK ] location profile     : {lpname}  ({lpid})")
    else:
        missing.append("location profile (/api/location_profiles)")
        log("  [!! ] location profile     : none found")

    # user alert group
    ugid, ugname, _ = first_id(grid, token, "/api/user_groups",
                               "user_group_id", "display_name")
    if ugid:
        found["user_group_ids"] = [ugid]
        log(f"  [OK ] user alert group     : {ugname}  ({ugid})")
    else:
        missing.append("user alert group (/api/user_groups)")
        log("  [!! ] user alert group     : none found")

    return found, missing


# ---------------------------------------------------------------------------
# monitor body (the exact shape proven to work by the account backups)
# ---------------------------------------------------------------------------

def build_monitor_body(name, url, profiles):
    """A minimal, valid URL-monitor POST body with the UP keyword applied."""
    kw = up_keyword(derive_keyword(url) or "example")
    return {
        "display_name": name,
        "type": "URL",
        "website": url,
        "check_frequency": "1",
        "timeout": 10,
        "http_method": "G",
        "http_protocol": "H1.1",
        "ssl_protocol": "Auto",
        "use_ipv6": False,
        "follow_redirect": True,
        "match_case": False,
        "ignore_cert_err": True,
        # the "page should NOT contain" rule — gibberish, so the monitor is UP
        "unmatching_keyword": {"severity": 0, "value": kw},
        "notification_profile_id": profiles["notification_profile_id"],
        "threshold_profile_id": profiles["threshold_profile_id"],
        "location_profile_id": profiles["location_profile_id"],
        "user_group_ids": profiles["user_group_ids"],
    }


# ---------------------------------------------------------------------------
# account paths (mirror the rest of the suite's accounts/<name>/ convention)
# ---------------------------------------------------------------------------

def account_dir(account):
    if account:
        d = os.path.join("accounts", account)
        os.makedirs(d, exist_ok=True)
        return d
    return "."


def load_json(path, default):
    if os.path.isfile(path):
        try:
            with open(path, encoding="utf-8") as fh:
                return json.load(fh)
        except Exception:
            return default
    return default


def save_json(path, obj):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=2)


# ---------------------------------------------------------------------------
# existing-monitor detection (idempotency)
# ---------------------------------------------------------------------------

def existing_test_monitors(grid, token):
    """Return {name: monitor_id} for monitors this script previously made."""
    code, payload = api(grid, "/api/monitors", token)
    out = {}
    for m in data_of(payload):
        name = str(m.get("display_name", ""))
        if name.startswith(NAME_PREFIX):
            out[name] = str(m.get("monitor_id"))
    return out


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------

def do_create(grid, token, count, urls, account, dry):
    adir = account_dir(account)
    manifest_path = os.path.join(adir, MANIFEST_FILE)
    allow_path = os.path.join(adir, ALLOWLIST_FILE)

    profiles, missing = discover_profiles(grid, token)
    if missing:
        die("cannot create monitors — this account is missing:\n        - "
            + "\n        - ".join(missing)
            + "\n\n        Create at least one of each in the Site24x7 UI "
              "(Admin -> Configuration Profiles), then re-run.")

    section("2. PLANNING TEST MONITORS")
    existing = existing_test_monitors(grid, token)
    targets = (urls or DEFAULT_TARGETS)
    if len(targets) < count:
        # cycle through the defaults so --count can exceed the list length
        while len(targets) < count:
            targets = targets + (urls or DEFAULT_TARGETS)
    targets = targets[:count]

    plan = []
    for i, url in enumerate(targets, start=1):
        name = f"{NAME_PREFIX} {i}"
        plan.append({"name": name, "url": url,
                     "exists_id": existing.get(name)})
        state = (f"REUSE existing id={existing[name]}" if name in existing
                 else "CREATE new")
        kw = up_keyword(derive_keyword(url) or "example")
        log(f"  {name:<30} {url:<32} [{state}]")
        log(f"      keyword (UP) : '{kw}'  (page never contains this)")

    if dry:
        section("DRY RUN — nothing was created")
        log("  Re-run without --dry-run to create the monitors above.")
        return

    section("3. CREATING / REUSING MONITORS")
    created, reused = [], []
    for p in plan:
        if p["exists_id"]:
            log(f"  [OK ] {p['name']} already exists — reusing "
                f"({p['exists_id']})")
            reused.append({"name": p["name"], "monitor_id": p["exists_id"],
                           "url": p["url"]})
            continue
        body = build_monitor_body(p["name"], p["url"], profiles)
        code, resp = api(grid, "/api/monitors", token, method="POST", body=body)
        if code in (200, 201):
            mid = str((resp.get("data") or {}).get("monitor_id")
                      or (resp.get("data") or {}).get("id") or "")
            log(f"  [OK ] created {p['name']}  -> id {mid}")
            created.append({"name": p["name"], "monitor_id": mid,
                            "url": p["url"]})
        else:
            log(f"  [!! ] FAILED to create {p['name']}: status={code}")
            log(f"        response: {str(resp)[:400]}")

    all_mons = created + reused
    if not all_mons:
        die("no monitors were created or reused — see errors above.")

    # ---- wire the new ids into the allowlist -----------------------------
    section("4. UPDATING ALLOWLIST (safety gate)")
    allow = load_json(allow_path, {"monitor_names": [], "monitor_ids": []})
    allow.setdefault("monitor_names", [])
    allow.setdefault("monitor_ids", [])
    ids_before = set(str(i) for i in allow["monitor_ids"])
    for m in all_mons:
        if m["monitor_id"] and m["monitor_id"] not in ids_before:
            allow["monitor_ids"].append(m["monitor_id"])
    # also allow by name pattern so a rebuild keeps working
    pat = f"{NAME_PREFIX}*"
    if pat not in allow["monitor_names"]:
        allow["monitor_names"].append(pat)
    save_json(allow_path, allow)
    log(f"  [OK ] {allow_path} now permits {len(allow['monitor_ids'])} id(s) "
        f"and pattern '{pat}'")

    # ---- write the manifest (for teardown) -------------------------------
    manifest = load_json(manifest_path,
                         {"created_by": "bootstrap_monitors.py",
                          "grid_url": grid, "monitors": []})
    manifest["grid_url"] = grid
    manifest["updated_at"] = datetime.now(timezone.utc).isoformat()
    known = {m["monitor_id"] for m in manifest.get("monitors", [])}
    for m in created:  # only track what WE created (never reused)
        if m["monitor_id"] not in known:
            manifest["monitors"].append(m)
    save_json(manifest_path, manifest)
    log(f"  [OK ] wrote {manifest_path} ({len(created)} newly created, "
        f"tracked for teardown)")

    section("DONE — ACCOUNT IS READY")
    log(f"  {len(created)} created, {len(reused)} reused. Next steps:")
    log("")
    log("     python3 stage1_inventory.py     # pick up the new monitors")
    log("     python3 run_all.py              # full end-to-end run")
    log("")
    log("  The monitors are UP now (gibberish keyword). stage2b will drive")
    log("  them DOWN -> UP to exercise every integration.")


# ---------------------------------------------------------------------------
# teardown
# ---------------------------------------------------------------------------

def do_teardown(grid, token, account, dry):
    adir = account_dir(account)
    manifest_path = os.path.join(adir, MANIFEST_FILE)
    allow_path = os.path.join(adir, ALLOWLIST_FILE)

    section("TEARDOWN — DELETE ONLY WHAT THIS SCRIPT CREATED")
    manifest = load_json(manifest_path, None)
    if not manifest or not manifest.get("monitors"):
        die(f"no {manifest_path} with tracked monitors — nothing to tear "
            f"down. (This script only ever deletes monitors it created and "
            f"recorded; it will not guess.)")

    mons = manifest["monitors"]
    log(f"  {len(mons)} monitor(s) recorded as created by this script:")
    for m in mons:
        log(f"      {m['name']:<30} id={m['monitor_id']}  {m.get('url','')}")

    if dry:
        section("DRY RUN — nothing was deleted")
        return

    deleted, failed = [], []
    for m in mons:
        mid = m["monitor_id"]
        code, resp = api(grid, f"/api/monitors/{mid}", token, method="DELETE")
        if code in (200, 204):
            log(f"  [OK ] deleted {m['name']} ({mid})")
            deleted.append(mid)
        else:
            log(f"  [!! ] could not delete {m['name']} ({mid}): status={code}")
            failed.append(mid)

    # prune allowlist of the deleted ids + our name pattern
    allow = load_json(allow_path, None)
    if allow:
        allow["monitor_ids"] = [i for i in allow.get("monitor_ids", [])
                                if str(i) not in set(deleted)]
        allow["monitor_names"] = [n for n in allow.get("monitor_names", [])
                                  if n != f"{NAME_PREFIX}*"]
        save_json(allow_path, allow)
        log(f"  [OK ] pruned {allow_path}")

    # keep only the ones we failed to delete in the manifest
    manifest["monitors"] = [m for m in mons
                            if m["monitor_id"] in set(failed)]
    save_json(manifest_path, manifest)

    section("TEARDOWN DONE")
    log(f"  deleted {len(deleted)}, failed {len(failed)}.")
    if failed:
        log("  Delete the failed ones by hand in the Site24x7 UI if needed.")


# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        description="Create (or remove) the website test monitors the "
                    "lifecycle suite needs, in any Site24x7 account.")
    ap.add_argument("--count", type=int, default=2,
                    help="how many test monitors to create (default 2 — the "
                         "suite drives two in lockstep)")
    ap.add_argument("--urls", default=None,
                    help="comma-separated URLs to monitor instead of the "
                         "built-in public defaults")
    ap.add_argument("--account", default=None,
                    help="account name (writes under accounts/<name>/, same "
                         "convention as the rest of the suite)")
    ap.add_argument("--teardown", action="store_true",
                    help="delete ONLY the monitors this script created "
                         "(tracked in the manifest)")
    ap.add_argument("--dry-run", action="store_true",
                    help="show what would happen, change nothing")
    args = ap.parse_args()

    log("Site24x7 ITSM Automation — Bootstrap Monitors")
    if args.dry_run:
        log(">>> DRY RUN: nothing will be created or deleted. <<<")

    grid = os.environ.get("S247_GRID_URL", "").strip()
    if not grid:
        die("S247_GRID_URL not set. Run: source env.sh")
    log(f"\n  Grid: {grid}")
    if args.account:
        log(f"  Account: {args.account}  (accounts/{args.account}/)")

    token = get_token()
    log(f"  Token: obtained ({len(token)} chars)")

    if args.teardown:
        do_teardown(grid, token, args.account, args.dry_run)
    else:
        urls = None
        if args.urls:
            urls = [u.strip() for u in args.urls.split(",") if u.strip()]
        if args.count < 1:
            die("--count must be at least 1")
        do_create(grid, token, args.count, urls, args.account, args.dry_run)


if __name__ == "__main__":
    main()
