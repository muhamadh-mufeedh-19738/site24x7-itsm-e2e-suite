# Jenkins + E2E Suite — Cheat Sheet

Jenkins: http://10.164.175.10:8100/job/S247-Integrations-QA-Pipeline/

## The one rule
Jenkins downloads the suite from **GitHub (branch `main`) on every run**.
So: **if it is pushed, Jenkins has it.** Nothing to copy, nothing to sync.

---

## 1. I changed the Python / JS suite code (in the IDE or anywhere)
**Easy way — one command** (sends new AND changed files):
```
cd ~/Projects/site24x7-itsm-e2e-suite && bash sync.sh "what I changed"
```
Done when it says `IN SYNC`. It refuses to send any .env / secret file.

**Committing from the IDE Git panel also works:** after `bash sync.sh --install`
(done once; `sync.sh` also does it automatically), every commit on `main` is
pushed to GitHub on its own (`githooks/post-commit`), and any commit containing
a secret file is blocked (`githooks/pre-commit`).

**Am I in sync?** `bash sync.sh --check` (changes nothing).

**Manual way** (same thing, step by step):

```
cd ~/Projects/site24x7-itsm-e2e-suite
```
```
git add -u
```
```
git status --short
```
Check the list: only files with `M` in front, and NO `.env` file. Then:
```
git commit -m "what I changed"
```
```
git push
```
Username: `muhamadh-mufeedh-19738` — Password: the GitHub token (Ctrl+Shift+V).
Done when you see `main -> main`.

New file (not just a changed one)? Add it by name first: `git add newfile.py`

## 2. Is my push really on GitHub? (the "am I in sync" check)
```
git status
```
- `Your branch is up to date with 'origin/main'` → in sync. The next Jenkins run uses it.
- `Your branch is ahead of 'origin/main' by N commit(s)` → NOT pushed yet. Run `git push`.

## 3. I changed a password / token / .env file
Jenkins keeps its own copy (secrets never go to GitHub). Update it:
1. `python3 jenkins_secrets.py` — lists exactly which credentials changed and puts
   ready-named copies in `~/jenkins-upload`
2. Jenkins → Manage Jenkins → Credentials → click the credential → Update → tick Replace → choose the file with the same name → Save
3. `python3 jenkins_secrets.py --done` — remembers them and deletes the upload copies

`sync.sh` reminds you whenever Jenkins' secrets are older than the laptop's.

| File on laptop | Jenkins credential |
|---|---|
| `accounts/<acct>/account.env` | `S247_<ACCT>_ACCOUNT_ENV` |
| `accounts/<acct>/.itsm.env`   | `S247_<ACCT>_ITSM_ENV` |
| `.token.<acct>.env` (suite folder) | `S247_<ACCT>_TOKEN_ENV` |

(`<ACCT>` in capitals, e.g. `S247_TPT1_ACCOUNT_ENV`)

## 4. I got a new Jenkins pipeline script (.groovy) from Claude
This is the only thing NOT taken from GitHub.
Job → Configure → click in Script box → Ctrl+A → Ctrl+V → Save.
If a new box (parameter) was added, it appears only after one run:
untick DEPLOY + RUN_SUITE → Build → it stops in seconds → Build with Parameters again.

## 5. Add a NEW ACCOUNT
1. Laptop: `python3 itsm.py --init <name>` and fill its files with nano
2. `git add accounts/<name>` (never the .env files — .gitignore blocks them) → commit → push
3. Jenkins: add the 3 credentials from section 3
4. Groovy script: add `'<name>'` to the `ACCOUNT` choices list → paste into Configure → Save

## 6. Add a NEW GRID
1. `grids.json` → add one line → commit → push
2. SD Build Update screen → F12 → Network → Schedule Build → build_update → Payload
3. Groovy script: add the grid to `GRIDS` (product, region, dc, stage, grid, tar from the payload)
   and to the `GRID` choices → paste into Configure → Save

## 7. Running a build
| I want to… | DEPLOY | RUN_SUITE |
|---|---|---|
| Deploy + test + report | ✅ | ✅ |
| Only test what is already on the grid | ☐ | ✅ |
| Only deploy | ✅ | ☐ |

- Same grid twice → 2nd build waits until the 1st report is done.
- Same account on two grids → both deploy, tests run one after the other.
- Report: open the build → **Integrations E2E Report** (left menu).

## 8. If it goes red — where to look
| Red at stage | Usually means |
|---|---|
| 1. Check inputs | a box is empty / wrong, or first run after a script update (run again) |
| 2. Token | `SD_REFRESH_TOKEN` credential expired / wrong |
| 3–4. Deploy | open SD → click the build → read the red "Reason" (script retries prep failures 2x) |
| 4b. Grid UP | grid still shows "We'll be right back" after 30 min |
| 5. Suite — login | open Build Artifacts → `login_failure.png` |
| 5. Suite — download | GitHub token (`GITHUB_SUITE`) expired |
