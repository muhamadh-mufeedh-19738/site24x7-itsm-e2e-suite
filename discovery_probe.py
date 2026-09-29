#!/usr/bin/env python3
"""
Site24x7 ITSM Automation — STAGE 0 DISCOVERY PROBE
===================================================

PURPOSE
    This script does NOT test anything. It inventories your environment so the
    automation framework can be designed against facts instead of assumptions.

    It answers the questions the automation brief demands be answered before
    any code is written:
      - What tooling already exists locally?
      - Which Playwright is installed, and is it usable from Python?
      - Is QEngine present, and can it reach the local grid?
      - Is the grid reachable? What authentication does it use?
      - Are the Alert Logs / monitor / integration endpoints reachable?
      - What CI tooling exists?

USAGE
    # Minimum (local tooling inventory only, no network calls):
    python3 discovery_probe.py

    # Full probe including the grid (recommended):
    export S247_GRID_URL="https://your-grid.localsite24x7.com"
    export S247_API_KEY="..."          # optional; probe reports if absent
    export S247_SESSION_COOKIE="..."   # optional; e.g. copied from browser
    python3 discovery_probe.py

OUTPUT
    - discovery_report.json   (machine-readable, paste this back)
    - console summary          (human-readable)

SAFETY
    - Read-only. Makes GET requests only. Never modifies monitors,
      integrations, or configuration.
    - Never prints secrets. Values are masked; only presence/absence reported.
    - Fails LOUDLY: every probe failure is recorded with its reason,
      never silently skipped.
"""

import json
import os
import platform
import shutil
import socket
import ssl
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

REPORT_PATH = "discovery_report.json"
HTTP_TIMEOUT = 15

report = {
    "probe_version": "1.0",
    "generated_at": datetime.now(timezone.utc).isoformat(),
    "sections": {},
    "blockers": [],
    "warnings": [],
}


# ----------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------

def log(msg):
    print(msg, flush=True)


def section(name):
    log("\n" + "=" * 70)
    log(name)
    log("=" * 70)


def blocker(msg):
    """A blocker prevents the automation from being built as specified."""
    report["blockers"].append(msg)
    log(f"  [BLOCKER] {msg}")


def warn(msg):
    report["warnings"].append(msg)
    log(f"  [WARN]    {msg}")


def mask(value):
    """Never emit secrets. Report presence and shape only."""
    if not value:
        return None
    return f"<set, length={len(value)}>"


def run_cmd(cmd, timeout=20):
    """Run a command; return (ok, output). Never raises."""
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, shell=False
        )
        out = (proc.stdout or "").strip() or (proc.stderr or "").strip()
        return proc.returncode == 0, out
    except FileNotFoundError:
        return False, "not found"
    except subprocess.TimeoutExpired:
        return False, f"timed out after {timeout}s"
    except Exception as exc:  # noqa: BLE001 - loud, never silent
        return False, f"error: {exc}"


def http_get(url, headers=None, timeout=HTTP_TIMEOUT, allow_insecure=True):
    """
    Read-only GET. Returns a dict describing the outcome.
    allow_insecure: local grids often use self-signed certs.
    """
    result = {
        "url": url,
        "ok": False,
        "status": None,
        "error": None,
        "content_type": None,
        "body_snippet": None,
        "set_cookie_names": [],
        "redirected_to": None,
    }
    req = urllib.request.Request(url, headers=headers or {}, method="GET")
    ctx = None
    if allow_insecure and url.lower().startswith("https"):
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            body = resp.read(4000).decode("utf-8", errors="replace")
            result["ok"] = True
            result["status"] = resp.status
            result["content_type"] = resp.headers.get("Content-Type")
            result["body_snippet"] = body[:600]
            result["redirected_to"] = resp.geturl() if resp.geturl() != url else None
            cookies = resp.headers.get_all("Set-Cookie") or []
            result["set_cookie_names"] = [c.split("=", 1)[0].strip() for c in cookies]
    except urllib.error.HTTPError as exc:
        result["status"] = exc.code
        result["error"] = f"HTTPError {exc.code} {exc.reason}"
        try:
            result["body_snippet"] = exc.read(1500).decode("utf-8", errors="replace")[:600]
        except Exception:
            pass
    except urllib.error.URLError as exc:
        result["error"] = f"URLError: {exc.reason}"
    except socket.timeout:
        result["error"] = f"timeout after {timeout}s"
    except Exception as exc:  # noqa: BLE001
        result["error"] = f"error: {exc}"
    return result


# ----------------------------------------------------------------------------
# 1. host + python
# ----------------------------------------------------------------------------

def probe_host():
    section("1. HOST & PYTHON")
    data = {
        "os": platform.platform(),
        "python_version": sys.version.split()[0],
        "python_executable": sys.executable,
        "cwd": os.getcwd(),
    }
    for k, v in data.items():
        log(f"  {k:22} {v}")
    if sys.version_info < (3, 8):
        blocker(f"Python {data['python_version']} is too old; 3.8+ required.")
    report["sections"]["host"] = data


# ----------------------------------------------------------------------------
# 2. local tooling
# ----------------------------------------------------------------------------

def probe_tooling():
    section("2. LOCAL TOOLING INVENTORY")
    tools = {
        "git": ["git", "--version"],
        "node": ["node", "--version"],
        "npm": ["npm", "--version"],
        "java": ["java", "-version"],
        "mvn": ["mvn", "--version"],
        "docker": ["docker", "--version"],
        "code_vscode": ["code", "--version"],
        "jenkins_cli": ["jenkins-cli", "--version"],
        "curl": ["curl", "--version"],
    }
    found = {}
    for name, cmd in tools.items():
        present = shutil.which(cmd[0]) is not None
        ok, out = run_cmd(cmd) if present else (False, "not installed")
        first_line = out.splitlines()[0] if out else ""
        found[name] = {
            "present": present,
            "version": first_line if present and ok else None,
            "detail": None if (present and ok) else out,
        }
        status = "OK " if present and ok else "-- "
        log(f"  [{status}] {name:14} {first_line if present else 'not installed'}")
    report["sections"]["tooling"] = found

    if not found["git"]["present"]:
        warn("git not found — repo integration and CI checkout may be affected.")


# ----------------------------------------------------------------------------
# 3. python packages
# ----------------------------------------------------------------------------

def probe_python_packages():
    section("3. PYTHON PACKAGES")
    packages = [
        "pytest", "playwright", "requests", "yaml", "openpyxl",
        "jinja2", "junit_xml", "dotenv",
    ]
    found = {}
    for pkg in packages:
        try:
            mod = __import__(pkg)
            ver = getattr(mod, "__version__", "unknown")
            found[pkg] = {"installed": True, "version": str(ver)}
            log(f"  [OK ] {pkg:14} {ver}")
        except Exception:
            found[pkg] = {"installed": False, "version": None}
            log(f"  [-- ] {pkg:14} not installed")
    report["sections"]["python_packages"] = found

    if not found["pytest"]["installed"]:
        warn("pytest not installed in this interpreter — needed for the test runner.")
    if not found["playwright"]["installed"]:
        warn("Python Playwright bindings NOT installed in this interpreter. "
             "A QEngine-bundled Playwright (Node/Java) is NOT usable from Python. "
             "Fix: pip install playwright && playwright install chromium")


# ----------------------------------------------------------------------------
# 4. playwright detail  (critical: which binding, which browsers)
# ----------------------------------------------------------------------------

def probe_playwright():
    section("4. PLAYWRIGHT DETAIL (binding + browser binaries)")
    detail = {
        "python_binding": None,
        "node_cli": None,
        "browsers_path_env": os.environ.get("PLAYWRIGHT_BROWSERS_PATH"),
        "browser_cache_dirs": [],
        "usable_from_python": False,
        "notes": [],
    }

    # Python binding
    try:
        import playwright  # noqa: F401
        from playwright.sync_api import sync_playwright  # noqa: F401
        detail["python_binding"] = getattr(playwright, "__version__", "unknown")
        detail["usable_from_python"] = True
        log(f"  [OK ] Python binding present: {detail['python_binding']}")
    except Exception as exc:  # noqa: BLE001
        detail["notes"].append(f"python binding import failed: {exc}")
        log("  [-- ] Python Playwright binding NOT importable")

    # Node CLI
    ok, out = run_cmd(["npx", "playwright", "--version"], timeout=45)
    detail["node_cli"] = out if ok else None
    log(f"  [{'OK ' if ok else '-- '}] Node Playwright CLI: {out if ok else 'not available'}")

    # Browser binary cache locations
    home = os.path.expanduser("~")
    candidates = [
        os.path.join(home, ".cache", "ms-playwright"),
        os.path.join(home, "Library", "Caches", "ms-playwright"),
        os.path.join(home, "AppData", "Local", "ms-playwright"),
    ]
    if detail["browsers_path_env"]:
        candidates.insert(0, detail["browsers_path_env"])
    for path in candidates:
        if os.path.isdir(path):
            try:
                entries = sorted(os.listdir(path))[:20]
            except Exception:
                entries = []
            detail["browser_cache_dirs"].append({"path": path, "entries": entries})
            log(f"  [OK ] browser cache: {path}")
            for e in entries[:6]:
                log(f"         - {e}")

    if not detail["browser_cache_dirs"]:
        warn("No Playwright browser binaries found in the usual cache locations. "
             "If QEngine bundles them elsewhere, they may not be reusable. "
             "Fix: playwright install chromium")

    if detail["node_cli"] and not detail["usable_from_python"]:
        warn("Playwright exists as a Node/CLI install (likely QEngine's) but the "
             "Python binding is missing. These are separate — installing the "
             "Python binding will not disturb the QEngine install.")

    report["sections"]["playwright"] = detail


# ----------------------------------------------------------------------------
# 5. qengine detection
# ----------------------------------------------------------------------------

def probe_qengine():
    section("5. QENGINE DETECTION")
    detail = {"hints": [], "env_vars": [], "paths": []}

    for key in os.environ:
        if "QENGINE" in key.upper() or "QE_" == key.upper()[:3]:
            detail["env_vars"].append(key)

    home = os.path.expanduser("~")
    for path in [
        os.path.join(home, "qengine"),
        os.path.join(home, "QEngine"),
        os.path.join(home, ".qengine"),
        "/opt/qengine",
    ]:
        if os.path.exists(path):
            detail["paths"].append(path)
            log(f"  [OK ] found path: {path}")

    if detail["env_vars"]:
        log(f"  [OK ] QEngine-related env vars: {detail['env_vars']}")
    if not detail["paths"] and not detail["env_vars"]:
        log("  [-- ] No local QEngine install detected from this shell.")
        detail["hints"].append(
            "QEngine may be cloud-hosted/browser-based rather than installed locally."
        )

    detail["open_question"] = (
        "MUST BE ANSWERED MANUALLY: can QEngine reach the local grid "
        "(localsite24x7.com)? If QEngine runs in Zoho cloud, it may have no network "
        "route to a local-only domain unless a private/on-prem agent exists. "
        "If it cannot reach the grid, QEngine cannot host this suite and local "
        "pytest execution is required."
    )
    log(f"  [?? ] {detail['open_question']}")
    report["sections"]["qengine"] = detail


# ----------------------------------------------------------------------------
# 6. repo / ci detection
# ----------------------------------------------------------------------------

def probe_repo_and_ci():
    section("6. REPOSITORY & CI DETECTION")
    detail = {"is_git_repo": False, "remotes": None, "branch": None, "ci_files": []}

    ok, out = run_cmd(["git", "rev-parse", "--is-inside-work-tree"])
    detail["is_git_repo"] = ok and out.strip() == "true"
    if detail["is_git_repo"]:
        _, remotes = run_cmd(["git", "remote", "-v"])
        _, branch = run_cmd(["git", "rev-parse", "--abbrev-ref", "HEAD"])
        # Remote URLs can embed tokens; do not emit raw.
        detail["remotes"] = "<present, redacted>" if remotes else None
        detail["branch"] = branch
        log(f"  [OK ] git repo detected (branch: {branch})")
    else:
        log("  [-- ] Not inside a git repository (run this from your repo root).")

    ci_candidates = [
        "Jenkinsfile", ".gitlab-ci.yml", ".github/workflows",
        "azure-pipelines.yml", "pytest.ini", "pyproject.toml",
        "requirements.txt", "package.json", "tox.ini", "conftest.py",
    ]
    for name in ci_candidates:
        if os.path.exists(name):
            detail["ci_files"].append(name)
            log(f"  [OK ] found: {name}")
    if not detail["ci_files"]:
        log("  [-- ] No CI/test config files found in the current directory.")
    report["sections"]["repo_ci"] = detail


# ----------------------------------------------------------------------------
# 7. credentials presence (never values)
# ----------------------------------------------------------------------------

def probe_credentials():
    section("7. CREDENTIAL / CONFIG PRESENCE (values never printed)")
    keys = [
        "S247_GRID_URL", "S247_API_KEY", "S247_SESSION_COOKIE",
        "S247_USERNAME", "S247_PASSWORD", "S247_CSRF_TOKEN",
        "HALO_URL", "SNOW_URL", "SDP_URL", "ZOHODESK_URL", "WEBHOOK_URL",
    ]
    detail = {}
    for k in keys:
        val = os.environ.get(k)
        detail[k] = mask(val)
        log(f"  {'[OK ]' if val else '[-- ]'} {k:22} {mask(val) or 'not set'}")

    if not os.environ.get("S247_GRID_URL"):
        warn("S247_GRID_URL not set — all network probes will be skipped. "
             "Set it and re-run for a complete report.")
    report["sections"]["credentials_present"] = detail


# ----------------------------------------------------------------------------
# 8. grid reachability + auth mechanism
# ----------------------------------------------------------------------------

def probe_grid():
    section("8. GRID REACHABILITY & AUTH MECHANISM")
    grid = os.environ.get("S247_GRID_URL")
    detail = {"grid_url": grid, "probes": {}, "auth_verdict": None}

    if not grid:
        log("  [SKIP] S247_GRID_URL not set.")
        report["sections"]["grid"] = detail
        return

    grid = grid.rstrip("/")
    parsed = urllib.parse.urlparse(grid)

    # DNS
    try:
        socket.gethostbyname(parsed.hostname)
        log(f"  [OK ] DNS resolves: {parsed.hostname}")
        detail["dns_resolves"] = True
    except Exception as exc:  # noqa: BLE001
        detail["dns_resolves"] = False
        blocker(f"DNS does NOT resolve for {parsed.hostname}: {exc}. "
                "The machine running the suite must reach the grid.")
        report["sections"]["grid"] = detail
        return

    # Base page
    base = http_get(grid)
    detail["probes"]["base"] = base
    log(f"  [{'OK ' if base['ok'] else '-- '}] GET {grid} -> "
        f"status={base['status']} err={base['error']}")
    if base["set_cookie_names"]:
        log(f"         Set-Cookie: {base['set_cookie_names']}")

    # Does it look like a login redirect (session auth) ?
    snippet = (base.get("body_snippet") or "").lower()
    looks_login = any(t in snippet for t in ["login", "signin", "password", "csrf"])
    detail["looks_like_login_page"] = looks_login

    # API-key style probe (read-only endpoint guess is NOT invented:
    # we only try what the user supplied via env, plus the base URL).
    api_key = os.environ.get("S247_API_KEY")
    if api_key:
        api_probe = http_get(
            grid, headers={"Authorization": f"Zoho-authtoken {api_key}",
                           "Accept": "application/json"}
        )
        detail["probes"]["with_api_key"] = api_probe
        log(f"  [{'OK ' if api_probe['ok'] else '-- '}] API-key auth probe -> "
            f"status={api_probe['status']}")
    else:
        log("  [-- ] No S247_API_KEY set; API-key path not evaluated.")

    # Session-cookie probe
    cookie = os.environ.get("S247_SESSION_COOKIE")
    if cookie:
        sess_probe = http_get(grid, headers={"Cookie": cookie,
                                             "Accept": "application/json"})
        detail["probes"]["with_session_cookie"] = sess_probe
        log(f"  [{'OK ' if sess_probe['ok'] else '-- '}] Session-cookie probe -> "
            f"status={sess_probe['status']}")
    else:
        log("  [-- ] No S247_SESSION_COOKIE set; session path not evaluated.")

    # Verdict
    if api_key and detail["probes"].get("with_api_key", {}).get("ok"):
        detail["auth_verdict"] = "API_KEY_VIABLE"
    elif cookie and detail["probes"].get("with_session_cookie", {}).get("ok"):
        detail["auth_verdict"] = "SESSION_COOKIE_VIABLE"
    elif base["ok"] and looks_login:
        detail["auth_verdict"] = "BROWSER_LOGIN_REQUIRED"
        warn("Grid reachable but appears to require interactive login. "
             "Expect a session-replay or Playwright-login architecture "
             "(consistent with the CSRF-protected endpoints seen previously).")
    else:
        detail["auth_verdict"] = "UNDETERMINED"
        warn("Auth mechanism undetermined. Re-run with S247_API_KEY and/or "
             "S247_SESSION_COOKIE set to disambiguate.")

    log(f"  ==> AUTH VERDICT: {detail['auth_verdict']}")
    report["sections"]["grid"] = detail


# ----------------------------------------------------------------------------
# 9. itsm vendor endpoint reachability
# ----------------------------------------------------------------------------

def probe_vendors():
    section("9. ITSM VENDOR ENDPOINT REACHABILITY (read-only)")
    vendors = {
        "HALO": os.environ.get("HALO_URL"),
        "ServiceNow": os.environ.get("SNOW_URL"),
        "SDP": os.environ.get("SDP_URL"),
        "ZohoDesk": os.environ.get("ZOHODESK_URL"),
        "Webhook": os.environ.get("WEBHOOK_URL"),
    }
    detail = {}
    any_set = False
    for name, url in vendors.items():
        if not url:
            detail[name] = {"configured": False}
            log(f"  [-- ] {name:12} URL not set")
            continue
        any_set = True
        res = http_get(url)
        detail[name] = {"configured": True, "reachable": res["ok"],
                        "status": res["status"], "error": res["error"]}
        log(f"  [{'OK ' if res['ok'] else '-- '}] {name:12} status={res['status']} "
            f"err={res['error']}")
    if not any_set:
        log("  [SKIP] No vendor URLs set. Ticket-level verification will need these.")
    report["sections"]["vendors"] = detail


# ----------------------------------------------------------------------------
# 10. verdict
# ----------------------------------------------------------------------------

def summarise():
    section("10. SUMMARY & RECOMMENDED ARCHITECTURE")

    pw = report["sections"].get("playwright", {})
    grid = report["sections"].get("grid", {})
    pkgs = report["sections"].get("python_packages", {})

    recs = []

    verdict = grid.get("auth_verdict")
    if verdict == "API_KEY_VIABLE":
        recs.append("API-FIRST: use the API for reads and state changes where "
                    "possible; browser only for UI-under-test controls "
                    "(Save / Save and Test / Trigger).")
    elif verdict == "SESSION_COOKIE_VIABLE":
        recs.append("SESSION-REPLAY + API: reuse the authenticated session for "
                    "reads; browser for UI-under-test controls.")
    elif verdict == "BROWSER_LOGIN_REQUIRED":
        recs.append("BROWSER-LED: Playwright performs login once, persists "
                    "storage_state, then reuses it for API calls and UI actions.")
    else:
        recs.append("AUTH UNDETERMINED: re-run with credentials set before "
                    "committing to an architecture.")

    if not pkgs.get("playwright", {}).get("installed"):
        recs.append("Install the Python Playwright binding in a dedicated venv: "
                    "python -m venv .venv && . .venv/bin/activate && "
                    "pip install playwright pytest requests pyyaml && "
                    "playwright install chromium  "
                    "(this does NOT affect the QEngine Playwright install).")

    if not pkgs.get("pytest", {}).get("installed"):
        recs.append("Install pytest — it provides JUnit XML output for CI directly.")

    recs.append("Keep QEngine as a candidate for orchestration ONLY if it can "
                "reach the local grid; otherwise run pytest locally / on a "
                "runner that sits inside the grid's network.")

    report["recommendations"] = recs
    for r in recs:
        log(f"  -> {r}")

    log("")
    log(f"  BLOCKERS: {len(report['blockers'])}")
    for b in report["blockers"]:
        log(f"    ! {b}")
    log(f"  WARNINGS: {len(report['warnings'])}")
    for w in report["warnings"]:
        log(f"    ~ {w}")


def main():
    log("Site24x7 ITSM Automation — Stage 0 Discovery Probe")
    log("Read-only. No monitors or integrations are modified.")

    probe_host()
    probe_tooling()
    probe_python_packages()
    probe_playwright()
    probe_qengine()
    probe_repo_and_ci()
    probe_credentials()
    probe_grid()
    probe_vendors()
    summarise()

    try:
        with open(REPORT_PATH, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2)
        log(f"\nWrote {REPORT_PATH} — paste this back to continue to Stage 1.")
    except Exception as exc:  # noqa: BLE001
        log(f"\n[BLOCKER] Could not write {REPORT_PATH}: {exc}")
        sys.exit(1)

    # Exit non-zero on blockers so CI can gate on this later.
    sys.exit(2 if report["blockers"] else 0)


if __name__ == "__main__":
    main()
