// =====================================================================
// S247 Integrations - CUSTOM TEST job
// Tests what is ALREADY on the grid (no deploy), with the options chosen
// on "Build with Parameters". The full sanity after a deploy stays in the
// job S247-Integrations-QA-Pipeline (untouched by this one).
// The helper code below is copied from that job - keep the two in step.
// =====================================================================

// ---------- Helper functions (no need to edit) ----------

import groovy.transform.Field

@Field String SD_ACCESS_TOKEN = ''
@Field List DEPLOY_TARGETS = []      // what this run deploys (WebMon and/or Plus), filled in stage 1
@Field String PLUS_BUILD_SUFFIX = 'Pluss247Server.zip'

// ===== GRIDS =====================================================
// One entry per grid. 'sd' = how SD deploys to it (copied from the SD
// "Build Update" screen payload). A grid with sd: null can run the E2E
// suite (untick DEPLOY) but cannot be deployed until its SD values are added.
// To add a grid: add it here AND in grids.json in the suite repo.
@Field Map GRIDS = [
    'integrations-qa' : [url: 'https://integrations-qa.localsite24x7.com',
                         sd : [product: 'S247INTEGRATIONS_QA', region: 'CT', dc: 'CT1',
                               stage: 'production', grid: 's247integrations-qa',
                               tar: 's247integrations-qa.tar']],
    'eum-dev'         : [url: 'https://eum-dev.localsite24x7.com',
                         sd : [product: 'WEBMONITOR_EUM_DEV', region: 'CT', dc: 'CT1',
                               stage: 'production', grid: 's247eum_dev',
                               tar: 'eumdev.tar']],
    's247integrations': [url: 'https://s247integrations.localsite24x7.com',
                         sd : [product: 'S247_INTERGRATIONS', region: 'CT', dc: 'CT1',   // spelled exactly as in SD
                               stage: 'production', grid: 'S247_Integrations',
                               tar: 's247_integrations.tar']],
    'pmclient'        : [url: 'https://pmclient.localsite24x7.com',
                         sd : [product: 'WEBMONITOR_PMCLIENT', region: 'CT', dc: 'CT1',
                               stage: 'production', grid: 'pmclient',
                               tar: 'webmonitor.tar']],
]

// ===== PLUS GRIDS ================================================
// Grids for the PLUS server build (server / network / poller / cloud monitors).
// Values copied from the SD "Build Update" screen payload. Add more here.
@Field Map PLUS_GRIDS = [
    'integrations-qa-plus': [sd: [product: 'QA_PLUS_INTEGRATIONS', region: 'CT', dc: 'CT1',
                                  stage: 'production', grid: 'Integrations-qa-plus',
                                  tar: 'Integrations-qa-plus.tar']],
]

// Small Python helper used by stage 5. This server blocks some Linux commands
// for Jenkins (rm is "Permission denied", git is missing), so every file job
// (unpack code, place secrets, clean secrets, pick report) is done in Python.
@Field String SUITE_TOOLS_PY = '''
import sys, os, shutil, tarfile, glob, time, hashlib

NL = chr(10)

def fail(msg, code=2):
    print("ERROR: " + msg)
    sys.exit(code)

def extract(tar_path, dest):
    if os.path.isdir(dest):
        shutil.rmtree(dest)
    os.makedirs(dest)
    with tarfile.open(tar_path) as t:
        for m in t.getmembers():
            parts = m.name.split("/", 1)
            if len(parts) < 2 or not parts[1] or m.issym() or m.islnk():
                continue
            m.name = parts[1]
            if hasattr(tarfile, "data_filter"):
                t.extract(m, dest, filter="data")
            else:
                t.extract(m, dest)
    os.remove(tar_path)
    if not os.path.isfile(os.path.join(dest, "itsm.py")):
        fail("itsm.py was not found in the code downloaded from GitHub")
    print("Suite code downloaded OK")

def place(acct, acc_env, itsm_env, token_env):
    d = os.path.join("accounts", acct)
    if not os.path.isdir(d):
        fail("accounts/" + acct + " is not in the GitHub repo")
    tok = os.path.abspath(".token." + acct + ".env")
    itsm = os.path.join(d, ".itsm.env")
    acc = os.path.join(d, "account.env")
    shutil.copyfile(token_env, tok)
    shutil.copyfile(itsm_env, itsm)
    shutil.copyfile(acc_env, acc)
    with open(acc, "a") as f:
        f.write(NL + 'export S247_TOKEN_SCRIPT="' + os.path.abspath("get_token.sh") + '"' + NL)
        f.write('export TOKEN_ENV_FILE="' + tok + '"' + NL)
    for p in (tok, itsm, acc):
        os.chmod(p, 0o600)
    if os.path.isfile("get_token.sh"):
        os.chmod("get_token.sh", 0o755)
    print("Secrets for " + acct + " placed (workspace only, deleted after the run)")
    # Show the LOGIN EMAIL each file sets (never the password). The suite reads
    # account.env first and then .itsm.env, so a line in .itsm.env WINS.
    final = None
    for label, p in (("account.env", acc), (".itsm.env", itsm)):
        with open(p) as f:
            for line in f:
                t = line.strip()
                if t.startswith("export "):
                    t = t[7:].strip()
                if t.startswith("S247_LOGIN_USER="):
                    v = t.split("=", 1)[1].strip().strip("'").strip('"')
                    print("  login email in " + label + ": " + v)
                    final = v
    print("  >> email the suite will type: " + str(final))
    if not final or "@" not in final:
        fail("S247_LOGIN_USER is missing or has no @ - fix it in the Jenkins credential(s) above")

def start(ws):
    out = os.path.join(ws, "suite-report")
    if os.path.isdir(out):
        shutil.rmtree(out)
    os.makedirs(out)
    with open(os.path.join(ws, ".suite_start"), "w") as f:
        f.write(str(time.time()))

def cleanup(suite):
    n = 0
    for pat in (".token.*.env", "accounts/*/.itsm.env", "accounts/*/account.env", "accounts/*/.session*.env"):
        for p in glob.glob(os.path.join(suite, pat)):
            os.remove(p)
            n += 1
    print("Secrets removed from workspace: " + str(n) + " file(s)")

def report(ws):
    with open(os.path.join(ws, ".suite_start")) as f:
        t0 = float(f.read().strip())
    def newest(pattern):
        files = [p for p in glob.glob(os.path.join(ws, "suite", "**", "reports", pattern), recursive=True)
                 if os.path.getmtime(p) >= t0]
        return max(files, key=os.path.getmtime) if files else None
    h = newest("report_*.html")
    summ = newest("summary_report_*.html")
    if not h and not summ:
        fail("the suite did not write an HTML report during this run", 1)
    out = os.path.join(ws, "suite-report")
    # FULL_RUN: the one-page summary IS the report; the two detailed reports
    # stay next to it for developers.
    shutil.copyfile(summ or h, os.path.join(out, "report.html"))
    if summ and h:
        shutil.copyfile(h, os.path.join(out, "consolidated_report.html"))
    ev = newest("events_report_*.html")
    if ev:
        shutil.copyfile(ev, os.path.join(out, "events_report.html"))
    dl = newest("diff_line.txt")
    if dl:
        shutil.copyfile(dl, os.path.join(out, "diff_line.txt"))
    for pat, name in (("report_*.json", "report.json"), ("junit_*.xml", "junit.xml"),
                      ("events_report_*.json", "events_report.json"),
                      ("events_junit_*.xml", "junit_events.xml")):
        f = newest(pat)
        if f:
            shutil.copyfile(f, os.path.join(out, name))
    print("Report picked up: " + (summ or h))

def shot(ws):
    src = "/tmp/s247_login_failure.png"
    out = os.path.join(ws, "suite-report")
    os.makedirs(out, exist_ok=True)
    t0 = 0.0
    try:
        with open(os.path.join(ws, ".suite_start")) as f:
            t0 = float(f.read().strip())
    except Exception:
        pass
    if os.path.isfile(src) and os.path.getmtime(src) >= t0:
        shutil.copyfile(src, os.path.join(out, "login_failure.png"))
        print("Login screenshot saved: see Build Artifacts > suite-report/login_failure.png")

def buildlabel(grid, deploy, url):
    # Which build is on the grid: the one deployed now, else the last one this
    # pipeline deployed to that grid (remembered on the Jenkins server).
    d = os.environ.get("S247_HISTORY_DIR", "/var/lib/jenkins/.s247-history")
    os.makedirs(d, exist_ok=True)
    f = os.path.join(d, "grid-" + grid + ".build")
    parts = [p for p in url.strip().rstrip("/").split("/") if p]
    label = "/".join(parts[-3:-1]) if len(parts) >= 3 and parts[-1].lower().endswith(".zip") else ""
    if deploy == "true" and label:
        with open(f, "w") as fh:
            fh.write(label)
    elif os.path.isfile(f):
        label = open(f).read().strip()
    print(label)

def itoc_seed(acct, seed):
    # Jenkins keeps its OWN refreshed iTOC360 session between builds. The
    # uploaded credential only replaces it when it changed (a new sign-in).
    d = os.environ.get("ITOC_STATE_DIR", "/var/lib/jenkins/.s247-itoc360")
    os.makedirs(d, exist_ok=True)
    os.chmod(d, 0o700)
    dest = os.path.join(d, "state-" + acct + ".json")
    mark = os.path.join(d, "seed-" + acct + ".sha256")
    with open(seed, "rb") as f:
        h = hashlib.sha256(f.read()).hexdigest()
    old = open(mark).read().strip() if os.path.isfile(mark) else ""
    if h != old or not os.path.isfile(dest):
        shutil.copyfile(seed, dest)
        os.chmod(dest, 0o600)
        with open(mark, "w") as f:
            f.write(h)
        print("iTOC360 session for " + acct + ": new sign-in installed")
    else:
        print("iTOC360 session for " + acct + ": keeping the refreshed session from earlier builds")

cmd, args = sys.argv[1], sys.argv[2:]
{"extract": extract, "place": place, "start": start, "cleanup": cleanup, "report": report, "shot": shot,
 "itoc_seed": itoc_seed, "buildlabel": buildlabel}[cmd](*args)
'''

// ---------- Pipeline ----------

pipeline {
    agent { label 'built-in' }

    parameters {
        choice(name: 'ACCOUNT', choices: ['automation', 'tpt', 'tpt1'], description: 'Which Site24x7 account to test.')
        choice(name: 'GRID', choices: ['integrations-qa', 'eum-dev', 's247integrations', 'pmclient'], description: 'Which grid. Nothing is deployed: the build already on the grid is tested.')
        string(name: 'INTEGRATIONS', defaultValue: '', description: 'Which integrations, by name, comma separated. * is a wildcard. Empty = all. Example: Slack Sanity, ServiceNow*')
        choice(name: 'ALERTS', choices: ['both', 'consolidated', 'events'], description: 'both = consolidated + event-based. consolidated = monitor-level alerts only. events = every-event-occurrence alerts only.')
        string(name: 'CONSOLIDATED_ROUTE', defaultValue: '', description: 'Consolidated states, one route per monitor, separated by ; - states: down, trouble. UP is added at the end. Empty = default (trouble,down;down,trouble).')
        string(name: 'EVENT_ROUTES', defaultValue: '', description: 'Event-based states, one route per event monitor, separated by ; - states: down, trouble, critical. UP is added at the end. Empty = default (down,trouble,critical;down,critical,trouble). One route = only the first event monitor runs.')
        booleanParam(name: 'ADD_INTEGRATIONS', defaultValue: true, description: 'Delete and ADD the chosen integrations first (DOWN/TROUBLE/CRITICAL, All Monitors), then test them. Untick to test the integrations as they are.')
        string(name: 'TEST_NOTES', defaultValue: '', description: 'Why this run (shown at the top of the report), e.g. "FTM 321863000042475002 - Slack after fix"')
    }

    environment {
        SUITE_REPO          = 'https://github.com/muhamadh-mufeedh-19738/site24x7-itsm-e2e-suite.git'
        SUITE_BRANCH        = 'main'
    }

    options {
        timeout(time: 4, unit: 'HOURS')
    }

    stages {
        stage('1. Check inputs') {
            steps {
                script {
                    if (params.ALERTS == null || params.ADD_INTEGRATIONS == null) {
                        error('The boxes are not loaded yet (happens once after the job is created or updated). Click "Build with Parameters" again.')
                    }
                    def g = GRIDS[params.GRID ?: '']
                    if (g == null) {
                        error("GRID '${params.GRID}' is not known. Known grids: ${GRIDS.keySet().join(', ')}.")
                    }
                    env.GRID_URL = g.url
                    echo "CUSTOM TEST on ${params.GRID} (${g.url}), account ${params.ACCOUNT}"
                    echo "  integrations : ${params.INTEGRATIONS ?: 'all'}"
                    echo "  alerts       : ${params.ALERTS}"
                    echo "  consolidated : ${params.CONSOLIDATED_ROUTE ?: 'default'}"
                    echo "  event-based  : ${params.EVENT_ROUTES ?: 'default'}"
                    echo "  add first    : ${params.ADD_INTEGRATIONS}"
                    echo "  notes        : ${params.TEST_NOTES ?: '-'}"
                    currentBuild.displayName = "#${env.BUILD_NUMBER} ${params.ACCOUNT} · ${params.ALERTS}" + (params.INTEGRATIONS ? " · ${params.INTEGRATIONS}" : '')
                }
            }
        }

        stage('2. Run custom test (on Jenkins server)') {
            tools { nodejs 'NodeJS-22' }
            // Same lock as the sanity job: never two runs on one account at once.
            options {
                lock(resource: "account-${params.ACCOUNT}")
            }
            environment {
                PLAYWRIGHT_BROWSERS_PATH = '/var/lib/jenkins/pw-browsers'
                S247_BROWSER_CHANNEL     = 'chromium'
                ITOC_STATE_DIR           = '/var/lib/jenkins/.s247-itoc360'
                S247_HISTORY_DIR         = '/var/lib/jenkins/.s247-history'
            }
                    steps {
                        script {
                            // 0) Python is needed for everything below - fail loudly if Jenkins cannot use it
                            sh '''
                                set +x
                                if ! python3 -c "print('python3 OK')"; then
                                    echo "ERROR: Jenkins cannot run python3 on this server"
                                    exit 2
                                fi
                            '''
                            writeFile file: 'suite_tools.py', text: SUITE_TOOLS_PY

                            // 1) Latest suite code from GitHub (your phases 2-4 arrive here when you push)
                            String ghRepo = env.SUITE_REPO.replaceFirst('^https://github.com/', '').replaceFirst('[.]git$', '')
                            withEnv(["GH_REPO=${ghRepo}"]) {
                                withCredentials([usernamePassword(credentialsId: 'GITHUB_SUITE', usernameVariable: 'GH_USER', passwordVariable: 'GH_TOKEN')]) {
                                    sh '''
                                        set +x
                                        echo "--- Downloading suite code: $GH_REPO (branch $SUITE_BRANCH) ---"
                                        code=$(curl -sL -w "%{http_code}" -o "$WORKSPACE/suite.tar.gz" -H "Authorization: Bearer $GH_TOKEN" -H "Accept: application/vnd.github+json" "https://api.github.com/repos/$GH_REPO/tarball/$SUITE_BRANCH")
                                        if [ "$code" != "200" ]; then
                                            echo "ERROR: GitHub download failed (HTTP $code). 401/404 = GITHUB_SUITE token is wrong, expired, or has no access to this repo."
                                            exit 1
                                        fi
                                    '''
                                }
                            }
                            sh 'set +x; python3 "$WORKSPACE/suite_tools.py" extract "$WORKSPACE/suite.tar.gz" "$WORKSPACE/suite"'

                            String A = params.ACCOUNT.toUpperCase()
                            def creds = [
                                file(credentialsId: "S247_${A}_ACCOUNT_ENV", variable: 'ACC_ENV'),
                                file(credentialsId: "S247_${A}_ITSM_ENV",    variable: 'ITSM_ENV'),
                                file(credentialsId: "S247_${A}_TOKEN_ENV",   variable: 'TOKEN_ENV')
                            ]

                            // OPTIONAL: iTOC360 session for Jenkins (only accounts with iTOC360).
                            // Missing credential = iTOC360 shows BLOCKED; nothing else changes.
                            try {
                                withCredentials([file(credentialsId: "S247_${A}_ITOC_STATE", variable: 'ITOC_SEED')]) {
                                    sh 'set +x; python3 "$WORKSPACE/suite_tools.py" itoc_seed "$ACCOUNT" "$ITOC_SEED"'
                                }
                            } catch (ignored) {
                                echo "No S247_${A}_ITOC_STATE credential - iTOC360 (if this account has it) will show BLOCKED."
                            }

                            // Custom run: still --full (one summary report); WHAT is tested comes
                            // from the S247_TEST_* settings below (see CUSTOM_TESTING.md).
                            String fullFlag = '--full'
                            // Which build is on the grid, for "vs previous build" in the report
                            String buildLabel = ''
                            withEnv(["BL_DEPLOY=false", "BL_URL="]) {
                                buildLabel = sh(returnStdout: true, script: 'set +x; python3 "$WORKSPACE/suite_tools.py" buildlabel "$GRID" "$BL_DEPLOY" "$BL_URL"').trim()
                            }
                            echo "Build on the grid: ${buildLabel ?: 'unknown'}"
                            int rc = 2
                            try {
                                withEnv(["FULL_FLAG=${fullFlag}", "S247_BUILD_LABEL=${buildLabel}",
                                         "S247_TEST_INTEGRATIONS=${params.INTEGRATIONS ?: ''}",
                                         "S247_TEST_ALERTS=${params.ALERTS ?: 'both'}",
                                         "S247_TEST_CONS_ROUTES=${params.CONSOLIDATED_ROUTE ?: ''}",
                                         "S247_TEST_EVENT_ROUTES=${params.EVENT_ROUTES ?: ''}",
                                         "S247_TEST_ADD_INTEGRATIONS=${params.ADD_INTEGRATIONS ? 'yes' : 'no'}",
                                         "S247_TEST_NOTES=${params.TEST_NOTES ?: ''}"]) {
                                withCredentials(creds) {
                                    // 2) Secrets + browser into place, 3) run the suite
                                    rc = sh(returnStatus: true, script: '''
                                        set +x
                                        cd "$WORKSPACE/suite" || exit 2
                                        python3 "$WORKSPACE/suite_tools.py" place "$ACCOUNT" "$ACC_ENV" "$ITSM_ENV" "$TOKEN_ENV" || exit 2

                                        echo "--- Installing Playwright + Chromium (cached after first run) ---"
                                        npm install --no-audit --no-fund --no-save playwright > /dev/null || exit 2
                                        npx playwright install chromium || exit 2

                                        python3 "$WORKSPACE/suite_tools.py" start "$WORKSPACE" || exit 2
                                        echo "--- Running: python3 itsm.py --grid $GRID --account $ACCOUNT $FULL_FLAG ---"
                                        python3 itsm.py --grid "$GRID" --account "$ACCOUNT" $FULL_FLAG < /dev/null
                                    ''')
                                }
                                }
                            } catch (e) {
                                if (!String.valueOf(e.message).toLowerCase().contains('credentials')) { throw e }
                                error("COULD NOT LOAD SECRETS for account '${params.ACCOUNT}': ${e.message}. Add the 3 credentials S247_${A}_ACCOUNT_ENV / S247_${A}_ITSM_ENV / S247_${A}_TOKEN_ENV in Jenkins Credentials.")
                            } finally {
                                // Never leave secrets lying in the workspace
                                sh 'set +x; python3 "$WORKSPACE/suite_tools.py" cleanup "$WORKSPACE/suite" || echo "WARNING: could not clean secrets from workspace"'
                            }
                            echo "Suite finished with exit code ${rc}  (0 = OK, 1 = defect found, 2 = blocked)"

                            // If the login failed, keep the screenshot the suite took so we can SEE why
                            sh 'set +x; python3 "$WORKSPACE/suite_tools.py" shot "$WORKSPACE" || true'
                            archiveArtifacts artifacts: 'suite-report/login_failure.png', allowEmptyArchive: true

                            // 4) Pick up THIS run's report files
                            int found = sh(returnStatus: true, script: 'set +x; python3 "$WORKSPACE/suite_tools.py" report "$WORKSPACE"')

                            if (found == 0 && fileExists('suite-report/diff_line.txt')) {
                                // "2 new problem(s), 1 fixed, ..." right on the build page
                                currentBuild.description = readFile('suite-report/diff_line.txt').trim()
                            }
                            if (found == 0) {
                                publishHTML(target: [
                                    allowMissing         : false,
                                    alwaysLinkToLastBuild: true,
                                    keepAll              : true,
                                    reportDir            : 'suite-report',
                                    reportFiles          : 'report.html',
                                    reportName           : 'Custom Test Report'
                                ])
                                archiveArtifacts artifacts: 'suite-report/**', allowEmptyArchive: false
                                if (fileExists('suite-report/junit.xml')) {
                                    junit testResults: 'suite-report/junit*.xml', allowEmptyResults: true, skipMarkingBuildUnstable: true
                                }
                            }

                            if (rc == 0 && found == 0) {
                                echo "SUITE PASSED for account '${params.ACCOUNT}'. Open 'Custom Test Report' on the left."
                            } else if (rc == 1 && found == 0) {
                                unstable("SUITE FOUND DEFECTS for account '${params.ACCOUNT}'. Open 'Custom Test Report' on the left for details.")
                            } else if (found != 0) {
                                error("SUITE PRODUCED NO REPORT (exit code ${rc}). Read the suite output above for the reason.")
                            } else {
                                error("SUITE BLOCKED (exit code ${rc}) for account '${params.ACCOUNT}': it could not test - usually login, cookie or credentials. Read 'NOT A PRODUCT BUG' in the output above.")
                            }
                        }
                    }
        }
    }

    post {
        failure {
            echo 'CUSTOM TEST FAILED: read the red stage above for the exact reason.'
        }
    }
}
