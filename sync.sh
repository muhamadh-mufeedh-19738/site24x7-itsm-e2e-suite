#!/usr/bin/env bash
# sync.sh — make GitHub (and therefore Jenkins) have exactly my local code.
#
#   bash sync.sh "what I changed"     commit everything new/changed, push, verify
#   bash sync.sh --check               only tell me if I am in sync (changes nothing)
#   bash sync.sh --install             one-time: turn on the auto-push + secret guard
#
# Jenkins downloads branch 'main' from GitHub at the start of EVERY run,
# so "pushed to GitHub" == "Jenkins has it". Secrets (.env, tokens, cookies)
# are never sent: .gitignore excludes them and the pre-commit guard blocks them.
# Secrets reach Jenkins separately:  python3 jenkins_secrets.py
set -euo pipefail
cd "$(dirname "$0")"

install_hooks() {
  chmod +x githooks/* 2>/dev/null || true
  git config core.hooksPath githooks
  echo "[ OK ] auto-push + secret guard are ON for this folder."
  echo "       Every commit on main (IDE or terminal) now goes to GitHub automatically."
}

report_sync() {
  git fetch -q origin
  local dirty; dirty=$(git status --porcelain | wc -l | tr -d ' ')
  local ahead behind
  ahead=$(git rev-list --count origin/main..HEAD)
  behind=$(git rev-list --count HEAD..origin/main)
  if [ "$dirty" = "0" ] && [ "$ahead" = "0" ] && [ "$behind" = "0" ]; then
    echo "IN SYNC: GitHub main = your laptop ($(git rev-parse --short HEAD)). The next Jenkins run uses it."
    return 0
  fi
  echo "NOT IN SYNC:"
  [ "$dirty"  != "0" ] && echo "   $dirty file(s) changed but not sent yet   -> bash sync.sh \"what I changed\""
  [ "$ahead"  != "0" ] && echo "   $ahead commit(s) not pushed yet           -> bash sync.sh \"retry\""
  [ "$behind" != "0" ] && echo "   GitHub has $behind newer commit(s)         -> bash sync.sh \"merge\""
  return 1
}

case "${1:-}" in
  --install) install_hooks; exit 0 ;;
  --check)   report_sync; exit $? ;;
  "")        echo "ERROR: give a short message, e.g.:  bash sync.sh \"fix report colours\""; exit 1 ;;
esac
msg="$1"

# Turn the hooks on automatically the first time
[ "$(git config --get core.hooksPath || true)" = "githooks" ] || install_hooks

# New AND changed files; .gitignore keeps secrets, reports and run output out
git add -A

bad=$(git diff --cached --name-only \
      | grep -Ei '(^|/)[^/]*\.env$|(^|/)\.session|(^|/)\.token[^/]*$|\.jenkins' \
      | grep -Evi '\.sample$' || true)
if [ -n "$bad" ]; then
  git restore --staged . >/dev/null
  echo "STOPPED: a secret file was about to be sent to GitHub:"
  echo "$bad" | sed 's/^/   /'
  echo "Nothing was sent. Add it to .gitignore, then run sync.sh again."
  exit 1
fi

if git diff --cached --quiet; then
  echo "No new changes to commit."
else
  echo "Files being sent:"
  git diff --cached --name-status | sed 's/^/   /'
  SKIP_AUTOPUSH=1 git commit -q -m "$msg"
fi

# Pick up anything a teammate pushed, then push
if ! git pull -q --rebase --autostash origin main; then
  git rebase --abort 2>/dev/null || true
  echo "STOPPED: GitHub has changes that clash with yours. Nothing was pushed."
  echo "Ask for help with the conflict before trying again."
  exit 1
fi
if [ "$(git rev-list --count origin/main..HEAD)" != "0" ]; then
  git push -q origin main
fi

echo ""
report_sync

# Remind if Jenkins' copy of the secrets is older than the laptop's
if [ -f jenkins_secrets.py ]; then
  python3 jenkins_secrets.py --check --quiet || true
fi
