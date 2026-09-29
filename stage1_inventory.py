#!/usr/bin/env python3
"""
Site24x7 ITSM Automation — STAGE 1 : ACCOUNT INVENTORY
======================================================

WHY
    The API probe proved the API works, but raised two questions:
      1. /api/integration/third_party_services returned 404 -> wrong path.
         Your older work used 'thirdparty' (no underscore). This script
         tries several real variants and reports which one answers.
      2. Only 2 monitors were returned. Your test monitors (hustle,
         Test Latest Web, bala-*) may live on a DIFFERENT grid.
         This script prints the monitors it can actually see so you
         can confirm you are pointed at the right account.

WHAT IT DOES  (all READ-ONLY)
    - Lists every monitor: name, id, type, and current status
    - Finds the correct third-party integration endpoint
    - Lists integrations if found
    - Lists notification profiles (needed for Consolidated vs Every-Event)
    - Lists threshold profiles (needed to drive Trouble/Critical)
    - Writes account_inventory.json

USAGE
    export S247_GRID_URL="https://integrations-qa.localsite24x7.com"
    export S247_TOKEN_SCRIPT="$HOME/Documents/qg/get_token.sh"
    python3 stage1_inventory.py

    # to check a DIFFERENT grid, just change S247_GRID_URL and re-run:
    export S247_GRID_URL="https://coreweb.localsite24x7.com"
    python3 stage1_inventory.py

SAFETY
    GET requests only. Nothing is created, changed or deleted.
    Your token is never printed or written to the report.
"""

import json
import os
import ssl
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

REPORT = "account_inventory.json"
TIMEOUT = 25

out = {
    "generated_at": datetime.now(timezone.utc).isoformat(),
    "grid_url": None,
    "monitors": [],
    "monitor_status": {},
    "integration_endpoint": None,
    "integrations": [],
    "notification_profiles": [],
    "threshold_profiles": [],
    "notes": [],
}

# Monitor type codes -> readable names (Site24x7 returns numeric or string types)
TYPE_HINTS = {
    "URL": "Website", "HOMEPAGE": "Website", "SERVER": "Server",
    "PING": "Ping", "PORT": "Port", "RESTAPI": "REST API",
    "SSL_CERT": "SSL", "DNS": "DNS", "CRON": "Cron", "HEARTBEAT": "Heartbeat",
}

# Site24x7 status codes
# CONFIRMED against the Site24x7 UI: 0 = DOWN, 1 = UP.
STATUS = {0: "DOWN", 1: "UP", 2: "TROUBLE", 3: "CRITICAL", 5: "SUSPENDED",
          7: "MAINTENANCE", 9: "DISCOVERING", 10: "CONFIG ERROR"}


def log(m):
    print(m, flush=True)


def section(t):
    log("\n" + "=" * 70)
    log(t)
    log("=" * 70)


def get_token():
    tok = os.environ.get("S247_ACCESS_TOKEN", "").strip()
    if tok:
        return tok
    script = os.path.expanduser(os.environ.get("S247_TOKEN_SCRIPT", "").strip())
    if not script or not os.path.isfile(script):
        log("[BLOCKER] No token. Set S247_ACCESS_TOKEN or S247_TOKEN_SCRIPT.")
        sys.exit(2)
    try:
        p = subprocess.run(["bash", script], capture_output=True, text=True, timeout=60)
        lines = [l for l in (p.stdout or "").splitlines() if l.strip()]
        if p.returncode != 0 or not lines:
            log(f"[BLOCKER] get_token.sh failed: {(p.stderr or '')[:300]}")
            sys.exit(2)
        return lines[-1].strip()
    except Exception as exc:  # noqa: BLE001
        log(f"[BLOCKER] Could not run get_token.sh: {exc}")
        sys.exit(2)


def api_get(grid, path, token):
    url = grid.rstrip("/") + path
    req = urllib.request.Request(url, method="GET", headers={
        "Authorization": f"Zoho-oauthtoken {token}",
        "Accept": "application/json; version=2.1",
    })
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT, context=ctx) as r:
            raw = r.read().decode("utf-8", errors="replace")
            return r.status, json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as e:
        return e.code, None
    except Exception:
        return None, None


def data_of(payload):
    """Site24x7 wraps results in {'data': [...]}."""
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

def list_monitors(grid, token):
    section("1. MONITORS IN THIS ACCOUNT")
    code, payload = api_get(grid, "/api/monitors", token)
    mons = data_of(payload)
    if not mons:
        log(f"  [--] No monitors returned (status={code}).")
        out["notes"].append("No monitors visible on this grid.")
        return []

    # current status for each
    code_s, payload_s = api_get(grid, "/api/current_status", token)
    status_map = {}
    if payload_s:
        blob = payload_s.get("data", {}) if isinstance(payload_s, dict) else {}
        for m in (blob.get("monitors") or []):
            status_map[str(m.get("monitor_id"))] = m.get("status")
        for grp in (blob.get("monitor_groups") or []):
            for m in (grp.get("monitors") or []):
                status_map[str(m.get("monitor_id"))] = m.get("status")

    log(f"  Found {len(mons)} monitor(s):\n")
    log(f"  {'NAME':<34} {'TYPE':<12} {'STATUS':<10} MONITOR ID")
    log(f"  {'-'*34} {'-'*12} {'-'*10} {'-'*20}")
    for m in mons:
        mid = str(m.get("monitor_id", ""))
        name = str(m.get("display_name", ""))[:33]
        mtype = str(m.get("type", ""))
        pretty = TYPE_HINTS.get(mtype.upper(), mtype)
        st = status_map.get(mid)
        st_txt = STATUS.get(st, str(st) if st is not None else "?")
        log(f"  {name:<34} {pretty:<12} {st_txt:<10} {mid}")
        out["monitors"].append({
            "name": m.get("display_name"), "monitor_id": mid,
            "type": mtype, "status": st_txt,
        })
    out["monitor_status"] = status_map
    return mons


def find_integrations(grid, token):
    section("2. THIRD-PARTY INTEGRATIONS (finding the correct endpoint)")
    # Real-world variants. Your MSP work used 'thirdparty_service' (singular,
    # no underscore between third and party) - that is the strongest candidate.
    candidates = [
        "/api/integration/thirdparty_services",
        "/api/integration/third_party_services",
        "/api/third_party_integrations",
        "/api/integration/thirdparty_service",
        "/api/short/third_party_services",
        "/api/integrations",
    ]
    for path in candidates:
        code, payload = api_get(grid, path, token)
        items = data_of(payload)
        if code == 200:
            log(f"  [OK ] {path}   -> {len(items)} item(s)")
            out["integration_endpoint"] = path
            for it in items:
                name = it.get("name") or it.get("display_name") or "?"
                iid = it.get("integration_id") or it.get("service_id") or it.get("id")
                itype = it.get("type") or it.get("service_type") or ""
                status = it.get("status", "")
                log(f"         - {str(name)[:40]:<42} id={iid}  type={itype} {status}")
                out["integrations"].append({
                    "name": name, "id": iid, "type": itype, "status": status
                })
            return path
        log(f"  [-- ] {path}   status={code}")

    log("\n  [!] None of the candidate paths worked.")
    log("      This is NOT fatal - integrations can still be driven via the UI.")
    log("      Next step would be to capture the real path from the browser's")
    log("      Network tab while loading the Third-Party Integrations page.")
    out["notes"].append("Integration list endpoint not found via API.")
    return None


def list_profiles(grid, token):
    section("3. NOTIFICATION PROFILES (Consolidated vs Every-Event)")
    code, payload = api_get(grid, "/api/notification_profiles", token)
    items = data_of(payload)
    log(f"  Found {len(items)} profile(s). Showing first 20:\n")
    for p in items[:20]:
        name = str(p.get("profile_name", ""))[:44]
        pid = p.get("profile_id")
        log(f"    - {name:<46} id={pid}")
        out["notification_profiles"].append({"name": p.get("profile_name"), "id": pid})

    section("4. THRESHOLD PROFILES (to drive Trouble / Critical)")
    code, payload = api_get(grid, "/api/threshold_profiles", token)
    items = data_of(payload)
    log(f"  Found {len(items)} profile(s):\n")
    for p in items:
        name = str(p.get("profile_name", ""))[:44]
        pid = p.get("profile_id")
        mtype = p.get("type", "")
        log(f"    - {name:<46} id={pid}  type={mtype}")
        out["threshold_profiles"].append({
            "name": p.get("profile_name"), "id": pid, "type": mtype
        })


def verdict():
    """
    Discovery-driven. NOTHING is hard-coded: we do not look for specific
    monitor or integration names. We report what exists and pick suitable
    test candidates automatically. If something is absent, that is fine -
    the suite simply tests what IS there.
    """
    section("5. TEST READINESS (discovery-driven, no hard-coded names)")

    mons = out["monitors"]
    ints = out["integrations"]

    # A lifecycle test must START from UP.
    up_monitors = [m for m in mons if m["status"] == "UP"]
    other_monitors = [m for m in mons if m["status"] != "UP"]

    log(f"  Monitors discovered : {len(mons)}")
    log(f"    usable now (UP)   : {len(up_monitors)}")
    for m in up_monitors:
        log(f"        -> {m['name']}  (id={m['monitor_id']}, {m['type']})")
    if other_monitors:
        log(f"    not usable yet    : {len(other_monitors)}")
        for m in other_monitors:
            log(f"        -- {m['name']}  ({m['status']}) - cannot drive DOWN "
                f"from this state")

    log(f"\n  Integrations discovered : {len(ints)}")
    for i in ints:
        log(f"        -> {i['name']}  (id={i['id']}, type={i['type']})")

    ready = bool(up_monitors)
    if ready:
        log("\n  [OK ] READY FOR STAGE 2.")
        log("       At least one monitor is UP, so a full lifecycle")
        log("       (UP -> problem -> UP) can be driven and verified.")
        out["test_candidates"] = {
            "primary_monitor": up_monitors[0],
            "all_up_monitors": up_monitors,
        }
    else:
        log("\n  [!!] NOT READY: no monitor is currently UP.")
        log("       A lifecycle test must start from UP. Wait for a monitor")
        log("       to recover, or restore one manually, then re-run.")
        out["notes"].append("No UP monitor available to drive a lifecycle.")

    if not ints:
        log("\n  [i ] No integrations readable via the API.")
        log("       Not a blocker - alert delivery is still verifiable via")
        log("       Alert Logs, and integrations can be driven through the UI.")


def write_config():
    """
    Write a config file that Stage 2 consumes, so test code never
    hard-codes a monitor or integration name.
    """
    cfg = {
        "_comment": "AUTO-GENERATED by stage1_inventory.py. Re-run to refresh.",
        "grid_url": out["grid_url"],
        "integration_endpoint": out["integration_endpoint"],
        "monitors": out["monitors"],
        "integrations": out["integrations"],
        "primary_test_monitor": (out.get("test_candidates") or {}).get("primary_monitor"),
        "poll_wait_seconds": 60,
        "max_wait_seconds": 600,
    }
    with open("test_config.json", "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2)
    log("\n  Wrote test_config.json — Stage 2 reads this, so no names are")
    log("  hard-coded anywhere in the test code.")


def main():
    grid = os.environ.get("S247_GRID_URL", "").strip()
    if not grid:
        log("[BLOCKER] Set S247_GRID_URL first.")
        sys.exit(2)
    out["grid_url"] = grid

    log("Site24x7 ITSM Automation — Stage 1 Account Inventory")
    log("READ-ONLY. Nothing is created, changed or deleted.")
    log(f"\n  Grid: {grid}")

    token = get_token()
    log(f"  Token: obtained (length {len(token)})")

    list_monitors(grid, token)
    find_integrations(grid, token)
    list_profiles(grid, token)
    verdict()
    write_config()

    with open(REPORT, "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=2)
    log(f"\n  Wrote {REPORT} — safe to share (contains NO token).")


if __name__ == "__main__":
    main()
