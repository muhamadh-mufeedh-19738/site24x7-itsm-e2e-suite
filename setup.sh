#!/usr/bin/env bash
# ============================================================================
#  setup.sh — one-time setup helper for new developers
# ============================================================================
#  Safe to run multiple times. It NEVER overwrites a credential file you have
#  already filled in — it only creates the two templates if they are missing,
#  sets safe permissions, and tells you exactly what to edit next.
# ============================================================================
set -euo pipefail

cd "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

green() { printf '\033[32m%s\033[0m\n' "$1"; }
yellow(){ printf '\033[33m%s\033[0m\n' "$1"; }
red()   { printf '\033[31m%s\033[0m\n' "$1"; }
bold()  { printf '\033[1m%s\033[0m\n'  "$1"; }

echo
bold "Site24x7 Third-Party Integrations Suite — setup"
echo "============================================================"

# ---- 1. prerequisites ------------------------------------------------------
echo
bold "1. Checking prerequisites"
ok=1
if command -v python3 >/dev/null 2>&1; then
  green "   [OK]  python3  $(python3 --version 2>&1)"
else
  red   "   [!!]  python3 NOT found — install Python 3.8+"; ok=0
fi
if command -v node >/dev/null 2>&1; then
  green "   [OK]  node     $(node --version 2>&1)"
else
  red   "   [!!]  node NOT found — install Node.js 18+ (needed for the cookie)"; ok=0
fi
if command -v curl >/dev/null 2>&1; then
  green "   [OK]  curl     present"
else
  red   "   [!!]  curl NOT found — needed by get_token.sh"; ok=0
fi
if command -v jq >/dev/null 2>&1; then
  green "   [OK]  jq       present (optional)"
else
  yellow "   [--]  jq not found (optional — get_token.sh has a fallback)"
fi

# ---- 2. credential files ---------------------------------------------------
echo
bold "2. Creating credential files (if missing)"

make_from_sample() {
  local sample="$1" target="$2"
  if [[ -f "$target" ]]; then
    yellow "   [skip] $target already exists — left untouched"
  elif [[ -f "$sample" ]]; then
    cp "$sample" "$target"
    chmod 600 "$target"
    green "   [new ] $target   (chmod 600)"
  else
    red   "   [!!]  $sample not found — cannot create $target"
  fi
}

make_from_sample ".token.env.sample" ".token.env"
make_from_sample ".itsm.env.sample"  ".itsm.env"

# ---- 3. make scripts executable --------------------------------------------
echo
bold "3. Making scripts executable"
chmod +x get_token.sh start.sh setup.sh 2>/dev/null || true
green "   [OK]  get_token.sh, start.sh, setup.sh are executable"

# ---- 4. what to do next ----------------------------------------------------
echo
bold "4. NEXT STEPS — fill in YOUR OWN values"
echo "============================================================"
echo
echo "   a) Site24x7 OAuth credentials:"
echo "        nano .token.env"
echo "        (client_id, client_secret, refresh_token, accounts_url)"
echo
echo "   b) ITSM tool credentials (only the tools you use):"
echo "        nano .itsm.env"
echo
echo "   c) Point at YOUR grid:"
echo "        nano env.sh      → set S247_GRID_URL"
echo
echo "   d) Verify:"
echo "        source env.sh"
echo "        bash get_token.sh          # prints a token?"
echo "        python3 check_session.py   # all green?"
echo
echo "   e) One-time browser login (session cookie for Alert Logs):"
echo "        node s247_login.js --setup"
echo "        source .session.env"
echo
echo "   Then run the suite:"
echo "        source env.sh && source .session.env && source .itsm.env"
echo "        python3 run_all.py"
echo
if [[ $ok -eq 1 ]]; then
  green "Setup files are ready. Fill them in and you're good to go."
else
  red  "Install the missing prerequisites above, then re-run: bash setup.sh"
fi
echo
echo "Full guide: README.md"
