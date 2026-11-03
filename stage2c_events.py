#!/usr/bin/env python3
"""
stage2c_events.py — drive an EVENT-BASED alert cycle through response time.
===========================================================================

WHY
  A notification profile set to "Notify for every event occurrence"
  (alert_setting = 1) sends one alert per ATTRIBUTE event instead of one
  per monitor status change (NewAlertNotifier.triggerThirdpartyForEvents).
  The keyword cycle (stage2b) changes the availability check only; this
  stage changes the RESPONSE TIME attribute so the event path is exercised,
  including CRITICAL, which keywords cannot produce.

HOW (one monitor, routes run one after the other)
  Response time (attribute 3, primary location) carries two rules in the
  threshold profile, read and written with API version 2.1:
      [{"severity": 2, "value": <trouble ms>}, {"severity": 3, "value": <critical ms>}]
  Real response time is ~300-1500 ms, so:
      CRITICAL  trouble 1 ms,     critical 2 ms      (critical outranks trouble)
      TROUBLE   trouble 1 ms,     critical 60000 ms
      UP        trouble 30000 ms, critical 60000 ms
  The monitor is moved onto a DEDICATED threshold profile for the test, so
  the profile it normally shares with other monitors is never edited. The
  original profile is put back at the end, even when a step fails.

USAGE
  python3 stage2c_events.py --monitor-id 73970000003442142 \
      --profile-id 73970000004080144 \
      --routes "critical,trouble;trouble,critical"

OUTPUT  stage2c_result.json — run_all.py --events reads it. Each route is
  its own entry in "monitors" (key, route, since, until), so every route is
  verified in its own time window.
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta

import stage2b_cycle as s2

RESULT = "stage2c_result.json"
STATE_CODE = {"critical": 3, "trouble": 2, "up": 1}
# (trouble ms, critical ms) for each target state
THRESHOLDS = {"critical": (1, 2), "trouble": (1, 60000), "up": (30000, 60000)}
SETTLE_AFTER_UP = 180       # recovery alerts reach every tool within this
STATE_WAIT = 420            # max seconds to reach a state

result = {"started_at": datetime.now().isoformat(timespec="seconds"),
          "mode": "events", "steps": [], "transitions": [], "monitors": []}


def log(m=""):
    print(m, flush=True)


def now_local():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def save():
    with open(RESULT, "w", encoding="utf-8") as fh:
        json.dump(result, fh, indent=2)


def step(name, ok, detail=""):
    result["steps"].append({"step": name, "ok": bool(ok), "detail": detail,
                            "at": now_local()})
    log(f"  [{' OK ' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    save()


def rule(sev, value):
    return {"severity": sev, "comparison_operator": 1, "strategy": 1,
            "value": value, "polls_check": 1}


def set_thresholds(grid, tok, pid, profile, state):
    trouble, critical = THRESHOLDS[state]
    body = json.loads(json.dumps(profile))
    body.pop("profile_id", None)
    body["response_time_threshold"] = {"primary": [rule(2, trouble),
                                                   rule(3, critical)]}
    code, resp = s2.api(grid, f"/api/threshold_profiles/{pid}", tok,
                        method="PUT", body=body)
    ok = code in (200, 201)
    if ok:
        code2, back = s2.api(grid, f"/api/threshold_profiles/{pid}", tok)
        got = sorted((r.get("severity"), r.get("value")) for r in
                     (((back or {}).get("data") or {})
                      .get("response_time_threshold") or {}).get("primary") or [])
        ok = got == [(2, trouble), (3, critical)]
    return ok, f"trouble {trouble} ms, critical {critical} ms (HTTP {code})"


def set_monitor_profile(grid, tok, mid, monitor, pid):
    body = json.loads(json.dumps(monitor))
    body["threshold_profile_id"] = str(pid)
    code, resp = s2.api(grid, f"/api/monitors/{mid}", tok, method="PUT", body=body)
    if code not in (200, 201):
        log(f"        response: {str(resp)[:300]}")
        return False
    code, back = s2.api(grid, f"/api/monitors/{mid}", tok)
    return str(((back or {}).get("data") or {}).get("threshold_profile_id")) == str(pid)


def drive(grid, tok, mid, name, pid, profile, state, route_key):
    ok, detail = set_thresholds(grid, tok, pid, profile, state)
    step(f"{name}: thresholds for {state.upper()}", ok, detail)
    if not ok:
        return False
    s2.api(grid, f"/api/monitor/poll_now/{mid}", tok)
    reached, last, waited = s2.wait_for(grid, tok, mid, STATE_CODE[state],
                                        STATE_WAIT, poll_every=15)
    result["transitions"].append({
        "monitor_id": mid, "monitor": name, "key": route_key, "state": state,
        "observed_status": last, "at": now_local(), "waited_seconds": waited,
        "reached": reached})
    step(f"{name}: monitor is {state.upper()}", reached,
         f"status {last} after {waited}s")
    return reached


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--monitor-id", required=True)
    ap.add_argument("--profile-id", required=True,
                    help="dedicated threshold profile used only for this test")
    ap.add_argument("--routes", default="critical,trouble;trouble,critical")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    routes = [[s.strip().lower() for s in r.split(",") if s.strip()]
              for r in a.routes.split(";") if r.strip()]
    for r in routes:
        bad = [s for s in r if s not in ("critical", "trouble")]
        if bad:
            s2.die(f"route states must be critical/trouble, got {bad}")

    grid = os.environ.get("S247_GRID_URL", "").rstrip("/")
    tok = s2.get_token()
    if not grid or not tok:
        s2.die("S247_GRID_URL / token missing")
    mid, pid = str(a.monitor_id), str(a.profile_id)

    log("=" * 70)
    log("STAGE 2C — EVENT-BASED ALERT CYCLE (response time)")
    log("=" * 70)
    code, m = s2.api(grid, f"/api/monitors/{mid}", tok)
    monitor = (m or {}).get("data")
    if not monitor:
        s2.die(f"could not read monitor {mid} (HTTP {code})")
    name = monitor.get("display_name") or mid
    original_pid = str(monitor.get("threshold_profile_id"))
    code, p = s2.api(grid, f"/api/threshold_profiles/{pid}", tok)
    profile = (p or {}).get("data")
    if not profile:
        s2.die(f"could not read threshold profile {pid} (HTTP {code})")
    if pid == original_pid:
        s2.die("the test profile must not be the monitor's own profile — it "
               "may be shared with other monitors")

    backup = f"events_backup_{mid}.json"
    with open(backup, "w", encoding="utf-8") as fh:
        json.dump({"monitor": monitor, "test_profile": profile}, fh, indent=2)
    result.update({"monitor": {"monitor_id": mid, "name": name, "type": "URL"},
                   "original_threshold_profile_id": original_pid,
                   "test_threshold_profile_id": pid, "backup": backup})
    step("original config saved", True, f"{backup} (threshold profile {original_pid})")
    if a.dry_run:
        log("  DRY RUN — nothing changed.")
        result["verdict"] = "DRY_RUN"
        save()
        return 0

    verdict = "PASS"
    try:
        ok, detail = set_thresholds(grid, tok, pid, profile, "up")
        step("test profile set to the UP baseline", ok, detail)
        ok = ok and set_monitor_profile(grid, tok, mid, monitor, pid)
        step(f"{name}: switched to the test threshold profile", ok, pid)
        if not ok:
            raise RuntimeError("could not switch the monitor's threshold profile")
        s2.api(grid, f"/api/monitor/poll_now/{mid}", tok)
        up, last, waited = s2.wait_for(grid, tok, mid, 1, STATE_WAIT, poll_every=15)
        step(f"{name}: UP before the first route", up, f"status {last} after {waited}s")
        if not up:
            raise RuntimeError(f"monitor not UP before the cycle (status {last})")

        result["cycle_started_at"] = now_local()
        for n, route in enumerate(routes, 1):
            key = f"{mid}:route{n}"
            label = " → ".join(s.upper() for s in route + ["up"])
            log(f"\n  ROUTE {n}: {label}")
            since = (datetime.now() - timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S")
            # never overlap the previous route's window: its late recovery
            # rows would be counted for this route
            if result["monitors"]:
                prev_until = datetime.strptime(result["monitors"][-1]["until"],
                                               "%Y-%m-%d %H:%M:%S")
                since = max(since, (prev_until + timedelta(seconds=1))
                            .strftime("%Y-%m-%d %H:%M:%S"))
            reached_all = True
            for state in route + ["up"]:
                if not drive(grid, tok, mid, name, pid, profile, state, key):
                    reached_all = False
                    break
            log(f"  settling {SETTLE_AFTER_UP}s so the recovery alerts reach every tool")
            time.sleep(SETTLE_AFTER_UP)
            until = now_local()
            result["monitors"].append({
                "monitor_id": mid, "key": key,
                "name": f"{name} [{label}]", "search_name": name,
                "type": "URL",
                "route": route, "since": since, "until": until,
                "complete": reached_all})
            save()
            if not reached_all:
                verdict = "FAIL"
                # leave the monitor UP before trying the next route
                drive(grid, tok, mid, name, pid, profile, "up", key)
        result["cycle_ended_at"] = now_local()
    except Exception as exc:  # noqa: BLE001
        verdict = "FAIL"
        result["error"] = str(exc)
        step("cycle stopped", False, str(exc))
    finally:
        # RESTORE: original profile back on the monitor, test profile at UP.
        okp = set_monitor_profile(grid, tok, mid, monitor, original_pid)
        okt, _ = set_thresholds(grid, tok, pid, profile, "up")
        result["restored"] = bool(okp and okt)
        step(f"{name}: original threshold profile restored", okp, original_pid)
        step("test profile left at the UP baseline", okt)
        s2.api(grid, f"/api/monitor/poll_now/{mid}", tok)
        up, last, waited = s2.wait_for(grid, tok, mid, 1, STATE_WAIT, poll_every=15)
        result["restore_verified"] = bool(okp and up)
        step(f"{name}: UP after restore", up, f"status {last}")
        if not result["restored"]:
            verdict = "FAIL_UNSAFE"
        result["verdict"] = verdict
        save()
    log(f"\n  CYCLE VERDICT: {verdict}")
    return 0 if verdict == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
