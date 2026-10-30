#!/usr/bin/env python3
"""
jenkins_secrets.py - keep Jenkins' copy of the secret files in step with this laptop.
                     AUTOMATIC once set up: no clicking in Jenkins.

Code reaches Jenkins through GitHub (sync.sh / auto-push). Secrets never go to
GitHub, so Jenkins keeps its own copy as 3 "Secret file" credentials per account:

    accounts/<acct>/account.env  ->  S247_<ACCT>_ACCOUNT_ENV
    accounts/<acct>/.itsm.env    ->  S247_<ACCT>_ITSM_ENV
    .token.<acct>.env            ->  S247_<ACCT>_TOKEN_ENV

    python3 jenkins_secrets.py --setup    ONE TIME: save your Jenkins user + API token
                                          (hidden input, saved to .jenkins.env, git-ignored)
    python3 jenkins_secrets.py            upload every changed credential to Jenkins now
    python3 jenkins_secrets.py --check    only report what is out of date (changes nothing)
    python3 jenkins_secrets.py --auto     upload if set up, otherwise just remind (used by
                                          sync.sh, the git hook and every save_*.py script)
    python3 jenkins_secrets.py --manual   old way: copy files to ~/jenkins-upload for hand upload
    python3 jenkins_secrets.py --done     after a manual upload: mark them as uploaded

Never prints a secret value. Remembers only SHA-256 fingerprints in
.jenkins_uploaded.json (git-ignored).
"""
import atexit, base64, getpass, hashlib, json, os, re, shutil, stat, sys
import urllib.error, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(HERE, ".jenkins_uploaded.json")
CONF = os.path.join(HERE, ".jenkins.env")
UPLOAD = os.path.expanduser("~/jenkins-upload")
DEFAULT_URL = "http://10.164.175.10:8100"
STORE = "/credentials/store/system/domain/_"
FILE_CRED = "org.jenkinsci.plugins.plaincredentials.impl.FileCredentialsImpl"


# ─── which files / what changed ───────────────────────────────────────────────
def accounts():
    d = os.path.join(HERE, "accounts")
    return sorted(a for a in os.listdir(d)
                  if a != "example" and os.path.isdir(os.path.join(d, a)))


def files_for(acct):
    A = acct.upper()
    return [
        (f"S247_{A}_ACCOUNT_ENV", os.path.join(HERE, "accounts", acct, "account.env")),
        (f"S247_{A}_ITSM_ENV", os.path.join(HERE, "accounts", acct, ".itsm.env")),
        (f"S247_{A}_TOKEN_ENV", os.path.join(HERE, f".token.{acct}.env")),
    ]


def fingerprint(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def load_state():
    try:
        with open(STATE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(state):
    with open(STATE, "w") as f:
        json.dump(state, f, indent=2, sort_keys=True)
    os.chmod(STATE, stat.S_IRUSR | stat.S_IWUSR)


def pending():
    state, out = load_state(), []
    for acct in accounts():
        for cred, path in files_for(acct):
            if os.path.isfile(path) and state.get(cred) != fingerprint(path):
                out.append((cred, path, fingerprint(path)))
    return out


# ─── Jenkins connection settings (.jenkins.env) ───────────────────────────────
def load_conf():
    conf = {}
    try:
        with open(CONF) as f:
            for line in f:
                m = re.match(r"\s*([A-Z_]+)=(.*)$", line.rstrip("\n"))
                if m:
                    v = m.group(2).strip()
                    if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
                        v = v[1:-1]
                    conf[m.group(1)] = v
    except OSError:
        return None
    if conf.get("JENKINS_USER") and conf.get("JENKINS_API_TOKEN"):
        conf.setdefault("JENKINS_URL", DEFAULT_URL)
        conf["JENKINS_URL"] = conf["JENKINS_URL"].rstrip("/")
        return conf
    return None


def git_ignored(path):
    return os.system(f'git -C "{HERE}" check-ignore -q "{path}"') == 0


class Jenkins:
    def __init__(self, conf):
        self.url = conf["JENKINS_URL"]
        raw = f'{conf["JENKINS_USER"]}:{conf["JENKINS_API_TOKEN"]}'.encode()
        self.auth = "Basic " + base64.b64encode(raw).decode()
        self.crumb = None

    def call(self, method, path, data=None, ctype=None):
        req = urllib.request.Request(self.url + path, data=data, method=method)
        req.add_header("Authorization", self.auth)
        if ctype:
            req.add_header("Content-Type", ctype)
        if self.crumb:
            req.add_header(*self.crumb)
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")
        except (urllib.error.URLError, OSError) as e:
            return 0, str(e)

    def login(self):
        st, body = self.call("GET", "/me/api/json")
        if st != 200:
            return st, body
        # Newer Jenkins skips CSRF for API tokens; older ones need a crumb.
        cs, cb = self.call("GET", "/crumbIssuer/api/json")
        if cs == 200:
            try:
                j = json.loads(cb)
                self.crumb = (j["crumbRequestField"], j["crumb"])
            except (ValueError, KeyError):
                pass
        return st, json.loads(body).get("fullName") or json.loads(body).get("id")


def new_xml(cred, b64):
    return (f'<{FILE_CRED}>\n  <scope>GLOBAL</scope>\n  <id>{cred}</id>\n'
            f'  <description>Site24x7 ITSM suite secret file (synced by jenkins_secrets.py)'
            f'</description>\n  <fileName>{cred}.env</fileName>\n'
            f'  <secretBytes>{b64}</secretBytes>\n</{FILE_CRED}>\n')


def updated_xml(existing, cred, b64):
    """Keep Jenkins' scope/description, swap only the file name + content."""
    if "<secretBytes>" not in existing:
        return None   # not a Secret-file credential - refuse to overwrite it
    x = re.sub(r"<secretBytes>.*?</secretBytes>",
               lambda _: f"<secretBytes>{b64}</secretBytes>", existing, flags=re.S)
    x = re.sub(r"<fileName>.*?</fileName>",
               lambda _: f"<fileName>{cred}.env</fileName>", x, flags=re.S)
    return x


def upload_one(jk, cred, path):
    with open(path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()
    cpath = f"{STORE}/credential/{cred}/config.xml"
    st, body = jk.call("GET", cpath)
    if st == 200:
        xml = updated_xml(body, cred, b64)
        if xml is None:
            return False, "exists in Jenkins but is not a 'Secret file' credential"
        st, body = jk.call("POST", cpath, xml.encode(), "application/xml")
        return (st in (200, 204, 302)), f"updated (HTTP {st})" if st in (200, 204, 302) \
            else f"update refused (HTTP {st})"
    if st == 404:
        st, body = jk.call("POST", f"{STORE}/createCredentials",
                           new_xml(cred, b64).encode(), "application/xml")
        return (st in (200, 204, 302)), f"created (HTTP {st})" if st in (200, 204, 302) \
            else f"create refused (HTTP {st})"
    return False, f"could not read it (HTTP {st})"


def push(todo, conf, quiet=False):
    jk = Jenkins(conf)
    st, who = jk.login()
    if st != 200:
        why = {0: "Jenkins not reachable (VPN / office network?)",
               401: "Jenkins rejected the user or API token -> python3 jenkins_secrets.py --setup",
               403: "this Jenkins user may not manage credentials"}.get(st, f"HTTP {st}")
        print(f"[JENKINS] NOT UPLOADED: {why}")
        return len(todo)
    if not quiet:
        print(f"[JENKINS] logged in to {jk.url} as {who}")
    state, failed = load_state(), 0
    for cred, path, fp in todo:
        ok, msg = upload_one(jk, cred, path)
        print(f"   [{' OK ' if ok else 'FAIL'}] {cred:<28} {msg}")
        if ok:
            state[cred] = fp
        else:
            failed += 1
    save_state(state)
    if failed:
        print(f"[JENKINS] {failed} credential(s) not uploaded - fix the reason above and run:"
              f"  python3 jenkins_secrets.py")
    else:
        print(f"[JENKINS] IN SYNC: Jenkins now has the same {len(todo)} changed secret(s) as this laptop.")
    return failed


# ─── one-time setup ───────────────────────────────────────────────────────────
def setup():
    old = load_conf() or {}
    url = input(f"Jenkins URL [{old.get('JENKINS_URL', DEFAULT_URL)}]: ").strip() \
        or old.get("JENKINS_URL", DEFAULT_URL)
    user = input(f"Jenkins username [{old.get('JENKINS_USER', '')}]: ").strip() \
        or old.get("JENKINS_USER", "")
    tok = getpass.getpass("Jenkins API token (hidden"
                          + (", Enter = keep existing" if old else "") + "): ").strip() \
        or old.get("JENKINS_API_TOKEN", "")
    if not user or not tok:
        print("[FAIL] username and API token are both needed - nothing saved.")
        sys.exit(2)
    if not git_ignored(CONF):
        print("[FAIL] .jenkins.env is not git-ignored - refusing to save a secret there.")
        sys.exit(2)
    fd = os.open(CONF, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write("# Jenkins login used by jenkins_secrets.py (git-ignored, never commit)\n")
        f.write(f"JENKINS_URL='{url.rstrip('/')}'\nJENKINS_USER='{user}'\n"
                f"JENKINS_API_TOKEN='{tok}'\n")
    os.chmod(CONF, 0o600)
    print(f"[ OK ] saved to .jenkins.env (permissions 600, token {len(tok)} chars)")
    st, who = Jenkins(load_conf()).login()
    if st == 200:
        print(f"[ OK ] Jenkins accepted it - logged in as {who}")
    else:
        print(f"[INFO] Jenkins answered HTTP {st} - saved anyway; check the token and run --setup again.")
        return
    todo = pending()
    if todo:
        print(f"\nUploading {len(todo)} changed credential(s) now ...")
        push(todo, load_conf())
    else:
        print("[ OK ] Jenkins already has the same secrets as this laptop.")


# ─── called automatically by save_*.py, sync.sh and the git hook ──────────────
def auto(quiet=True):
    todo = pending()
    if not todo:
        return 0
    conf = load_conf()
    if conf:
        print(f"\n[JENKINS] {len(todo)} credential(s) changed - uploading to Jenkins automatically ...")
        return push(todo, conf, quiet=quiet)
    print(f"\n[JENKINS] {len(todo)} credential(s) changed but Jenkins still has the old ones.")
    print("          One-time fix (then it is automatic):  python3 jenkins_secrets.py --setup")
    return len(todo)


def register_auto_push():
    """save_*.py call this so every saved credential also reaches Jenkins."""
    def _run():
        try:
            auto()
        except Exception as e:   # never break the save script itself
            print(f"[JENKINS] auto-upload skipped: {e}")
    atexit.register(_run)


# ─── old manual way (fallback) ────────────────────────────────────────────────
def manual(todo):
    os.makedirs(UPLOAD, exist_ok=True)
    os.chmod(UPLOAD, stat.S_IRWXU)
    print(f"{len(todo)} Jenkins credential(s) need updating. Upload copies are in {UPLOAD}:\n")
    for cred, path, _ in todo:
        dst = os.path.join(UPLOAD, cred + ".env")
        shutil.copyfile(path, dst)
        os.chmod(dst, stat.S_IRUSR | stat.S_IWUSR)
        print(f"   {cred:<28} <- {cred}.env")
    print(f"\nFor each: {DEFAULT_URL}/manage/credentials/ -> click the name -> Update -> "
          f"Replace -> choose the file -> Save.\nThen run:  python3 jenkins_secrets.py --done")


def main():
    args = set(sys.argv[1:])
    if "--setup" in args:
        return setup()
    todo = pending()

    if "--check" in args:
        if todo:
            print(f"\n[JENKINS SECRETS] {len(todo)} credential(s) changed on this laptop since the last upload:")
            for cred, _, _ in todo:
                print(f"   {cred}")
            print("   -> python3 jenkins_secrets.py   (uploads them)")
            sys.exit(1)
        if "--quiet" not in args:
            print("[JENKINS SECRETS] Jenkins has the same secrets as this laptop.")
        return

    if "--auto" in args:
        sys.exit(1 if auto() else 0)

    if "--done" in args:
        state = load_state()
        for cred, _, fp in todo:
            state[cred] = fp
        save_state(state)
        if os.path.isdir(UPLOAD):
            shutil.rmtree(UPLOAD)
        print(f"[ OK ] remembered {len(todo)} credential(s) as uploaded; {UPLOAD} deleted.")
        return

    if not todo:
        print("[ OK ] nothing to upload - Jenkins already has the same secrets as this laptop.")
        return
    if "--manual" in args:
        return manual(todo)

    conf = load_conf()
    if not conf:
        print("Jenkins login is not set up yet. Do this once (about 1 minute):")
        print("   python3 jenkins_secrets.py --setup")
        sys.exit(1)
    sys.exit(1 if push(todo, conf, quiet=False) else 0)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[ABORTED] nothing further was uploaded.")
        sys.exit(130)
