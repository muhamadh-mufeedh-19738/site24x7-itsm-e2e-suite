# PIPELINE_CONTEXT.md — Jenkins CI for the Site24x7 Third-Party Integrations E2E Suite

> **For an AI assistant / IDE reading this:** you already know the suite's code
> (`itsm.py`, `run_all.py`, `stage*.py`, `s247_*.js`, …). This file explains
> everything **around** the code: the Jenkins pipeline, how a build gets deployed
> through Service Delivery (SD), how the suite runs on the Jenkins server, the
> grids, the credentials, the server's limitations, and the rules the owner
> (Mufi) wants followed. Read it fully before changing anything that touches
> Jenkins, SD, login, tokens, grids or reports.
>
> The Jenkins pipeline script itself is stored in this repo at
> **`jenkins/S247-Integrations-QA-Pipeline.groovy`** (a copy; the live copy is
> pasted into the Jenkins job — see §9).

---

## 1. What this system does (one paragraph)

A person opens Jenkins, clicks **Build with Parameters**, picks a **GRID**, an
**ACCOUNT**, pastes a **build URL**, and clicks Build. Jenkins then, with no
laptop involved: gets a fresh SD access token → asks SD to deploy that build to
the chosen grid (diff report bypassed) → waits until SD says *Completed* →
waits until the grid web app is really up again → downloads the **latest suite
code from GitHub** → logs in to Site24x7 headlessly → runs the full E2E suite
(`python3 itsm.py --grid <GRID> --account <ACCOUNT>`) → publishes the HTML
report, JSON and JUnit inside the Jenkins build page.

Goal: Team Lead and developers can run it any time, even when Mufi is on leave.

---

## 2. Big picture

```
 Person ──► Jenkins (http://10.164.175.10:8100, job "S247-Integrations-QA-Pipeline")
              │
              ├─ SD API  (https://sd.csez.zohocorpin.com)  ── deploys build to the grid
              │
              ├─ GitHub  (private repo, branch main)       ── suite code, downloaded every run
              │
              └─ Site24x7 grid (e.g. https://integrations-qa.localsite24x7.com)
                    ▲ headless Chromium (Playwright) login + REST calls
                    └─ ITSM tools (ServiceNow, Zoho Desk, SDP Cloud, PagerDuty, TOPdesk, HALO…)
```

**Source of truth for suite code = GitHub `main`.** Editing a file on the laptop
does nothing for Jenkins until it is pushed (§10). The pipeline script is the
only piece not taken from GitHub.

---

## 3. Jenkins facts

| Item | Value |
|---|---|
| URL | `http://10.164.175.10:8100` (Mufi's own Jenkins, not the team Jenkins) |
| Version | Jenkins 2.583, Java 21 on the server |
| Job | `S247-Integrations-QA-Pipeline` (Pipeline, "Pipeline script" pasted in Configure, Groovy sandbox ON) |
| Runs on | `agent { label 'built-in' }` — the Jenkins server itself, user `jenkins`, workspace `/var/lib/jenkins/workspace/S247-Integrations-QA-Pipeline` |
| Executors | Built-In Node set to **4** (so builds on different grids run in parallel) |
| Plugins used | Pipeline Utility Steps (`readJSON`/`writeJSON`), HTML Publisher (`publishHTML`), JUnit, NodeJS (tool name **`NodeJS-22`**), Credentials Binding, **Lockable Resources** (`lock`) |
| Old laptop agent | `mufi-laptop` node is obsolete (everything runs on the server now) — can be deleted |

### Jenkins-wide setting: report CSP (already applied)
Jenkins blocks CSS in published HTML by default (reports looked unstyled). Fixed
via Script Console and persisted in `$JENKINS_HOME/init.groovy.d/report-csp.groovy`:
```
hudson.model.DirectoryBrowserSupport.CSP =
  "sandbox; default-src 'none'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; font-src 'self' data:;"
```
Scripts stay blocked. If the report ever needs JavaScript (buttons, expand/collapse),
this CSP must be extended (`allow-scripts` + `script-src`) — a deliberate security decision.

---

## 4. Build parameters (what the person fills in)

| Parameter | Type | Meaning |
|---|---|---|
| `GRID` | choice | `integrations-qa`, `eum-dev`, `s247integrations`, `pmclient` |
| `ACCOUNT` | choice | `automation`, `tpt`, `tpt1` (folder names under `accounts/`) |
| `DEPLOY` | bool (✅) | Deploy via SD first. Untick = only run the suite on what is already on the grid |
| `RUN_SUITE` | bool (✅) | Run the E2E suite. Untick = only deploy |
| `SD_BUILD_URL` | string | `https://build.zohocorp.com/zoho/webmonitor/webhost/<BRANCH>/<DATE>/AdventNetWebMon.zip` (must start `https://`, end `.zip`) |
| `BUILD_COMMENT` | string | Shown in SD as Release Notes (required when deploying) |
| `NOTIFY_EMAIL` | string | Who SD emails (default `muhamadh.mufeedh@zohocorp.com`) |
| `SKIP_DIFF_REPORT` | bool (✅) | `skip_continue` in SD — bypasses the diff report so the deploy never pauses |

**Gotcha:** after the pipeline script adds/changes a parameter, Jenkins shows the
new box only **after one run**. Do a throw-away run (untick DEPLOY + RUN_SUITE →
Build → it stops in seconds), then use Build with Parameters again.

---

## 5. Pipeline stages (in order)

| Stage | What it does | Fails loudly when |
|---|---|---|
| **1. Check inputs** | Resolves `GRID` → URL + SD settings from the `GRIDS` map; validates URL/comment/email; prints a summary | Unknown grid; DEPLOY on a grid without SD settings; empty/invalid URL; new params not loaded yet |
| **Grid run** *(wrapper)* | `lock("grid-${GRID}")` around stages 2–5 | — (2nd build on the same grid **waits** until the 1st one's report is done) |
| **2. Get fresh SD access token** | `POST /tokens/v1/regenerate` with refresh token → 1-hour access token (kept in a `@Field`, never printed) | Refresh token wrong/expired |
| **3. Deploy build in SD** | Records newest existing SD build_id for this URL (baseline), writes `sd_build_update.json`, `POST …/action/build_update` | SD rejects (non-SUCCESS) |
| **4. Wait for deploy to finish** | Polls SD status every 60 s, up to 45 min. Tracks only build_ids newer than the baseline. **Auto re-submits up to 2×** if SD fails *while preparing* (zac status empty = grid not touched) | Failed/killed/reverted/skipped after grid started; timeout; grid not in `initiated_groups` as Completed |
| **4b. Wait for grid to be UP** | `curl $GRID_URL/app/client` every 30 s; DOWN if HTTP ≥400 or page contains "be right back"/"temporarily unavailable"; needs **2 good checks in a row**; max 30 min | Grid still down after 30 min |
| **5. Run E2E suite** | `lock("account-${ACCOUNT}")` (same account never tests twice at once). Downloads suite tarball from GitHub, places secrets, installs Playwright Chromium (cached), runs `itsm.py`, cleans secrets, collects report | See §8 exit codes |
| **post** | Clears token, archives `sd_build_update.json` | — |

Stages 2–4 are skipped when `DEPLOY` is unticked. Stage 5 skipped when `RUN_SUITE` unticked.

### Parallel builds
- Different grids → run at the same time.
- Same grid → strictly one after another (deploy → suite → report, then the next deploy).
- Same account on two grids → both deploy in parallel; the second build's **tests** wait.
- While waiting, the log shows `Trying to acquire lock on [grid-…] … waiting`.

---

## 6. Grids

Two places describe grids and **both must be updated** when adding one:

**a) Suite repo — `grids.json`** (used by `itsm.py` for the "WHICH GRID?" prompt):
```json
{ "grids": {
  "integrations-qa":  { "url": "https://integrations-qa.localsite24x7.com" },
  "eum-dev":          { "url": "https://eum-dev.localsite24x7.com",          "ip": "10.62.107.166" },
  "s247integrations": { "url": "https://s247integrations.localsite24x7.com", "ip": "10.62.107.246" },
  "pmclient":         { "url": "https://pmclient.localsite24x7.com",         "ip": "10.65.150.223" } } }
```

**b) Pipeline — `@Field Map GRIDS`** (SD deploy values, copied from the SD
"Build Update" screen payload: F12 → Network → Schedule Build → `build_update` → Payload):

| GRID | SD product | region / DC / stage | SD sub_grid | tar |
|---|---|---|---|---|
| integrations-qa | `S247INTEGRATIONS_QA` (id 6759) | CT / CT1 / production | `s247integrations-qa` | `s247integrations-qa.tar` |
| eum-dev | `WEBMONITOR_EUM_DEV` | CT / CT1 / production | `s247eum_dev` | `eumdev.tar` |
| s247integrations | `S247_INTERGRATIONS` *(misspelt in SD — keep exactly)* | CT / CT1 / production | `S247_Integrations` | `s247_integrations.tar` |
| pmclient | `WEBMONITOR_PMCLIENT` | CT / CT1 / production | `pmclient` | `webmonitor.tar` |

Also add the new grid name to the `GRID` choice list in the pipeline.

---

## 7. Service Delivery (SD) API — what we learned the hard way

- Base: `https://sd.csez.zohocorpin.com`. Service `Site24x7` (id 126).
- Auth headers: `Authorization: <access token>` (no prefix) **and** `Email: <user>`.
- Token: `POST /tokens/v1/regenerate`, header `Authorization: <refresh token>`,
  body `grant_type=refresh_token`. Access token ≈ 1 h; refresh token ≈ 1 year
  (contains `|`). Stored in Jenkins credential `SD_REFRESH_TOKEN`.
- Deploy: `POST /buildprovision/v1/services/Site24x7/products/<PRODUCT>/action/build_update`
  — body **must** be sent with `Content-Type: application/json` and
  `curl --data-binary @file` (without it SD returns HTTP 422 "all fields missing").
- Status: `GET /external/v1/status/services/Site24x7/products/<PRODUCT>/build_update?build_url=<urlencoded>&region=CT&sort_by=build_scheduled_at&sort_order=desc&limit=10`.
  `details[].details.build_details` is an **object keyed by numbers** (not a list).
  Status values: `start`, `start_sucess`, `In progress`, `Completed`; failures
  `Failed`/`killed`/`Reverted`/`skipped`. "build url provided does not exist" just
  means never deployed before (harmless).
- Lookup APIs live under `api/v2/...` (no trailing slash — a trailing slash returns non-JSON).

**Request body = exactly what the SD UI sends** (the UI payload was captured and copied):
```json
{ "service_name":"Site24x7", "product_name":"<PRODUCT>",
  "data_center":"CT1", "region":"CT", "deployment_mode":"live", "build_stage":"production",
  "build_url":"<URL>", "alternate_url":true, "is_patch_update":false, "is_grid_edited":false,
  "sub_grids":["<SUB_GRID>"], "tar_names":["<TAR>"],
  "parallel_products":[], "regions_to_be_synced":[], "ro_sync_disabled":0,
  "scheduler_details":{"time_to_sync":"0","ro_time_to_sync":"20"},
  "build_options":{ "start_server":true, "static_needed":true, "iast_jar_needed":true,
    "skip_continue":true, "skip_continue_for_parallel_products":true,
    "incremental_update":false, "milestone_tag_needed":false, "fresh_upload":false },
  "notify_to":["…"], "comment":"…", "provision_type":"build_update" }
```
- **`start_server: true` is essential.** Without it some builds (e.g. `feature/…`
  branches) failed with *"Failed while preparing the build's object for the start
  process"* while the same build deployed fine manually.
- `skip_continue: true` = "Skip Continue" in the UI = **diff report bypassed**
  (without it SD pauses at "Diff Report Generated" and the pipeline times out).
- After *Completed*, the grid restarts and shows **"We'll be right back"** for a few
  minutes — that is why stage 4b exists.

---

## 8. How the suite runs on the Jenkins server

### Server limitations (no sudo — all worked around, do not "fix" by assuming tools exist)
| Missing / blocked | Workaround in the pipeline |
|---|---|
| `git` not installed | Suite downloaded as a tarball: `curl https://api.github.com/repos/<owner>/<repo>/tarball/main` with `GITHUB_SUITE` token |
| `rm` → "Permission denied" for the jenkins user | All file work (unpack, place/clean secrets, pick report) done by an inline **Python helper** (`SUITE_TOOLS_PY`, written to `suite_tools.py`) |
| `jq` not installed | `get_token.sh` grep fallback fixed with `|| true` (under `set -o pipefail` a grep with no match killed the script silently) |
| No Google Chrome, no display | Playwright **Chromium** via NodeJS-22 tool; `PLAYWRIGHT_BROWSERS_PATH=/var/lib/jenkins/pw-browsers`; `S247_BROWSER_CHANNEL=chromium` (the JS files read this, default `chrome` on laptops). The `--headed` retry in `itsm.py` always fails on the server — harmless |
| Jenkins `sh` runs with `-xe` | Every script starts `set +x` (no secret echo); errors handled explicitly |

### Stage 5 step by step
1. `python3 -c print` check (loud fail if python3 unusable).
2. Download + unpack suite into `$WORKSPACE/suite` (fails if `itsm.py` missing).
3. Secrets placed from Jenkins file credentials (see §9):
   `.token.<acct>.env`, `accounts/<acct>/.itsm.env`, `accounts/<acct>/account.env`
   (chmod 600). Two lines are appended to `account.env` so the server uses its own paths:
   `S247_TOKEN_SCRIPT=$PWD/get_token.sh`, `TOKEN_ENV_FILE=$PWD/.token.<acct>.env`.
   The helper prints the **login email** that will be typed (never the password) and
   **fails if `S247_LOGIN_USER` is missing or has no `@`**. Note `itsm.py` loads
   `account.env` then `.itsm.env`, so a `S247_LOGIN_USER` in `.itsm.env` wins.
4. `npm install playwright` + `npx playwright install chromium` (cached after first run).
5. `python3 itsm.py --grid "$GRID" --account "$ACCOUNT" < /dev/null`
6. `finally`: secrets removed (`.token.*.env`, `accounts/*/.itsm.env`,
   `accounts/*/account.env`, `accounts/*/.session*.env`).
7. If login failed: `/tmp/s247_login_failure.png` → `suite-report/login_failure.png` (Build Artifacts).
8. Newest `report_*.html/.json`, `junit_*.xml` written **during this run** →
   `suite-report/` → `publishHTML` ("Integrations E2E Report"), `archiveArtifacts`, `junit`.

### Exit code → Jenkins result
| Suite exit | Report found | Jenkins |
|---|---|---|
| 0 | yes | ✅ SUCCESS |
| 1 (defect) | yes | 🟡 UNSTABLE |
| any | no | 🔴 "SUITE PRODUCED NO REPORT" |
| 2 (blocked) | yes | 🔴 "SUITE BLOCKED … read NOT A PRODUCT BUG" |

---

## 9. Credentials & secrets

**Never** put secrets in code, the pipeline script, chat, logs or GitHub. `.gitignore`
blocks `*.env`, `.itsm.env`, `.session*.env`, `account.env`, `.token*.env`, `env.sh`.

| Jenkins credential ID | Type | Contents |
|---|---|---|
| `SD_REFRESH_TOKEN` | Secret text | SD refresh token |
| `GITHUB_SUITE` | Username + password | GitHub user + fine-grained token (**Contents: read**, this repo only) |
| `S247_<ACCT>_ACCOUNT_ENV` | Secret file | `accounts/<acct>/account.env` (grid URL, `S247_LOGIN_USER`, `S247_LOGIN_PASS`, …) |
| `S247_<ACCT>_ITSM_ENV` | Secret file | `accounts/<acct>/.itsm.env` (ServiceNow/Desk/SDP/HALO/PagerDuty creds) |
| `S247_<ACCT>_TOKEN_ENV` | Secret file | that account's `.token.*.env` (OAuth client_id / secret / refresh_token for `get_token.sh`) |

`<ACCT>` = account name in capitals (`AUTOMATION`, `TPT`, `TPT1`) → 9 files + 2 = 11 credentials.

When a secret changes on the laptop, the Jenkins copy must be replaced:
Manage Jenkins → Credentials → the ID → Update → tick **Replace** → choose file → Save.
(Upload tip: copy hidden files to a visible `~/jenkins-upload/` folder first, delete the copy after.)

Login emails look like `muhamadh.mufeedh+<acct>@zohotest.com` (accounts server `accounts.localzoho.com`).

---

## 10. Keeping the laptop/IDE and Jenkins in sync

Jenkins never sees the laptop. **Push = sync.** In the suite folder:
```
bash sync.sh "what I changed"
```
`sync.sh` (in the repo root): `git add -u` (changed tracked files only — never the
untracked result files), **refuses if any `.env`/`.session`/`.token` file is staged**,
commits, pushes, then confirms `HEAD == origin/main` and prints `IN SYNC`.
Brand-new files: `git add <file>` once first. Check any time: `git status` →
"up to date with 'origin/main'".

GitHub push needs a fine-grained token with **Contents: read and write** as the
password (normal passwords are rejected). Optional: `git config --global credential.helper 'cache --timeout=28800'`.

The pipeline script (`jenkins/S247-Integrations-QA-Pipeline.groovy`) does NOT
auto-sync: after editing it, paste it into Job → Configure → Script → Save.

---

## 11. Suite changes made for the server / multi-grid setup (know these)

| File | Change | Why |
|---|---|---|
| `itsm.py` | Asks **WHICH GRID?** first (from `grids.json`), then **WHICH ACCOUNT?**; `--grid/-g` flag; dies loudly if no tty and no `--grid`. The chosen grid **overrides** `S247_GRID_URL`; cookie file is per grid: `accounts/<acct>/.session.<grid>.env`. All accounts work on all grids unless `account.env` has `export S247_GRIDS="a,b"`. `--init <name> --grid <g>` | Multi-grid testing with the same accounts/monitors |
| `grids.json` | New — list of grids (§6) | Dynamic, no hard-coded grids in code |
| `s247_login.js` | `channel: process.env.S247_BROWSER_CHANNEL \|\| 'chrome'`. After password submit, clicks **Skip / Remind me / Not now** on Zoho's "Secure your account using MFA" interstitial (never enrols MFA). Session probe tries **every `s247cname` cookie** valid for the grid host (fresh profiles hold two) and keeps the one that returns 200; saved cookie header is de-duplicated per name, grid-host cookies only; failure messages include the server's reply text | Headless login on a brand-new server profile |
| `s247_logreport.js`, `s247_integrations.js` | Same `S247_BROWSER_CHANNEL` channel switch | Chromium on server |
| `get_token.sh` | `\|\| true` on the grep/sed fallbacks; tolerant of `": "` spacing | Silent exit on machines without `jq` |
| `stage0_trigger_test.py` | 1121/1100/1102/401 on trigger_test = **SKIP (harness auth gap)**, never FAIL/BLOCKED | Old behaviour falsely marked every integration BLOCKED |
| `sync.sh`, `JENKINS_CHEATSHEET.md` | New helpers (§10) | Beginner-friendly sync |

Known report nuances (not bugs): ServiceNow tickets stay **New** when the
integration's "Action on Recovery" = Manual Close (UP posts a work-note);
PagerDuty in-tool lookup by Alert-Log id can return 400/404 (that id is not the
incident id) even though incidents exist — shown as ⚠, still PASS on Alert Logs.

---

## 12. Troubleshooting — where to look

| Symptom | Cause / fix |
|---|---|
| Red at stage 1 "not known"/"not loaded yet" | First run after a script update — just run again |
| "Invalid option type lock" (compile error) | Lockable Resources plugin missing |
| Stage 2 fails | `SD_REFRESH_TOKEN` expired/wrong |
| SD 422 | Body not sent as JSON (keep `Content-Type` + `--data-binary`) |
| Stage 4 "Failed while preparing … start process" | Was missing `start_server`; now also auto-retried 2× |
| Stage 4 timeout with "Diff Report Generated" | `skip_continue` false — keep SKIP_DIFF_REPORT ticked |
| Stage 4b down | Grid restart slow / broken — check grid in SD |
| Stage 5 "GitHub download failed 401/404" | `GITHUB_SUITE` token expired or no repo access |
| "email the suite will type" wrong / no `@` | Fix `S247_LOGIN_USER` in the account's env files and replace the Jenkins credential |
| Login "no usable session after 120s" | Open Build Artifacts → `login_failure.png`: wrong email (red text), MFA page (should auto-skip), OTP (needs a non-MFA test account) |
| `get_token.sh failed:` with empty message | jq-less fallback bug (fixed) — make sure the latest `get_token.sh` is pushed |
| Report BLOCKED although tickets were created | Jenkins ran an old suite version — `bash sync.sh …` then rerun |
| Report looks unstyled | Report CSP (§3) not applied / Jenkins restarted without the init script |
| Laptop changes "not used" by Jenkins | Not pushed — `git status` |

---

## 13. Rules Mufi wants followed

1. **Loud, explicit failures** — never silent skips or fake greens. A red build must say exactly why and what to do.
2. **Dynamic code** — no hard-coded monitor names, integration names, IDs or endpoints in the suite. Grids come from `grids.json`; accounts from `accounts/`.
3. **No secrets anywhere** except Jenkins Credentials and local `.env` files (git-ignored).
4. **Nothing depends on the laptop** — Jenkins must run end to end on the server.
5. Mufi is a **beginner** with terminals/Jenkins: explain steps one at a time, say what output to expect. No heredocs in terminal instructions (use `nano`). Mufi may be on a Mac — explain Mac basics (Command key, opening Terminal) when relevant.
6. Before changing the SD request body, compare it with the SD UI payload — the UI payload is the reference.
7. When adding a grid: `grids.json` + pipeline `GRIDS` map + `GRID` choices. When adding an account: `accounts/<name>/` + 3 Jenkins credentials + pipeline `ACCOUNT` choices.
