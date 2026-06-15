#!/usr/bin/env bash
# Pre-authenticate Aegis Google MCP (saves tokens for mcp-remote / Claude Desktop).
#
# Run ONCE with Claude Desktop fully quit:
#   curl -fsSL https://aegis.infrasingularity.com/mcp_preauth.sh | bash
set -euo pipefail

SERVER="${AEGIS_SERVER:-https://aegis.infrasingularity.com}"

echo ""
echo "Aegis MCP — pre-authenticate (run with Claude Desktop quit)"
echo ""

if pgrep -f "mcp-remote.*aegis.infrasingularity.com" >/dev/null 2>&1; then
    echo "ERROR: mcp-remote is already running."
    echo "       Quit Claude Desktop and run: pkill -f 'mcp-remote.*aegis.infrasingularity.com'"
    exit 1
fi

if [ ! -f "$HOME/.aegis/mcp-oauth-client.json" ]; then
    echo "ERROR: Missing $HOME/.aegis/mcp-oauth-client.json"
    echo "       Run install.sh first to register this device."
    exit 1
fi

TMPDIR="${TMPDIR:-/tmp}"
SCRIPT="$TMPDIR/aegis-mcp-preauth-$$.py"

cleanup() {
    rm -f "$SCRIPT"
}
trap cleanup EXIT

curl -fsSL "$SERVER/mcp_preauth.py" -o "$SCRIPT"
chmod +x "$SCRIPT" 2>/dev/null || true

exec python3 "$SCRIPT" --server "$SERVER" "$@"
