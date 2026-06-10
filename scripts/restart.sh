#!/usr/bin/env bash
# Restart Aegis Google MCP and reload .env from disk.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

source "${NVM_DIR:-$HOME/.nvm}/nvm.sh" 2>/dev/null || true

echo "Restarting aegis-google-mcp (reload .env) ..."
pm2 restart aegis-google-mcp --update-env
pm2 save

echo "Waiting for backend ..."
bash "$REPO_ROOT/scripts/verify_oauth.sh"
