#!/usr/bin/env bash
# Install nginx snippets and wire Aegis Google MCP into soros.conf.
#
# Idempotent — safe to re-run after git pull or server migration.
#
# Usage (on the server):
#   sudo bash scripts/setup_nginx.sh
#   sudo bash scripts/setup_nginx.sh --check   # nginx -t only, no reload
#
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NGINX_SNIPPETS="/etc/nginx/snippets"
SOROS_CONF="${SOROS_CONF:-/etc/nginx/sites-available/soros.conf}"
MARKER_START="# >>> AEGIS_GOOGLE_MCP_NGINX (managed by AEGIS_MCP/scripts/setup_nginx.sh)"
MARKER_END="# <<< AEGIS_GOOGLE_MCP_NGINX"

CHECK_ONLY=false
[[ "${1:-}" == "--check" ]] && CHECK_ONLY=true

if [[ "$(id -u)" -ne 0 ]]; then
    echo "error: run as root (sudo bash scripts/setup_nginx.sh)" >&2
    exit 1
fi

if [[ ! -f "$SOROS_CONF" ]]; then
    echo "error: $SOROS_CONF not found — copy soros.conf from the old server first" >&2
    exit 1
fi

echo "Installing nginx snippets from $REPO_ROOT/deploy/nginx/ ..."
install -m 644 "$REPO_ROOT/deploy/nginx/proxy-mcp-streamable.conf" "$NGINX_SNIPPETS/proxy-mcp-streamable.conf"
install -m 644 "$REPO_ROOT/deploy/nginx/aegis-google-mcp.locations.conf" "$NGINX_SNIPPETS/aegis-google-mcp.locations.conf"

python3 <<PY
from pathlib import Path
import re

soros = Path("$SOROS_CONF")
text = soros.read_text()
start = "$MARKER_START"
end = "$MARKER_END"
include_block = f"""{start}
    include /etc/nginx/snippets/aegis-google-mcp.locations.conf;
{end}"""

if start in text and end in text:
    text = re.sub(
        re.escape(start) + r".*?" + re.escape(end),
        include_block,
        text,
        count=1,
        flags=re.DOTALL,
    )
else:
    legacy_pattern = re.compile(
        r"    # OAuth protected-resource discovery.*?"
        r"(?=    location = /install\.sh \{|    location /device/ \{|    location /webhook/calendar \{)",
        re.DOTALL,
    )
    if not legacy_pattern.search(text):
        anchor = "    location /webhook/calendar {"
        if anchor not in text:
            raise SystemExit(f"Could not find injection point in {soros}")
        text = text.replace(anchor, include_block + "\n\n" + anchor, 1)
    else:
        text = legacy_pattern.sub(include_block + "\n\n", text, count=1)

header_line = "    large_client_header_buffers 8 64k;"
for match in list(re.finditer(r"server_name aegis\.infrasingularity\.com;", text)):
    block_start = match.start()
    next_server = text.find("\nserver {", block_start + 1)
    block_end = next_server if next_server != -1 else len(text)
    block = text[block_start:block_end]
    if header_line in block:
        continue
    insert_at = block.find("client_max_body_size")
    if insert_at == -1:
        continue
    block = block[:insert_at] + header_line + "\n\n    " + block[insert_at:]
    text = text[:block_start] + block + text[block_end:]

text = re.sub(
    r"(server_name aegis\.infrasingularity\.com;)+",
    "server_name aegis.infrasingularity.com;",
    text,
)

if "location = /install.sh" not in text:
    install_block = """    location = /install.sh {
        proxy_pass http://127.0.0.1:8010/install.sh;
        include /etc/nginx/snippets/proxy-mcp.conf;
    }

    location = /mcp_preauth.sh {
        proxy_pass http://127.0.0.1:8010/mcp_preauth.sh;
        include /etc/nginx/snippets/proxy-mcp.conf;
    }

    location = /mcp_preauth.py {
        proxy_pass http://127.0.0.1:8010/mcp_preauth.py;
        include /etc/nginx/snippets/proxy-mcp.conf;
    }

"""
    text = text.replace(end + "\n", end + "\n\n" + install_block, 1)

if "location = /mcp_preauth.py" not in text:
    text = text.replace(
        """    location = /mcp_preauth.sh {
        proxy_pass http://127.0.0.1:8010/mcp_preauth.sh;
        include /etc/nginx/snippets/proxy-mcp.conf;
    }

""",
        """    location = /mcp_preauth.sh {
        proxy_pass http://127.0.0.1:8010/mcp_preauth.sh;
        include /etc/nginx/snippets/proxy-mcp.conf;
    }

    location = /mcp_preauth.py {
        proxy_pass http://127.0.0.1:8010/mcp_preauth.py;
        include /etc/nginx/snippets/proxy-mcp.conf;
    }

""",
        1,
    )

soros.write_text(text)
print(f"Patched {soros}")
PY

echo "Testing nginx configuration ..."
nginx -t

if $CHECK_ONLY; then
    echo "Check passed (--check: not reloading)"
    exit 0
fi

systemctl reload nginx
echo "nginx reloaded."
echo "If you just restarted PM2, wait for the backend (~15s) then run:"
echo "  bash scripts/verify_oauth.sh"
