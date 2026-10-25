// =====================================================================
// S247 Integrations QA - SD build deployment pipeline
// Flow: check inputs -> fresh SD token -> deploy build -> wait until done
// (Python test stage will be added later)
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

// Returns all SD 'build_update' entries for this exact build URL on one SD product.
def findMyBuilds(String buildUrl, Map sd) {
    String q = "build_url=" + java.net.URLEncoder.encode(buildUrl, 'UTF-8') +
               "&region=${sd.region}&sort_by=build_scheduled_at&sort_order=desc&limit=10"
    def raw = sdRequest('GET', "${env.SD_BASE}/external/v1/status/services/${env.SD_SERVICE}/products/${sd.product}/build_update?${q}", '')
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

// The exact request the SD "Build Update" screen sends (copied from its
// Network > Payload). Same shape for WebMon and Plus; only sd.* and url differ.
def sdBody(Map t) {
    return [
        service_name        : env.SD_SERVICE,
        product_name        : t.sd.product,
        data_center         : t.sd.dc,
        region              : t.sd.region,
        deployment_mode     : 'live',
        build_stage         : t.sd.stage,
        build_url           : t.url,
        alternate_url       : true,
        is_patch_update     : false,
        is_grid_edited      : false,
        sub_grids           : [t.sd.grid],
        tar_names           : [t.sd.tar],
        parallel_products   : [],
        regions_to_be_synced: [],
        ro_sync_disabled    : 0,
        scheduler_details   : [time_to_sync: '0', ro_time_to_sync: env.SD_RO_SYNC_MIN],
        build_options       : [
            start_server                       : true,      // "Start Server" (required)
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
}

// How many deploy targets are not finished yet (plain loop: CPS-safe).
int pendingDeploys() {
    int n = 0
    for (t in DEPLOY_TARGETS) { if (!t.done) { n++ } }
    return n
}

// POSTs t.bodyFile to SD for t.sd.product. Fails loudly if SD does not accept it.
def submitDeploy(Map t, String what) {
    def raw = sdRequest('POST', "${env.SD_BASE}/buildprovision/v1/services/${env.SD_SERVICE}/products/${t.sd.product}/action/build_update", t.bodyFile)
    def r = parseResponse(raw, "${what} [${t.kind}]")
    if (r.json.code != 'SUCCESS') {
        error("[${t.kind}] SD REJECTED THE DEPLOY to ${t.sd.grid} (HTTP ${r.status}): ${r.json.message}. Nothing more was deployed.")
    }
    echo "[${t.kind}] SD says: ${r.json.message}"
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
        booleanParam(name: 'DEPLOY_PLUS', defaultValue: false, description: 'Want to check PLUS monitors as well (server / network / poller / cloud)? Tick to ALSO deploy a Plus server build to the Plus grid below.')
        choice(name: 'PLUS_GRID', choices: ['integrations-qa-plus'], description: 'Plus grid that receives the Plus server build (only used when DEPLOY_PLUS is ticked).')
        string(name: 'PLUS_BUILD_URL', defaultValue: '', description: 'Plus server build URL - must end with Pluss247Server.zip, e.g. https://build.zohocorp.com/zoho/pluss247server/webhost/master/<DATE>/Pluss247Server.zip (only used when DEPLOY_PLUS is ticked).')
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
                    if (params.ACCOUNT == null || params.RUN_SUITE == null || params.DEPLOY_PLUS == null) {
                        error('The new boxes are not loaded yet (this happens once after updating the script). Click "Build with Parameters" again - you will now see them. Nothing was deployed.')
                    }

                    // ---- which grid? (always first) ----
                    def g = GRIDS[params.GRID ?: '']
                    if (g == null) {
                        error("GRID '${params.GRID}' is not known. Known grids: ${GRIDS.keySet().join(', ')}.")
                    }
                    env.GRID_URL = g.url
                    echo "Grid      : ${params.GRID}  (${g.url})"
                    echo "Account   : ${params.ACCOUNT} (suite will " + (params.RUN_SUITE ? 'RUN' : 'NOT run') + ")"

                    DEPLOY_TARGETS = []
                    String webUrl  = params.SD_BUILD_URL?.trim()
                    String plusUrl = params.PLUS_BUILD_URL?.trim()

                    // ---- 1) WebMon build -> the chosen GRID ----
                    if (params.DEPLOY != false) {
                        if (g.sd == null) {
                            error("DEPLOY is ticked, but the SD settings for grid '${params.GRID}' are not in GRIDS yet. Nothing was deployed.")
                        }
                        if (!webUrl) {
                            error('SD_BUILD_URL is empty. Paste the WebMon build URL (…/AdventNetWebMon.zip), or untick DEPLOY.')
                        }
                        if (!webUrl.startsWith('https://') || !webUrl.endsWith('.zip')) {
                            error("SD_BUILD_URL does not look like a build link: ${webUrl} (it must start with https:// and end with .zip)")
                        }
                        if (webUrl.endsWith(PLUS_BUILD_SUFFIX)) {
                            error("SD_BUILD_URL is a PLUS build (${PLUS_BUILD_SUFFIX}). Put it in PLUS_BUILD_URL and tick DEPLOY_PLUS; SD_BUILD_URL is for the WebMon build. Nothing was deployed.")
                        }
                        DEPLOY_TARGETS << [kind: 'WebMon', url: webUrl, sd: g.sd, gridName: params.GRID,
                                           bodyFile: 'sd_build_update.json']
                    } else {
                        echo "DEPLOY is OFF: no WebMon build will be deployed to ${params.GRID}."
                    }

                    // ---- 2) Plus server build -> the Plus grid (optional) ----
                    if (params.DEPLOY_PLUS == true) {
                        def pg = PLUS_GRIDS[params.PLUS_GRID ?: '']
                        if (pg == null) {
                            error("PLUS_GRID '${params.PLUS_GRID}' is not known. Known Plus grids: ${PLUS_GRIDS.keySet().join(', ')}.")
                        }
                        if (!plusUrl) {
                            error("DEPLOY_PLUS is ticked but PLUS_BUILD_URL is empty. Paste the Plus build URL (it must end with ${PLUS_BUILD_SUFFIX}), or untick DEPLOY_PLUS. Nothing was deployed.")
                        }
                        if (!plusUrl.startsWith('https://') || !plusUrl.endsWith(PLUS_BUILD_SUFFIX)) {
                            error("PLUS_BUILD_URL must start with https:// and end with '${PLUS_BUILD_SUFFIX}'. Got: ${plusUrl}. Nothing was deployed.")
                        }
                        DEPLOY_TARGETS << [kind: 'Plus', url: plusUrl, sd: pg.sd, gridName: params.PLUS_GRID,
                                           bodyFile: 'sd_build_update_plus.json']
                    } else if (plusUrl) {
                        echo "NOTE: PLUS_BUILD_URL is filled but DEPLOY_PLUS is not ticked - the Plus build will NOT be deployed."
                    }

                    if (DEPLOY_TARGETS) {
                        if (!params.BUILD_COMMENT?.trim()) {
                            error('BUILD_COMMENT is empty. Add a short comment so the deploy is recognisable in SD.')
                        }
                        if (!params.NOTIFY_EMAIL?.contains('@')) {
                            error("NOTIFY_EMAIL is not a valid email: ${params.NOTIFY_EMAIL}")
                        }
                        echo "Comment   : ${params.BUILD_COMMENT}"
                        for (t in DEPLOY_TARGETS) {
                            echo "Deploy ${t.kind}: ${t.url}"
                            echo "   target : ${t.sd.product} / ${t.sd.region} / ${t.sd.dc} / ${t.sd.stage} / grid ${t.sd.grid}"
                        }
                        echo "Diff report: " + (skipDiffReport() ? 'BYPASSED (Skip Continue on)' : 'NOT bypassed - SD will pause for a manual resume')
                    } else {
                        echo "Nothing to deploy. Only the E2E suite runs, on whatever is on the grid(s) now."
                    }
                }
            }
        }

        // Everything that touches a GRID runs inside locks, so two builds can never
        // deploy/test the SAME grid (or the same Plus grid) at the same time.
        // Builds for DIFFERENT grids run in parallel. (Needs 'Lockable Resources'.)
        stage('Grid run') {
            options {
                lock(resource: "grid-${params.GRID}",
                     extra: [[resource: "plusgrid-${params.DEPLOY_PLUS ? params.PLUS_GRID : 'unused-' + env.BUILD_TAG}"]])
            }
            stages {
                stage('2. Get fresh SD access token') {
                    when { expression { DEPLOY_TARGETS } }
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

                stage('3. Deploy build(s) in SD') {
                    when { expression { DEPLOY_TARGETS } }
                    steps {
                        script {
                            // WebMon and Plus are submitted one after the other, then
                            // stage 4 waits for BOTH in parallel (saves ~10 min).
                            for (t in DEPLOY_TARGETS) {
                                // newest existing SD entry for this URL, so stage 4 only
                                // tracks the NEW deploy we start now
                                t.baseline = 0L
                                for (e in findMyBuilds(t.url, t.sd)) {
                                    long id = e.build_id as Long
                                    if (id > t.baseline) { t.baseline = id }
                                }
                                echo "[${t.kind}] newest existing SD entry for this URL: build_id ${t.baseline} (0 = never deployed before)"
                                writeJSON file: t.bodyFile, json: sdBody(t), pretty: 2
                                echo "[${t.kind}] request sent to SD:\n" + readFile(t.bodyFile)
                                submitDeploy(t, 'Build update')
                            }
                        }
                    }
                }

                stage('4. Wait for deploy(s) to finish') {
                    when { expression { DEPLOY_TARGETS } }
                    steps {
                        script {
                            int maxChecks = Integer.parseInt(env.SD_MAX_WAIT_MINUTES)
                            int maxRetries = Integer.parseInt(env.SD_MAX_RETRIES)
                            def failStates = ['failed', 'killed', 'reverted', 'skipped']
                            for (t in DEPLOY_TARGETS) { t.done = false; t.retries = 0; t.checks = 0 }

                            while (pendingDeploys() > 0) {
                                for (t in DEPLOY_TARGETS) {
                                    if (t.done) { continue }
                                    t.checks++
                                    if (t.checks > maxChecks) {
                                        error("[${t.kind}] TIMED OUT: deploy to ${t.sd.grid} not Completed after ${maxChecks} minutes. Open it in SD: if it says 'Diff Report Generated', it is waiting for a manual resume. Otherwise it may still be running - check SD before re-running.")
                                    }
                                    def mine = null
                                    for (e in findMyBuilds(t.url, t.sd)) {
                                        long id = e.build_id as Long
                                        if (id > t.baseline && (mine == null || id > (mine.build_id as Long))) { mine = e }
                                    }
                                    if (mine == null) {
                                        echo "[${t.kind}] check ${t.checks}/${maxChecks}: SD has not listed the new deploy yet"
                                        continue
                                    }
                                    String st = String.valueOf(mine.overall_status)
                                    echo "[${t.kind}] check ${t.checks}/${maxChecks}: build_id ${mine.build_id} | overall=${st} | sd=${mine.status?.sd_status} | zac=${mine.status?.zac_status}"

                                    if (st.toLowerCase() in failStates) {
                                        String zac = String.valueOf(mine.status?.zac_status ?: '').trim()
                                        boolean neverReachedGrid = (zac == '' || zac == 'null' || zac == '-')
                                        if (neverReachedGrid && t.retries < maxRetries) {
                                            // SD failed while PREPARING (grid not touched): submit the same
                                            // request again, like re-deploying it by hand from the SD screen.
                                            t.retries++
                                            echo "!!! [${t.kind}] SD FAILED WHILE PREPARING build_id ${mine.build_id} (grid not touched). Re-submitting - attempt ${t.retries + 1} of ${maxRetries + 1} ..."
                                            t.baseline = mine.build_id as Long
                                            t.checks = 0
                                            submitDeploy(t, 'Build update (retry)')
                                            continue
                                        }
                                        error("[${t.kind}] SD DEPLOY FAILED: build_id ${mine.build_id} on ${t.sd.grid} ended with '${st}'" + (t.retries > 0 ? " (after ${t.retries} automatic re-submit(s))" : '') + ". Open SD, click the build and read the red 'Reason'.")
                                    }

                                    if (st == 'Completed') {
                                        boolean gridOk = false
                                        def groups = mine.initiated_groups
                                        if (groups instanceof String) {
                                            gridOk = groups.toLowerCase().contains(t.sd.grid.toLowerCase())
                                        } else {
                                            for (gr in groups) {
                                                if (String.valueOf(gr.group_name).equalsIgnoreCase(t.sd.grid) && String.valueOf(gr.status) == 'Completed') { gridOk = true }
                                            }
                                        }
                                        if (!gridOk) {
                                            error("[${t.kind}] SD says Completed, but grid '${t.sd.grid}' is not marked Completed. Check SD before testing.")
                                        }
                                        echo "[${t.kind}] DEPLOY COMPLETE on ${t.sd.grid} (build_id ${mine.build_id})"
                                        t.done = true
                                    }
                                }
                                if (pendingDeploys() > 0) {
                                    sleep(time: 60, unit: 'SECONDS')
                                }
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
            archiveArtifacts artifacts: 'sd_build_update*.json', allowEmptyArchive: true
        }
        success {
            script {
                for (t in DEPLOY_TARGETS) { echo "DEPLOYED [${t.kind}]: ${t.url} is live on ${t.sd.grid}" }
                echo "PIPELINE PASSED" + (params.RUN_SUITE ? ": the E2E suite passed for '${params.ACCOUNT}' on ${params.GRID}" : '')
            }
        }
        unstable {
            echo "PIPELINE UNSTABLE: deploy OK, but the E2E suite found defects. See 'Integrations E2E Report'."
        }
        failure {
            echo 'PIPELINE FAILED: read the red stage above for the exact reason.'
        }
    }
}
