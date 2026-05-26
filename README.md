# Google Workspace MCP Server

MCP server for Google Workspace (Gmail, Calendar, Drive, Docs, Sheets, Slides, Forms, Chat, Tasks, Contacts, Apps Script, Custom Search).

This repo is configured for **OAuth 2.1 multi-user** use with Claude custom connectors (and similar MCP clients). Each user signs in with their own Google account; the server acts on their behalf.

## Requirements

- Python 3.10+
- [uv](https://github.com/astral-sh/uv)
- Google Cloud project with OAuth 2.0 **Web application** client (for hosted/ngrok/VPS) or Desktop client (local stdio only)
- Required Google APIs enabled in GCP (Gmail, Calendar, Drive, Docs, Sheets, etc.)

## Quick start (CEO / Claude connector)

### 1. Configure environment

Copy settings into `.env` (see `.env.oauth21` for a template). Minimum for OAuth 2.1 + Claude:

```bash
GOOGLE_OAUTH_CLIENT_ID="your-client-id.apps.googleusercontent.com"
GOOGLE_OAUTH_CLIENT_SECRET="your-client-secret"
MCP_ENABLE_OAUTH21=true
OAUTHLIB_INSECURE_TRANSPORT=1
WORKSPACE_MCP_PORT=8010
WORKSPACE_EXTERNAL_URL=https://your-public-host.example.com
GOOGLE_OAUTH_REDIRECT_URI=https://your-public-host.example.com/oauth2callback
WORKSPACE_MCP_ALLOWED_CLIENT_REDIRECT_URIS=https://claude.ai/api/mcp/auth_callback,https://claude.com/api/mcp/auth_callback
WORKSPACE_MCP_TOOL_TIER=extended

# Optional: restrict which Google accounts may use the server
WORKSPACE_MCP_ALLOWED_USER_EMAILS=jitin@infrasingularity.com
```

**Notes**

- `.env` is loaded automatically by `main.py` (you do not need `source .env`).
- Commenting out `WORKSPACE_MCP_ALLOWED_USER_EMAILS` disables the user allowlist.
- **localhost cannot be used as the Claude connector URL** — Anthropic's servers must reach your MCP over public HTTPS (ngrok or a VPS).

### 2. Start the server

```bash
cd google_workspace_mcp
uv run main.py --transport streamable-http --tool-tier extended
```

On startup, confirm:

- `OAuth 2.1 enabled using FastMCP GoogleProvider`
- `OAuth user allowlist enabled for: ...` (if set) or `OAuth user allowlist disabled`

### 3. Claude custom connector

1. Add connector URL: `https://your-public-host.example.com/mcp/` (trailing slash matters).
2. Click **Connect / Authenticate** in Claude (not Extension settings).
3. Sign in with the allowed Google account (e.g. `jitin@infrasingularity.com`).

## Google Cloud setup

1. **OAuth consent screen** — Internal (Workspace org only) or External.
2. **Credentials** → OAuth client ID → **Web application** for production/ngrok:
   - Authorized redirect URI: `https://YOUR-HOST/oauth2callback`
3. Enable APIs: Calendar, Drive, Gmail, Docs, Sheets, Slides, Forms, Tasks, People, Chat, Apps Script, Custom Search (as needed).

## Authentication modes

| Mode | Use case | Key env vars |
|------|----------|--------------|
| **OAuth 2.1** (default here) | Claude connector, multi-user, each user signs in | `MCP_ENABLE_OAUTH21=true`, OAuth client ID/secret |
| **Service account + DWD** | Headless automation (e.g. Aegis bot) | `GOOGLE_SERVICE_ACCOUNT_KEY_FILE`, `USER_GOOGLE_EMAIL` |
| **Legacy stdio** | Local MCP clients only | `uv run main.py` (no OAuth 2.1) |

OAuth 2.1 and service account mode are **mutually exclusive** on one server instance.

## User allowlist

`WORKSPACE_MCP_ALLOWED_USER_EMAILS` restricts who can complete OAuth and use tools:

```bash
# Single user
WORKSPACE_MCP_ALLOWED_USER_EMAILS=jitin@infrasingularity.com

# Multiple users (comma-separated)
WORKSPACE_MCP_ALLOWED_USER_EMAILS=jitin@infrasingularity.com,admin@example.com
```

Non-allowed users fail at token exchange with `401` / Claude shows "Authorization with the MCP server failed".

## Tool tiers

| Tier | Description |
|------|-------------|
| `core` | Essential read/write tools |
| `extended` | Core + management operations (recommended) |
| `complete` | All tools |

```bash
uv run main.py --transport streamable-http --tool-tier extended
# or cherry-pick services:
uv run main.py --transport streamable-http --tools gmail drive calendar
```

## Important environment variables

| Variable | Purpose |
|----------|---------|
| `GOOGLE_OAUTH_CLIENT_ID` | OAuth client ID (required) |
| `GOOGLE_OAUTH_CLIENT_SECRET` | OAuth client secret (web/confidential clients) |
| `MCP_ENABLE_OAUTH21` | `true` for Claude connector / bearer auth |
| `WORKSPACE_MCP_PORT` | Local listen port (default `8000`) |
| `WORKSPACE_EXTERNAL_URL` | Public base URL (ngrok or VPS) |
| `GOOGLE_OAUTH_REDIRECT_URI` | Must match GCP redirect URI exactly |
| `WORKSPACE_MCP_ALLOWED_CLIENT_REDIRECT_URIS` | Lock DCR redirects to Claude callbacks |
| `WORKSPACE_MCP_ALLOWED_USER_EMAILS` | Optional Google account allowlist |
| `WORKSPACE_MCP_TOOL_TIER` | `core`, `extended`, or `complete` |
| `WORKSPACE_MCP_READ_ONLY` | `true` to disable write tools |
| `GOOGLE_SERVICE_ACCOUNT_KEY_FILE` | Service account JSON (automation mode only) |
| `USER_GOOGLE_EMAIL` | Impersonated user for service account mode |

See `.env.oauth21` for OAuth proxy storage, stateless mode, and other advanced options.

## Development

```bash
uv sync --group dev
uv run ruff check .
uv run pytest
```

### Project layout

```
auth/          OAuth, allowlist, middleware
core/          Server, config, tool registry
gmail/         Gmail tools
gcalendar/     Calendar tools
gdrive/        Drive tools
...            Other Google service modules
main.py        Entry point
tests/         Test suite
```

## Security

- Do not commit `.env` (gitignored).
- Use `WORKSPACE_MCP_ALLOWED_USER_EMAILS` on public deployments.
- Use `WORKSPACE_MCP_ALLOWED_CLIENT_REDIRECT_URIS` in production.
- Rotate OAuth client secrets if exposed.

## License

MIT
