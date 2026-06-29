#!/bin/bash
# update_provider.sh
#
# When the IPTV provider rotates the URL or credentials, run this script
# to update both the local provider.env AND the GitHub Actions secrets
# in one shot. Keeps the two sources in sync.
#
# Usage:
#   ./update_provider.sh                   # prompt for each value
#   ./update_provider.sh --show            # just print what's currently configured
#   ./update_provider.sh --local-only      # update provider.env only, skip GitHub
#   ./update_provider.sh --remote-only     # update GitHub secrets only, skip local
#
# Requires:
#   - gh CLI installed and authenticated (gh auth status)
#   - Run from the repo root (where provider.env lives)

set -euo pipefail

ENV_FILE="provider.env"
LOCAL_ONLY=0
REMOTE_ONLY=0
SHOW_ONLY=0

for arg in "$@"; do
    case "$arg" in
        --local-only)  LOCAL_ONLY=1 ;;
        --remote-only) REMOTE_ONLY=1 ;;
        --show)        SHOW_ONLY=1 ;;
        -h|--help)
            sed -n '2,16p' "$0"
            exit 0
            ;;
        *)
            echo "Unknown option: $arg" >&2
            exit 2
            ;;
    esac
done

# --- Sanity checks ---

if [ ! -f generate_epg.py ]; then
    echo "ERROR: Run this from the repo root (where generate_epg.py lives)." >&2
    exit 2
fi

if [ "$REMOTE_ONLY" -eq 0 ] && [ "$SHOW_ONLY" -eq 0 ]; then
    : # local update wanted; we'll create provider.env if missing
fi

if [ "$LOCAL_ONLY" -eq 0 ] && [ "$SHOW_ONLY" -eq 0 ]; then
    if ! command -v gh >/dev/null 2>&1; then
        echo "ERROR: gh CLI not found. Install with: brew install gh" >&2
        echo "       Or run with --local-only to skip GitHub secrets." >&2
        exit 2
    fi
    if ! gh auth status >/dev/null 2>&1; then
        echo "ERROR: gh is not authenticated. Run: gh auth login" >&2
        exit 2
    fi
fi

# --- Read current values (best-effort) ---

current_base_url=""
current_username=""
current_password=""

if [ -f "$ENV_FILE" ]; then
    # shellcheck disable=SC1090
    while IFS='=' read -r key val; do
        case "$key" in
            BASE_URL)  current_base_url="$val"  ;;
            USERNAME)  current_username="$val"  ;;
            PASSWORD)  current_password="$val"  ;;
        esac
    done < "$ENV_FILE"
fi

mask() {
    local v="$1"
    local n=${#v}
    if [ "$n" -le 4 ]; then
        printf '%s' "****"
    else
        local last4="${v: -4}"
        printf '%s' "****${last4}"
    fi
}

# --- --show mode ---

if [ "$SHOW_ONLY" -eq 1 ]; then
    echo "Local provider.env:"
    if [ -f "$ENV_FILE" ]; then
        echo "  BASE_URL=$current_base_url"
        echo "  USERNAME=$current_username"
        echo "  PASSWORD=$(mask "$current_password")"
    else
        echo "  (file does not exist)"
    fi
    echo ""
    echo "GitHub Actions secrets:"
    if command -v gh >/dev/null 2>&1 && gh auth status >/dev/null 2>&1; then
        gh secret list | awk '{print "  " $0}'
    else
        echo "  (gh not available)"
    fi
    exit 0
fi

# --- Prompt for new values ---

echo "Update IPTV provider credentials"
echo "Press Enter at any prompt to keep the current value."
echo ""

# BASE_URL
if [ -n "$current_base_url" ]; then
    read -r -p "BASE_URL [$current_base_url]: " new_base_url
else
    read -r -p "BASE_URL: " new_base_url
fi
new_base_url="${new_base_url:-$current_base_url}"

# USERNAME
if [ -n "$current_username" ]; then
    read -r -p "USERNAME [$current_username]: " new_username
else
    read -r -p "USERNAME: " new_username
fi
new_username="${new_username:-$current_username}"

# PASSWORD (don't echo current value; show masked hint instead)
masked_pw="$(mask "$current_password")"
if [ -n "$current_password" ]; then
    read -r -p "PASSWORD [$masked_pw]: " new_password
else
    read -r -p "PASSWORD: " new_password
fi
new_password="${new_password:-$current_password}"

# Validate that we ended up with all three
if [ -z "$new_base_url" ] || [ -z "$new_username" ] || [ -z "$new_password" ]; then
    echo "ERROR: All three values (BASE_URL, USERNAME, PASSWORD) must be non-empty." >&2
    exit 3
fi

# Strip trailing slash from base URL — the script appends paths assuming none
new_base_url="${new_base_url%/}"

# --- Confirm before writing anything ---

echo ""
echo "About to set:"
echo "  BASE_URL = $new_base_url"
echo "  USERNAME = $new_username"
echo "  PASSWORD = $(mask "$new_password")"
echo ""
if [ "$LOCAL_ONLY" -eq 1 ]; then
    echo "Will update: provider.env (local only)"
elif [ "$REMOTE_ONLY" -eq 1 ]; then
    echo "Will update: GitHub Actions secrets (remote only)"
else
    echo "Will update: provider.env AND GitHub Actions secrets"
fi
echo ""
read -r -p "Proceed? [y/N] " confirm
if [ "$confirm" != "y" ] && [ "$confirm" != "Y" ]; then
    echo "Aborted, nothing changed."
    exit 0
fi

# --- Write provider.env ---

if [ "$REMOTE_ONLY" -eq 0 ]; then
    # Backup whatever was there before
    if [ -f "$ENV_FILE" ]; then
        cp "$ENV_FILE" "${ENV_FILE}.bak"
    fi

    cat > "$ENV_FILE" << EOF
BASE_URL=$new_base_url
USERNAME=$new_username
PASSWORD=$new_password
EOF
    chmod 600 "$ENV_FILE"
    echo "✓ Updated $ENV_FILE (previous version saved as ${ENV_FILE}.bak)"
fi

# --- Update GitHub secrets ---

if [ "$LOCAL_ONLY" -eq 0 ]; then
    # Note the secret name mapping: the workflow expects BASE_URL,
    # XTREAM_USERNAME, XTREAM_PASSWORD (with the XTREAM_ prefix on
    # user/pass to disambiguate from generic GitHub env names).
    gh secret set BASE_URL         --body "$new_base_url" >/dev/null
    gh secret set XTREAM_USERNAME  --body "$new_username" >/dev/null
    gh secret set XTREAM_PASSWORD  --body "$new_password" >/dev/null
    echo "✓ Updated GitHub secrets (BASE_URL, XTREAM_USERNAME, XTREAM_PASSWORD)"
fi

echo ""
echo "Done. Next steps:"
echo "  1. Quick local sanity check:"
echo "       python3 generate_epg.py --output /tmp/epg_test.xml --categories 911"
echo "  2. If local works, trigger a cloud rebuild:"
echo "       gh workflow run regenerate-epg.yml"
echo "  3. Watch it run:"
echo "       gh run watch"
