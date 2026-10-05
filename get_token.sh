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
RESPONSE="$(curl -sS -X POST "$ACCOUNTS_URL/oauth/v2/token" \
  --data-urlencode "grant_type=refresh_token" \
  --data-urlencode "client_id=$CLIENT_ID" \
  --data-urlencode "client_secret=$CLIENT_SECRET" \
  --data-urlencode "refresh_token=$REFRESH_TOKEN")"

# --- Extract access_token (jq if available, else grep/sed fallback) ----------
if command -v jq >/dev/null 2>&1; then
  ACCESS_TOKEN="$(printf '%s' "$RESPONSE" | jq -r '.access_token // empty')"
  ERROR_MSG="$(printf '%s' "$RESPONSE" | jq -r '.error // empty')"
else
  ACCESS_TOKEN="$(printf '%s' "$RESPONSE" | grep -o '"access_token"[[:space:]]*:[[:space:]]*"[^"]*"' | sed 's/.*:"//;s/"$//')"
  ERROR_MSG="$(printf '%s' "$RESPONSE" | grep -o '"error"[[:space:]]*:[[:space:]]*"[^"]*"' | sed 's/.*:"//;s/"$//')"
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

# --- Output modes -------------------------------------------------------------
case "${1:-}" in
  --json)   printf '%s\n' "$RESPONSE" ;;
  --export) printf 'export ACCESS_TOKEN=%s\n' "$ACCESS_TOKEN" ;;
  --header) printf 'Authorization: Zoho-oauthtoken %s\n' "$ACCESS_TOKEN" ;;
  *)        printf '%s\n' "$ACCESS_TOKEN" ;;
esac
