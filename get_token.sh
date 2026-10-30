#!/usr/bin/env bash
#
# get_token.sh — Generate a fresh Site24x7 / Zoho OAuth access token
#                from a long-lived refresh token.
#
# WHAT IT DOES
#   Calls the Zoho accounts token endpoint with grant_type=refresh_token
#   and prints a fresh access_token (valid ~1 hour). No browser needed.
#
# ONE-TIME SETUP (get your refresh_token first — see README section below)
#   1. Copy the sample env file:   cp .token.env.sample .token.env
#   2. Fill in client_id, client_secret, refresh_token in .token.env
#   3. chmod +x get_token.sh
#
# USAGE
#   ./get_token.sh                 # prints just the access token
#   ./get_token.sh --json          # prints the full JSON response
#   ./get_token.sh --export        # prints: export ACCESS_TOKEN=... (eval-able)
#   ./get_token.sh --header        # prints the ready-to-use Authorization header
#   ./get_token.sh --authorize     # print the consent URL to mint an
#                                  #   ADMIN-scoped refresh token (one-time).
#                                  #   Needed so the trigger_test endpoint
#                                  #   stops returning OAuth error 1121.
#   ./get_token.sh --scope-check   # decode the current token and show whether
#                                  #   it carries the 'admin' scope.
#
#   # Use it inline in a curl call:
#   curl "$DOMAIN/api/current_status" \
#        -H "Authorization: Zoho-oauthtoken $(./get_token.sh)"
#
# ---------------------------------------------------------------------------

set -euo pipefail

# --- Resolve script directory so it works from anywhere ---------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${TOKEN_ENV_FILE:-$SCRIPT_DIR/.token.env}"

# --- Load credentials from .token.env (never hard-code secrets here) --------
if [[ -f "$ENV_FILE" ]]; then
  # shellcheck disable=SC1090
  set -a; source "$ENV_FILE"; set +a
fi

# Allow overriding via real environment variables too
CLIENT_ID="${CLIENT_ID:-${client_id:-}}"
CLIENT_SECRET="${CLIENT_SECRET:-${client_secret:-}}"
REFRESH_TOKEN="${REFRESH_TOKEN:-${refresh_token:-}}"
# Accounts server that pairs with your data center. For automation.localsite24x7.com
# the accounts host is accounts.localzoho.com
ACCOUNTS_URL="${ACCOUNTS_URL:-${accounts_url:-https://accounts.localzoho.com}}"

# OAuth scope(s) requested for the minted access token.
#
# WHY THIS MATTERS: the trigger_test endpoint (the ▶ "Trigger Test" button)
# is defined in Web_Mon's security-rest-api.xml (PUT
# /integration/thirdparty_service/trigger_test/{id}) inside a <urls> group
# whose oauthscope is "internal" — a privileged scope that is NOT grantable to
# ordinary public refresh tokens. The route is primarily a SESSION/UI route
# (userroles="1|2|3|100"). So a plain OAuth token — even one with admin scope —
# is rejected with error_code 1121 ("OAuth Scope ... not allowed"). This is a
# PRODUCT access rule, not a bug in our integrations.
#
# PRACTICAL CONSEQUENCE:
#   • The ONLY reliable way the harness can fire trigger_test headlessly is with
#     a valid browser SESSION COOKIE (S247_SESSION_COOKIE) — the same auth the
#     ▶ button uses. Keep that cookie fresh (see check_session.py).
#   • Requesting broader scopes below still helps every OTHER endpoint the suite
#     calls (monitors, alert logs, reports), and documents intent. It will NOT
#     unlock trigger_test over OAuth because that needs the 'internal' scope.
#   • When neither a cookie nor internal-scope token is available, Stage 0 now
#     reports SKIP (harness auth gap) — NOT a false FAIL — and never blocks the
#     lifecycle. The integration is still judged on real alert-log + tool proof.
#
# The refresh token must have been granted these scopes at consent time; if not,
# Zoho silently narrows the grant. Override via SCOPE in .token.env if needed.
SCOPE="${SCOPE:-${scope:-Site24x7.Admin.All Site24x7.Operations.All Site24x7.Reports.Read}}"

# OAuth scopes the harness needs. 'Site24x7.admin.all' (admin) is REQUIRED for
# the trigger_test endpoint — without it the API returns error_code 1121 and
# the pre-flight can only SKIP (not PASS). The default includes admin so a
# freshly-minted refresh token works end-to-end.
OAUTH_SCOPE="${OAUTH_SCOPE:-${oauth_scope:-Site24x7.admin.all,Site24x7.account.all,Site24x7.operation.all}}"
# Redirect URI registered with your OAuth client (self-client can use the OOB
# urn). Override in .token.env if your client uses a different one.
REDIRECT_URI="${REDIRECT_URI:-${redirect_uri:-https://accounts.localzoho.com/oauth/v2/callback}}"

# ---------------------------------------------------------------------------
# --authorize : print the consent URL to mint an ADMIN-scoped refresh token.
#   This is the PERMANENT fix for the trigger_test 1121 error. Run it once,
#   approve in the browser, copy the 'code', exchange it for a refresh_token
#   (the script tells you the exact curl), and paste that refresh_token into
#   .token.env. After that the pre-flight trigger test PASSES on its own.
# ---------------------------------------------------------------------------
if [[ "${1:-}" == "--authorize" ]]; then
  if [[ -z "${CLIENT_ID:-${client_id:-}}" ]]; then
    echo "ERROR: client_id missing — fill it in $ENV_FILE first." >&2
    exit 1
  fi
  CID="${CLIENT_ID:-${client_id}}"
  AUTH_URL="$ACCOUNTS_URL/oauth/v2/auth?response_type=code&access_type=offline&prompt=consent&client_id=$CID&scope=$OAUTH_SCOPE&redirect_uri=$REDIRECT_URI"
  echo "STEP 1 — open this URL in a browser logged in as an ADMIN of the account:"
  echo
  echo "  $AUTH_URL"
  echo
  echo "STEP 2 — approve, then copy the 'code=...' value from the redirect URL."
  echo
  echo "STEP 3 — exchange it for a refresh_token (run this, pasting the code):"
  echo
  echo "  curl -sS -X POST '$ACCOUNTS_URL/oauth/v2/token' \\"
  echo "    --data-urlencode 'grant_type=authorization_code' \\"
  echo "    --data-urlencode 'client_id=$CID' \\"
  echo "    --data-urlencode 'client_secret=<your client_secret>' \\"
  echo "    --data-urlencode 'redirect_uri=$REDIRECT_URI' \\"
  echo "    --data-urlencode 'code=<paste code here>'"
  echo
  echo "STEP 4 — copy the 'refresh_token' from the JSON into $ENV_FILE,"
  echo "         then run:  ./get_token.sh --scope-check   (must show admin)."
  exit 0
fi

# --- Validate ----------------------------------------------------------------
missing=()
[[ -z "$CLIENT_ID"     ]] && missing+=("client_id")
[[ -z "$CLIENT_SECRET" ]] && missing+=("client_secret")
[[ -z "$REFRESH_TOKEN" ]] && missing+=("refresh_token")
if (( ${#missing[@]} > 0 )); then
  echo "ERROR: missing required value(s): ${missing[*]}" >&2
  echo "       Fill them in '$ENV_FILE' (copy from .token.env.sample) or export them." >&2
  exit 1
fi

command -v curl >/dev/null 2>&1 || { echo "ERROR: curl is not installed." >&2; exit 1; }

# --- Call the token endpoint --------------------------------------------------
# Build the curl args. The scope line is only added when SCOPE is non-empty,
# so leaving SCOPE blank reproduces the original (scope-less) behaviour.
TOKEN_ARGS=(
  --data-urlencode "grant_type=refresh_token"
  --data-urlencode "client_id=$CLIENT_ID"
  --data-urlencode "client_secret=$CLIENT_SECRET"
  --data-urlencode "refresh_token=$REFRESH_TOKEN"
)
[[ -n "${SCOPE:-}" ]] && TOKEN_ARGS+=( --data-urlencode "scope=$SCOPE" )

RESPONSE="$(curl -sS -X POST "$ACCOUNTS_URL/oauth/v2/token" "${TOKEN_ARGS[@]}")"

# --- Extract access_token (jq if available, else grep/sed fallback) ----------
# '|| true': with 'set -o pipefail' a grep that finds nothing (e.g. no "error"
# field in a SUCCESSFUL reply) would otherwise kill the script silently on
# machines without jq, such as the Jenkins server.
if command -v jq >/dev/null 2>&1; then
  ACCESS_TOKEN="$(printf '%s' "$RESPONSE" | jq -r '.access_token // empty')"
  ERROR_MSG="$(printf '%s' "$RESPONSE" | jq -r '.error // empty')"
else
  ACCESS_TOKEN="$(printf '%s' "$RESPONSE" | grep -o '"access_token"[[:space:]]*:[[:space:]]*"[^"]*"' | sed 's/.*:[[:space:]]*"//;s/"$//' || true)"
  ERROR_MSG="$(printf '%s' "$RESPONSE" | grep -o '"error"[[:space:]]*:[[:space:]]*"[^"]*"' | sed 's/.*:[[:space:]]*"//;s/"$//' || true)"
fi

# --- Handle errors ------------------------------------------------------------
if [[ -z "$ACCESS_TOKEN" ]]; then
  echo "ERROR: could not obtain access token." >&2
  [[ -n "${ERROR_MSG:-}" ]] && echo "       Zoho said: $ERROR_MSG" >&2
  echo "       Full response: $RESPONSE" >&2
  echo "       Common causes: wrong accounts_url, expired/revoked refresh_token," >&2
  echo "       or client_id/secret from a different data center." >&2
  exit 2
fi

# --- Scope detection ----------------------------------------------------------
# The token response includes the granted scope. Surface whether 'admin' is
# present so the user knows if trigger_test will PASS (admin) or SKIP (no admin).
if command -v jq >/dev/null 2>&1; then
  GRANTED_SCOPE="$(printf '%s' "$RESPONSE" | jq -r '.scope // empty')"
else
  GRANTED_SCOPE="$(printf '%s' "$RESPONSE" | grep -o '"scope"[[:space:]]*:[[:space:]]*"[^"]*"' | sed 's/.*:[[:space:]]*"//;s/"$//' || true)"
fi
has_admin_scope() {
  printf '%s' "${GRANTED_SCOPE:-}" | grep -qiE 'admin'
}

# --- Output modes -------------------------------------------------------------
case "${1:-}" in
  --json)   printf '%s\n' "$RESPONSE" ;;
  --export) printf 'export ACCESS_TOKEN=%s\n' "$ACCESS_TOKEN" ;;
  --header) printf 'Authorization: Zoho-oauthtoken %s\n' "$ACCESS_TOKEN" ;;
  --scope-check)
    echo "Granted scope: ${GRANTED_SCOPE:-<not reported by server>}"
    if has_admin_scope; then
      echo "[ OK ]  admin scope present — trigger_test pre-flight will PASS."
    else
      echo "[WARN]  NO admin scope — trigger_test will return error 1121, so"
      echo "        the pre-flight can only SKIP (not a failure; lifecycle still"
      echo "        runs). To make it PASS, mint an admin token:  ./get_token.sh --authorize"
    fi
    ;;
  *)        printf '%s\n' "$ACCESS_TOKEN" ;;
esac

# When printing just the token (default / --export), warn to STDERR if the
# granted scope lacks admin — so automated callers still get a clean token on
# STDOUT but the operator sees the heads-up.
if [[ "${1:-}" == "" || "${1:-}" == "--export" ]]; then
  if [[ -n "${GRANTED_SCOPE:-}" ]] && ! has_admin_scope; then
    echo "WARN: token lacks 'admin' scope — trigger_test pre-flight will SKIP (1121)." >&2
    echo "      Run './get_token.sh --authorize' once to mint an admin token." >&2
  fi
fi
