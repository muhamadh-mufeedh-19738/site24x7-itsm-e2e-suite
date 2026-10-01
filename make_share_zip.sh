#!/usr/bin/env bash
# ============================================================================
#  make_share_zip.sh — build a CLEAN, SECRET-FREE zip to share with developers
# ============================================================================
#  Produces:  itsm-e2e-suite-share.zip
#
#  What goes IN   : all code, the .sample templates, README, setup.sh,
#                   the safe per-account config (allowlist/known_issues/
#                   test_config/integrations snapshots — no secrets).
#  What stays OUT : every *.env secret file, session cookies, token scripts,
#                   generated reports, __pycache__, node_modules, .git, and
#                   anything else that could leak a credential.
#
#  The script REFUSES to build if it detects a real secret file in the set.
# ============================================================================
set -euo pipefail
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

OUT="itsm-e2e-suite-share.zip"
STAGE="$(mktemp -d)"
DEST="$STAGE/itsm-e2e-suite"
mkdir -p "$DEST"

green() { printf '\033[32m%s\033[0m\n' "$1"; }
red()   { printf '\033[31m%s\033[0m\n' "$1"; }
bold()  { printf '\033[1m%s\033[0m\n'  "$1"; }

echo
bold "Building a clean, secret-free share package"
echo "============================================================"

# ---- files/patterns we must NEVER ship -------------------------------------
EXCLUDES=(
  "--exclude=*.env"                 # .token.env .itsm.env .session.env etc.
  "--exclude=*.env.local"
  "--exclude=env.sh"                # may contain real grid specifics
  "--exclude=env.big.sh"
  "--exclude=get_token.sh"          # points at real token files (keep local)
  "--exclude=get_refresh_token.sh"
  "--exclude=extract_cookie.py"     # not needed to share; cookie tooling
  "--exclude=.git"
  "--exclude=.git/*"
  "--exclude=__pycache__"
  "--exclude=__pycache__/*"
  "--exclude=*.pyc"
  "--exclude=.pytest_cache"
  "--exclude=.pytest_cache/*"
  "--exclude=node_modules"
  "--exclude=node_modules/*"
  "--exclude=accounts/*/reports"           # generated reports
  "--exclude=accounts/*/reports/*"
  "--exclude=accounts/*/.session.env"
  "--exclude=accounts/*/.itsm.env"
  "--exclude=accounts/*/account.env"
  "--exclude=accounts/*/stage0_trigger_test.json"   # runtime output
  "--exclude=accounts/*/stage3_*.json"
  "--exclude=accounts/*/stage4_*.json"
  "--exclude=accounts/*/stage4_results.json"
  "--exclude=accounts/*/stage2b_result.json"
  "--exclude=accounts/*/logreport_*.json"
  "--exclude=accounts/*/backup_*.json"
  "--exclude=accounts/*/patch_cookie_check.py"
  "--exclude=accounts/*/account_inventory.json"     # runtime output
  "--exclude=accounts/*/s247_config.json"
  "--exclude=accounts/*/itsm_first_results.json"
  "--exclude=accounts/*/ticket_verification.json"
  "--exclude=accounts/*/integrations_list.txt"
  "--exclude=make_share_zip.sh"                      # the packager itself
  "--exclude=itsm-e2e-suite"                         # any prior staging
  "--exclude=itsm-e2e-suite/*"
  # account-owner's live snapshots — recipients regenerate their own.
  # (the neutral accounts/example/ starter folder is kept — see below)
  "--exclude=accounts/automation"
  "--exclude=accounts/automation/*"
  "--exclude=accounts/tpt"
  "--exclude=accounts/tpt/*"
  "--exclude=accounts/tpt1"
  "--exclude=accounts/tpt1/*"
  "--exclude=itsm-e2e-suite-share.zip"
)

# ---- copy everything except the excludes -----------------------------------
# (rsync makes the excludes easy and reliable; the EXCLUDES entries are
#  already in rsync's own --exclude=PATTERN form)
if command -v rsync >/dev/null 2>&1; then
  rsync -a "${EXCLUDES[@]}" ./ "$DEST/" >/dev/null
else
  # fallback: tar with the same excludes, then untar into DEST
  tar "${EXCLUDES[@]}" -cf - . | tar -xf - -C "$DEST"
fi

# ---- include get_token.sh (code is generic; secrets live in .token.env) ----
# It is safe: it hard-codes NO secrets, only reads .token.env at runtime.
cp get_token.sh "$DEST/" 2>/dev/null || true
chmod +x "$DEST/get_token.sh" 2>/dev/null || true

# ---- provide a neutral env.sh template -------------------------------------
cat > "$DEST/env.sh" <<'EOF'
# Point this at YOUR Site24x7 grid, then: source env.sh
export S247_GRID_URL="https://CHANGE-ME.localsite24x7.com"
export S247_TOKEN_SCRIPT="$PWD/get_token.sh"
EOF

# ---- SAFETY GATE: refuse to ship if any secret slipped through -------------
echo
bold "Safety scan (no real secrets may ship)"
LEAKS=0
while IFS= read -r f; do
  red "   [LEAK] $f"
  LEAKS=1
done < <(find "$DEST" -type f \( \
      -name "*.env" -o -name ".session.env" -o -name ".itsm.env" \
      -o -name ".token.env" -o -name "account.env" \) \
      ! -name "*.sample" 2>/dev/null)

# grep for obvious LIVE-secret signatures inside any file.
#  - *.sample files are templates and are skipped.
#  - placeholder values made only of X/x (e.g. 1000.XXXX) are NOT secrets.
while IFS= read -r hit; do
  # strip the filename:line prefix to inspect the value only
  val="${hit#*:}"
  # ignore if the token body is all-placeholder (only X/x/0-9 . after 1000.)
  if printf '%s' "$val" | grep -qE "1000\.[Xx0]{6,}"; then
    continue
  fi
  red "   [LEAK] real-looking secret in: ${hit%%:*}"
  LEAKS=1
done < <(grep -rInE "(refresh_token|client_secret|access_token)[\"'[:space:]]*[:=][\"'[:space:]]*1000\.[A-Za-z0-9_-]{8,}" \
            "$DEST" --include="*" 2>/dev/null | grep -v "\.sample:" || true)

if [[ $LEAKS -ne 0 ]]; then
  red "ABORTED — secret-like content found above. Nothing was zipped."
  rm -rf "$STAGE"
  exit 1
fi
green "   [OK] no secret files, no secret-like content"

# ---- confirm the templates ARE present -------------------------------------
for must in ".token.env.sample" ".itsm.env.sample" "README.md" "setup.sh"; do
  if [[ -f "$DEST/$must" ]]; then
    green "   [OK] included $must"
  else
    red   "   [!!] MISSING $must"; LEAKS=1
  fi
done
[[ $LEAKS -ne 0 ]] && { red "ABORTED — a required template is missing."; rm -rf "$STAGE"; exit 1; }

# ---- zip it ----------------------------------------------------------------
rm -f "$OUT"
( cd "$STAGE" && zip -rq "itsm-e2e-suite.zip" "itsm-e2e-suite" )
mv "$STAGE/itsm-e2e-suite.zip" "$OUT"
rm -rf "$STAGE"

echo
bold "Done"
green "   Created: $OUT"
echo  "   Size   : $(du -h "$OUT" | cut -f1)"
echo
echo  "   Share this file. The recipient runs:"
echo  "      unzip $OUT && cd itsm-e2e-suite && bash setup.sh"
echo
