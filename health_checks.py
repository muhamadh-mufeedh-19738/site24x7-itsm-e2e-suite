#!/usr/bin/env python3
"""
health_checks.py — 10-second checks at the start of every full run.
===================================================================

WHY
  9 Nov: builds #88/#89 failed after minutes with "BLOCKED ... usually
  login, cookie or credentials". The real causes were:
    * the deployed build (USER_ABILASH_COMMON_MATH_REMOVAL/Nov_02_2026)
      answered HTTP 200 to every monitor edit but SAVED NOTHING, so no
      alert could ever fire (a PRODUCT defect), and
    * the ServiceNow developer instance was asleep (ServiceNow hibernates
      idle developer instances), so every ServiceNow search got its
      "wake up" page.
  Neither can be prevented by the suite. Both are now detected up front
  and reported plainly at the top of the report.

CHECKS
  monitor_edit   Switch the first event monitor to its TEST threshold profile
                 (first set to the all-UP baseline, so no alert can fire),
                 read it back, switch it back. Not saved -> PRODUCT DEFECT.
  servicenow     Each ServiceNow instance the account uses must answer like
                 ServiceNow, not with the developer-portal wake-up page.

OUTPUT  accounts/<account>/health_checks.json  (read by summary_report.py)
EXIT    0 always (a failed check is reported, it never stops the run)
"""

import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


def log(m=""):
    print(m, flush=True)


def check_monitor_edit(grid, tok, ev):
    import stage2b_cycle as s2
    import stage2c_events as s2c
    mons = (ev or {}).get("monitors") or []
    if not mons and (ev or {}).get("monitor_id"):
        mons = [ev]
    if not mons:
        return {"check": "monitor_edit", "status": "SKIP",
                "detail": "no event monitor configured (events.json) to test with"}
    mid, test_pid = str(mons[0]["monitor_id"]), str(mons[0]["profile_id"])
    c, m = s2.api(grid, f"/api/monitors/{mid}", tok)
    mon = (m or {}).get("data")
    if not mon:
        return {"check": "monitor_edit", "status": "BLOCKED",
                "detail": f"could not read monitor {mid} (HTTP {c})"}
    name = mon.get("display_name") or mid
    orig = str(mon.get("threshold_profile_id"))
    if orig == test_pid:
        # an interrupted earlier run left it on the test profile: the event
        # cycle restores it from its backup; do not touch it here.
        return {"check": "monitor_edit", "status": "SKIP",
                "detail": f"{name} is still on its test profile from an earlier run "
                          f"(the event cycle restores it)"}
    c, p = s2.api(grid, f"/api/threshold_profiles/{test_pid}", tok)
    prof = (p or {}).get("data")
    if not prof:
        return {"check": "monitor_edit", "status": "BLOCKED",
                "detail": f"could not read the test threshold profile {test_pid} (HTTP {c})"}
    ok, how = s2c.set_thresholds(grid, tok, test_pid, prof, "up")
    if not ok:
        return {"check": "monitor_edit", "status": "DEFECT",
                "detail": f"Site24x7 did not save a THRESHOLD PROFILE change ({how})"}
    body = json.loads(json.dumps(mon))
    body["threshold_profile_id"] = test_pid
    c, resp = s2.api(grid, f"/api/monitors/{mid}", tok, method="PUT", body=body)
    c2, back = s2.api(grid, f"/api/monitors/{mid}", tok)
    saved = str(((back or {}).get("data") or {}).get("threshold_profile_id")) == test_pid
    restored = True
    if saved:
        body["threshold_profile_id"] = orig
        s2.api(grid, f"/api/monitors/{mid}", tok, method="PUT", body=body)
        c3, b3 = s2.api(grid, f"/api/monitors/{mid}", tok)
        restored = str(((b3 or {}).get("data") or {}).get("threshold_profile_id")) == orig
    if saved and restored:
        return {"check": "monitor_edit", "status": "PASS",
                "detail": f"monitor edits are saved ({name}: changed, read back, restored)"}
    if saved and not restored:
        return {"check": "monitor_edit", "status": "DEFECT",
                "detail": f"{name}: the change was saved but switching it BACK was not "
                          f"(left on test profile {test_pid}, which is at the all-UP "
                          f"baseline). Restore its threshold profile {orig} by hand."}
    answer = "an empty answer" if resp in ({}, None) else f"answer {json.dumps(resp)[:120]}"
    return {"check": "monitor_edit", "status": "DEFECT",
            "detail": (f"Site24x7 does NOT save monitor changes on this build: "
                       f"PUT /api/monitors/{mid} ({name}) returned HTTP {c} with "
                       f"{answer}, but reading it back shows nothing changed. "
                       f"No status change can be driven, so no alert can fire — "
                       f"every lifecycle result below is a consequence of this.")}


def check_servicenow(d):
    urls = set()
    for fname in ("integration_templates.json", "integrations.json"):
        try:
            data = json.load(open(os.path.join(d, fname)))
        except (OSError, ValueError):
            continue
        for i in data.get("integrations") or []:
            src = i.get("template") or i.get("raw") or {}
            if str(i.get("type_id") or src.get("type")) == "11" and src.get("instance_url"):
                urls.add(src["instance_url"].rstrip("/"))
    if not urls:
        return []
    ctx = ssl.create_default_context()
    out = []
    for u in sorted(urls):
        req = urllib.request.Request(u + "/api/now/table/incident?sysparm_limit=1",
                                     headers={"Accept": "application/json"})
        status, body = None, ""
        try:
            with urllib.request.urlopen(req, timeout=30, context=ctx) as r:
                status, body = r.status, r.read(3000).decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            status, body = e.code, e.read(3000).decode("utf-8", "replace")
        except Exception as exc:  # noqa: BLE001
            out.append({"check": "servicenow", "status": "BLOCKED",
                        "detail": f"{u} did not answer ({str(exc)[:100]})"})
            continue
        asleep = bool(re.search(r"developer\.servicenow\.com|hibernat|instance is (?:currently )?(?:asleep|sleeping)",
                                body, re.I))
        if asleep:
            out.append({"check": "servicenow", "status": "BLOCKED",
                        "detail": (f"the ServiceNow developer instance {u} is ASLEEP "
                                   f"(ServiceNow hibernates idle developer instances). "
                                   f"Wake it: sign in at https://developer.servicenow.com "
                                   f"and open the instance, wait until it is up, re-run. "
                                   f"ServiceNow results in this run cannot be trusted.")})
        else:
            out.append({"check": "servicenow", "status": "PASS",
                        "detail": f"{u} is awake (HTTP {status})"})
    return out


def main():
    account = os.environ.get("S247_ACCOUNT", "")
    grid = os.environ.get("S247_GRID_URL", "").rstrip("/")
    d = os.path.join(HERE, "accounts", account)
    results = []
    try:
        import stage1_inventory as s1
        tok = s1.get_token()
        ev = None
        try:
            ev = json.load(open(os.path.join(d, "events.json")))
        except (OSError, ValueError):
            pass
        results.append(check_monitor_edit(grid, tok, ev))
    except SystemExit:
        results.append({"check": "monitor_edit", "status": "BLOCKED",
                        "detail": "no API token, monitor edits not checked"})
    except Exception as exc:  # noqa: BLE001
        results.append({"check": "monitor_edit", "status": "BLOCKED",
                        "detail": f"check crashed: {str(exc)[:150]}"})
    try:
        results += check_servicenow(d)
    except Exception as exc:  # noqa: BLE001
        results.append({"check": "servicenow", "status": "BLOCKED",
                        "detail": f"check crashed: {str(exc)[:150]}"})
    for r in results:
        log(f"  [{r['status']:<7}] {r['check']}: {r['detail']}")
    with open(os.path.join(d, "health_checks.json"), "w") as fh:
        json.dump({"run_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                   "build": os.environ.get("S247_BUILD_LABEL", ""),
                   "results": results}, fh, indent=2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
