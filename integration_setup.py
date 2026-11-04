#!/usr/bin/env python3
"""
integration_setup.py — ADD the integrations at the start of every run.
======================================================================

WHY
  "Add integration" is part of what the suite must prove works. So every
  run deletes each covered integration and adds it again, with
  DOWN/TROUBLE/CRITICAL on, All Monitors selected, its hook URL / key and
  its credential profile or Zoho connection, then reads it back and checks
  every setting. The status-change validation that follows uses these
  freshly added integrations (they keep the same names, so every later
  stage finds them as before).

COVERED NOW (6):  Slack, Discord, PagerDuty, ServiceNow, Zoho Desk,
                  ServiceDesk Plus OnDemand.
ON HOLD:          TOPdesk, Mattermost, iTOC360 (new framework, not in the
                  default build yet) — left exactly as they are.

FILES (per account folder accounts/<account>/)
  integration_templates.json   settings to add with, NO secrets (committed).
                               Made once with --snapshot.
  .itsm.env  (INTEG_SECRET_*)  hook URLs / PagerDuty key (git-ignored).
                               Entered once with --secrets (hidden input).
                               Jenkins gets it in S247_<ACCT>_ITSM_ENV.
  add_integrations.json        this run's result, read by the report.

USAGE
  python3 integration_setup.py -a automation --snapshot   # save templates
  python3 integration_setup.py -a automation --secrets    # hidden input
  python3 integration_setup.py -a automation --check      # dry check only
  python3 integration_setup.py -a automation              # delete + add + verify
  (itsm.py --full runs the last one automatically before the suite.)

SAFETY
  * Only integrations listed in integration_templates.json are touched.
  * Nothing is deleted unless its template AND (if needed) its secret are
    present — a run can never delete something it cannot add back.
  * Add is retried; if it still fails the tool is reported BLOCKED and the
    exact command to add it again is printed.
  * Secret values are never printed or written anywhere except the
    git-ignored secrets file.
"""

import argparse
import getpass
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))

# type id -> (API path, secret field or None, label)
TYPES = {
    1:  ("pager_duty",  "service_key", "PagerDuty"),
    4:  ("sdpod",       None,          "ServiceDesk Plus OnDemand"),
    5:  ("slack",       "url",         "Slack"),
    11: ("service_now", None,          "ServiceNow"),
    20: ("zdesk",       None,          "Zoho Desk"),
    26: ("discord",     "url",         "Discord"),
}
# Fields the server owns or that are never returned — not part of the
# template and not compared on read-back.
SERVER_FIELDS = {"service_id", "service_status", "is_edit_allowed"}
SECRET_FIELDS = {"url", "service_key", "token", "password", "api_key",
                 "username"}
ALERT_FLAGS = ("down_alert", "trouble_alert", "critical_alert")


def log(m=""):
    print(m, flush=True)


def read_env_file(path):
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
            v = v.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
                v = v[1:-1]
            env[k.strip()] = v
    return env


def slug(name):
    return re.sub(r"[^A-Z0-9]+", "_", name.upper()).strip("_")


def secret_key(name):
    return "INTEG_SECRET_" + slug(name)


# ---------------------------------------------------------------- HTTP ----
class Api:
    """Site24x7 web API with the suite's saved session (cookie + CSRF),
    the same way stage3 reads the Alert Log. Works on the Mac and Jenkins."""

    def __init__(self, grid, cookie, csrf):
        self.grid = grid.rstrip("/")
        self.h = {
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "Cookie": cookie,
            "x-zcsrf-token": csrf,
            "Referer": f"{self.grid}/app/client?a=f",
            "Origin": self.grid,
            "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"),
            "time-zone": "Asia/Kolkata",
        }
        self.ctx = ssl.create_default_context()
        self.ctx.check_hostname = False
        self.ctx.verify_mode = ssl.CERT_NONE

    def call(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.grid + "/app/api" + path, data=data,
                                     method=method, headers=self.h)
        try:
            with urllib.request.urlopen(req, timeout=60, context=self.ctx) as r:
                raw = r.read().decode("utf-8", "replace")
                return r.status, (json.loads(raw) if raw.strip() else {})
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read().decode("utf-8", "replace"))
            except Exception:  # noqa: BLE001
                return e.code, {}
        except Exception as exc:  # noqa: BLE001
            return None, {"message": str(exc)[:200]}

    def list(self):
        c, b = self.call("GET", "/third_party_services")
        if c != 200:
            raise RuntimeError(f"could not list integrations (HTTP {c}: "
                               f"{(b or {}).get('message', '')})")
        return [x for x in (b.get("data") or []) if x.get("service_id")]

    def get(self, type_id, service_id):
        p = TYPES[type_id][0]
        path = "/integration/sdpod" if type_id == 4 else f"/integration/{p}/{service_id}"
        c, b = self.call("GET", path)
        return c, (b or {}).get("data") or {}, (b or {}).get("message", "")

    def add(self, type_id, body):
        c, b = self.call("POST", "/integration/" + TYPES[type_id][0], body)
        d = (b or {}).get("data") or {}
        return c, (b or {}).get("code"), d, (b or {}).get("message", "")

    def delete(self, service_id):
        c, b = self.call("DELETE", f"/integration/thirdparty_service/{service_id}")
        return c, (b or {}).get("code"), (b or {}).get("message", "")


# ------------------------------------------------------------ template ----
def make_template(type_id, data):
    """GET response -> what we POST to add it again. No secrets, no ids;
    DOWN/TROUBLE/CRITICAL on and All Monitors forced (the suite's rule)."""
    t = {k: v for k, v in data.items()
         if k not in SERVER_FIELDS and k not in SECRET_FIELDS}
    t["type"] = type_id
    t["selection_type"] = 0
    t.pop("monitors", None)
    if type_id == 4:                       # SDP OD keeps its settings in details
        det = dict(t.get("details") or {})
        for f in ALERT_FLAGS:
            det[f] = True
        t["details"] = det
    else:
        for f in ALERT_FLAGS:
            t[f] = True
    return t


def compare(template, got):
    """Every template field must read back the same. Returns mismatches."""
    bad = []

    def walk(exp, act, pre=""):
        for k, v in exp.items():
            if k in SERVER_FIELDS or k == "type":
                continue
            a = act.get(k) if isinstance(act, dict) else None
            if isinstance(v, dict):
                walk(v, a or {}, pre + k + ".")
            elif str(a) != str(v) and not (v in ([], "", None) and a in ([], "", None)):
                bad.append(f"{pre}{k}: expected {short(v)}, got {short(a)}")
    walk(template, got)
    return bad


def short(v):
    s = json.dumps(v) if not isinstance(v, str) else v
    return s if len(s) <= 40 else s[:37] + "..."


# ----------------------------------------------------------------- main ---
def account_ctx(account, grid_name):
    d = os.path.join(HERE, "accounts", account)
    if not os.path.isdir(d):
        sys.exit(f"[BLOCKER] no account folder {d}")
    grids = json.load(open(os.path.join(HERE, "grids.json")))["grids"]
    grid_name = grid_name or os.environ.get("S247_GRID_NAME") or "integrations-qa"
    grid = os.environ.get("S247_GRID_URL") or grids[grid_name]["url"]
    sess = read_env_file(os.path.join(d, f".session.{grid_name}.env"))
    cookie = os.environ.get("S247_SESSION_COOKIE") or sess.get("S247_SESSION_COOKIE", "")
    csrf = os.environ.get("S247_CSRF_TOKEN") or sess.get("S247_CSRF_TOKEN", "")
    if not csrf:
        m = re.search(r"s247cname=([^;]+)", cookie)
        csrf = ("s247pname=" + m.group(1)) if m else ""
    return d, grid, cookie, csrf


def do_snapshot(api, d):
    out = {"_comment": ("Settings the suite ADDS these integrations with on "
                        "every run (integration_setup.py). No secrets here: "
                        "hook URLs / keys live in .itsm.env (INTEG_SECRET_*)."),
           "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
           "integrations": []}
    for it in api.list():
        tid = int(it.get("type") or 0)
        if tid not in TYPES:
            continue
        c, data, msg = api.get(tid, it["service_id"])
        if c != 200 or not data:
            log(f"  [FAIL] {it['name']}: could not read settings (HTTP {c} {msg})")
            continue
        tpl = make_template(tid, data)
        out["integrations"].append({"name": it["name"], "type_id": tid,
                                    "app": TYPES[tid][2],
                                    "secret_field": TYPES[tid][1],
                                    "template": tpl})
        filled = sorted(k for k, v in tpl.items() if v not in ("", None, []))
        log(f"  [OK] {it['name']:<28} {TYPES[tid][2]:<26} fields: {', '.join(filled)}")
    path = os.path.join(d, "integration_templates.json")
    with open(path, "w") as fh:
        json.dump(out, fh, indent=2)
    log(f"\n  saved {len(out['integrations'])} template(s): {path}")


def do_secrets(d, templates):
    """Hidden input. Stored in the account's .itsm.env (git-ignored), which
    already travels to Jenkins as the S247_<ACCT>_ITSM_ENV credential and
    is loaded into every run. Only INTEG_SECRET_* lines are touched."""
    path = os.path.join(d, ".itsm.env")
    cur = read_env_file(path)
    need = [t for t in templates if t.get("secret_field")]
    log("Enter the webhook URL (Slack, Discord) or the integration key (PagerDuty). Input is hidden.")
    log("Press Enter to keep the value already saved.\n")
    new = {}
    for t in need:
        k = secret_key(t["name"])
        have = " (saved — Enter keeps it)" if cur.get(k) else ""
        what = "integration key" if t["secret_field"] == "service_key" else "webhook URL"
        v = getpass.getpass(f"  {t['app']} '{t['name']}' {what}{have}: ").strip()
        if v:
            if "'" in v or "\n" in v:
                sys.exit("[BLOCKER] value contains a quote or newline — not saved")
            new[k] = v
    lines = []
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as fh:
            for raw in fh:
                key = raw.strip()
                key = key[7:].strip() if key.startswith("export ") else key
                key = key.split("=", 1)[0].strip()
                if key in new:
                    continue                 # replaced below
                lines.append(raw if raw.endswith("\n") else raw + "\n")
    for k, v in sorted(new.items()):
        lines.append(f"{k}='{v}'\n")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.writelines(lines)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    cur.update(new)
    missing = [t["name"] for t in need if not cur.get(secret_key(t["name"]))]
    log(f"\n  saved in {path}  ({len(need) - len(missing)}/{len(need)} set)")
    if missing:
        log("  still missing: " + ", ".join(missing))
    log("  Jenkins: run  python3 jenkins_secrets.py  to upload the change.")


def do_recreate(api, d, templates, check_only, only):
    secrets = read_env_file(os.path.join(d, ".itsm.env"))
    secrets.update({k: v for k, v in os.environ.items() if k.startswith("INTEG_SECRET_")})
    live = api.list()
    results = []
    for t in templates:
        name, tid = t["name"], t["type_id"]
        if only and name not in only:
            continue
        r = {"name": name, "app": t["app"], "type_id": tid, "status": "FAIL",
             "steps": [], "mismatches": [], "old_service_id": None,
             "new_service_id": None, "at": time.strftime("%H:%M:%S")}
        results.append(r)
        log(f"\n  ── {name} ({t['app']})")
        body = json.loads(json.dumps(t["template"]))
        sf = t.get("secret_field")
        if sf:
            sv = secrets.get(secret_key(name))
            if not sv:
                r["status"] = "BLOCKED"
                what = ("PagerDuty integration key" if sf == "service_key"
                        else "webhook URL")
                r["steps"].append(f"not touched: its {what} is not saved "
                                  f"(run: python3 integration_setup.py -a <account> --secrets)")
                log(f"     [BLOCKED] {what} not saved — integration left as it is")
                continue
            body[sf] = sv
        old = [x for x in live if x.get("name") == name and int(x.get("type") or 0) == tid]
        if tid == 4:   # only one SDP OD integration per account
            old = [x for x in live if int(x.get("type") or 0) == 4]
        if check_only:
            r["status"] = "READY"
            r["steps"].append(f"would delete {len(old)} and add 1")
            log(f"     [READY] would delete {len(old)} existing, then add")
            continue
        # 1. delete the existing one(s)
        del_ok = True
        for o in old:
            r["old_service_id"] = o["service_id"]
            c, code, msg = api.delete(o["service_id"])
            ok = c == 200 and code in (0, None)
            r["steps"].append(f"delete {o['service_id']}: {'OK' if ok else f'FAILED (HTTP {c} {msg})'}")
            log(f"     delete existing: {'OK' if ok else f'FAILED HTTP {c} {msg}'}")
            del_ok = del_ok and ok
        if not del_ok:
            r["steps"].append("add skipped because the old one could not be deleted")
            continue
        if not old:
            r["steps"].append("no existing integration to delete")
        # 2. add it (retry once)
        new_id, last = None, ""
        for attempt in (1, 2):
            c, code, data, msg = api.add(tid, body)
            new_id = (data or {}).get("service_id")
            if c in (200, 201) and code in (0, None) and new_id:
                break
            last = f"HTTP {c} code {code}: {msg}"
            log(f"     add attempt {attempt}: FAILED {last}")
            new_id = None
            time.sleep(5)
        if not new_id:
            r["steps"].append(f"add FAILED ({last})")
            r["steps"].append(f"add it again: python3 integration_setup.py -a "
                              f"<account> --only '{name}'")
            continue
        r["new_service_id"] = str(new_id)
        r["steps"].append(f"added: new id {new_id}")
        log(f"     added: new id {new_id}")
        # 3. read it back and compare every setting
        time.sleep(2)
        c, got, msg = api.get(tid, new_id)
        if c != 200 or not got:
            r["steps"].append(f"read-back FAILED (HTTP {c} {msg})")
            continue
        exp = t["template"]
        if tid == 4:
            if str(got.get("service_id")) != str(new_id):
                r["mismatches"].append(f"service_id: expected {new_id}, got {got.get('service_id')}")
        r["mismatches"] += compare(exp, got)
        if r["mismatches"]:
            r["steps"].append("read-back: settings differ")
            for m in r["mismatches"]:
                log(f"     [MISMATCH] {m}")
        else:
            r["status"] = "PASS"
            r["steps"].append("read-back: every setting saved correctly "
                              "(DOWN/TROUBLE/CRITICAL, All Monitors, profile)")
            log("     [PASS] read back — every setting matches")
    out = {"run_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
           "check_only": check_only, "results": results}
    if not check_only:
        with open(os.path.join(d, "add_integrations.json"), "w") as fh:
            json.dump(out, fh, indent=2)
    return results


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-a", "--account", required=True)
    ap.add_argument("--grid", default=None)
    ap.add_argument("--snapshot", action="store_true")
    ap.add_argument("--secrets", action="store_true")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--only", action="append", default=[])
    a = ap.parse_args()

    d, grid, cookie, csrf = account_ctx(a.account, a.grid)
    tpl_path = os.path.join(d, "integration_templates.json")
    templates = (json.load(open(tpl_path)).get("integrations") or []) \
        if os.path.isfile(tpl_path) else []

    if a.secrets:
        if not templates:
            sys.exit("[BLOCKER] no integration_templates.json yet (run --snapshot)")
        do_secrets(d, templates)
        return
    if not cookie:
        sys.exit("[BLOCKER] no Site24x7 session for this account")
    api = Api(grid, cookie, csrf)
    log(f"ADD INTEGRATIONS — {a.account}  ({grid})")
    if a.snapshot:
        do_snapshot(api, d)
        return
    if not templates:
        log("  [skip] no integration_templates.json for this account — "
            "integrations are used as they are.")
        return
    res = do_recreate(api, d, templates, a.check, set(a.only))
    n_pass = sum(r["status"] == "PASS" for r in res)
    log(f"\n  {n_pass}/{len(res)} integration(s) added and verified.")
    sys.exit(0 if all(r["status"] in ("PASS", "READY") for r in res) else 1)


if __name__ == "__main__":
    main()
