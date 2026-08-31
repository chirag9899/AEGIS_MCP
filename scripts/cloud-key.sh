#!/usr/bin/env bash
# Mint an Aegis device key for the claude.ai CLOUD connector.
#
#   export AEGIS_REGISTRATION_SECRET='<shared registration secret>'
#   curl -fsSL https://aegis.infrasingularity.com/cloud-key.sh | bash
#
# Prints a device key to paste into the connector's X-Account-Key header.
# The key is *pending* until first use, then binds to the first Google account
# that signs in with it — so it becomes private to that person.
set -euo pipefail

SERVER="${AEGIS_SERVER:-https://aegis.infrasingularity.com}"

if [ -z "${AEGIS_REGISTRATION_SECRET:-}" ]; then
    echo "ERROR: AEGIS_REGISTRATION_SECRET not set." >&2
    echo "  Run:  export AEGIS_REGISTRATION_SECRET='<ask your admin>'" >&2
    echo "  then: curl -fsSL $SERVER/cloud-key.sh | bash" >&2
    exit 1
fi

# Unique-ish fingerprint per person+machine so re-runs are traceable.
who="$(whoami 2>/dev/null || echo user)"
host="$(hostname -s 2>/dev/null || echo host)"
fp="cloud-${who}-${host}"

resp="$(curl -s -X POST "$SERVER/device/register" \
    -H "Content-Type: application/json" \
    -d "{\"registration_secret\":\"${AEGIS_REGISTRATION_SECRET}\",\"fingerprint\":\"${fp}\",\"label\":\"cloud-${who}\"}")"

key="$(printf '%s' "$resp" | python3 -c "import sys,json;print(json.load(sys.stdin).get('key',''))" 2>/dev/null || true)"

if [ -z "$key" ]; then
    echo "ERROR: could not mint a key. Server said:" >&2
    printf '%s\n' "$resp" >&2
    exit 1
fi

cat <<EOF

  Aegis cloud connector — your device key
  ═══════════════════════════════════════════════════════════

  In claude.ai → Settings → Connectors → Add custom connector:

    URL             : $SERVER/google/mcp
    Authentication  : Always required (OAuth)
    OAuth client    : No client ID — register one automatically
    Transport       : Streamable HTTP

    Additional request headers → add one:
      Name   : X-Account-Key
      Value  : $key

  Then Continue and sign in with YOUR Google account (must be on the
  allowlist). This key binds to you on that first sign-in — keep it private.
  ═══════════════════════════════════════════════════════════

EOF
