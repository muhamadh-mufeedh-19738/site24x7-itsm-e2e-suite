import pathlib

path = pathlib.Path("run_all.py")
text = path.read_text()

old = '''    # ---- session cookie liveness, BEFORE any verification ---------------
    # Stage 3 reads Alert Logs with a browser session cookie. A dead cookie
    # returns zero rows, which is indistinguishable from "nothing happened"
    # unless we check first. Reporting an auth failure as an empty result is
    # how a perfectly good run gets written up as untested.
    if not args.report_only and not args.dry_run:
        section("SESSION COOKIE — IS IT STILL ALIVE?")
        probe = subprocess.run(
            [sys.executable, script("stage3_verify.py"), "probe"],
            capture_output=True, text=True)
        out = (probe.stdout or "") + (probe.stderr or "")
        # Use the EXIT CODE, not a substring search. Searching the output
        # for "401"/"403" was a false-positive machine: the probe prints
        # timestamps and field lists, so those digits appear by chance and
        # a perfectly good session gets declared dead.
        dead = probe.returncode != 0
        if not dead and "endpoint works" not in out.lower():
            # exit 0 but no success marker -- trust the exit code, say so
            log("  [note] probe exited 0 without its usual success line; "
                "continuing")
        if dead:
            log("  [BLOCKER] the Alert Logs session cookie is DEAD.")
            log("")
            log("            Every verification would come back empty, and")
            log("            the report would claim your monitors produced no")
            log("            alerts. They probably did — we just cannot read")
            log("            them. NO REPORT WAS WRITTEN.")
            log("")
            log("            Usual causes: you logged out of Site24x7, the")
            log("            cookie aged out (a few hours), or the cookie")
            log("            belongs to a DIFFERENT account.")
            log("")
            log("            Refresh it, then re-run WITHOUT repeating the")
            log("            cycle:")
            log("              python3 extract_cookie.py")
            log("              source .session.env")
            log("              python3 run_all.py --skip-cycle")
            log("")
            log("            The alert cycle you already ran still counts —")
            log("            --skip-cycle reuses it instead of driving the")
            log("            monitors again.")
            log("")
            log("  probe said: " + out.strip()[:300])
            sys.exit(2)
        ok_line = out.strip().splitlines()[-1] if out.strip() else "reachable"
        log(f"  [OK ] Alert Logs readable — {ok_line[:120]}")
'''

new = '''    # ---- session cookie liveness, BEFORE any verification ---------------
    # Stage 3 reads Alert Logs with a browser session cookie. A dead cookie
    # returns zero rows, which is indistinguishable from "nothing happened"
    # unless we check first. Reporting an auth failure as an empty result is
    # how a perfectly good run gets written up as untested.
    #
    # FIX (2026-09-24): this cookie can age out within a few minutes on
    # this grid. By the time this check used to run -- AFTER the Log
    # Report capture and the full ITSM-first tool search -- several
    # minutes had already passed, so a cookie that was fine at login was
    # reported as dead every time. Now, if it is found dead, we refresh it
    # ONCE right here and re-check, instead of aborting the whole run.
    def _probe_alert_logs():
        p = subprocess.run(
            [sys.executable, script("stage3_verify.py"), "probe"],
            capture_output=True, text=True)
        return p.returncode == 0, (p.stdout or "") + (p.stderr or "")

    def _relogin():
        login_js = os.environ.get(
            "S247_LOGIN_JS",
            os.path.join(os.path.expanduser("~"), "Documents", "qg",
                         "s247_login.js"))
        if not os.path.isfile(login_js):
            log(f"  [WARN] {login_js} not found -- cannot auto-refresh.")
            return False
        log("  Session looked dead. Refreshing it automatically...")
        r = subprocess.run(["node", login_js], env=os.environ, timeout=180)
        if r.returncode != 0:
            log("  [WARN] automatic re-login failed.")
            return False
        session_file = os.environ.get("S247_SESSION_FILE", ".session.env")
        if os.path.isfile(session_file):
            for line in open(session_file, encoding="utf-8"):
                line = line.strip()
                if line.startswith("export "):
                    line = line[len("export "):]
                if "=" in line and not line.startswith("#"):
                    k, _, v = line.partition("=")
                    v = v.strip().strip("'").strip('"')
                    os.environ[k.strip()] = v
        return True

    if not args.report_only and not args.dry_run:
        section("SESSION COOKIE — IS IT STILL ALIVE?")
        alive, out = _probe_alert_logs()
        if not alive and _relogin():
            alive, out = _probe_alert_logs()
        if not alive:
            log("  [BLOCKER] the Alert Logs session cookie is DEAD, even")
            log("            after an automatic refresh attempt.")
            log("")
            log("            Every verification would come back empty, and")
            log("            the report would claim your monitors produced no")
            log("            alerts. They probably did — we just cannot read")
            log("            them. NO REPORT WAS WRITTEN.")
            log("")
            log("            Refresh it by hand, then re-run WITHOUT repeating")
            log("            the cycle:")
            log("              python3 itsm.py -a automation --login-only")
            log("              python3 itsm.py -a automation --skip-cycle")
            log("")
            log("  probe said: " + out.strip()[:300])
            sys.exit(2)
        ok_line = out.strip().splitlines()[-1] if out.strip() else "reachable"
        log(f"  [OK ] Alert Logs readable — {ok_line[:120]}")
'''

if old not in text:
    raise SystemExit(
        "Could not find the exact block to replace -- the file may have "
        "changed since this patch was written. Nothing was modified.")

text = text.replace(old, new, 1)
path.write_text(text)
print("Patched run_all.py successfully.")
