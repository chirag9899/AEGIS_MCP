#!/usr/bin/env bash
# Quick smoke tests for Aegis Google MCP OAuth + nginx wiring.
set -euo pipefail

BASE="${AEGIS_SERVER:-https://aegis.infrasingularity.com}"
BACKEND_PORT="${AEGIS_MCP_PORT:-8010}"
WAIT_SECS="${VERIFY_WAIT_SECS:-60}"
FAIL=0

wait_for_backend() {
    local url="http://127.0.0.1:${BACKEND_PORT}/mcp"
    local deadline=$((SECONDS + WAIT_SECS))
    while (( SECONDS < deadline )); do
        local code
        code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 2 "$url" 2>/dev/null || echo "000")"
        if [[ "$code" == "401" || "$code" == "200" || "$code" == "307" ]]; then
            echo "Backend ready on port ${BACKEND_PORT} (HTTP $code)"
            return 0
        fi
        sleep 2
    done
    echo "FAIL backend not ready on port ${BACKEND_PORT} after ${WAIT_SECS}s — start PM2 first:" >&2
    echo "  pm2 start ecosystem.config.js --only aegis-google-mcp" >&2
    exit 1
}

check() {
    local name="$1"
    local url="$2"
    local expect="$3"
    local code
    code="$(curl -sS -o /dev/null -w '%{http_code}' "$url" || echo "000")"
    if [[ "$code" == "$expect" ]]; then
        echo "OK  $name ($code)"
    else
        echo "FAIL $name — expected HTTP $expect, got $code — $url" >&2
        FAIL=1
    fi
}

check_json_field() {
    local name="$1"
    local url="$2"
    local py="$3"
    if curl -sS "$url" | python3 -c "$py" >/dev/null 2>&1; then
        echo "OK  $name"
    else
        echo "FAIL $name — $url" >&2
        FAIL=1
    fi
}

echo "Verifying Aegis Google MCP at $BASE ..."
wait_for_backend
check "protected resource discovery" "$BASE/.well-known/oauth-protected-resource/google/mcp" "200"
check_json_field "resource URL" "$BASE/.well-known/oauth-protected-resource/google/mcp" \
    "import sys,json; d=json.load(sys.stdin); assert d.get('resource')=='$BASE/google/mcp'"
check_json_field "auth server list" "$BASE/.well-known/oauth-protected-resource/google/mcp" \
    "import sys,json; d=json.load(sys.stdin); assert '$BASE/google' in d.get('authorization_servers',[])"
check "RFC8414 auth server metadata" "$BASE/.well-known/oauth-authorization-server/google" "200"
check "issuer-path auth server (via /google/)" "$BASE/google/.well-known/oauth-authorization-server" "200"
check "MCP endpoint (unauth)" "$BASE/google/mcp" "401"
check "legacy google-mcp alias" "$BASE/google-mcp/.well-known/oauth-authorization-server" "200"
check "install script" "$BASE/install.sh" "200"

if pm2 describe aegis-google-mcp &>/dev/null; then
    echo "OK  pm2 aegis-google-mcp is registered"
else
    echo "WARN pm2 aegis-google-mcp not running (start with: pm2 start ecosystem.config.js --only aegis-google-mcp)" >&2
fi

if [[ "$FAIL" -eq 0 ]]; then
    echo "All checks passed."
else
    exit 1
fi
