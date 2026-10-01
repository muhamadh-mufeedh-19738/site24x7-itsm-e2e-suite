# Site24x7 — Third-Party Integrations End-to-End Suite

Automated end-to-end validation for Site24x7 ↔ ITSM integrations
(ServiceNow, Zoho Desk, ServiceDesk Plus Cloud, HaloITSM, PagerDuty, and
collaboration tools like Slack/Teams/Cliq).

The suite drives a real alert lifecycle on a monitor (DOWN → UP), then
proves — end to end — that each integration:

1. **Trigger test** passes (integration is valid & reachable) — *Stage 0*
2. **Creates** a ticket/alert on DOWN — *Stage 2b + 3*
3. **Closes / resolves / work-notes** on UP — *Stage 3*
4. **Actually exists inside the ITSM tool** — *Stage 4*

It produces a single HTML report with a clear **PASS / DEFECT / BLOCKED**
verdict for **every integration**.

> ⚠️ **IMPORTANT — this suite ships with NO credentials.**
> Every developer runs it against **their own** Site24x7 account, their own
> `integrations-qa` (or other) grid, and their own ITSM tool accounts.
> You must fill in your own values in two files (`.token.env` and
> `.itsm.env`) before anything will run. See [Setup](#setup) below.

---

## Contents

- [What you need first](#what-you-need-first)
- [Setup](#setup)  ← **start here**
- [The two files YOU must edit](#the-two-files-you-must-edit)
- [Running the suite](#running-the-suite)
- [Understanding the report](#understanding-the-report)
- [Troubleshooting](#troubleshooting)
- [Security notes](#security-notes)
- [How it works (architecture)](#how-it-works-architecture)

---

## What you need first

| Requirement | Why | Check |
|---|---|---|
| **Python 3.8+** | runs all stages | `python3 --version` |
| **Node.js 18+** | mints the browser session cookie | `node --version` |
| **Google Chrome** | the grid needs a client certificate; real Chrome supplies it | installed |
| **`curl`** + (optional) **`jq`** | the OAuth token script | `curl --version` |
| **A Site24x7 account** on your grid | the system under test | — |
| **Your own ITSM tool accounts** | Stage 4 verification | — |

No Python packages to install — the suite uses only the standard library.

---

## Setup

### Step 1 — Copy the two credential templates

```bash
cd "Third-Party Integrations End to End Suite"

cp .token.env.sample .token.env     # Site24x7 OAuth credentials
cp .itsm.env.sample  .itsm.env      # ITSM tool credentials

chmod 600 .token.env .itsm.env      # make them readable only by you
```

Or just run the helper, which does the copy + permissions for you and tells
you exactly what to edit:

```bash
bash setup.sh
```

### Step 2 — Fill in **your own** values

Edit the two files (see the next section for what every field means):

```bash
nano .token.env     # client_id, client_secret, refresh_token, accounts_url
nano .itsm.env      # only the ITSM tools you actually use
```

### Step 3 — Point the suite at **your** grid

Edit `env.sh` and set your grid URL:

```bash
export S247_GRID_URL="https://integrations-qa.localsite24x7.com"   # ← change to YOUR grid
export S247_TOKEN_SCRIPT="$PWD/get_token.sh"
```

### Step 4 — Verify credentials

```bash
source env.sh
bash get_token.sh          # should print a long token, no errors
python3 check_session.py   # checks OAuth + session cookie, tells you what's missing
```

### Step 5 — One-time browser login (for the session cookie)

Stage 3 reads Alert Logs through your **browser session**, not the OAuth
token. Create it once:

```bash
node s247_login.js --setup   # a Chrome window opens — log in once, that's it
source .session.env          # load the cookie it wrote
```

After that, every run refreshes the cookie silently.

You're ready. Jump to [Running the suite](#running-the-suite).

---

## The two files YOU must edit

### `.token.env` — Site24x7 OAuth (required)

| Field | What to put | Where to get it |
|---|---|---|
| `client_id` | Your OAuth app's Client ID | Zoho API console (`api-console.*`) → Self Client |
| `client_secret` | Your OAuth app's Client Secret | same place |
| `refresh_token` | Long-lived refresh token | one-time grant-token exchange (steps in the sample file) |
| `accounts_url` | Accounts server for **your** data center | e.g. `https://accounts.localzoho.com` (local/QA) |

> The refresh token must include **Admin scope** — the Stage-0 trigger-test
> API requires it. Scopes: `Site24x7.Admin.All`, `Site24x7.Operations.All`,
> `Site24x7.Reports.All`.

### `.itsm.env` — ITSM tool credentials (fill only what you use)

Any tool left blank is **skipped**, never failed. Fill in only the tools
your integrations actually point at.

| Tool | Variables |
|---|---|
| **ServiceNow** | `SNOW_URL`, `SNOW_USER`, `SNOW_PASSWORD` |
| **Zoho Desk** | `ZOHODESK_ORG_ID`, `ZOHODESK_CLIENT_ID`, `ZOHODESK_CLIENT_SECRET`, `ZOHODESK_REFRESH_TOKEN` |
| **ServiceDesk Plus Cloud** | `SDP_PORTAL`, `SDP_CLIENT_ID`, `SDP_CLIENT_SECRET`, `SDP_REFRESH_TOKEN` |
| **HaloITSM** | `HALO_URL`, `HALO_CLIENT_ID`, `HALO_CLIENT_SECRET`, `HALO_TENANT` |
| **PagerDuty** | `PAGERDUTY_API_KEY`, `PAGERDUTY_USER_EMAIL` |

Each field is documented inline in `.itsm.env.sample`.

### Also check: `env.sh` — your grid URL

```bash
export S247_GRID_URL="https://<YOUR-GRID>.localsite24x7.com"   # ← the one line you must change
```

---

## Running the suite

### Quick sanity check (changes nothing)

```bash
source env.sh
source .session.env
python3 preflight.py          # is this account ready? blast-radius, pollers, cookie
```

### Full end-to-end run

```bash
source env.sh
source .session.env
source .itsm.env
python3 run_all.py
```

The report opens automatically in your browser, and its path is printed at
the end.

### Just the trigger test (Stage 0, fast)

```bash
python3 stage0_trigger_test.py
```

### Common flags

| Command | Does |
|---|---|
| `python3 run_all.py` | full run, all stages |
| `python3 run_all.py --skip-cycle` | reuse the last DOWN/UP cycle, just re-verify |
| `python3 stage0_trigger_test.py` | Stage 0 trigger test only |
| `python3 check_session.py` | diagnose auth problems |
| `python3 preflight.py` | readiness check, no changes |

> **Safety:** the suite only ever touches monitors listed in
> `accounts/<account>/allowlist.json`. Confirm that list before running on a
> shared account.

---

## Understanding the report

Each integration gets one of:

| Verdict | Meaning |
|---|---|
| ✅ **PASS** | Full lifecycle verified (create + close/resolve/work-note). |
| ❌ **DEFECT** | Something genuinely did not work (e.g. ticket never created). |
| 🔴 **BLOCKED** | Could not test — misconfigured integration, failed trigger test, or missing ITSM credentials. |
| ⚠️ **INCONCLUSIVE** | No create/close seen in the window (often timing or suppression). |

**Stage 0 (trigger test)** appears at the very top: if an integration fails
its trigger test, it is **blocked** from the lifecycle so you don't chase a
false defect caused by a bad API key.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `missing required value(s): client_id ...` | Fill in `.token.env` (Step 2). |
| Token prints but `/api` returns 401 | Your refresh token is for a different grid/DC — check `accounts_url` and grid URL. |
| Trigger test returns `error_code 1121` | Your OAuth token lacks **Admin** scope. The suite falls back to the session cookie — run `node s247_login.js --setup`. |
| Stage 3 says session rejected (401/403) | Cookie expired. `node s247_login.js` then `source .session.env`. |
| A tool shows "not configured" | You left its block blank in `.itsm.env` — fill it in if you want Stage 4 to verify it. |
| `node: command not found` | Install Node.js 18+. |

When in doubt: `python3 check_session.py` explains every credential and the
exact command to fix each one.

---

## Security notes

- **Never commit** `.token.env`, `.itsm.env`, `.session.env`, or any `*.env`
  file. They are already in `.gitignore`.
- Keep those files `chmod 600` (readable only by you).
- The scripts **never print** secret values — only their length and whether
  they work.
- Every developer uses **their own** credentials. Do not share token or ITSM
  secrets in chat.
- The `*.sample` files contain **placeholders only** and are safe to commit
  and share.

---

## How it works (architecture)

```
run_all.py  (orchestrator)
   │
   ├─ Stage 0  stage0_trigger_test.py   → validates each integration (trigger test)
   │                                       API check + Alert-Log confirmation
   ├─ Stage 1  stage1_inventory.py      → discovers monitors in the account
   ├─ Stage 2b stage2b_cycle.py         → drives one real DOWN → UP cycle
   ├─ Stage 3  stage3_verify.py         → reads Alert Logs, proves create/close/resolve
   └─ Stage 4  stage4_tickets.py        → logs into each ITSM tool, confirms the ticket
                                          exists there too

Auth:
   • OAuth token   get_token.sh  ← .token.env   (Site24x7 REST APIs)
   • Session cookie s247_login.js ← Chrome login (Alert Logs endpoint)
   • ITSM creds    .itsm.env                     (Stage 4 per-tool login)
```

**Multi-account?** Use `itsm.py` to keep several accounts isolated (separate
grid, token, cookie, Chrome profile, reports). See `python3 itsm.py --list`.

---

*Questions? Run `python3 check_session.py` and `python3 preflight.py` first —
between them they diagnose almost everything.*
