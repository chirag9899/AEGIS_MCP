#!/usr/bin/env bash
# ============================================================
#  Aegis Device Onboarding Script
#  Run once per machine. Registers this device and writes
#  the Claude Desktop config automatically.
#
#  Usage when the server requires a registration secret (get from admin):
#
#    export AEGIS_REGISTRATION_SECRET='<secret-from-admin>'
#    curl -fsSL https://aegis.infrasingularity.com/install.sh | bash
#
#  Or put the secret on bash (NOT on curl — env before curl does not reach bash):
#    curl -fsSL https://aegis.infrasingularity.com/install.sh | \
#      AEGIS_REGISTRATION_SECRET='<secret-from-admin>' bash
#
#  Or run locally:
#    AEGIS_REGISTRATION_SECRET='...' bash install_device.sh
# ============================================================
set -euo pipefail

SERVER="${AEGIS_SERVER:-https://aegis.infrasingularity.com}"
REGISTER_URL="$SERVER/device/register"

# ── Detect OS and Claude Desktop config path ─────────────────
detect_claude_config_path() {
    case "$(uname -s)" in
        Darwin)  echo "$HOME/Library/Application Support/Claude/claude_desktop_config.json" ;;
        Linux)   echo "$HOME/.config/claude/claude_desktop_config.json" ;;
        MINGW*|CYGWIN*|MSYS*) echo "$APPDATA/Claude/claude_desktop_config.json" ;;
        *)       echo "$HOME/.claude/claude_desktop_config.json" ;;
    esac
}

# ── Generate machine fingerprint ─────────────────────────────
generate_fingerprint() {
    local fp=""
    # Try MAC address
    if command -v ip &>/dev/null; then
        fp=$(ip link show 2>/dev/null | awk '/ether/{print $2; exit}' || true)
    fi
    if [ -z "$fp" ] && command -v ifconfig &>/dev/null; then
        fp=$(ifconfig 2>/dev/null | awk '/ether/{print $2; exit}' || true)
    fi
    # macOS
    if [ -z "$fp" ]; then
        fp=$(networksetup -listallhardwareports 2>/dev/null | awk '/Ethernet Address/{print $3; exit}' || true)
    fi
    # Fallback: machine-id
    if [ -z "$fp" ] && [ -f /etc/machine-id ]; then
        fp=$(cat /etc/machine-id)
    fi
    if [ -z "$fp" ] && [ -f /var/lib/dbus/machine-id ]; then
        fp=$(cat /var/lib/dbus/machine-id)
    fi
    # macOS hardware UUID
    if [ -z "$fp" ] && command -v ioreg &>/dev/null; then
        fp=$(ioreg -rd1 -c IOPlatformExpertDevice 2>/dev/null | awk -F'"' '/IOPlatformUUID/{print $4}' || true)
    fi
    # Final fallback: hostname
    [ -z "$fp" ] && fp="$(hostname)"
    echo "$fp"
}

# ── Merge JSON into Claude Desktop config ────────────────────
merge_claude_config() {
    local config_file="$1"
    local new_servers="$2"   # JSON string: {"server-name": {...}}

    mkdir -p "$(dirname "$config_file")"

    if [ ! -f "$config_file" ] || [ ! -s "$config_file" ]; then
        echo "{\"mcpServers\": $new_servers}" > "$config_file"
        return
    fi

    # Use python3 to merge (available on macOS and most Linux)
    python3 - "$config_file" "$new_servers" <<'PYEOF'
import sys, json

config_path = sys.argv[1]
new_servers_str = sys.argv[2]

with open(config_path, "r") as f:
    config = json.load(f)

new_servers = json.loads(new_servers_str)
config.setdefault("mcpServers", {}).update(new_servers)

with open(config_path, "w") as f:
    json.dump(config, f, indent=2)

print("Merged successfully.")
PYEOF
}

# ── Main ─────────────────────────────────────────────────────
echo ""
echo "╔══════════════════════════════════════════════╗"
echo "║     Aegis Google Workspace MCP — Setup       ║"
echo "╚══════════════════════════════════════════════╝"
echo ""

FINGERPRINT=$(generate_fingerprint)
LABEL="${AEGIS_LABEL:-$(hostname)}"

echo "  Machine     : $LABEL"
echo "  Fingerprint : $FINGERPRINT"
echo ""

# Register with server
echo "Registering device with Aegis server..."
REGISTER_BODY=$(python3 -c "
import json, os, sys
payload = {
    'fingerprint': sys.argv[1],
    'label': sys.argv[2],
}
secret = os.environ.get('AEGIS_REGISTRATION_SECRET', '').strip()
if secret:
    payload['registration_secret'] = secret
print(json.dumps(payload))
" "$FINGERPRINT" "$LABEL")

RESPONSE=$(curl -s -w "\n%{http_code}" -X POST "$REGISTER_URL" \
    -H "Content-Type: application/json" \
    -d "$REGISTER_BODY")
HTTP_CODE=$(echo "$RESPONSE" | tail -n1)
RESPONSE=$(echo "$RESPONSE" | sed '$d')

if [ -z "$RESPONSE" ]; then
    echo "ERROR: Could not reach $REGISTER_URL"
    echo "       Make sure you're connected and the server is running."
    exit 1
fi

# Parse response
KEY=$(echo "$RESPONSE" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('key',''))" 2>/dev/null || true)
MCP_URL_WITH_KEY=$(echo "$RESPONSE" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('mcp_url',''))" 2>/dev/null || true)
SERVERS=$(echo "$RESPONSE" | python3 -c "import sys,json; d=json.load(sys.stdin); print(json.dumps(d.get('claude_desktop_config',{}).get('mcpServers',{})))" 2>/dev/null || true)

if [ -z "$KEY" ]; then
    ERROR=$(echo "$RESPONSE" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('error','unknown error'))" 2>/dev/null || echo "$RESPONSE")
    echo "ERROR: Registration failed (HTTP $HTTP_CODE) — $ERROR"
    if [ "$ERROR" = "registration_secret required" ] || [ "$ERROR" = "invalid registration_secret" ]; then
        echo ""
        echo "  The server requires AEGIS_REGISTRATION_SECRET in the script's environment."
        if [ -z "${AEGIS_REGISTRATION_SECRET:-}" ]; then
            echo ""
            echo "  Common mistake: putting the secret only before curl does NOT pass it to bash:"
            echo "    AEGIS_REGISTRATION_SECRET='...' curl ... | bash   # WRONG — bash never sees it"
            echo ""
        fi
        echo "  Get the secret from your admin, then use ONE of:"
        echo ""
        echo "    export AEGIS_REGISTRATION_SECRET='<secret>'"
        echo "    curl -fsSL https://aegis.infrasingularity.com/install.sh | bash"
        echo ""
        echo "    curl -fsSL https://aegis.infrasingularity.com/install.sh | \\"
        echo "      AEGIS_REGISTRATION_SECRET='<secret>' bash"
        echo ""
    fi
    exit 1
fi

# Strip any query params from the MCP URL (key is always passed as a header, not URL param)
MCP_URL=$(echo "$MCP_URL_WITH_KEY" | python3 -c "import sys; url=sys.stdin.read().strip(); print(url.split('?')[0])")

echo "  Device key  : $KEY"
echo "  MCP URL     : $MCP_URL"
echo ""

# Write to claude_desktop_config.json (local file, NOT synced via Claude account)
CLAUDE_CONFIG=$(detect_claude_config_path)
echo "Writing to Claude Desktop local config:"
echo "  $CLAUDE_CONFIG"
echo ""

mkdir -p "$(dirname "$CLAUDE_CONFIG")"

# Use mcp-remote with --header flag to pass device key securely
# Key in args (not URL) avoids OAuth resource URL mismatch in mcp-remote validation
SERVER_NAME=$(echo "$SERVERS" | python3 -c "import sys,json; print(list(json.load(sys.stdin).keys())[0])")

if [ ! -f "$CLAUDE_CONFIG" ] || [ ! -s "$CLAUDE_CONFIG" ]; then
    python3 -c "
import json, sys
name, url, key = sys.argv[1], sys.argv[2], sys.argv[3]
entry = {'command': 'npx', 'args': ['mcp-remote', url, '--header', 'X-Device-Key: ' + key]}
print(json.dumps({'mcpServers': {name: entry}}, indent=2))
" "$SERVER_NAME" "$MCP_URL" "$KEY" > "$CLAUDE_CONFIG"
    echo "Created new config."
else
    python3 -c "
import sys, json
config_path, name, url, key = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
with open(config_path, 'r') as f:
    config = json.load(f)
entry = {'command': 'npx', 'args': ['mcp-remote', url, '--header', 'X-Device-Key: ' + key]}
config.setdefault('mcpServers', {})[name] = entry
with open(config_path, 'w') as f:
    json.dump(config, f, indent=2)
print('Merged successfully.')
" "$CLAUDE_CONFIG" "$SERVER_NAME" "$MCP_URL" "$KEY"
fi

echo "╔══════════════════════════════════════════════╗"
echo "║  Setup complete!                             ║"
echo "║                                              ║"
echo "║  1. Quit and reopen Claude Desktop.          ║"
echo "║  2. Sign in with your Google Workspace       ║"
echo "║     account when prompted.                   ║"
echo "║  3. Your email is verified automatically —   ║"
echo "║     no manual input needed.                  ║"
echo "║                                              ║"
echo "║  DO NOT re-add via Connectors UI —           ║"
echo "║  that would sync to all devices.             ║"
echo "╚══════════════════════════════════════════════╝"
echo ""
echo "  Device key : $KEY"
echo "  MCP URL    : $MCP_URL"
echo ""
