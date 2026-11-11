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
  Response time (attribute 3, primary location) carries three rules in the
  threshold profile, read and written with API version 2.1:
      [{"severity": 2, "value": <trouble ms>}, {"severity": 3, "value": <critical ms>},
       {"severity": 0, "value": <down ms>}]
  Severity 0 = DOWN: a DOWN-severity attribute also makes the monitor DOWN
  (NewAlertNotifier.triggerThirdpartyForEvents: "Only a DOWN(0) attribute
  propagates to availability"), and the product must NOT send a second,
  availability DOWN alert for it. Real response time is ~300-1500 ms, so:
      DOWN      trouble 1 ms,     critical 2 ms,     down 3 ms
      CRITICAL  trouble 1 ms,     critical 2 ms,     down 60000 ms
      TROUBLE   trouble 1 ms,     critical 60000 ms, down 60000 ms
      UP        trouble 30000 ms, critical 60000 ms, down 60000 ms
  The monitor is moved onto a DEDICATED threshold profile for the test, so
  the profile it normally shares with other monitors is never edited. The
  original profile is put back at the end, even when a step fails.

USAGE
  python3 stage2c_events.py --monitor-id 73970000003442142 \
      --profile-id 73970000004080144 \
      --routes "down,trouble,critical;down,critical,trouble"

OUTPUT  stage2c_result.json — run_all.py --events reads it. Each route is
  its own entry in "monitors" (key, route, since, until), so every route is
  verified in its own time window.
"""
import argparse
import json
import os
import sys
import threading
import time
from datetime import datetime, timedelta

import stage2b_cycle as s2

RESULT = "stage2c_result.json"
STATE_CODE = {"down": 0, "critical": 3, "trouble": 2, "up": 1}
# (trouble ms, critical ms, down ms) for each target state
THRESHOLDS = {"down": (1, 2, 3), "critical": (1, 2, 60000),
              "trouble": (1, 60000, 60000), "up": (30000, 60000, 60000)}
ROUTE_STATES = ("down", "critical", "trouble")
SETTLE_AFTER_UP = 90        # recovery rows land within ~30s (3 Nov runs);
                            # the in-tool checks retry if a tool lags
STATE_WAIT = 420            # max seconds to reach a state

result = {"started_at": datetime.now().isoformat(timespec="seconds"),
          "mode": "events", "steps": [], "transitions": [], "monitors": []}


LOCK = threading.RLock()          # workers share `result` and the result file


def log(m=""):
    print(m, flush=True)


def now_local():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def save():
    with LOCK:
        with open(RESULT, "w", encoding="utf-8") as fh:
            json.dump(result, fh, indent=2)


def step(name, ok, detail=""):
    with LOCK:
        result["steps"].append({"step": name, "ok": bool(ok), "detail": detail,
                                "at": now_local()})
    log(f"  [{' OK ' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    save()


def rule(sev, value):
    return {"severity": sev, "comparison_operator": 1, "strategy": 1,
            "value": value, "polls_check": 1}


def set_thresholds(grid, tok, pid, profile, state):
    trouble, critical, down = THRESHOLDS[state]
    body = json.loads(json.dumps(profile))
    body.pop("profile_id", None)
    body["response_time_threshold"] = {"primary": [rule(2, trouble),
                                                   rule(3, critical),
                                                   rule(0, down)]}
    code, resp = s2.api(grid, f"/api/threshold_profiles/{pid}", tok,
                        method="PUT", body=body)
    ok = code in (200, 201)
    if ok:
        code2, back = s2.api(grid, f"/api/threshold_profiles/{pid}", tok)
        got = sorted((r.get("severity"), r.get("value")) for r in
                     (((back or {}).get("data") or {})
                      .get("response_time_threshold") or {}).get("primary") or [])
        ok = got == [(0, down), (2, trouble), (3, critical)]
    return ok, (f"trouble {trouble} ms, critical {critical} ms, down {down} ms "
                f"(HTTP {code})")


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
                                        STATE_WAIT, poll_every=10)
    with LOCK:
        result["transitions"].append({
            "monitor_id": mid, "monitor": name, "key": route_key, "state": state,
            "observed_status": last, "at": now_local(), "waited_seconds": waited,
            "reached": reached})
    step(f"{name}: monitor is {state.upper()}", reached,
         f"status {last} after {waited}s")
    return reached


def parse_route(text):
    route = [x.strip().lower() for x in text.split(",") if x.strip()]
    bad = [x for x in route if x not in ROUTE_STATES]
    if not route or bad:
        s2.die(f"route states must be down/critical/trouble, got {text!r}")
    return route


def prepare(grid, tok, mid, pid, test_pids):
    """Read the monitor and its test profile, decide its TRUE own profile
    and save a backup. Returns a dict, or dies with the reason."""
    code, m = s2.api(grid, f"/api/monitors/{mid}", tok)
    monitor = (m or {}).get("data")
    if not monitor:
        s2.die(f"could not read monitor {mid} (HTTP {code})")
    name = monitor.get("display_name") or mid
    original_pid = str(monitor.get("threshold_profile_id"))
    backup = f"events_backup_{mid}.json"
    if original_pid in test_pids:
        # Still on a TEST profile (an interrupted run, or a copy made while
        # another run was in progress): its real profile is in the backup.
        try:
            with open(backup, encoding="utf-8") as fh:
                saved = json.load(fh)["monitor"]
            real = str(saved.get("threshold_profile_id"))
        except (OSError, ValueError, KeyError):
            real = ""
        if not real or real in test_pids:
            s2.die(f"{name}: its own threshold profile ({original_pid}) is one "
                   f"of the TEST profiles and no backup says what it should be. "
                   f"Put the monitor back on its normal profile in Site24x7 and "
                   f"re-run. Nothing was changed.")
        log(f"  [note] {name} was still on test profile {original_pid}; its "
            f"real profile {real} comes from {backup}")
        monitor = dict(monitor, threshold_profile_id=real)
        original_pid = real
    code, pr = s2.api(grid, f"/api/threshold_profiles/{pid}", tok)
    profile = (pr or {}).get("data")
    if not profile:
        s2.die(f"could not read threshold profile {pid} (HTTP {code})")
    with open(backup, "w", encoding="utf-8") as fh:
        json.dump({"monitor": monitor, "test_profile": profile}, fh, indent=2)
    step(f"{name}: original config saved", True,
         f"{backup} (threshold profile {original_pid})")
    return {"mid": mid, "pid": pid, "name": name, "monitor": monitor,
            "profile": profile, "original_pid": original_pid, "backup": backup}


def worker(grid, tok, w, routes, outcome):
    """One monitor: switch to its test profile, run its route(s) one after
    the other, ALWAYS restore. Several workers run in parallel."""
    mid, pid, name = w["mid"], w["pid"], w["name"]
    monitor, profile = w["monitor"], w["profile"]
    verdict, restored, verified = "PASS", False, False
    try:
        ok, detail = set_thresholds(grid, tok, pid, profile, "up")
        step(f"{name}: test profile set to the UP baseline", ok, detail)
        ok = ok and set_monitor_profile(grid, tok, mid, monitor, pid)
        step(f"{name}: switched to the test threshold profile", ok, pid)
        if not ok:
            raise RuntimeError("could not switch the monitor's threshold profile")
        s2.api(grid, f"/api/monitor/poll_now/{mid}", tok)
        up, last, waited = s2.wait_for(grid, tok, mid, 1, STATE_WAIT, poll_every=10)
        step(f"{name}: UP before its route", up, f"status {last} after {waited}s")
        if not up:
            raise RuntimeError(f"monitor not UP before the cycle (status {last})")
        prev_until = None
        for n, (rnum, route) in enumerate(routes):
            key = f"{mid}:route{rnum}"
            label = " → ".join(x.upper() for x in route + ["up"])
            log(f"\n  {name} — ROUTE {rnum}: {label}")
            since = (datetime.now() - timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S")
            # never overlap this monitor's previous route window
            if prev_until:
                since = max(since, (datetime.strptime(prev_until, "%Y-%m-%d %H:%M:%S")
                                    + timedelta(seconds=1)).strftime("%Y-%m-%d %H:%M:%S"))
            reached_all = True
            for state in route + ["up"]:
                if not drive(grid, tok, mid, name, pid, profile, state, key):
                    reached_all = False
                    break
            log(f"  {name}: settling {SETTLE_AFTER_UP}s so the recovery alerts reach every tool")
            time.sleep(SETTLE_AFTER_UP)
            prev_until = now_local()
            with LOCK:
                result["monitors"].append({
                    "monitor_id": mid, "key": key,
                    "name": f"{name} [{label}]", "search_name": name,
                    "type": "URL", "route": route, "since": since,
                    "until": prev_until, "complete": reached_all})
                save()
            if not reached_all:
                verdict = "FAIL"
                drive(grid, tok, mid, name, pid, profile, "up", key)
    except Exception as exc:  # noqa: BLE001
        verdict = "FAIL"
        with LOCK:
            result.setdefault("errors", []).append(f"{name}: {exc}")
            # 11 Nov (#93): a monitor that stopped BEFORE its route wrote no
            # entry, so verification dropped it (E2E Events Monitor 2 vanished
            # from the report) or showed "route unknown". Record every route
            # that never ran, with the reason, so the report shows it.
            done = {m["key"] for m in result["monitors"]}
            for rnum, route in routes:
                key = f"{mid}:route{rnum}"
                if key in done:
                    continue
                label = " → ".join(x.upper() for x in route + ["up"])
                result["monitors"].append({
                    "monitor_id": mid, "key": key, "name": f"{name} [{label}]",
                    "search_name": name, "type": "URL", "route": route,
                    "since": now_local(), "until": now_local(), "complete": False,
                    "not_driven": True,
                    "reason": f"{name} was never driven into {route[0].upper()}: {exc}"})
            save()
        step(f"{name}: cycle stopped", False, str(exc))
    finally:
        okp = set_monitor_profile(grid, tok, mid, monitor, w["original_pid"])
        okt, _ = set_thresholds(grid, tok, pid, profile, "up")
        restored = bool(okp and okt)
        step(f"{name}: original threshold profile restored", okp, w["original_pid"])
        step(f"{name}: test profile left at the UP baseline", okt)
        s2.api(grid, f"/api/monitor/poll_now/{mid}", tok)
        up, last, waited = s2.wait_for(grid, tok, mid, 1, STATE_WAIT, poll_every=10)
        verified = bool(okp and up)
        step(f"{name}: UP after restore", up, f"status {last}")
        if not restored:
            verdict = "FAIL_UNSAFE"
        outcome[mid] = {"name": name, "verdict": verdict, "restored": restored,
                        "restore_verified": verified,
                        "original_threshold_profile_id": w["original_pid"],
                        "test_threshold_profile_id": pid, "backup": w["backup"]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pair", action="append", default=[],
                    metavar="MONITOR_ID:PROFILE_ID:ROUTE",
                    help="one monitor, its dedicated test threshold profile and "
                         "its route, e.g. 739...:739...:critical,trouble. Give "
                         "it once per monitor; the monitors run IN PARALLEL.")
    ap.add_argument("--monitor-id", help="(single monitor) monitor id")
    ap.add_argument("--profile-id", help="(single monitor) test threshold profile")
    ap.add_argument("--routes", default="down,trouble,critical;down,critical,trouble",
                    help="(single monitor) routes, run one after the other")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    # plan: [(monitor_id, profile_id, [(route_no, route), ...]), ...]
    plan = []
    if a.pair:
        for n, spec in enumerate(a.pair, 1):
            parts = spec.split(":", 2)
            if len(parts) != 3:
                s2.die(f"--pair must be MONITOR_ID:PROFILE_ID:ROUTE, got {spec!r}")
            plan.append((parts[0].strip(), parts[1].strip(),
                         [(n, parse_route(parts[2]))]))
    elif a.monitor_id and a.profile_id:
        routes = [parse_route(r) for r in a.routes.split(";") if r.strip()]
        plan.append((str(a.monitor_id), str(a.profile_id),
                     list(enumerate(routes, 1))))
    else:
        s2.die("give --pair (one per monitor) or --monitor-id + --profile-id")
    mids = [x[0] for x in plan]
    test_pids = {x[1] for x in plan}
    if len(set(mids)) != len(mids) or len(test_pids) != len(plan):
        s2.die("each monitor needs its OWN test profile: two monitors on one "
               "profile would move each other")

    grid = os.environ.get("S247_GRID_URL", "").rstrip("/")
    tok = s2.get_token()
    if not grid or not tok:
        s2.die("S247_GRID_URL / token missing")

    log("=" * 70)
    log("STAGE 2C — EVENT-BASED ALERT CYCLE (response time)"
        + (f" — {len(plan)} monitors in parallel" if len(plan) > 1 else ""))
    log("=" * 70)
    workers = [prepare(grid, tok, mid, pid, test_pids) for mid, pid, _ in plan]
    first = workers[0]
    result.update({"monitor": {"monitor_id": first["mid"], "name": first["name"], "type": "URL"},
                   "original_threshold_profile_id": first["original_pid"],
                   "test_threshold_profile_id": first["pid"], "backup": first["backup"]})
    if a.dry_run:
        log("  DRY RUN — nothing changed.")
        result["verdict"] = "DRY_RUN"
        save()
        return 0

    outcome = {}
    result["cycle_started_at"] = now_local()
    threads = [threading.Thread(target=worker, args=(grid, tok, w, routes, outcome),
                                name=w["name"])
               for w, (_, _, routes) in zip(workers, plan)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    result["cycle_ended_at"] = now_local()

    result["per_monitor"] = outcome
    result["restored"] = all(o["restored"] for o in outcome.values()) and len(outcome) == len(plan)
    result["restore_verified"] = all(o["restore_verified"] for o in outcome.values()) and len(outcome) == len(plan)
    verdicts = [o["verdict"] for o in outcome.values()]
    verdict = ("FAIL_UNSAFE" if "FAIL_UNSAFE" in verdicts or len(outcome) != len(plan)
               else "FAIL" if "FAIL" in verdicts else "PASS")
    if result.get("errors"):
        result["error"] = "; ".join(result["errors"])
    result["verdict"] = verdict
    result["monitors"].sort(key=lambda m: m["key"].split("route")[-1])
    save()
    log(f"\n  CYCLE VERDICT: {verdict}")
    return 0 if verdict == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
