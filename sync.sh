#!/usr/bin/env bash
# sync.sh — send my local suite changes to GitHub, so Jenkins runs the same code.
#
#   bash sync.sh "what I changed"
#
# It adds every CHANGED file that git already tracks (never new junk/result
# files), refuses if any .env / secret file would be sent, commits, pushes,
# and finally confirms GitHub has it. Brand-new files must be added by name
# first:  git add new_file.py
set -euo pipefail
cd "$(dirname "$0")"

msg="${1:-}"
if [ -z "$msg" ]; then
  echo "ERROR: give a short message, e.g.:  bash sync.sh \"fix report colours\""
  exit 1
fi

git add -u

if git diff --cached --name-only | grep -Ei '(^|/)\.?[^/]*\.env$|\.session|\.token' >/dev/null; then
  echo "STOPPED: a secret file is about to be sent to GitHub:"
  git diff --cached --name-only | grep -Ei '(^|/)\.?[^/]*\.env$|\.session|\.token'
  echo "Nothing was sent. Remove it with:  git restore --staged <file>"
  exit 1
fi

if git diff --cached --quiet; then
  echo "No new changes to commit."
else
  echo "Files being sent:"
  git diff --cached --name-only | sed 's/^/   /'
  git commit -q -m "$msg"
fi

if git status -sb | head -1 | grep -q 'ahead'; then
  git push
fi

git fetch -q origin
if [ "$(git rev-parse HEAD)" = "$(git rev-parse origin/main)" ]; then
  echo ""
  echo "IN SYNC: GitHub has your latest code. The next Jenkins run uses it."
else
  echo ""
  echo "NOT IN SYNC: GitHub is different from your laptop. Read the messages above."
  exit 1
fi
