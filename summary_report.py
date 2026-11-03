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
                tool += (f'<code>{esc(t.get("ticket_id"))}</code> in {esc(f.get("tool"))}: '
                         f'{esc(t.get("status") or t.get("error") or ("found" if t.get("found") else "not found"))}; ')
            body += (f'<div class="dsc"><div class="dh"><span class="v v-{f["bucket"]}">'
                     f'{ICON[f["bucket"]]} {WORD[f["bucket"]]}</span> '
                     f'{"Event-based" if kind == "events" else "Consolidated"} · '
                     f'{esc(f.get("monitor"))}</div>'
                     f'<div class="small">{esc(f.get("headline"))}</div>'
                     + (f'<div class="small muted">Inside the tool: {tool}</div>' if tool else "")
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
.dh{margin-bottom:3px}.st-t{margin-top:6px;font-size:12.5px}
.st-t th,.st-t td{border-bottom:1px solid var(--line);padding:5px 8px;text-align:left;vertical-align:top}
.mono{font-family:ui-monospace,Menlo,monospace;white-space:nowrap}
.foot{color:var(--mut);font-size:12px;margin-top:26px}
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
    started = min((r.get("started") for _, r in reps if r), default="")
    dur = " + ".join(r.get("duration") or "" for _, r in reps if r)

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

    tiles = "".join(f'<div class="tile"><div class="n" style="color:var(--{c})">'
                    f'{counts[b]}</div><div class="l">{WORD[b]}</div></div>'
                    for b, c in ((PASS, "pass"), (DEFECT, "def"), (BLOCKED, "blk"),
                                 (KNOWN, "kn")))
    doc = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Integrations — {esc(account)} — {esc(started)}</title><style>{CSS}</style></head>
<body><div class="wrap">
<h1>Third-Party Integrations — {esc(account)}</h1>
<div class="sub">{esc(grid)} · run {esc(started)} · {esc(dur)}</div>
<div class="banner b-{health}">{ICON[health]} {WORD[health].upper()} — {esc(msg)}
<small>Consolidated alerts (monitor level) and event-based alerts (every event), each checked state by state.</small></div>
<div class="tiles">{tiles}</div>

<h2>Needs attention</h2><div class="card">{attn}</div>

<h2>Trigger tests</h2><div class="tts">{trigger_tests(cons or evts)}</div>

<h2>Consolidated alerts — monitor level</h2>
<div class="card">{matrix(cons, "consolidated", recs) or '<p class="muted">Not run.</p>'}</div>

<h2>Event-based alerts — every event occurrence</h2>
<div class="card">{matrix(evts, "events", recs) or '<p class="muted">Not run.</p>'}</div>

<h2>Details</h2>{details(reps, recs)}

<div class="foot">{rest}<br>
✓ Pass · ✗ Defect (product problem) · ! Blocked (our access/setup, not a product verdict) ·
• Known (tracked) — state marks show each alert Site24x7 sent: CRITICAL, TROUBLE, DOWN, UP.
Ticket ids are the ids inside the tool. No passwords or tokens are in this report.</div>
</div></body></html>"""
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(doc)
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
    print(f"  [OK] summary report: {os.path.abspath(out)}  (health {health})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
