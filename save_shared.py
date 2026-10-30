#!/usr/bin/env python3
"""
save_shared.py - save ONE ITSM tool login into several accounts' .itsm.env.

    python3 save_shared.py pagerduty      # same PagerDuty for automation, tpt, tpt1
    python3 save_shared.py halo           # same HaloITSM (API key) for all three
    python3 save_shared.py halo --accounts tpt,tpt1

How it behaves (same rules as save_snow.py):
  * Visible values (URL, email) are pre-filled - press Enter to keep them.
  * Secrets (API keys) are typed hidden and never printed, only their length.
    Press Enter on a secret to reuse the one already saved in --source
    (default: automation), so nothing has to be retyped.
  * SAVE FIRST, check afterwards. A failed check never blocks the save.
  * Only that tool's lines change; every other line in .itsm.env is kept.
  * Files are chmod 600 and must be git-ignored, or nothing is written.
"""
import argparse
import getpass
import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
SAMPLE = os.path.join(HERE, ".itsm.env.sample")
DEFAULT_ACCOUNTS = "automation,tpt,tpt1"
LINE_RE = re.compile(r"\s*(?:export\s+)?(\w+)\s*=\s*(.*)$")

# kind: "text" = visible, "secret" = hidden.  "default" used when nothing saved.
TOOLS = {
    "pagerduty": {
        "title": "PagerDuty",
        "fields": [
            ("PAGERDUTY_API_KEY", "REST API key", "secret", ""),
            ("PAGERDUTY_USER_EMAIL", "User email on the PagerDuty account",
             "text", ""),
            ("PAGERDUTY_API_URL", "API URL", "text",
             "https://api.pagerduty.com"),
        ],
    },
    "halo": {
        "title": "HaloITSM (API key)",
        "fields": [
            ("HALO_URL", "Halo URL (e.g. https://yourco.haloitsm.com)",
             "text", ""),
            ("HALO_API_KEY", "Halo API key", "secret", ""),
        ],
    },
}


def fail(msg, code=2):
    print(f"[FAIL] {msg}")
    sys.exit(code)


def unquote(v):
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
        return v[1:-1]
    return v


def read_env(path):
    vals = {}
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                if line.lstrip().startswith("#"):
                    continue
                m = LINE_RE.match(line)
                if m:
                    vals[m.group(1)] = unquote(m.group(2))
    return vals


def quote(value, what):
    """Quote so both the suite's parser and bash `source` read it back exactly."""
    if "\n" in value or "\r" in value:
        fail(f"{what} contains a line break. Nothing was saved.")
    if "'" not in value:
        return f"'{value}'"
    if not any(c in value for c in '"$`\\'):
        return f'"{value}"'
    fail(f"{what} contains both ' and one of \" $ ` \\ - cannot be stored "
         "safely. Nothing was saved.")


def is_ignored(path):
    r = subprocess.run(["git", "check-ignore", "-q", path], cwd=HERE,
                       capture_output=True)
    return r.returncode == 0


def write_keys(path, values, title):
    lines = []
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    out, done = [], set()
    for line in lines:
        m = LINE_RE.match(line)
        if m and m.group(1) in values and not line.lstrip().startswith("#"):
            k = m.group(1)
            if k not in done:
                out.append(f"export {k}={values[k]}")
                done.add(k)
            continue
        out.append(line)
    missing = [k for k in values if k not in done]
    if missing:
        out += ["", f"# {title} (written by save_shared.py)"]
        out += [f"export {k}={values[k]}" for k in missing]
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write("\n".join(out) + "\n")
    os.replace(tmp, path)
    os.chmod(path, 0o600)


def http_get(url, headers):
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30,
                                    context=ssl.create_default_context()) as r:
            body = r.read().decode("utf-8", errors="replace")
            return r.status, body
    except urllib.error.HTTPError as e:
        return e.code, ""
    except urllib.error.URLError as e:
        return None, str(e.reason)


def check_pagerduty(v):
    st, body = http_get(
        f"{v['PAGERDUTY_API_URL'].rstrip('/')}/incidents?limit=1",
        {"Authorization": f"Token token={v['PAGERDUTY_API_KEY']}",
         "Accept": "application/json",
         "From": v["PAGERDUTY_USER_EMAIL"]})
    if st == 200:
        return True, "incidents readable (HTTP 200)"
    hints = {401: "API key rejected", 403: "key valid but lacks access",
             400: "bad request - check the user email"}
    return False, f"HTTP {st} - {hints.get(st, body[:120] if body else '')}"


def check_halo(v):
    st, body = http_get(
        f"{v['HALO_URL'].rstrip('/')}/api/Tickets?count=1",
        {"X-Halo-Api-Key": v["HALO_API_KEY"], "Accept": "application/json"})
    if st == 200:
        return True, "tickets readable (HTTP 200)"
    hints = {401: "API key rejected", 403: "key valid but cannot read tickets",
             404: "wrong Halo URL"}
    return False, f"HTTP {st} - {hints.get(st, body[:120] if body else '')}"


CHECKS = {"pagerduty": check_pagerduty, "halo": check_halo}


def configured_halo_urls(names):
    """Halo URLs Site24x7's own integrations point at (from Stage 1 capture)."""
    urls = {}
    for n in names:
        p = os.path.join(HERE, "accounts", n, "integrations.json")
        try:
            with open(p, encoding="utf-8") as fh:
                d = json.load(fh)
        except Exception:
            continue
        for it in d.get("integrations", []):
            raw = it.get("raw") or {}
            if str(raw.get("type")) == "43" and raw.get("url"):
                urls.setdefault(raw["url"].rstrip("/"), set()).add(n)
    return urls


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("tool", choices=sorted(TOOLS))
    ap.add_argument("--accounts", default=DEFAULT_ACCOUNTS)
    ap.add_argument("--source", default="automation",
                    help="account whose saved values are offered as defaults")
    a = ap.parse_args()
    spec = TOOLS[a.tool]

    names = [n.strip() for n in a.accounts.split(",") if n.strip()]
    paths = {}
    for n in names + [a.source]:
        if not re.match(r"^[A-Za-z0-9_-]+$", n):
            fail(f"bad account name {n!r}")
    for n in names:
        d = os.path.join(HERE, "accounts", n)
        if not os.path.isdir(d):
            fail(f"no such account folder: {d}")
        p = os.path.join(d, ".itsm.env")
        if not is_ignored(p):
            fail(f"{p} is NOT git-ignored - refusing to write secrets there.")
        paths[n] = p
    src = read_env(os.path.join(HERE, "accounts", a.source, ".itsm.env"))

    print(f"{spec['title']} - one login for: {', '.join(names)}")
    print(f"Press Enter to keep the value in [brackets] (from '{a.source}').\n")

    if a.tool == "halo":
        urls = configured_halo_urls(names)
        if urls:
            print("Halo URLs your Site24x7 integrations point at:")
            for u, who in sorted(urls.items()):
                print(f"  {u}   ({', '.join(sorted(who))})")
            print()

    values = {}
    for key, label, kind, default in spec["fields"]:
        have = src.get(key, "") or default
        if kind == "secret":
            hint = f"keep {a.source}'s, {len(have)} chars" if have else "none saved"
            val = getpass.getpass(f"{label} (hidden) [{hint}]: ").strip()
            val = val or have
            if not val:
                fail(f"{label} is required - nothing was saved.")
            if val != have and getpass.getpass("Type it again (hidden): ").strip() != val:
                fail("the two entries don't match - nothing was saved.")
        else:
            val = input(f"{label} [{have}]: ").strip().strip('"').strip("'") or have
            if not val:
                fail(f"{label} is required - nothing was saved.")
        if key.endswith("_URL"):
            val = val.rstrip("/")
            if not re.match(r"^https://[\w.-]+(/[\w./-]*)?$", val):
                fail(f"{label} should start with https:// - nothing was saved.")
        if key.endswith("_EMAIL") and not re.match(r"^[^@\s]+@[^@\s]+\.\w+$", val):
            fail("that doesn't look like an email - nothing was saved.")
        values[key] = val

    quoted = {k: quote(v, k) for k, v in values.items()}
    for n, p in paths.items():
        if not os.path.isfile(p):
            if os.path.isfile(SAMPLE):
                shutil.copyfile(SAMPLE, p)
            else:
                open(p, "w").close()
            os.chmod(p, 0o600)
        write_keys(p, quoted, spec["title"])
        back = read_env(p)
        same = all(back.get(k) == v for k, v in values.items())
        print(f"[{' OK ' if same else 'FAIL'}] saved for '{n}' (permissions 600)")
        if not same:
            fail(f"read-back mismatch in {p}", 3)
    print(f"\n[ OK ] SAVED - {spec['title']} stored for: {', '.join(names)}")

    print("\nAPI check (informational only, the save is already done) ...")
    ok, msg = CHECKS[a.tool](values)
    print(f"[{' OK ' if ok else 'INFO'}] {msg}")


if __name__ == "__main__":
    main()
