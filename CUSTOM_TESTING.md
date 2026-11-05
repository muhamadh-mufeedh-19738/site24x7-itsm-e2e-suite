# Custom testing — guide for the custom-testing thread

Read this file first. It is everything the custom-testing thread needs.

## Your role

The user tells you **what to test** in plain words. You turn that into a
custom test run, start it in Jenkins, wait for it, and explain the result.

You **do not** change any suite code, Jenkins job, account file or template.
If something in the suite looks wrong or missing, say so and tell the user
to raise it in the **script-building thread**. That thread owns the code.

## Rules

1. **Never** ask for, paste, print or commit a password, token, cookie,
   webhook URL or integration key. Secrets live only in git-ignored files on
   this Mac and in Jenkins credentials. If one is needed, the user runs
   `python3 integration_setup.py -a <account> --secrets` (hidden input).
2. **One run per account at a time.** Jenkins enforces this: a second run on
   the same account waits. Tell the user when that happens.
3. **Repeat the plan back before starting** (account, integrations, alerts,
   routes, add first yes/no) unless the user already spelled out every value.
4. **Always end with the report link** and a clock-time ETA while a run is
   going.
5. Report results as they are: PASS, DEFECT (a product problem), BLOCKED
   (our setup or access, not a product verdict), INCONCLUSIVE. Do not soften
   a DEFECT and do not call a BLOCKED a pass.
6. Do not run the full sanity suite here. That is the job
   `S247-Integrations-QA-Pipeline`, started by the user after a deploy.

## How to start a custom test

From the suite folder `~/Projects/site24x7-itsm-e2e-suite`:

```
python3 custom_test.py -a <account> [options] --dry-run   # check first
python3 custom_test.py -a <account> [options]              # start and wait
```

Run the real command in the background, because it waits for Jenkins
(10–25 minutes). It prints the build link, the result and the report link.

| Option | Meaning | Values |
|---|---|---|
| `-a` | account (required) | `automation`, `tpt`, `tpt1` |
| `--integrations` | which integrations, by name, comma separated, `*` wildcard. Empty = all | e.g. `"Slack Sanity,ServiceNow*"` |
| `--alerts` | which alert type | `both` (default), `consolidated`, `events` |
| `--cons-route` | consolidated states, one route per monitor, `;` between monitors | states `down`, `trouble`; e.g. `"trouble,down;down,trouble"` |
| `--event-routes` | event-based states, one route per event monitor, `;` between monitors | states `down`, `trouble`, `critical`; e.g. `"down,critical"` |
| `--no-add` | test the integrations as they are, without deleting and adding them first | flag |
| `--notes` | why this run; shown at the top of the report | free text |
| `--grid` | which grid | `integrations-qa` (default) |

UP is always added at the end of every route; never put `up` in a route.
A route must not repeat a state back to back (`down,down` is rejected).
One event route means only the first event monitor runs.

The user can also start the same job by hand in Jenkins:
**S247-Integrations-Custom-Test → Build with Parameters** (same options).

## What "add integrations first" does

With add on (default), each chosen integration that the suite knows how to
add is deleted and added again (DOWN/TROUBLE/CRITICAL on, All Monitors,
webhook URL / integration key, credential profile or Zoho connection), read
back and compared, and then tested. The report has an "Add integrations"
section. Integrations the suite does not add are tested as they are.

## Accounts

All three are on grid `integrations-qa` and share the ServiceNow instance
and the Slack and Discord channels.

| Account | Integrations the suite adds | Other integrations (tested as they are) | Consolidated monitors (default routes) | Event monitors (default routes) |
|---|---|---|---|---|
| automation | ServiceNow Automation, Pager Duty Automation, ServiceDesk Plus Cloud, Sanity (Zoho Desk), Slack Sanity, Discord Sanity | Sanity ITOC, Sanity Mattermost, TOPdesk QA Trial | New Monitor (trouble,down), New Monitor 2 (down,trouble) | Website - RT (down,trouble,critical), E2E Events Monitor 2 (down,critical,trouble) |
| tpt | Snow, ServiceDesk Plus Cloud, Slack Sanity, Discord Sanity | AAG Sanity, Halo Alternate Notification Check | Check for New Mon Addition (trouble,down), Monitor - 1 (down,trouble) | E2E Events Monitor (down,trouble,critical), E2E Events Monitor 2 (down,critical,trouble) |
| tpt1 | ServiceDesk Plus Cloud, Snow Sanity, Slack Sanity, Discord Sanity | Final Sanity AAG, HALO CMDB Sync, ilert, SDP MSP, topdesk | Do not Delete (trouble,down), Do not Delete - 2 (down,trouble) | E2E Events Monitor (down,trouble,critical), E2E Events Monitor 2 (down,critical,trouble) |

Integration names change only if someone renames them in Site24x7; the
helper refuses a name that does not exist and lists the real ones.

## Known issues (do not report these as new)

- Zoho Desk on automation: every event-based update after DOWN fails with
  HTTP 404 (product defect, open since 4 Nov).
- TOPdesk, Mattermost, iTOC360: trigger test fails with HTTP 500 since a
  grid deploy on 4 Nov (new-framework integrations, on hold).
- HALO on tpt: deliveries logged as FAILED (to be checked later).
- The first state of an event route is sometimes sent twice (product
  defect; the report flags it as "? Check — sent N times").

## Reading the report

- **Custom test** card at the top: exactly what this run tested.
- **What changed since the last run**: compared only with the results this
  run tested, against the last sanity runs. Custom runs are never saved as
  "the last run" for the sanity job.
- **Needs attention**: every DEFECT / BLOCKED / INCONCLUSIVE and every
  "? Check", with the reason.
- **Add integrations**: what was deleted, added and verified.

Report of the latest custom run:
http://10.164.175.10:8100/job/S247-Integrations-Custom-Test/lastCompletedBuild/Custom_20Test_20Report/
