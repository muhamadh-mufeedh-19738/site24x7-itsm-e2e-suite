// =====================================================================
// S247 Integrations QA - SD build deployment pipeline
// Flow: check inputs -> fresh SD token -> deploy build -> wait until done
// (Python test stage will be added later)
// =====================================================================

// ---------- Helper functions (no need to edit) ----------

import groovy.transform.Field

@Field String SD_ACCESS_TOKEN = ''
@Field Long BASELINE_BUILD_ID = 0L

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

// Small Python helper used by stage 5. This server blocks some Linux commands
// for Jenkins (rm is "Permission denied", git is missing), so every file job
// (unpack code, place secrets, clean secrets, pick report) is done in Python.
@Field String SUITE_TOOLS_PY = '''
import sys, os, shutil, tarfile, glob, time

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
    if not h:
        fail("the suite did not write an HTML report during this run", 1)
    out = os.path.join(ws, "suite-report")
    shutil.copyfile(h, os.path.join(out, "report.html"))
    j = newest("report_*.json")
    x = newest("junit_*.xml")
    if j:
        shutil.copyfile(j, os.path.join(out, "report.json"))
    if x:
        shutil.copyfile(x, os.path.join(out, "junit.xml"))
    print("Report picked up: " + h)

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

cmd, args = sys.argv[1], sys.argv[2:]
{"extract": extract, "place": place, "start": start, "cleanup": cleanup, "report": report, "shot": shot}[cmd](*args)
'''

// Calls the SD API with the current access token. Token is never printed.
def sdRequest(String method, String url, String bodyFile) {
    def out = ''
    withEnv(["SD_URL=${url}", "SD_METHOD=${method}", "SD_BODY_FILE=${bodyFile}", "SD_ACCESS_TOKEN=${SD_ACCESS_TOKEN}"]) {
        out = sh(returnStdout: true, script: '''
            set +x
            if [ -n "$SD_BODY_FILE" ]; then
                curl -s --max-time 60 -X "$SD_METHOD" "$SD_URL" -H "Authorization: $SD_ACCESS_TOKEN" -H "Email: $SD_USER_EMAIL" -H "Content-Type: application/json" --data-binary "@$SD_BODY_FILE" -w "\\nHTTP_STATUS:%{http_code}"
            else
                curl -s --max-time 60 -X "$SD_METHOD" "$SD_URL" -H "Authorization: $SD_ACCESS_TOKEN" -H "Email: $SD_USER_EMAIL" -w "\\nHTTP_STATUS:%{http_code}"
            fi
        ''').trim()
    }
    return out
}

// Splits SD's reply into HTTP status + JSON. Fails loudly if it isn't JSON.
def parseResponse(String raw, String what) {
    int idx = raw.lastIndexOf('HTTP_STATUS:')
    if (idx < 0) {
        error("${what}: no reply from SD.")
    }
    String status = raw.substring(idx + 12).trim()
    String body = raw.substring(0, idx).trim()
    def json
    try {
        json = readJSON(text: body)
    } catch (e) {
        String preview = body.length() > 300 ? body.substring(0, 300) : body
        error("${what}: SD did not return JSON (HTTP ${status}). Reply starts with: ${preview}")
    }
    return [status: status, json: json]
}

// True = bypass SD's diff report ("Skip Continue"). Defaults to true if the
// parameter is missing (e.g. the first run after this option was added).
def skipDiffReport() {
    return (params.SKIP_DIFF_REPORT == null) ? true : (params.SKIP_DIFF_REPORT as Boolean)
}

// Returns all SD 'build_update' entries for this exact build URL.
def findMyBuilds(String buildUrl) {
    String q = "build_url=" + java.net.URLEncoder.encode(buildUrl, 'UTF-8') +
               "&region=${env.SD_REGION}&sort_by=build_scheduled_at&sort_order=desc&limit=10"
    def raw = sdRequest('GET', "${env.SD_BASE}/external/v1/status/services/${env.SD_SERVICE}/products/${env.SD_PRODUCT}/build_update?${q}", '')
    def r = parseResponse(raw, 'Build status check')
    def result = []
    if (r.json.code != 'SUCCESS') {
        echo "SD status API says: ${r.json.message}"
        return result
    }
    for (block in r.json.details) {
        def bd = block.details?.build_details
        if (bd == null) { continue }
        def entries = (bd instanceof List) ? bd : bd.values()
        for (e in entries) {
            if (e.process_type == 'build_update' && e.url == buildUrl) {
                result << e
            }
        }
    }
    return result
}

// ---------- Pipeline ----------

pipeline {
    // Everything, including the E2E suite, runs on the Jenkins server.
    agent { label 'built-in' }

    parameters {
        choice(name: 'GRID', choices: ['integrations-qa', 'eum-dev', 's247integrations', 'pmclient'], description: 'Which grid to deploy the build to AND run the E2E suite on. Every account can run on every grid.')
        choice(name: 'ACCOUNT', choices: ['automation', 'tpt', 'tpt1'], description: 'Which Site24x7 account the E2E suite runs against (folder name under accounts/)')
        booleanParam(name: 'DEPLOY', defaultValue: true, description: 'Deploy the build in SD first. Untick to ONLY run the E2E suite on what is already on the grid (SD_BUILD_URL can then be left empty).')
        booleanParam(name: 'RUN_SUITE', defaultValue: true, description: 'Run the E2E suite after the deploy completes. Untick to only deploy.')
        string(name: 'SD_BUILD_URL', defaultValue: '', description: 'Full build URL to deploy, e.g. https://build.zohocorp.com/zoho/webmonitor/webhost/<BRANCH>/<DATE>/AdventNetWebMon.zip')
        string(name: 'BUILD_COMMENT', defaultValue: '', description: 'Comment shown in SD for this deploy (feature / fix name)')
        string(name: 'NOTIFY_EMAIL', defaultValue: 'muhamadh.mufeedh@zohocorp.com', description: 'Who SD emails about this deploy')
        booleanParam(name: 'SKIP_DIFF_REPORT', defaultValue: true, description: 'Bypass the SD diff report (same as "Skip Continue" in the SD portal). Keep ticked so the deploy runs to the end without waiting for a manual resume.')
    }

    environment {
        // ===== SD settings (found with the SD lookup APIs) =====
        SD_BASE             = 'https://sd.csez.zohocorpin.com'
        SD_USER_EMAIL       = 'muhamadh.mufeedh@zohocorp.com'
        SD_SERVICE          = 'Site24x7'
        SD_MAX_WAIT_MINUTES = '45'
        SD_RO_SYNC_MIN      = '20'     // 'Sync Time' shown on the SD screen
        SD_MAX_RETRIES      = '2'      // re-submit automatically if SD fails while preparing (before touching the grid)

        // ===== E2E suite (pulled from GitHub on every run) =====
        SUITE_REPO          = 'https://github.com/muhamadh-mufeedh-19738/site24x7-itsm-e2e-suite.git'
        SUITE_BRANCH        = 'main'
        GRID_MAX_WAIT_MIN   = '30'     // after deploy, how long to wait for the grid to come back up
    }

    options {
        timeout(time: 6, unit: 'HOURS')
        // Parallel builds ARE allowed: one per grid (see 'Grid run' lock) and
        // one per account (see the lock on stage 5).
    }

    stages {

        stage('1. Check inputs') {
            steps {
                script {
                    // ---- which grid? (always first) ----
                    def g = GRIDS[params.GRID ?: '']
                    if (g == null) {
                        error("GRID '${params.GRID}' is not known. Known grids: ${GRIDS.keySet().join(', ')}. (First run after a script update? Click Build with Parameters again.)")
                    }
                    env.GRID_URL = g.url
                    echo "Grid      : ${params.GRID}  (${g.url})"
                    if (g.sd != null) {
                        env.SD_PRODUCT    = g.sd.product
                        env.SD_REGION     = g.sd.region
                        env.SD_DATACENTER = g.sd.dc
                        env.SD_STAGE      = g.sd.stage
                        env.SD_GRID       = g.sd.grid
                        env.SD_TAR        = g.sd.tar
                    } else if (params.DEPLOY != false) {
                        error("DEPLOY is ticked, but the SD settings for grid '${params.GRID}' are not added to this script yet. Nothing was deployed. Either untick DEPLOY (run the suite only), or add this grid's SD values to GRIDS at the top of the script.")
                    }

                    String url = params.SD_BUILD_URL?.trim()
                    if (params.DEPLOY == false) {
                        echo "DEPLOY is OFF: no build will be deployed. Only the E2E suite runs, on whatever is on the grid now."
                        echo "Account   : ${params.ACCOUNT}"
                        return
                    }
                    if (!url) {
                        error('SD_BUILD_URL is empty. Click "Build with Parameters" and paste the build URL.')
                    }
                    if (!url.startsWith('https://') || !url.endsWith('.zip')) {
                        error("SD_BUILD_URL does not look like a build link: ${url} (it must start with https:// and end with .zip)")
                    }
                    if (!params.BUILD_COMMENT?.trim()) {
                        error('BUILD_COMMENT is empty. Add a short comment so the deploy is recognisable in SD.')
                    }
                    if (!params.NOTIFY_EMAIL?.contains('@')) {
                        error("NOTIFY_EMAIL is not a valid email: ${params.NOTIFY_EMAIL}")
                    }
                    if (params.ACCOUNT == null || params.RUN_SUITE == null) {
                        error('The new boxes (ACCOUNT / RUN_SUITE) are not loaded yet. This happens once after updating the script. Click "Build with Parameters" again - you will now see them. Nothing was deployed.')
                    }
                    echo "Build URL : ${url}"
                    echo "Account   : ${params.ACCOUNT} (suite will " + (params.RUN_SUITE ? 'RUN' : 'NOT run') + " after deploy)"
                    echo "Comment   : ${params.BUILD_COMMENT}"
                    echo "Target    : ${env.SD_PRODUCT} / ${env.SD_REGION} / ${env.SD_DATACENTER} / ${env.SD_STAGE} / grid ${env.SD_GRID}"
                    echo "Diff report: " + (skipDiffReport() ? 'BYPASSED (Skip Continue on)' : 'NOT bypassed - SD will pause for a manual resume')
                }
            }
        }

        // Everything that touches the GRID runs inside one lock per grid, so two
        // builds can never deploy/test the SAME grid at the same time. Builds for
        // DIFFERENT grids run in parallel. (Needs the 'Lockable Resources' plugin.)
        stage('Grid run') {
            options { lock(resource: "grid-${params.GRID}") }
            stages {
                stage('2. Get fresh SD access token') {
                    when { expression { params.DEPLOY != false } }
                    steps {
                        script {
                            withCredentials([string(credentialsId: 'SD_REFRESH_TOKEN', variable: 'SD_REFRESH')]) {
                                def raw = sh(returnStdout: true, script: '''
                                    set +x
                                    curl -s --max-time 60 -X POST "$SD_BASE/tokens/v1/regenerate" -H "Authorization: $SD_REFRESH" -d "grant_type=refresh_token" -w "\\nHTTP_STATUS:%{http_code}"
                                ''').trim()
                                def r = parseResponse(raw, 'Token refresh')
                                if (r.json.code != 'SUCCESS' || !r.json.data?.token) {
                                    error("TOKEN REFRESH FAILED (HTTP ${r.status}): ${r.json.message}. Check the SD_REFRESH_TOKEN credential.")
                                }
                                SD_ACCESS_TOKEN = r.json.data.token
                                echo "Fresh SD access token received (hidden). Expires at: ${r.json.data.expires_at}"
                            }
                        }
                    }
                }

                stage('3. Deploy build in SD') {
                    when { expression { params.DEPLOY != false } }
                    steps {
                        script {
                            String buildUrl = params.SD_BUILD_URL.trim()

                            // Remember the newest existing SD entry for this URL,
                            // so stage 4 only tracks the NEW deploy we start now.
                            BASELINE_BUILD_ID = 0L
                            for (e in findMyBuilds(buildUrl)) {
                                long id = e.build_id as Long
                                if (id > BASELINE_BUILD_ID) { BASELINE_BUILD_ID = id }
                            }
                            echo "Newest existing SD entry for this URL: build_id ${BASELINE_BUILD_ID} (0 = never deployed before)"

                            // Same request the SD "Build Update" screen sends (copied from its Network > Payload)
                            def body = [
                                service_name        : env.SD_SERVICE,
                                product_name        : env.SD_PRODUCT,
                                data_center         : env.SD_DATACENTER,
                                region              : env.SD_REGION,
                                deployment_mode     : 'live',
                                build_stage         : env.SD_STAGE,
                                build_url           : buildUrl,
                                alternate_url       : true,
                                is_patch_update     : false,
                                is_grid_edited      : false,
                                sub_grids           : [env.SD_GRID],
                                tar_names           : [env.SD_TAR],
                                parallel_products   : [],
                                regions_to_be_synced: [],
                                ro_sync_disabled    : 0,
                                scheduler_details   : [time_to_sync: '0', ro_time_to_sync: env.SD_RO_SYNC_MIN],
                                build_options       : [
                                    start_server                       : true,      // "Start Server" - was missing before
                                    static_needed                      : true,      // "Static Needed"
                                    iast_jar_needed                    : true,      // "IAST Scan Needed"
                                    skip_continue                      : skipDiffReport(),  // "Skip Continue" (diff bypass)
                                    skip_continue_for_parallel_products: true,
                                    incremental_update                 : false,
                                    milestone_tag_needed               : false,
                                    fresh_upload                       : false
                                ],
                                notify_to           : [params.NOTIFY_EMAIL.trim()],
                                comment             : params.BUILD_COMMENT.trim(),
                                provision_type      : 'build_update'
                            ]
                            writeJSON file: 'sd_build_update.json', json: body, pretty: 2
                            echo "Request sent to SD:\n" + readFile('sd_build_update.json')

                            def raw = sdRequest('POST', "${env.SD_BASE}/buildprovision/v1/services/${env.SD_SERVICE}/products/${env.SD_PRODUCT}/action/build_update", 'sd_build_update.json')
                            def r = parseResponse(raw, 'Build update')
                            if (r.json.code != 'SUCCESS') {
                                error("SD REJECTED THE DEPLOY (HTTP ${r.status}): ${r.json.message}")
                            }
                            echo "SD says: ${r.json.message}"
                        }
                    }
                }

                stage('4. Wait for deploy to finish') {
                    when { expression { params.DEPLOY != false } }
                    steps {
                        script {
                            String buildUrl = params.SD_BUILD_URL.trim()
                            int maxChecks = Integer.parseInt(env.SD_MAX_WAIT_MINUTES)
                            def failStates = ['failed', 'killed', 'reverted', 'skipped']
                            boolean done = false
                            int retries = 0
                            int maxRetries = Integer.parseInt(env.SD_MAX_RETRIES)

                            for (int i = 1; i <= maxChecks && !done; i++) {
                                def mine = null
                                for (e in findMyBuilds(buildUrl)) {
                                    long id = e.build_id as Long
                                    if (id > BASELINE_BUILD_ID && (mine == null || id > (mine.build_id as Long))) {
                                        mine = e
                                    }
                                }

                                if (mine == null) {
                                    echo "Check ${i}/${maxChecks}: SD has not listed the new deploy yet. Waiting 1 minute..."
                                } else {
                                    String st = String.valueOf(mine.overall_status)
                                    echo "Check ${i}/${maxChecks}: build_id ${mine.build_id} | overall=${st} | sd=${mine.status?.sd_status} | zac=${mine.status?.zac_status}"

                                    if (st.toLowerCase() in failStates) {
                                        String zac = String.valueOf(mine.status?.zac_status ?: '').trim()
                                        boolean neverReachedGrid = (zac == '' || zac == 'null' || zac == '-')
                                        if (neverReachedGrid && retries < maxRetries) {
                                            // SD failed while PREPARING (before touching the grid).
                                            // The same build usually goes through when submitted again,
                                            // exactly like re-deploying it by hand from the SD screen.
                                            retries++
                                            echo "!!! SD FAILED WHILE PREPARING build_id ${mine.build_id} (grid not touched). Re-submitting the same request - attempt ${retries + 1} of ${maxRetries + 1} ..."
                                            BASELINE_BUILD_ID = mine.build_id as Long
                                            sleep(time: 60, unit: 'SECONDS')
                                            def raw2 = sdRequest('POST', "${env.SD_BASE}/buildprovision/v1/services/${env.SD_SERVICE}/products/${env.SD_PRODUCT}/action/build_update", 'sd_build_update.json')
                                            def r2 = parseResponse(raw2, 'Build update (retry)')
                                            if (r2.json.code != 'SUCCESS') {
                                                error("SD REJECTED THE RE-SUBMIT (HTTP ${r2.status}): ${r2.json.message}")
                                            }
                                            echo "SD says: ${r2.json.message}"
                                            i = 0          // restart the wait for the new attempt
                                            continue
                                        }
                                        error("SD DEPLOY FAILED: build_id ${mine.build_id} ended with '${st}'" + (retries > 0 ? " (after ${retries} automatic re-submit(s))" : '') + ". Open SD, click the build and read the red 'Reason'.")
                                    }

                                    if (st == 'Completed') {
                                        boolean gridOk = false
                                        def groups = mine.initiated_groups
                                        if (groups instanceof String) {
                                            gridOk = groups.toLowerCase().contains(env.SD_GRID.toLowerCase())
                                        } else {
                                            for (g in groups) {
                                                if (String.valueOf(g.group_name).equalsIgnoreCase(env.SD_GRID) && String.valueOf(g.status) == 'Completed') {
                                                    gridOk = true
                                                }
                                            }
                                        }
                                        if (!gridOk) {
                                            error("SD says Completed, but grid '${env.SD_GRID}' is not marked Completed. Check SD before testing.")
                                        }
                                        echo "DEPLOY COMPLETE on ${env.SD_GRID} (build_id ${mine.build_id})"
                                        done = true
                                    }
                                }

                                if (!done) {
                                    sleep(time: 60, unit: 'SECONDS')
                                }
                            }

                            if (!done) {
                                error("TIMED OUT: deploy not Completed after ${maxChecks} minutes. Open the build in SD: if it says 'Diff Report Generated', it is waiting for a manual resume. Otherwise it may still be running - check SD before re-running.")
                            }
                        }
                    }
                }

                stage('4b. Wait for grid to be UP') {
                    steps {
                        script {
                            // After SD says Completed, the grid restarts and shows
                            // "We'll be right back" for a while. Wait until it answers for real,
                            // and only trust it after 2 good checks in a row.
                            int maxChecks = Integer.parseInt(env.GRID_MAX_WAIT_MIN) * 2   // one check every 30 s
                            int good = 0
                            for (int i = 1; i <= maxChecks; i++) {
                                String r = sh(returnStdout: true, script: '''
                                    set +x
                                    code=$(curl -skL --max-time 30 -o grid_check.html -w "%{http_code}" "$GRID_URL/app/client" || echo 000)
                                    if grep -qiE "be right back|temporarily unavailable" grid_check.html 2>/dev/null; then
                                        echo "DOWN $code (maintenance page)"
                                    elif [ "$code" -ge 200 ] 2>/dev/null && [ "$code" -lt 400 ]; then
                                        echo "UP $code"
                                    else
                                        echo "DOWN $code"
                                    fi
                                ''').trim()
                                if (r.startsWith('UP')) { good++ } else { good = 0 }
                                echo "Grid check ${i}/${maxChecks}: ${r}"
                                if (good >= 2) {
                                    echo "GRID IS UP: ${env.GRID_URL}"
                                    return
                                }
                                sleep(time: 30, unit: 'SECONDS')
                            }
                            error("GRID STILL DOWN after ${env.GRID_MAX_WAIT_MIN} min: ${env.GRID_URL} keeps showing 'We'll be right back'. The deploy finished, but the grid did not come back. Check the grid in SD / with the grid team, then re-run.")
                        }
                    }
                }

                stage('5. Run E2E suite (on Jenkins server)') {
                    when { expression { params.RUN_SUITE == true } }
                    tools { nodejs 'NodeJS-22' }
                    // One account = one set of monitors/tickets: never two runs on it at once.
                    // A second build for the same account WAITS here (it says so in the log).
                    options {
                        timeout(time: 4, unit: 'HOURS')
                        lock(resource: "account-${params.ACCOUNT}")
                    }
                    environment {
                        PLAYWRIGHT_BROWSERS_PATH = '/var/lib/jenkins/pw-browsers'
                        S247_BROWSER_CHANNEL     = 'chromium'
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

                            int rc = 2
                            try {
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
                                        echo "--- Running: python3 itsm.py --grid $GRID --account $ACCOUNT ---"
                                        python3 itsm.py --grid "$GRID" --account "$ACCOUNT" < /dev/null
                                    ''')
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

                            if (found == 0) {
                                publishHTML(target: [
                                    allowMissing         : false,
                                    alwaysLinkToLastBuild: true,
                                    keepAll              : true,
                                    reportDir            : 'suite-report',
                                    reportFiles          : 'report.html',
                                    reportName           : 'Integrations E2E Report'
                                ])
                                archiveArtifacts artifacts: 'suite-report/**', allowEmptyArchive: false
                                if (fileExists('suite-report/junit.xml')) {
                                    junit testResults: 'suite-report/junit.xml', allowEmptyResults: true, skipMarkingBuildUnstable: true
                                }
                            }

                            if (rc == 0 && found == 0) {
                                echo "SUITE PASSED for account '${params.ACCOUNT}'. Open 'Integrations E2E Report' on the left."
                            } else if (rc == 1 && found == 0) {
                                unstable("SUITE FOUND DEFECTS for account '${params.ACCOUNT}'. Open 'Integrations E2E Report' on the left for details.")
                            } else if (found != 0) {
                                error("SUITE PRODUCED NO REPORT (exit code ${rc}). Read the suite output above for the reason.")
                            } else {
                                error("SUITE BLOCKED (exit code ${rc}) for account '${params.ACCOUNT}': it could not test - usually login, cookie or credentials. Read 'NOT A PRODUCT BUG' in the output above.")
                            }
                        }
                    }
                }
            }
        }
    }

    post {
        always {
            script { SD_ACCESS_TOKEN = '' }
            archiveArtifacts artifacts: 'sd_build_update.json', allowEmptyArchive: true
        }
        success {
            echo "PIPELINE PASSED: ${params.SD_BUILD_URL} is live on ${params.GRID}" + (params.RUN_SUITE ? " and the E2E suite passed for '${params.ACCOUNT}'" : '')
        }
        unstable {
            echo "PIPELINE UNSTABLE: deploy OK, but the E2E suite found defects. See 'Integrations E2E Report'."
        }
        failure {
            echo 'PIPELINE FAILED: read the red stage above for the exact reason.'
        }
    }
}
