#!/usr/bin/env python3
"""
jenkins_secrets.py - keep Jenkins' copy of the secret files in step with this laptop.

Code reaches Jenkins through GitHub (sync.sh). Secrets never go to GitHub, so
Jenkins keeps its own copy as 3 "Secret file" credentials per account:

    accounts/<acct>/account.env  ->  S247_<ACCT>_ACCOUNT_ENV
    accounts/<acct>/.itsm.env    ->  S247_<ACCT>_ITSM_ENV
    .token.<acct>.env            ->  S247_<ACCT>_TOKEN_ENV

    python3 jenkins_secrets.py            show which credentials Jenkins needs updated,
                                          and put ready-to-upload copies in ~/jenkins-upload
    python3 jenkins_secrets.py --done     after uploading: remember them as "Jenkins has these"
                                          and delete the upload copies
    python3 jenkins_secrets.py --check    only report (used by sync.sh)

Never prints a secret value. It stores only SHA-256 fingerprints, in
.jenkins_uploaded.json (git-ignored).
"""
import hashlib, json, os, shutil, stat, sys

HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(HERE, ".jenkins_uploaded.json")
UPLOAD = os.path.expanduser("~/jenkins-upload")
JENKINS = "http://10.164.175.10:8100/manage/credentials/"


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


def pending():
    state, out = load_state(), []
    for acct in accounts():
        for cred, path in files_for(acct):
            if not os.path.isfile(path):
                continue
            fp = fingerprint(path)
            if state.get(cred) != fp:
                out.append((cred, path, fp))
    return out


def main():
    args = set(sys.argv[1:])
    todo = pending()

    if "--check" in args:
        if todo:
            print(f"\n[JENKINS SECRETS] {len(todo)} credential(s) changed on this laptop since the last upload:")
            for cred, _, _ in todo:
                print(f"   {cred}")
            print("   -> python3 jenkins_secrets.py   (prepares the files and tells you where to upload)")
            sys.exit(1)
        if "--quiet" not in args:
            print("[JENKINS SECRETS] Jenkins has the same secrets as this laptop.")
        return

    if "--done" in args:
        state = load_state()
        for cred, _, fp in todo:
            state[cred] = fp
        with open(STATE, "w") as f:
            json.dump(state, f, indent=2, sort_keys=True)
        os.chmod(STATE, stat.S_IRUSR | stat.S_IWUSR)
        if os.path.isdir(UPLOAD):
            shutil.rmtree(UPLOAD)
        print(f"[ OK ] remembered {len(todo)} credential(s) as uploaded; {UPLOAD} deleted.")
        return

    if not todo:
        print("[ OK ] nothing to upload - Jenkins already has the same secrets as this laptop.")
        return

    os.makedirs(UPLOAD, exist_ok=True)
    os.chmod(UPLOAD, stat.S_IRWXU)
    print(f"{len(todo)} Jenkins credential(s) need updating. Upload copies are in {UPLOAD}:\n")
    for cred, path, _ in todo:
        dst = os.path.join(UPLOAD, cred + ".env")
        shutil.copyfile(path, dst)
        os.chmod(dst, stat.S_IRUSR | stat.S_IWUSR)
        print(f"   {cred:<28} <- {cred}.env")
    print(f"""
For EACH line above:
  1. Open {JENKINS}
  2. Click the credential with that name -> Update
  3. Tick "Replace", choose the file with the same name from {UPLOAD} -> Save
     (If it does not exist yet: Add Credentials -> Kind "Secret file",
      ID = that exact name, choose the file -> Create)

When all are uploaded, run:   python3 jenkins_secrets.py --done""")


if __name__ == "__main__":
    main()
