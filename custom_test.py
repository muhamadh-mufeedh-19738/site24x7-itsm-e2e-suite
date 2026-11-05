#!/usr/bin/env python3
"""
custom_test.py — start the Jenkins CUSTOM TEST job and wait for its report.
==========================================================================

Used by the custom-testing thread (see CUSTOM_TESTING.md). It only starts
the job S247-Integrations-Custom-Test with the options given here, waits,
and prints the result and the report link. It never changes suite code,
never deploys, and never touches the sanity job.

EXAMPLES
  python3 custom_test.py -a automation --integrations "Slack Sanity,ServiceNow*" \\
      --alerts events --event-routes "down,critical" --notes "Slack + SNOW after fix"
  python3 custom_test.py -a automation --alerts consolidated --cons-route "trouble,down"
  python3 custom_test.py -a automation --dry-run      (check the options only)

OPTIONS (all optional except -a)
  --integrations   names, comma separated, * wildcard; empty = all
  --alerts         both | consolidated | events
  --cons-route     consolidated states per monitor, ';' between monitors
                   (down, trouble; UP is added at the end)
  --event-routes   event states per event monitor, ';' between monitors
                   (down, trouble, critical; UP is added at the end)
  --no-add         test the integrations as they are (no delete + add)
  --notes          why this run; shown at the top of the report
  --grid           default integrations-qa
  --no-wait        start it and return immediately

Uses the Jenkins login in .jenkins.env (git-ignored). Prints no secrets.
"""

import argparse
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
JOB = "S247-Integrations-Custom-Test"


def conf():
    c = {}
    p = os.path.join(HERE, ".jenkins.env")
    if os.path.isfile(p):
        for line in open(p, encoding="utf-8"):
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                c[k.replace("export ", "").strip()] = v.strip().strip("'\"")
    if not (c.get("JENKINS_USER") and c.get("JENKINS_API_TOKEN")):
        sys.exit("[BLOCKER] Jenkins login not set up (python3 jenkins_secrets.py --setup)")
    c.setdefault("JENKINS_URL", "http://10.164.175.10:8100")
    c["JENKINS_URL"] = c["JENKINS_URL"].rstrip("/")
    return c


class J:
    def __init__(self, c):
        self.url = c["JENKINS_URL"]
        self.auth = "Basic " + base64.b64encode(
            f'{c["JENKINS_USER"]}:{c["JENKINS_API_TOKEN"]}'.encode()).decode()
        self.crumb = None

    def call(self, method, path, data=None, absolute=False):
        req = urllib.request.Request(path if absolute else self.url + path,
                                     data=data, method=method)
        req.add_header("Authorization", self.auth)
        if method == "POST":
            if self.crumb is None:
                _, b, _ = self.call("GET", "/crumbIssuer/api/json")
                cb = json.loads(b or "{}")
                self.crumb = (cb.get("crumbRequestField"), cb.get("crumb"))
            if self.crumb[0]:
                req.add_header(*self.crumb)
            req.add_header("Content-Type", "application/x-www-form-urlencoded")
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.status, r.read().decode("utf-8", "replace"), r.headers
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace"), e.headers


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-a", "--account", required=True, choices=["automation", "tpt", "tpt1"])
    ap.add_argument("--grid", default="integrations-qa")
    ap.add_argument("--integrations", default="")
    ap.add_argument("--alerts", default="both", choices=["both", "consolidated", "events"])
    ap.add_argument("--cons-route", default="")
    ap.add_argument("--event-routes", default="")
    ap.add_argument("--no-add", action="store_true")
    ap.add_argument("--notes", default="")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-wait", action="store_true")
    a = ap.parse_args()

    # Same checks the suite makes, BEFORE anything is started.
    os.environ.update({"S247_TEST_INTEGRATIONS": a.integrations, "S247_TEST_ALERTS": a.alerts,
                       "S247_TEST_CONS_ROUTES": a.cons_route, "S247_TEST_EVENT_ROUTES": a.event_routes,
                       "S247_TEST_ADD_INTEGRATIONS": "no" if a.no_add else "yes",
                       "S247_TEST_NOTES": a.notes})
    sys.path.insert(0, HERE)
    import itsm  # noqa: E402
    itsm.custom_plan()
    names = [n.strip() for n in a.integrations.split(",") if n.strip()]
    if names:
        live = json.load(open(os.path.join(HERE, "accounts", a.account, "integrations.json")))
        import fnmatch
        have = [i["name"] for i in live.get("integrations") or []]
        for n in names:
            if not any(fnmatch.fnmatch(h.lower(), n.lower()) for h in have):
                sys.exit(f"[BLOCKER] no integration called {n!r} on {a.account}. "
                         f"Names: {', '.join(sorted(have))}")
    params = {"ACCOUNT": a.account, "GRID": a.grid, "INTEGRATIONS": a.integrations,
              "ALERTS": a.alerts, "CONSOLIDATED_ROUTE": a.cons_route,
              "EVENT_ROUTES": a.event_routes,
              "ADD_INTEGRATIONS": "false" if a.no_add else "true", "TEST_NOTES": a.notes}
    print("CUSTOM TEST")
    for k, v in params.items():
        print(f"  {k:<19}: {v or '(default)'}")
    if a.dry_run:
        print("\n  [OK] options are valid (dry run — nothing started)")
        return 0

    j = J(conf())
    st, body, hdr = j.call("POST", f"/job/{JOB}/buildWithParameters",
                           urllib.parse.urlencode(params).encode())
    if st not in (200, 201):
        sys.exit(f"[BLOCKER] Jenkins did not start the job (HTTP {st}): {body[:200]}")
    queue = (hdr.get("Location") or "").rstrip("/")
    print(f"\n  queued: {queue}")
    build = None
    for _ in range(120):                       # up to 10 min in the queue
        st, body, _ = j.call("GET", queue + "/api/json", absolute=True)
        q = json.loads(body or "{}")
        if q.get("executable"):
            build = q["executable"]["url"].rstrip("/")
            break
        if q.get("cancelled"):
            sys.exit("[BLOCKER] the queued build was cancelled")
        why = q.get("why") or ""
        if "account-" in why or "lock" in why.lower():
            print(f"  waiting: {why[:120]}")
        time.sleep(5)
    if not build:
        sys.exit("[BLOCKER] still queued after 10 minutes")
    print(f"  build : {build}/console")
    if a.no_wait:
        return 0
    t0 = time.time()
    while True:
        st, body, _ = j.call("GET", build + "/api/json?tree=building,result,description",
                             absolute=True)
        b = json.loads(body or "{}")
        if not b.get("building"):
            break
        print(f"  running ... {int(time.time() - t0) // 60} min", flush=True)
        time.sleep(60)
    print(f"\n  RESULT : {b.get('result')}  (SUCCESS = all passed, UNSTABLE = "
          f"defects found, FAILURE = could not test)")
    if b.get("description"):
        print(f"  changes: {b['description']}")
    print(f"  REPORT : {build}/Custom_20Test_20Report/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
