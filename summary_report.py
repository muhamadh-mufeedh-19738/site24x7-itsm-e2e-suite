#!/usr/bin/env python3
"""
summary_report.py — ONE short report for a full run (consolidated + event-based).
================================================================================

WHY
  A full run produces two detailed reports: the consolidated (monitor-level)
  keyword cycle, report_<time>.json, and the event-based response-time
  cycle, events_report_<time>.json. Each is long. This builds a single
  one-page summary from both: the verdict, only the problems, trigger tests,
  every ticket id and every state reached or not, with the evidence folded
  away under "Details". It reads the reports' JSON only, so every verdict is
  exactly what the verification produced.

USAGE  (run from the account folder; itsm.py --full does this for you)
  python3 summary_report.py --consolidated reports/report_X.json \
                            --events reports/events_report_Y.json
  Either input may be left out; the latest of each is used by default.
"""
import argparse
import glob
import html
import json
import os
import re
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import run_all as ra  # noqa: E402  (app_label / is_itsm / bucket names)

PASS, DEFECT, BLOCKED, KNOWN, INCONC = (ra.PASS, ra.DEFECT, ra.BLOCKED,
                                        ra.KNOWN, ra.INCONCLUSIVE)
ORDER = [DEFECT, BLOCKED, INCONC, KNOWN, PASS]
ICON = {PASS: "✓", DEFECT: "✗", BLOCKED: "!", KNOWN: "•", INCONC: "?"}
WORD = {PASS: "Pass", DEFECT: "Defect", BLOCKED: "Blocked", KNOWN: "Known",
        INCONC: "Inconclusive"}


def esc(x):
    return html.escape(str(x if x is not None else ""))


def latest(pattern):
    files = sorted(glob.glob(pattern))
    return files[-1] if files else None


def load(path):
    if not path or not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def route_text(states):
    return " → ".join(s.upper() for s in states)


def scenarios(rep, kind):
    """Columns: one per monitor (consolidated) or per route (event-based)."""
    cols = []
    if not rep:
        return cols
    cyc = rep.get("cycle") or {}
    for m in cyc.get("monitors") or []:
        if kind == "events":
            label = m.get("name") or ""
            title = m.get("search_name") or label.split(" [")[0]
            route = label[label.find("[") + 1:-1] if "[" in label else ""
        else:
            label = m.get("name") or m.get("monitor_id")
            title = label
            route = route_text(list(m.get("route") or []) + ["up"])
        cols.append({"label": label, "title": title, "route": route})
    seen = {c["label"] for c in cols}
    for f in rep.get("findings") or []:
        if f.get("monitor") and f["monitor"] not in seen \
                and f.get("integration") != "(server coverage)":
            seen.add(f["monitor"])
            cols.append({"label": f["monitor"], "title": f["monitor"], "route": ""})
    return cols


def steps_of(f):
    src = f.get("collab") or f.get("itsm_steps") or f.get("log_steps") or {}
    return src.get("steps") or []


def ticket_ids(f):
    return [t for t in (f.get("created_ticket_ids") or [])
            if not ra.INTERNAL_REF_RE.match(str(t))]


def missed(f):
    """States with no successful Alert Log row, on a finding that PASSED on
    its ticket lifecycle -- shown as a 'Check', never hidden."""
    return [s["state"] for s in steps_of(f) if not s.get("ok")]


def short_reason(f):
    bad = [s for s in steps_of(f) if not s.get("ok")]
    if bad:
        parts = []
        for s in bad:
            m = re.search(r"HTTP Response Code - (\d+)", str(s.get("message") or ""))
            why = f"HTTP {m.group(1)}" if m else (s.get("why") or "failed")
            parts.append(f"{s['state']}: {why}")
        return "; ".join(parts)
    return f.get("headline") or ""


def integ_label(name, recs):
    rec = next((r for r in recs if str(r.get("name")) == str(name)), None)
    app = ra.app_label(rec) if rec else ""
    if app and app.lower() not in str(name).lower():
        return name, app
    return name, ""


def chips(f):
    out = []
    for s in steps_of(f):
        cls = "ok" if s.get("ok") else "bad"
        tip = s.get("sent_at") or s.get("why") or ""
        out.append(f'<span class="st {cls}" title="{esc(tip)}">'
                   f'{"✓" if s.get("ok") else "✗"} {esc(s["state"])}</span>')
    return "".join(out)


def cell(f):
    if not f:
        return '<td class="na">—</td>'
    b = f["bucket"]
    ids = ticket_ids(f)
    idh = "".join(f'<code>{esc(t)}</code>' for t in ids)
    if not ids and (f.get("collab") or f.get("notify_only")):
        idh = '<span class="muted">message, no ticket</span>'
    note = ""
    if b in (KNOWN, BLOCKED, INCONC) and not steps_of(f):
        note = f'<div class="muted small">{esc(f.get("headline"))}</div>'
    elif b == PASS and missed(f):
        note = (f'<div class="chk">Check: {esc(", ".join(missed(f)))} alert not '
                f'logged for this integration</div>')
    return (f'<td><div class="v v-{b}">{ICON[b]} {WORD[b]}</div>'
            f'<div class="chips">{chips(f)}</div>'
            f'<div class="ids">{idh}</div>{note}</td>')


def matrix(rep, kind, recs):
    cols = scenarios(rep, kind)
    if not cols:
        return ""
    by = {}
    for f in rep.get("findings") or []:
        by[(f.get("integration"), f.get("monitor"))] = f
    names = sorted({f.get("integration") for f in rep.get("findings") or []
                    if f.get("integration") not in ("(all integrations)",
                                                     "(server coverage)")},
                   key=lambda n: (0 if any(ra.is_itsm(r) for r in recs
                                           if str(r.get("name")) == str(n)) else 1,
                                  str(n).lower()))
    head = "".join(f'<th><div class="mon">{esc(c["title"])}</div>'
                   f'<div class="route">{esc(c["route"])}</div></th>' for c in cols)
    rows = ""
    for n in names:
        nm, app = integ_label(n, recs)
        rows += (f'<tr><th class="ig">{esc(nm)}'
                 + (f'<div class="muted small">{esc(app)}</div>' if app else "")
                 + "</th>"
                 + "".join(cell(by.get((n, c["label"]))) for c in cols) + "</tr>")
    blocked = [f for f in rep.get("findings") or []
               if f.get("integration") == "(all integrations)"]
    extra = "".join(f'<p class="warn">! {esc(f.get("monitor"))}: '
                    f'{esc(f.get("headline"))}</p>' for f in blocked)
    return (f'<div class="tw"><table class="mx"><thead><tr><th></th>{head}</tr>'
            f'</thead><tbody>{rows}</tbody></table></div>{extra}')


def details(reps, recs):
    per = {}
    for kind, rep in reps:
        for f in (rep or {}).get("findings") or []:
            if f.get("integration") in ("(all integrations)", "(server coverage)"):
                continue
            per.setdefault(f["integration"], []).append((kind, f))
    out = ""
    for n in sorted(per, key=str.lower):
        nm, app = integ_label(n, recs)
        worst = min((f["bucket"] for _, f in per[n]), key=ORDER.index)
        body = ""
        for kind, f in per[n]:
            st = steps_of(f)
            trs = "".join(
                f'<tr><td><b>{esc(s["state"])}</b></td>'
                f'<td class="mono">{esc(s.get("sent_at") or "—")}</td>'
                f'<td>{esc(s.get("message") or s.get("why") or "")}</td>'
                f'<td class="{"ok" if s.get("ok") else "bad"}">'
                f'{"✓" if s.get("ok") else "✗ " + esc(s.get("why") or "")}</td></tr>'
                for s in st)
            tool = ""
            for t in f.get("tool_tickets") or []:
                tname = esc(f.get("tool") or "the tool")
                if t.get("found") is False:
                    state = (f'<span class="bad">NOT found in {tname}</span>'
                             + (f' ({esc(t.get("error"))})' if t.get("error") else ""))
                else:
                    state = f'Status in {tname}: <b>{esc(t.get("status") or "found")}</b>'
                tool += (f'<div class="tk">Ticket ID: <code>{esc(t.get("ticket_id"))}</code>'
                         f' · {state}</div>')
            body += (f'<div class="dsc"><div class="dh"><span class="v v-{f["bucket"]}">'
                     f'{ICON[f["bucket"]]} {WORD[f["bucket"]]}</span> '
                     f'{"Event-based" if kind == "events" else "Consolidated"} · '
                     f'{esc(f.get("monitor"))}</div>'
                     f'<div class="small">{esc(f.get("headline"))}</div>'
                     + (f'<div class="small">{tool}</div>' if tool else "")
                     + (f'<table class="st-t"><tr><th>State</th><th>Time</th>'
                        f'<th>What Site24x7 / the tool recorded</th><th></th></tr>{trs}</table>'
                        if trs else "")
                     + "</div>")
        out += (f'<details><summary><span class="v v-{worst}">{ICON[worst]}</span> '
                f'<b>{esc(nm)}</b> <span class="muted">{esc(app)}</span></summary>'
                f'{body}</details>')
    return out


def trigger_tests(rep):
    res = ((rep or {}).get("trigger_test") or {}).get("results") or []
    out = ""
    for r in sorted(res, key=lambda x: str(x.get("name")).lower()):
        ok = r.get("final_verdict") in ("PASS", "SKIP")
        tid = next((t.get("ticket_id") for t in (r.get("itsm_tickets") or [])
                    if t.get("ticket_id")), None)
        out += (f'<div class="tt {"ok" if ok else "bad"}"><b>{"✓" if ok else "✗"} '
                f'{esc(r.get("name"))}</b>'
                + (f'<code>{esc(tid)}</code>' if tid else
                   '<span class="muted small">sent</span>' if ok else
                   f'<span class="small">{esc(r.get("api_message"))}</span>')
                + "</div>")
    return out or '<p class="muted">Trigger tests were not run.</p>'


# ---------------------------------------------------------------- run diff
# Each run leaves a small RESULT snapshot (verdict + every state per
# integration and scenario, trigger tests, build). The report compares it
# with the previous run of the SAME account on the SAME grid, and adds one
# line against the last run on a DIFFERENT build. Ticket ids and times
# change every run, so they are never counted as a difference.

def build_label():
    """'<branch>/<date>' from the SD build URL Jenkins deployed, if known."""
    if os.environ.get("S247_BUILD_LABEL"):
        return os.environ["S247_BUILD_LABEL"].strip()
    url = (os.environ.get("S247_BUILD_URL") or "").strip().rstrip("/")
    parts = [p for p in url.split("/") if p]
    if len(parts) >= 3 and parts[-1].lower().endswith(".zip"):
        return "/".join(parts[-3:-1])
    return ""


def norm_key(key):
    """Event results are compared BY ROUTE, not by monitor: the two routes may
    move between monitors (one shared monitor -> one monitor per route, 4 Nov)
    and that must not read as a new/removed result."""
    if key.startswith("Event-based · ") and "[" in key and "]" in key:
        route = key[key.index("[") + 1:key.index("]")]
        integ = key[key.index("]") + 1:].lstrip(" ·")
        return f"Event-based · [{route}] · {integ}"
    return key


def snapshot(reps, account, grid_name, started, finished, total):
    res = {}
    for kind, r in reps:
        for f in (r or {}).get("findings") or []:
            if f.get("integration") in ("(all integrations)", "(server coverage)"):
                continue
            mode = "Event-based" if kind == "events" else "Consolidated"
            key = norm_key(f"{mode} · {f.get('monitor')} · {f.get('integration')}")
            res[key] = {"mode": mode, "scenario": f.get("monitor"),
                        "integration": f.get("integration"), "bucket": f["bucket"],
                        "states": {st["state"]: bool(st.get("ok")) for st in steps_of(f)},
                        "tickets": ticket_ids(f)}
    trig = {t.get("name"): t.get("final_verdict") for _, r in reps
            for t in (((r or {}).get("trigger_test") or {}).get("results") or [])}
    return {"account": account, "grid": grid_name, "started": started,
            "finished": finished, "total": total, "build": build_label(),
            "results": res, "trigger": trig}


def history_dir(out_dir):
    d = os.environ.get("S247_HISTORY_DIR") or os.path.join(out_dir, "history")
    os.makedirs(d, exist_ok=True)
    return d


def previous_runs(hdir, account, grid_name):
    runs = []
    for p in glob.glob(os.path.join(hdir, f"{account}__{grid_name}__*.json")):
        snap = load(p)
        if snap and snap.get("results"):
            snap["results"] = {norm_key(k): v for k, v in snap["results"].items()}
            runs.append(snap)
    return sorted(runs, key=lambda x: x.get("started") or "")


def compare(cur, prev):
    out = {"new": [], "fixed": [], "still": [], "states": [], "other": [], "same": 0}
    keys = sorted(set(cur["results"]) | set(prev["results"]))
    for k in keys:
        a, b = prev["results"].get(k), cur["results"].get(k)
        if not a:
            out["other"].append((k, "new in this run", b))
            continue
        if not b:
            out["other"].append((k, "not in this run", a))
            continue
        ba, bb = a["bucket"], b["bucket"]
        flips = [f'{st} {"✓" if a["states"].get(st) else "✗"} → {"✓" if ok else "✗"}'
                 for st, ok in b["states"].items()
                 if st in a["states"] and a["states"][st] != ok]
        if bb == DEFECT and ba != DEFECT:
            out["new"].append((k, f"{WORD[ba]} → Defect", b, flips))
        elif ba == DEFECT and bb == PASS:
            out["fixed"].append((k, "Defect → Pass", b, flips))
        elif ba == DEFECT and bb == DEFECT:
            out["still"].append((k, "Defect", b, flips))
        elif ba != bb:
            out["other"].append((k, f"{WORD[ba]} → {WORD[bb]}", b))
        elif flips:
            out["states"].append((k, "", b, flips))
        else:
            out["same"] += 1
    for name in sorted(set(cur["trigger"]) | set(prev["trigger"]), key=str):
        x, y = prev["trigger"].get(name), cur["trigger"].get(name)
        if x != y:
            out["other"].append((f"Trigger test · {name}", f"{x or 'not run'} → {y or 'not run'}", None))
    return out


def bucket_counts(snap):
    c = {b: 0 for b in ORDER}
    for r in (snap.get("results") or {}).values():
        c[r["bucket"]] = c.get(r["bucket"], 0) + 1
    return c


def short_time(started):
    try:
        return datetime.strptime(started, "%Y-%m-%d %H:%M:%S").strftime("%d %b %H:%M")
    except (TypeError, ValueError):
        return str(started or "")


def streak(key, runs, want):
    """How many runs in a row, ending with the newest in `runs`, had this
    result in bucket `want`; and when that streak started."""
    n, since = 0, None
    for r in reversed(runs):
        res = (r.get("results") or {}).get(key)
        if not res or res["bucket"] != want:
            break
        n, since = n + 1, r.get("started")
    return n, since


def flaky(cur, runs, window=6):
    """Results that flipped between Pass and Defect at least twice in the
    last `window` runs (this run included)."""
    hist = (runs + [cur])[-window:]
    out = []
    for k in cur["results"]:
        seq = [(r.get("results") or {}).get(k, {}).get("bucket") for r in hist]
        seq = [b for b in seq if b in (PASS, DEFECT)]
        flips = sum(1 for a, b in zip(seq, seq[1:]) if a != b)
        if flips >= 2:
            out.append((k, seq))
    return out


def counts_line(d):
    return (f'{len(d["new"])} new problem(s), {len(d["fixed"])} fixed, '
            f'{len(d["still"])} still failing')


def diff_html(cur, runs):
    prev = runs[-1] if runs else None
    if not prev:
        return ("<p class=\"muted\">No previous run of this account on this grid "
                "to compare with. The next run will show what changed.</p>", "first run - nothing to compare")
    d = compare(cur, prev)
    hist = runs + [cur]
    same_build = cur["build"] and cur["build"] == prev.get("build")
    btxt = (f'Build {esc(prev.get("build"))} → {esc(cur["build"])}' if cur["build"] and prev.get("build")
            and not same_build else "same build, re-run" if same_build
            else "build not recorded for a local run")
    head = (f'<div class="dhd">Compared with the previous run: <b>{esc(prev.get("started"))}</b> · {btxt}'
            f' · time {esc(prev.get("total"))} → {esc(cur["total"])}</div>')
    nn, nf = len(d["new"]), len(d["fixed"])
    if nn and nn >= nf:
        verdict = ("worse", "✗ Worse than the last run",
                   f"{nn} new problem{'s' if nn != 1 else ''}" + (f", {nf} fixed" if nf else ""))
    elif nf:
        verdict = ("better", "✓ Better than the last run",
                   f"{nf} fixed" + (f", {nn} new problem{'s' if nn != 1 else ''}" if nn else ", no new problems"))
    else:
        verdict = ("same", "= Same as the last run",
                   f"no new problems, nothing fixed, {len(d['still'])} still failing")
    ca, cb = bucket_counts(prev), bucket_counts(cur)

    def delta(b, good_up):
        x, y = ca.get(b, 0), cb.get(b, 0)
        diff = y - x
        cls = "dz" if diff == 0 else ("dg" if (diff > 0) == good_up else "dr")
        sign = f"+{diff}" if diff > 0 else (str(diff) if diff else "±0")
        return (f'<div class="dc"><span class="l">{WORD[b]}</span>'
                f'<span class="n">{x} → <b>{y}</b></span> <span class="{cls}">{sign}</span></div>')
    counts_html = ('<div class="dcs">' + delta(PASS, True) + delta(DEFECT, False)
                   + delta(BLOCKED, False) + delta(KNOWN, False) + "</div>")
    # history strip: defects per run, last 8 runs, this run highlighted
    last = hist[-8:]
    peak = max([bucket_counts(r)[DEFECT] for r in last] + [1])
    bars = ""
    for i, r in enumerate(last):
        n = bucket_counts(r)[DEFECT]
        now = i == len(last) - 1
        h = max(3, round(48 * n / peak)) if n else 2
        bars += (f'<div class="hb{" now" if now else ""}" title="{esc(r.get("started"))}: '
                 f'{n} defect(s)"><span class="hv">{n}</span><span class="hbar" '
                 f'style="height:{h}px"></span><span class="ht">'
                 f'{"this run" if now else esc(short_time(r.get("started")))}</span></div>')
    strip = (f'<div class="hist"><div class="hl">Defects per run (last {len(last)} runs)</div>'
             f'<div class="hbs">{bars}</div></div>')
    head = (f'<div class="dverdict {verdict[0]}"><b>{verdict[1]}</b> — {esc(verdict[2])}</div>'
            + head + f'<div class="dtop">{counts_html}{strip}</div>')

    def label(k):
        r = cur["results"].get(k) or prev["results"].get(k) or {}
        if not r:
            return esc(k)
        return (f'<b>{esc(r.get("integration"))}</b> <span class="muted">· {esc(r.get("mode"))} · '
                f'{esc(r.get("scenario"))}</span>')

    def age(k, cls):
        if cls == "warnc":            # still failing: how long
            n, since = streak(k, hist, DEFECT)
            return (f' <span class="age">failing {n} runs in a row, since '
                    f'{esc(short_time(since))}</span>') if n > 1 else ""
        if cls == "ok":               # fixed: how long it had been failing
            n, since = streak(k, runs, DEFECT)
            return (f' <span class="age">after {n} failing run{"s" if n != 1 else ""}'
                    f'</span>') if n else ""
        if cls == "bad":              # new: was it fine for long?
            n, _ = streak(k, runs, PASS)
            return (f' <span class="age">was passing for {n} run{"s" if n != 1 else ""}'
                    f'</span>') if n else ""
        return ""

    def item(k, change, flips, cls):
        return (f'<li>{label(k)} <span class="{cls}">{esc(change)}</span>{age(k, cls)}'
                + (f' <span class="muted small">{esc("; ".join(flips))}</span>' if flips else "") + "</li>")
    body = ""
    for key, title, cls in (("new", "New problems", "bad"), ("fixed", "Fixed", "ok"),
                            ("still", "Still failing", "warnc")):
        rows = d[key]
        body += (f'<div class="dgrp"><div class="dt {cls}">{title} ({len(rows)})</div>'
                 + (f'<ul class="dl">{"".join(item(k, c, fl, cls) for k, c, _, fl in rows)}</ul>'
                    if rows else '<div class="muted small">none</div>') + "</div>")
    fl_rows = flaky(cur, runs)
    if fl_rows:
        body += ('<div class="dgrp"><div class="dt warnc">Unstable — flips between pass and fail '
                 f'({len(fl_rows)})</div><ul class="dl">'
                 + "".join(f'<li>{label(k)} <span class="muted small">'
                           f'{esc(" → ".join(WORD[b] for b in seq))}</span></li>' for k, seq in fl_rows)
                 + '</ul><div class="muted small">Re-run before raising a bug for these.</div></div>')
    extra = [f'{label(k)}: {esc("; ".join(fl))}' for k, _, _, fl in d["states"]]
    extra += [f'{label(k) if k in cur["results"] or k in prev["results"] else esc(k)}: {esc(c)}'
              for k, c, _ in d["other"]]
    body += ('<div class="dgrp"><div class="dt">Other changes</div>'
             + (f'<ul class="dl">{"".join(f"<li>{x}</li>" for x in extra)}</ul>' if extra
                else '<div class="muted small">none</div>')
             + f'<div class="muted small">Unchanged: {d["same"]} result(s)</div></div>')

    # one line against the last run on a DIFFERENT build
    other = [r for r in runs if cur["build"] and r.get("build") and r.get("build") != cur["build"]]
    if other:
        pb = other[-1]
        db = compare(cur, pb)
        names = ", ".join(k for k, *_ in db["new"]) or "none"
        bline = (f'<div class="dbl">vs previous build <b>{esc(pb.get("build"))}</b> '
                 f'(run {esc(pb.get("started"))}): {esc(counts_line(db))}. New problems: {esc(names)}</div>')
    elif cur["build"]:
        bline = '<div class="dbl muted">vs previous build: no earlier run on a different build yet.</div>'
    else:
        bline = ('<div class="dbl muted">vs previous build: this run did not record which build '
                 'was deployed (local run). Jenkins runs record it.</div>')
    line = f'vs run {prev.get("started")} ({btxt}): {counts_line(d)}'
    return head + f'<div class="dgrid">{body}</div>' + bline, line


CSS = """
:root{--bg:#f5f6f8;--card:#fff;--ink:#16191d;--mut:#667080;--line:#e3e6ea;
--pass:#1a7f37;--passbg:#e8f5ec;--def:#c62828;--defbg:#fdecec;--blk:#a15c00;
--blkbg:#fff4e0;--kn:#5f6b7a;--knbg:#eef0f3;--inc:#0b62c4;--incbg:#e8f1fc}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);
font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
.wrap{max-width:1180px;margin:0 auto;padding:28px 18px 40px}
h1{font-size:22px;margin:0}h2{font-size:15px;margin:26px 0 10px;text-transform:uppercase;
letter-spacing:.06em;color:var(--mut)}
.sub{color:var(--mut);margin:4px 0 16px;font-size:13px}
html{scroll-behavior:smooth}h2{scroll-margin-top:12px}
.meta{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px 18px;background:var(--card);
border:1px solid var(--line);border-radius:10px;padding:12px 16px;margin:10px 0 14px;font-size:13.5px}
.meta div{overflow-wrap:anywhere}.meta span{display:block;font-size:11px;color:var(--mut);
text-transform:uppercase;letter-spacing:.05em}.meta small{display:block;color:var(--mut);font-size:11.5px}
.meta .wide{grid-column:1/-1}.meta em{color:var(--mut);font-style:normal;font-size:12px}
@media(max-width:700px){.meta{grid-template-columns:1fr 1fr}}
a.tile{color:inherit;text-decoration:none;transition:box-shadow .12s,transform .12s}
a.tile:hover{box-shadow:0 2px 8px rgba(0,0,0,.08);transform:translateY(-1px)}
.tile .go{font-size:11px;color:#0b62c4;margin-top:2px}.tile.zero{opacity:.55}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px 18px}
.banner{border-radius:10px;padding:16px 20px;color:#fff;font-size:17px;font-weight:600}
.banner small{display:block;font-weight:400;font-size:13px;opacity:.92;margin-top:2px}
.b-PASS{background:var(--pass)}.b-DEFECT{background:var(--def)}.b-BLOCKED{background:var(--blk)}
.tiles{display:flex;gap:10px;margin-top:12px;flex-wrap:wrap}
.tile{flex:1;min-width:120px;background:var(--card);border:1px solid var(--line);border-radius:10px;
padding:10px 14px}.tile .n{font-size:26px;font-weight:700}.tile .l{font-size:12px;color:var(--mut)}
.attn{list-style:none;margin:0;padding:0}.attn li{padding:9px 0;border-bottom:1px solid var(--line)}
.attn li:last-child{border:0}.attn .what{font-weight:600}
.tts{display:grid;grid-template-columns:repeat(auto-fill,minmax(250px,1fr));gap:8px}
.tt{border:1px solid var(--line);border-radius:8px;padding:8px 10px;background:var(--card)}
.tt.ok b{color:var(--pass)}.tt.bad b{color:var(--def)}.tt code{display:block;margin-top:4px}
.tt .small{display:block;margin-top:2px}
.tw{overflow-x:auto}table{border-collapse:collapse;width:100%}
.mx th,.mx td{border-bottom:1px solid var(--line);padding:9px 10px;text-align:left;vertical-align:top}
.mx thead th{background:#fafbfc;font-weight:600}.mx .mon{font-size:13px}
.mx .route{font-size:11.5px;color:var(--mut);font-weight:500}
.mx th.ig{width:190px;font-weight:600}
.v{font-weight:700;font-size:12px;display:inline-block;padding:1px 8px;border-radius:12px}
.v-PASS{color:var(--pass);background:var(--passbg)}.v-DEFECT{color:var(--def);background:var(--defbg)}
.v-BLOCKED{color:var(--blk);background:var(--blkbg)}.v-KNOWN{color:var(--kn);background:var(--knbg)}
.v-INCONCLUSIVE{color:var(--inc);background:var(--incbg)}
.v-CHECK{color:var(--blk);background:var(--blkbg)}
.chk{font-size:11.5px;color:var(--blk);margin-top:3px}
.chips{margin-top:5px}.st{display:inline-block;font-size:11px;font-weight:600;margin:0 6px 2px 0}
.st.ok,.ok{color:var(--pass)}.st.bad,.bad{color:var(--def)}
code{font:12px ui-monospace,SFMono-Regular,Menlo,monospace;background:#f1f3f5;border-radius:4px;
padding:1px 5px;word-break:break-all;display:inline-block;margin:3px 4px 0 0}
.ids{margin-top:2px}.muted{color:var(--mut)}.small{font-size:12px}.na{color:#b0b6bd}
.warn{color:var(--blk);font-size:13px;margin:8px 0 0}
details{background:var(--card);border:1px solid var(--line);border-radius:8px;margin-bottom:8px}
summary{cursor:pointer;padding:10px 14px}details[open] summary{border-bottom:1px solid var(--line)}
.dsc{padding:10px 14px;border-bottom:1px dashed var(--line)}.dsc:last-child{border:0}
.dh{margin-bottom:3px}.tk{margin-top:3px}.tk code{margin:0 2px}.st-t{margin-top:6px;font-size:12.5px}
.st-t th,.st-t td{border-bottom:1px solid var(--line);padding:5px 8px;text-align:left;vertical-align:top}
.mono{font-family:ui-monospace,Menlo,monospace;white-space:nowrap}
.foot{color:var(--mut);font-size:12px;margin-top:26px}
.warnc{color:var(--blk)}.dhd{font-size:13px;margin-bottom:10px}
.dgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:14px}
.dgrp{min-width:0}.dt{font-weight:700;font-size:13px;margin-bottom:4px}
.dl{margin:0;padding-left:18px;font-size:12.5px}.dl li{margin:3px 0}
.dbl{margin-top:12px;padding-top:10px;border-top:1px dashed var(--line);font-size:12.5px}
.dverdict{font-size:15px;padding:8px 12px;border-radius:8px;margin-bottom:10px}
.dverdict.better{background:var(--passbg);color:var(--pass)}
.dverdict.worse{background:var(--defbg);color:var(--def)}
.dverdict.same{background:var(--knbg);color:var(--kn)}
.dtop{display:flex;flex-wrap:wrap;gap:14px 24px;align-items:flex-end;margin-bottom:14px}
.dcs{display:flex;flex-wrap:wrap;gap:8px}
.dc{border:1px solid var(--line);border-radius:8px;padding:6px 10px;min-width:110px;font-variant-numeric:tabular-nums}
.dc .l{display:block;font-size:11px;color:var(--mut);text-transform:uppercase;letter-spacing:.05em}
.dc .n{font-size:14px}.dg{color:var(--pass);font-weight:700}.dr{color:var(--def);font-weight:700}
.dz{color:var(--mut)}
.age{font-size:11.5px;color:var(--mut);font-style:italic}
.hist{min-width:0}.hl{font-size:11px;color:var(--mut);text-transform:uppercase;letter-spacing:.05em;margin-bottom:4px}
.hbs{display:flex;gap:6px;align-items:flex-end}
.hb{display:flex;flex-direction:column;align-items:center;gap:2px;font-variant-numeric:tabular-nums}
.hbar{width:22px;background:var(--defbg);border:1px solid var(--def);border-radius:4px 4px 0 0;opacity:.75}
.hb.now .hbar{background:var(--def);opacity:1}
.hv{font-size:11px;color:var(--ink)}.ht{font-size:10px;color:var(--mut);white-space:nowrap}
"""


def build(cons, evts, account, out_path):
    reps = [("consolidated", cons), ("events", evts)]
    recs = []
    for _, r in reps:
        recs += ((r or {}).get("env") or {}).get("integration_records") or []
    findings = [f for _, r in reps for f in (r or {}).get("findings") or []]
    counts = {b: sum(1 for f in findings if f["bucket"] == b) for b in ORDER}
    health = (DEFECT if counts[DEFECT] else BLOCKED if counts[BLOCKED] or counts[INCONC]
              else PASS)
    msg = {DEFECT: f"{counts[DEFECT]} problem(s) need a developer",
           BLOCKED: "Some checks could not run — not a product verdict",
           PASS: "Every integration worked for every state"}[health]
    grid = next(((r or {}).get("env") or {}).get("grid") for _, r in reps if r) or ""
    # Wall-clock: first start to last finish (the event cycle may run in
    # parallel with the consolidated run, so the parts must not be added).
    fmt = "%Y-%m-%d %H:%M:%S"
    starts = [r.get("started") for _, r in reps if r and r.get("started")]
    ends = [r.get("finished") for _, r in reps if r and r.get("finished")]
    cyc_ev = ((evts or {}).get("cycle") or {}).get("started_at")
    if cyc_ev:
        starts.append(cyc_ev.replace("T", " ")[:19])
    started = min(starts) if starts else ""
    finished = max(ends) if ends else ""
    try:
        secs = int((datetime.strptime(finished, fmt)
                    - datetime.strptime(started, fmt)).total_seconds())
        total = f"{secs // 60}m {secs % 60:02d}s"
    except ValueError:
        total = "—"
    parts = " · ".join(f'{"consolidated" if k == "consolidated" else "event-based"} '
                       f'{r.get("duration")}' for k, r in reps if r and r.get("duration"))
    mon_c = ", ".join(c["title"] for c in scenarios(cons, "consolidated"))
    mon_e = ", ".join(sorted({c["title"] for c in scenarios(evts, "events")}))
    user = os.environ.get("S247_LOGIN_USER") or "—"
    grid_name = os.environ.get("S247_GRID_NAME") or grid
    cur = snapshot(reps, account, grid_name, started, finished, total)
    hdir = history_dir(os.path.dirname(out_path) or ".")
    runs = [r for r in previous_runs(hdir, account, grid_name)
            if (r.get("started") or "") < started]
    diff_block, diff_line = diff_html(cur, runs)
    build_txt = cur["build"] or "not recorded (local run)"

    attn = ""
    for f in sorted((f for f in findings if f["bucket"] in (DEFECT, BLOCKED, INCONC)),
                    key=lambda f: ORDER.index(f["bucket"])):
        nm, app = integ_label(f["integration"], recs)
        ids = ticket_ids(f)
        attn += (f'<li><span class="v v-{f["bucket"]}">{ICON[f["bucket"]]} '
                 f'{WORD[f["bucket"]]}</span> <span class="what">{esc(nm)}</span>'
                 + (f' <span class="muted">({esc(app)})</span>' if app else "")
                 + f' · {esc(f.get("monitor"))}<br><span>{esc(short_reason(f))}</span>'
                 + "".join(f'<code>{esc(t)}</code>' for t in ids) + "</li>")
    for f in findings:
        if f["bucket"] == PASS and missed(f):
            nm, app = integ_label(f["integration"], recs)
            attn += (f'<li><span class="v v-CHECK">? Check</span> '
                     f'<span class="what">{esc(nm)}</span>'
                     + (f' <span class="muted">({esc(app)})</span>' if app else "")
                     + f' · {esc(f.get("monitor"))}<br><span>Ticket lifecycle passed, '
                     f'but no {esc(", ".join(missed(f)))} alert was logged for it</span>'
                     + "".join(f'<code>{esc(t)}</code>' for t in ticket_ids(f)) + "</li>")
    attn = (f'<ul class="attn">{attn}</ul>' if attn else
            '<p class="ok"><b>Nothing — every integration passed.</b></p>')

    def restored(r):
        c = (r or {}).get("cycle") or {}
        return bool(c.get("restore_verified") or (c.get("restored") and c.get("restore_verified") is None))

    rest = " · ".join(f'{"Consolidated" if k == "consolidated" else "Event-based"} monitors '
                      + ('<span class="ok">restored ✓</span>' if restored(r)
                         else '<span class="bad">NOT confirmed restored — check by hand</span>')
                      for k, r in reps if r)

    # Each tile jumps to where those results are (plain links: Jenkins'
    # report viewer blocks JavaScript).
    target = {PASS: "consolidated", DEFECT: "attention", BLOCKED: "attention",
              KNOWN: "details"}
    tiles = ""
    for b, c in ((PASS, "pass"), (DEFECT, "def"), (BLOCKED, "blk"), (KNOWN, "kn")):
        inner = (f'<div class="n" style="color:var(--{c})">{counts[b]}</div>'
                 f'<div class="l">{WORD[b]}</div>')
        tiles += (f'<a class="tile" href="#{target[b]}">{inner}'
                  f'<div class="go">view &darr;</div></a>' if counts[b] else
                  f'<div class="tile zero">{inner}</div>')
    doc = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Integrations — {esc(account)} — {esc(started)}</title><style>{CSS}</style></head>
<body><div class="wrap">
<h1>Third-Party Integrations — {esc(account)}</h1>
<div class="meta">
<div><span>Build deployed in</span>{esc(grid) or "—"}</div>
<div><span>Account</span>{esc(account) or "—"}</div>
<div><span>User</span>{esc(user)}</div>
<div><span>Started</span>{esc(started) or "—"}</div>
<div><span>Finished</span>{esc(finished) or "—"}</div>
<div><span>Total time</span>{esc(total)}<small>{esc(parts)}</small></div>
<div><span>Build</span>{esc(build_txt)}</div>
<div class="wide"><span>Monitors</span>{esc(mon_c) or "—"} <em>(consolidated)</em>
{(" · " + esc(mon_e) + " <em>(event-based)</em>") if mon_e else ""}</div>
</div>
<div class="banner b-{health}">{ICON[health]} {WORD[health].upper()} — {esc(msg)}
<small>Consolidated alerts (monitor level) and event-based alerts (every event), each checked state by state.</small></div>
<div class="tiles">{tiles}</div>

<h2 id="changes">What changed since the last run</h2><div class="card">{diff_block}</div>

<h2 id="attention">Needs attention</h2><div class="card">{attn}</div>

<h2>Trigger tests</h2><div class="tts">{trigger_tests(cons or evts)}</div>

<h2 id="consolidated">Consolidated alerts — monitor level</h2>
<div class="card">{matrix(cons, "consolidated", recs) or '<p class="muted">Not run.</p>'}</div>

<h2 id="events">Event-based alerts — every event occurrence</h2>
<div class="card">{matrix(evts, "events", recs) or '<p class="muted">Not run.</p>'}</div>

<h2 id="details">Details</h2>{details(reps, recs)}

<div class="foot">{rest}<br>
✓ Pass · ✗ Defect (product problem) · ! Blocked (our access/setup, not a product verdict) ·
• Known (tracked) — state marks show each alert Site24x7 sent: CRITICAL, TROUBLE, DOWN, UP.
Ticket ids are the ids inside the tool. No passwords or tokens are in this report.</div>
</div></body></html>"""
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(doc)
    stamp = (started or "").replace("-", "").replace(":", "").replace(" ", "_")
    with open(os.path.join(hdir, f"{account}__{grid_name}__{stamp}.json"), "w",
              encoding="utf-8") as fh:
        json.dump(cur, fh, indent=1)
    with open(os.path.join(os.path.dirname(out_path) or ".", "diff_line.txt"), "w",
              encoding="utf-8") as fh:
        fh.write(diff_line + "\n")
    print(f"  [OK] diff: {diff_line}")
    return health


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--consolidated", default=None)
    ap.add_argument("--events", default=None)
    ap.add_argument("--account", default=os.environ.get("S247_ACCOUNT", ""))
    ap.add_argument("--out-dir", default="reports")
    a = ap.parse_args()
    cons = load(a.consolidated or latest(os.path.join(a.out_dir, "report_2*.json")))
    evts = load(a.events or latest(os.path.join(a.out_dir, "events_report_2*.json")))
    if not cons and not evts:
        print("[BLOCKER] no report JSON found", file=sys.stderr)
        return 2
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = os.path.join(a.out_dir, f"summary_report_{stamp}.html")
    health = build(cons, evts, a.account, out)
    # A FIXED name as well, so one bookmarked link always opens the newest.
    latest_copy = os.path.join(a.out_dir, "latest_summary.html")
    with open(out, encoding="utf-8") as src, \
            open(latest_copy, "w", encoding="utf-8") as dst:
        dst.write(src.read())
    print(f"  [OK] summary report: {os.path.abspath(out)}  (health {health})")
    print(f"  [OK] latest link   : file://{os.path.abspath(latest_copy)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
