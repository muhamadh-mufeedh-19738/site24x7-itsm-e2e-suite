#!/usr/bin/env python3
"""
Site24x7 ITSM Automation — STAGE 2 : FIRST REAL LIFECYCLE TEST
===============================================================

WHAT THIS DOES
    Drives a real alert lifecycle and verifies it:

        UP  ->  force a problem state  ->  confirm alert  ->  restore  ->  UP

    It picks whichever monitor is currently UP at run time (Option C),
    so it never gets blocked by monitors flapping between states.

SAFETY — READ THIS
    This is the FIRST script that CHANGES anything. It is built to be safe:

      * It saves the monitor's ORIGINAL configuration to disk BEFORE
        touching anything  ->  backup_<monitor_id>.json
      * It restores that configuration in a 'finally' block, so the restore
        runs even if the test crashes or you press Ctrl-C.
      * It VERIFIES the restore by re-reading from the server. It never
        assumes the restore worked.
      * It NEVER deletes a monitor, integration, or ticket.
      * --dry-run shows exactly what it WOULD do, and changes nothing.

    If the script is killed so hard that restore cannot run, you can always
    restore by hand from backup_<monitor_id>.json.

HOW IT FORCES A PROBLEM STATE
    It adds a 'matching_keyword' (a "page should contain this text" rule)
    set to a long random nonsense string. No real web page contains that
    string, so the check fails and the monitor leaves UP. Removing the
    keyword returns it to normal. This is deterministic and does not depend
    on what the monitored site actually says.

USAGE
    # 1. Always look first - changes nothing:
    python3 stage2_lifecycle.py --dry-run

    # 2. Run for real:
    python3 stage2_lifecycle.py

    # 3. Run without the confirmation prompt (for CI later):
    python3 stage2_lifecycle.py --yes

PREREQUISITES
    export S247_GRID_URL="https://integrations-qa.localsite24x7.com"
    export S247_TOKEN_SCRIPT="$HOME/Documents/qg/get_token.sh"
    test_config.json  (created by stage1_inventory.py)
"""

import argparse
import json
import os
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

CONFIG_FILE = "test_config.json"
RESULT_FILE = "stage2_result.json"
TIMEOUT = 30

# Site24x7 monitor status codes
# CONFIRMED against the Site24x7 UI: 0 = DOWN, 1 = UP.
# (An earlier version of this script had 0 and 1 swapped, which made it
#  read a DOWN monitor as UP. Verified by comparing API output with the
#  Monitor Status page.)
STATUS = {0: "DOWN", 1: "UP", 2: "TROUBLE", 3: "CRITICAL", 5: "SUSPENDED",
          7: "MAINTENANCE", 9: "DISCOVERING", 10: "CONFIG ERROR"}

UP_CODE = 1
PROBLEM_CODES = (0, 2, 3)  # DOWN, TROUBLE, CRITICAL

# A string no real page will contain.
NONSENSE = "S247AUTOMATIONSENTINEL7Q4XZK9NOTREALCONTENT"

result = {
    "started_at": datetime.now(timezone.utc).isoformat(),
    "dry_run": False,
    "monitor": None,
    "steps": [],
    "alert_log": {"checked": False, "endpoint": None, "entries": 0, "note": None},
    "restored": False,
    "restore_verified": False,
    "verdict": None,
}


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------

def log(m=""):
    print(m, flush=True)


def section(t):
    log("\n" + "=" * 70)
    log(t)
    log("=" * 70)


def step(name, ok, detail=""):
    mark = "OK " if ok else "!! "
    log(f"  [{mark}] {name}" + (f"  — {detail}" if detail else ""))
    result["steps"].append({"step": name, "ok": ok, "detail": detail,
                            "at": datetime.now(timezone.utc).isoformat()})


def die(msg, code=2):
    log(f"\n[BLOCKER] {msg}")
    result["verdict"] = "BLOCKED"
    save_result()
    sys.exit(code)


def save_result():
    try:
        with open(RESULT_FILE, "w", encoding="utf-8") as fh:
            json.dump(result, fh, indent=2)
    except Exception as exc:  # noqa: BLE001
        log(f"[WARN] could not write {RESULT_FILE}: {exc}")


# ---------------------------------------------------------------------------
# auth + http
# ---------------------------------------------------------------------------

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
    """Returns (status_code, parsed_json_or_None)."""
    url = grid.rstrip("/") + path
    data = None
    headers = {
        "Authorization": f"Zoho-oauthtoken {token}",
        "Accept": "application/json; version=2.1",
    }
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
    """Current numeric status for one monitor, or None."""
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


def wait_for(grid, token, monitor_id, want_up, max_wait, poll_every=20):
    """
    Poll until the monitor reaches the desired condition.
    want_up=True  -> wait for UP
    want_up=False -> wait for ANY non-UP problem state
    Returns (reached: bool, last_status_text: str, seconds: int)
    """
    waited = 0
    last = None
    target = "UP" if want_up else "a problem state (DOWN/TROUBLE/CRITICAL)"
    log(f"        waiting for {target} (up to {max_wait}s, checking every {poll_every}s)")
    while waited <= max_wait:
        st = monitor_status(grid, token, monitor_id)
        last = STATUS.get(st, str(st))
        reached = (st == UP_CODE) if want_up else (st in PROBLEM_CODES)
        log(f"          t+{waited:>4}s  status = {last}")
        if reached:
            return True, last, waited
        time.sleep(poll_every)
        waited += poll_every
    return False, last, waited


# ---------------------------------------------------------------------------
# alert-log discovery (best effort; never fails the run)
# ---------------------------------------------------------------------------

def check_alert_logs(grid, token):
    section("6. ALERT LOG CHECK (best effort)")
    candidates = [
        "/api/alert_logs",
        "/api/reports/alert_logs",
        "/api/log_report/alert_logs",
    ]
    for path in candidates:
        code, payload = api(grid, path, token)
        if code == 200:
            items = []
            if isinstance(payload, dict):
                d = payload.get("data")
                if isinstance(d, list):
                    items = d
            step(f"alert logs readable via {path}", True, f"{len(items)} entries")
            result["alert_log"] = {"checked": True, "endpoint": path,
                                   "entries": len(items), "note": None}
            return
        log(f"  [-- ] {path}  status={code}")

    note = ("No Alert Logs API endpoint responded. This does NOT fail the test. "
            "Delivery can still be confirmed in the Alert Logs UI page. "
            "To automate it, capture the real request from the browser "
            "Network tab while the Alert Logs page loads.")
    step("alert logs via API", False, "endpoint not found (not fatal)")
    log(f"\n  NOTE: {note}")
    result["alert_log"] = {"checked": True, "endpoint": None, "entries": 0,
                           "note": note}


# ---------------------------------------------------------------------------
# main flow
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="show what would happen; change nothing")
    ap.add_argument("--yes", action="store_true",
                    help="skip the confirmation prompt")
    ap.add_argument("--max-wait", type=int, default=600,
                    help="max seconds to wait for a state change (default 600)")
    args = ap.parse_args()
    result["dry_run"] = args.dry_run

    log("Site24x7 ITSM Automation — Stage 2 Lifecycle Test")
    if args.dry_run:
        log(">>> DRY RUN: nothing will be changed. <<<")

    grid = os.environ.get("S247_GRID_URL", "").strip()
    if not grid:
        die('S247_GRID_URL not set.')
    if not os.path.isfile(CONFIG_FILE):
        die(f"{CONFIG_FILE} not found. Run stage1_inventory.py first.")

    with open(CONFIG_FILE, encoding="utf-8") as fh:
        cfg = json.load(fh)

    token = get_token()

    # ---- 1. pick a monitor that is UP right now -----------------------------
    section("1. CHOOSING A MONITOR (whichever is UP right now)")
    candidates = cfg.get("monitors") or []
    if not candidates:
        die("No monitors in test_config.json. Re-run stage1_inventory.py.")

    chosen = None
    for m in candidates:
        st = monitor_status(grid, token, m["monitor_id"])
        txt = STATUS.get(st, str(st))
        log(f"  {m['name']:<28} id={m['monitor_id']}  status={txt}  (raw code={st})")
        if st == UP_CODE and chosen is None:
            chosen = dict(m)
            chosen["status_code"] = st

    log("\n  Status code reference: 0=DOWN  1=UP  2=TROUBLE  3=CRITICAL")
    log("  Cross-check the above against the Monitor Status page in the UI.")
    log("  If they disagree, STOP and tell me - the mapping would be wrong.")

    if not chosen:
        die("No monitor is currently UP. A lifecycle must start from UP. "
            "Wait for one to recover and re-run.")

    mid = chosen["monitor_id"]
    result["monitor"] = chosen
    step("monitor selected", True, f"{chosen['name']} (id={mid})")

    # ---- 2. capture original configuration ----------------------------------
    section("2. CAPTURING ORIGINAL CONFIGURATION (safety first)")
    code, payload = api(grid, f"/api/monitors/{mid}", token)
    if code != 200 or not payload or "data" not in payload:
        die(f"Could not read monitor {mid} (status={code}). Nothing was changed.")

    original = payload["data"]
    backup_path = f"backup_{mid}.json"
    with open(backup_path, "w", encoding="utf-8") as fh:
        json.dump(original, fh, indent=2)
    step("original config saved", True, backup_path)
    log(f"        If anything goes wrong you can restore by hand from this file.")

    had_keyword = "matching_keyword" in original
    log(f"        monitor already has a matching_keyword: {had_keyword}")

    if args.dry_run:
        section("DRY RUN — WHAT WOULD HAPPEN NEXT")
        log(f"  1. PUT /api/monitors/{mid} adding matching_keyword:")
        log(f"       {{'severity': 0, 'value': '{NONSENSE}'}}")
        log(f"  2. Poll until {chosen['name']} leaves UP")
        log(f"  3. Check Alert Logs for delivery")
        log(f"  4. PUT /api/monitors/{mid} restoring the ORIGINAL config")
        log(f"  5. Verify the restore by re-reading from the server")
        log(f"  6. Poll until {chosen['name']} returns to UP")
        log("\n  Nothing was changed. Re-run without --dry-run to execute.")
        result["verdict"] = "DRY_RUN"
        save_result()
        return

    # ---- confirmation --------------------------------------------------------
    if not args.yes:
        log("")
        log(f"  About to TEMPORARILY modify: {chosen['name']} (id={mid})")
        log(f"  It will be driven into a problem state, then restored.")
        answer = input("  Type 'yes' to continue: ").strip().lower()
        if answer != "yes":
            log("  Aborted by user. Nothing was changed.")
            result["verdict"] = "ABORTED"
            save_result()
            return

    # ---- 3..7 with guaranteed restore ---------------------------------------
    try:
        section("3. FORCING A PROBLEM STATE")
        modified = json.loads(json.dumps(original))  # deep copy
        modified["matching_keyword"] = {"severity": 0, "value": NONSENSE}

        code, resp = api(grid, f"/api/monitors/{mid}", token,
                         method="PUT", body=modified)
        ok = code in (200, 201)
        step("keyword applied (PUT)", ok, f"status={code}")
        if not ok:
            log(f"        response: {str(resp)[:300]}")
            raise RuntimeError("could not apply the keyword")

        # verify the change actually persisted
        code, payload = api(grid, f"/api/monitors/{mid}", token)
        persisted = (code == 200 and payload
                     and payload.get("data", {}).get("matching_keyword", {})
                     .get("value") == NONSENSE)
        step("change verified on server", bool(persisted),
             "re-read confirms keyword" if persisted else "NOT persisted")
        if not persisted:
            raise RuntimeError("configuration change did not persist")

        section("4. WAITING FOR THE MONITOR TO LEAVE 'UP'")
        reached, last, secs = wait_for(grid, token, mid, want_up=False,
                                       max_wait=args.max_wait)
        step("problem state reached", reached, f"status={last} after {secs}s")
        problem_state_reached = reached

        section("5. PROBLEM STATE CONFIRMED" if reached else
                "5. PROBLEM STATE NOT REACHED")
        if reached:
            log(f"  The monitor is now {last}. An alert should have been sent")
            log(f"  to every Active integration set to 'All Monitors'.")
        else:
            log("  The monitor did not change state within the time limit.")
            log("  Possible causes: long polling interval, or the check does")
            log("  not use keyword matching. Restoring anyway.")

        check_alert_logs(grid, token)

    except Exception as exc:  # noqa: BLE001
        log(f"\n  [!!] Test error: {exc}")
        problem_state_reached = False

    finally:
        # ---- ALWAYS restore -------------------------------------------------
        section("7. RESTORING ORIGINAL CONFIGURATION (always runs)")
        code, resp = api(grid, f"/api/monitors/{mid}", token,
                         method="PUT", body=original)
        restored = code in (200, 201)
        result["restored"] = restored
        step("original config restored (PUT)", restored, f"status={code}")
        if not restored:
            log(f"        response: {str(resp)[:300]}")
            log(f"        !! RESTORE FAILED — restore by hand from {backup_path}")

        # verify by re-reading, never assume
        code, payload = api(grid, f"/api/monitors/{mid}", token)
        if code == 200 and payload:
            now_kw = payload.get("data", {}).get("matching_keyword", {})
            clean = (now_kw.get("value") != NONSENSE)
            result["restore_verified"] = clean
            step("restore verified by re-read", clean,
                 "sentinel keyword is gone" if clean
                 else "SENTINEL STILL PRESENT — fix manually")
        else:
            step("restore verified by re-read", False, f"could not re-read (status={code})")

        section("8. WAITING FOR RECOVERY TO 'UP'")
        back_up, last, secs = wait_for(grid, token, mid, want_up=True,
                                       max_wait=args.max_wait)
        step("monitor back to UP", back_up, f"status={last} after {secs}s")

    # ---- verdict -------------------------------------------------------------
    section("9. VERDICT")
    safe = result["restored"] and result["restore_verified"]
    if problem_state_reached and safe and back_up:
        result["verdict"] = "PASS"
        log("  PASS — full lifecycle driven and environment restored cleanly.")
    elif not safe:
        result["verdict"] = "FAIL_UNSAFE"
        log("  FAIL — the environment may NOT be fully restored.")
        log(f"         Check the monitor and restore from {backup_path} if needed.")
    elif not problem_state_reached:
        result["verdict"] = "INCONCLUSIVE"
        log("  INCONCLUSIVE — could not drive a problem state, but the")
        log("  environment was restored safely. Try a longer --max-wait,")
        log("  or the monitor may not support keyword checks.")
    else:
        result["verdict"] = "PARTIAL"
        log("  PARTIAL — see the steps above.")

    log(f"\n  Environment restored : {result['restored']}")
    log(f"  Restore verified     : {result['restore_verified']}")
    save_result()
    log(f"\n  Wrote {RESULT_FILE} — safe to share (contains NO token).")


if __name__ == "__main__":
    main()
