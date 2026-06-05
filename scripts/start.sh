#!/usr/bin/env bash
# CEO Google Workspace MCP (OAuth 2.1) — proxied at /google/ on aegis.infrasingularity.com
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

if [[ ! -f .env ]]; then
  echo "error: .env not found at $repo_root/.env" >&2
  exit 1
fi

set -a
# shellcheck disable=SC1091
source .env
set +a

: "${GOOGLE_OAUTH_CLIENT_ID:?GOOGLE_OAUTH_CLIENT_ID not set in .env}"
: "${GOOGLE_OAUTH_CLIENT_SECRET:?GOOGLE_OAUTH_CLIENT_SECRET not set in .env}"

port="${WORKSPACE_MCP_PORT:-8010}"
tier="${WORKSPACE_MCP_TOOL_TIER:-extended}"
external="${WORKSPACE_EXTERNAL_URL:-}"

echo "starting CEO google workspace MCP:"
echo "  port      = $port"
echo "  tier      = $tier"
echo "  external  = ${external:-(localhost only)}"
if [[ -n "$external" ]]; then
  echo "  mcp path  = ${external%/}/mcp/"
else
  echo "  mcp path  = http://localhost:${port}/mcp/"
fi

exec uv run main.py --transport streamable-http --tool-tier "$tier"
