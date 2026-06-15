#!/usr/bin/env python3
"""Pre-authenticate Aegis Google MCP and save mcp-remote compatible tokens.

Bypasses mcp-remote's duplicate OAuth flow (which can delete the PKCE verifier
with "No code verifier saved for session"). Writes tokens to ~/.mcp-auth in the
format expected by mcp-remote@0.1.38.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import secrets
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

MCP_REMOTE_VERSION = "0.1.37"
DEFAULT_SERVER = "https://aegis.infrasingularity.com"
DEVICE_FILE = Path.home() / ".aegis" / "device.json"


def detect_claude_config_path() -> Path:
    home = Path.home()
    if sys.platform == "darwin":
        return home / "Library/Application Support/Claude/claude_desktop_config.json"
    if os.name == "nt":
        appdata = os.environ.get("APPDATA", "")
        if appdata:
            return Path(appdata) / "Claude/claude_desktop_config.json"
    if (home / ".config/claude/claude_desktop_config.json").exists():
        return home / ".config/claude/claude_desktop_config.json"
    return home / ".config/claude/claude_desktop_config.json"


def load_device_key_and_server_name(config_path: Path) -> tuple[str, str]:
    with config_path.open(encoding="utf-8") as f:
        cfg = json.load(f)
    servers = cfg.get("mcpServers") or {}
    for name, entry in servers.items():
        args = entry.get("args") or []
        url_blob = " ".join(str(a) for a in args)
        if "aegis.infrasingularity.com" not in url_blob or "google/mcp" not in url_blob:
            continue
        device_key = ""
        for i, arg in enumerate(args):
            if arg == "--header" and i + 1 < len(args):
                header = str(args[i + 1])
                if header.startswith("X-Device-Key:"):
                    device_key = header.split(":", 1)[1].strip()
                    break
        if device_key:
            return device_key, name
    raise SystemExit(
        f"ERROR: Could not find aegis-google MCP entry with X-Device-Key in {config_path}"
    )


def load_device_credentials(
    *,
    config_path: Path,
    mcp_url: str,
) -> tuple[str, str]:
    """Load device key from ~/.aegis/device.json, else Claude config."""
    if DEVICE_FILE.is_file():
        try:
            data = json.loads(DEVICE_FILE.read_text(encoding="utf-8"))
            key = (data.get("device_key") or data.get("key") or "").strip()
            name = (data.get("server_name") or "aegis-google").strip()
            if key:
                return key, name
        except (json.JSONDecodeError, OSError):
            pass
    if config_path.is_file():
        return load_device_key_and_server_name(config_path)
    raise SystemExit(
        "ERROR: Missing device credentials.\n"
        f"       Expected {DEVICE_FILE} or {config_path}\n"
        "       Run: curl -fsSL https://aegis.infrasingularity.com/install.sh | bash"
    )


def mcp_remote_server_url_hash(server_url: str, headers: dict[str, str]) -> str:
    """Match mcp-remote getServerUrlHash (JSON.stringify without spaces)."""
    parts = [server_url]
    if headers:
        ordered = {k: headers[k] for k in sorted(headers)}
        parts.append(json.dumps(ordered, separators=(",", ":")))
    return hashlib.md5("|".join(parts).encode()).hexdigest()


def default_callback_port(server_url_hash: str) -> int:
    offset = int(server_url_hash[:4], 16)
    return 3335 + offset % 45816


def find_available_port(preferred: int) -> int:
    for port in (preferred,) + tuple(range(preferred + 1, preferred + 20)):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).decode().rstrip("=")
    return verifier, challenge


def http_json(url: str, *, headers: dict[str, str] | None = None, timeout: float = 30.0) -> Any:
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def http_form_post(
    url: str,
    data: dict[str, str],
    *,
    headers: dict[str, str] | None = None,
    timeout: float = 60.0,
) -> tuple[int, dict[str, Any] | str]:
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded", **(headers or {})},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode()
            try:
                return resp.status, json.loads(raw)
            except json.JSONDecodeError:
                return resp.status, raw
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode()
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, raw


def discover_auth_server(mcp_url: str) -> tuple[str, list[str]]:
    prm_url = f"{mcp_url.rsplit('/', 1)[0]}/.well-known/oauth-protected-resource/mcp"
    # nginx serves PRM at /.well-known/oauth-protected-resource/google/mcp
    parsed = urllib.parse.urlparse(mcp_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    prm_candidates = [
        f"{origin}/.well-known/oauth-protected-resource/google/mcp",
        prm_url,
    ]
    auth_server = f"{origin}/google"
    scopes: list[str] = []
    for candidate in prm_candidates:
        try:
            prm = http_json(candidate)
        except Exception:
            continue
        servers = prm.get("authorization_servers") or []
        if servers:
            auth_server = str(servers[0]).rstrip("/")
        scopes = list(prm.get("scopes_supported") or [])
        break
    if not scopes:
        meta = http_json(f"{origin}/.well-known/oauth-authorization-server")
        scopes = list(meta.get("scopes_supported") or [])
    return auth_server, scopes


def find_aegis_entry(cfg: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    for name, entry in (cfg.get("mcpServers") or {}).items():
        blob = json.dumps(entry)
        if "aegis.infrasingularity.com" in blob and "google/mcp" in blob:
            return name, entry
    return None


def validate_claude_config(
    config_path: Path,
    *,
    mcp_url: str,
    oauth_path: Path,
    device_key: str,
) -> list[str]:
    """Return human-readable config problems (empty list = OK)."""
    with config_path.open(encoding="utf-8") as f:
        cfg = json.load(f)
    found = find_aegis_entry(cfg)
    problems: list[str] = []
    if found is None:
        problems.append("no aegis-google MCP entry in claude_desktop_config.json")
        return problems

    _name, entry = found
    if entry.get("command") != "npx":
        problems.append('MCP entry must use "command": "npx" (not a URL connector)')
    args = [str(a) for a in (entry.get("args") or [])]
    blob = " ".join(args)
    if f"mcp-remote@{MCP_REMOTE_VERSION}" not in blob and "mcp-remote@0.1.38" not in blob:
        problems.append(f"args must include mcp-remote@{MCP_REMOTE_VERSION}")
    if mcp_url not in args:
        problems.append(f"args must include exact MCP URL: {mcp_url}")
    if f"X-Device-Key: {device_key}" not in blob and f"X-Device-Key:{device_key}" not in blob:
        problems.append("args must include --header X-Device-Key: <your-key>")
    oauth_marker = f"@{oauth_path}"
    if "--static-oauth-client-info" not in args or oauth_marker not in blob:
        problems.append(f"args must include --static-oauth-client-info @{oauth_path}")
    if entry.get("url"):
        problems.append('remove "url" from the entry — URL connectors ignore ~/.mcp-auth tokens')
    return problems


def fix_claude_config(
    config_path: Path,
    *,
    server_name: str,
    mcp_url: str,
    device_key: str,
    oauth_path: Path,
) -> None:
    """Rewrite the Aegis MCP entry to the known-good npx/mcp-remote shape."""
    with config_path.open(encoding="utf-8") as f:
        cfg = json.load(f)
    args = [
        "-y",
        f"mcp-remote@{MCP_REMOTE_VERSION}",
        mcp_url,
        "--header",
        f"X-Device-Key: {device_key}",
        "--static-oauth-client-info",
        f"@{oauth_path}",
        "--transport",
        "http-first",
    ]
    entry = {"command": "npx", "args": args}
    cfg.setdefault("mcpServers", {})[server_name] = entry
    with config_path.open("w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
        f.write("\n")


def verify_mcp_connection(
    mcp_url: str,
    access_token: str,
    device_key: str,
) -> None:
    """Confirm saved tokens work against the live MCP endpoint."""
    init = {
        "jsonrpc": "2.0",
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "aegis-preauth", "version": "1"},
        },
        "id": 1,
    }
    req = urllib.request.Request(
        mcp_url,
        data=json.dumps(init).encode(),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {access_token}",
            "X-Device-Key": device_key,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            if resp.status != 200:
                raise SystemExit(f"ERROR: MCP verify failed (HTTP {resp.status})")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")[:500]
        raise SystemExit(
            f"ERROR: Saved tokens were rejected by the MCP server (HTTP {exc.code}).\n"
            f"       {body}\n"
            "       Fix Claude config (see warnings above) and run pre-auth again."
        ) from exc


def normalize_mcp_remote_tokens(raw: dict[str, Any]) -> dict[str, Any]:
    """Shape tokens so mcp-remote@0.1.38 will load and reuse them.

    mcp-remote ignores the cache unless refresh_token is present and Zod parse
    succeeds (token_type is required; expires_in must be numeric).
    """
    access_token = raw.get("access_token")
    if not access_token:
        raise ValueError("Token response missing access_token")

    refresh_token = raw.get("refresh_token")
    if not refresh_token:
        raise ValueError(
            "Token response missing refresh_token. "
            "Without it, mcp-remote always opens OAuth in Claude Desktop. "
            "Try again in a private browser window, or revoke Aegis access at "
            "https://myaccount.google.com/permissions and re-run install."
        )

    normalized: dict[str, Any] = {
        "access_token": str(access_token),
        "token_type": str(raw.get("token_type") or "Bearer"),
        "refresh_token": str(refresh_token),
    }

    expires_in = raw.get("expires_in")
    if expires_in is not None:
        try:
            normalized["expires_in"] = int(expires_in)
        except (TypeError, ValueError):
            normalized["expires_in"] = 3600
    else:
        normalized["expires_in"] = 3600

    if raw.get("scope"):
        normalized["scope"] = str(raw["scope"])

    return normalized


def save_mcp_remote_tokens(
    server_url_hash: str,
    tokens: dict[str, Any],
    *,
    version: str = MCP_REMOTE_VERSION,
) -> Path:
    normalized = normalize_mcp_remote_tokens(tokens)
    config_dir = Path.home() / ".mcp-auth" / f"mcp-remote-{version}"
    config_dir.mkdir(parents=True, exist_ok=True)
    path = config_dir / f"{server_url_hash}_tokens.json"
    path.write_text(json.dumps(normalized, indent=2) + "\n", encoding="utf-8")
    os.chmod(path, 0o600)
    return path


def run_oauth(
    *,
    mcp_url: str,
    auth_server: str,
    scopes: list[str],
    client_id: str,
    client_secret: str,
    callback_port: int,
    timeout_seconds: int,
) -> dict[str, Any]:
    redirect_uri = f"http://127.0.0.1:{callback_port}/oauth/callback"
    verifier, challenge = pkce_pair()
    state = secrets.token_urlsafe(16)
    scope = " ".join(scopes) if scopes else "openid email profile"
    resource = mcp_url

    result: dict[str, str | None] = {"code": None, "error": None}
    done = threading.Event()

    class CallbackHandler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:
            return

        def do_GET(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path != "/oauth/callback":
                self.send_response(404)
                self.end_headers()
                return
            params = urllib.parse.parse_qs(parsed.query)
            if params.get("error"):
                result["error"] = params["error"][0]
            else:
                result["code"] = params.get("code", [None])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            if result["error"]:
                body = (
                    "<html><body><h1>Authorization failed</h1>"
                    f"<p>{result['error']}</p>"
                    "<p>You can close this window.</p></body></html>"
                )
            else:
                body = (
                    "<html><body><h1>Authorization successful</h1>"
                    "<p>Return to the terminal. You can close this window.</p></body></html>"
                )
            self.wfile.write(body.encode())
            done.set()

    server = HTTPServer(("127.0.0.1", callback_port), CallbackHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    auth_params = {
        "response_type": "code",
        "client_id": client_id,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "redirect_uri": redirect_uri,
        "state": state,
        "scope": scope,
        "resource": resource,
        "prompt": "consent",
    }
    authorize_url = f"{auth_server}/authorize?{urllib.parse.urlencode(auth_params)}"

    print(f"  Callback     : {redirect_uri}")
    print("")
    print("Opening browser for Google sign-in...")
    print("If the browser does not open, visit this URL:")
    print(authorize_url)
    print("")
    webbrowser.open(authorize_url)

    if not done.wait(timeout=timeout_seconds):
        server.shutdown()
        raise SystemExit(
            f"ERROR: Timed out after {timeout_seconds}s waiting for OAuth callback."
        )

    server.shutdown()
    if result["error"]:
        raise SystemExit(f"ERROR: OAuth authorization failed: {result['error']}")
    if not result["code"]:
        raise SystemExit("ERROR: OAuth callback did not include an authorization code.")

    token_url = f"{auth_server}/token"
    token_data = {
        "grant_type": "authorization_code",
        "code": result["code"],
        "redirect_uri": redirect_uri,
        "client_id": client_id,
        "code_verifier": verifier,
        "resource": resource,
    }
    if client_secret:
        token_data["client_secret"] = client_secret
    status, token_resp = http_form_post(token_url, token_data)
    if status != 200 or not isinstance(token_resp, dict) or "access_token" not in token_resp:
        err = token_resp if isinstance(token_resp, dict) else {"raw": token_resp}
        raise SystemExit(
            "ERROR: Token exchange failed "
            f"(HTTP {status}): {json.dumps(err, indent=2)}"
        )
    return token_resp


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Pre-authenticate Aegis Google MCP")
    parser.add_argument("--server", default=os.environ.get("AEGIS_SERVER", DEFAULT_SERVER))
    parser.add_argument("--oauth-file", default=str(Path.home() / ".aegis/mcp-oauth-client.json"))
    parser.add_argument("--claude-config", default=str(detect_claude_config_path()))
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument(
        "--from-install",
        action="store_true",
        help="Called from install.sh (shorter messages)",
    )
    args = parser.parse_args(argv)

    server = args.server.rstrip("/")
    mcp_url = f"{server}/google/mcp"
    oauth_path = Path(args.oauth_file)
    claude_path = Path(args.claude_config)

    if not oauth_path.is_file():
        raise SystemExit(f"ERROR: Missing {oauth_path}\n       Run install.sh first.")

    with oauth_path.open(encoding="utf-8") as f:
        oauth_client = json.load(f)
    client_id = oauth_client.get("client_id") or ""
    client_secret = oauth_client.get("client_secret") or ""
    if not client_id:
        raise SystemExit(f"ERROR: Invalid OAuth client file: {oauth_path}")

    device_key, server_name = load_device_credentials(config_path=claude_path, mcp_url=mcp_url)
    headers = {"X-Device-Key": device_key}
    server_url_hash = mcp_remote_server_url_hash(mcp_url, headers)
    callback_port = find_available_port(default_callback_port(server_url_hash))

    if not args.from_install:
        print("")
        print("Aegis MCP — pre-authenticate (run with Claude Desktop quit)")
        print("")
    else:
        print("")
        print("Step 2/2 — Google sign-in (opens your browser once)")
        print("")
    print(f"  Server entry : {server_name}")
    print(f"  MCP URL      : {mcp_url}")
    print(f"  OAuth client : {client_id[:8]}...")
    print(f"  Token cache  : ~/.mcp-auth/mcp-remote-{MCP_REMOTE_VERSION}/{server_url_hash}_tokens.json")
    print("")

    auth_server, scopes = discover_auth_server(mcp_url)
    tokens = run_oauth(
        mcp_url=mcp_url,
        auth_server=auth_server,
        scopes=scopes,
        client_id=client_id,
        client_secret=client_secret,
        callback_port=callback_port,
        timeout_seconds=args.timeout,
    )

    try:
        normalized = normalize_mcp_remote_tokens(tokens)
    except ValueError as exc:
        raise SystemExit(f"ERROR: {exc}") from exc

    token_path = save_mcp_remote_tokens(server_url_hash, normalized)
    print(
        "  Token fields : access_token, refresh_token, "
        f"token_type={normalized['token_type']!r}, expires_in={normalized['expires_in']}"
    )
    print("")
    print("Verifying tokens against MCP server...")
    verify_mcp_connection(mcp_url, normalized["access_token"], device_key)
    print("  MCP connection check: OK")

    claude_path.parent.mkdir(parents=True, exist_ok=True)
    config_problems = validate_claude_config(
        claude_path,
        mcp_url=mcp_url,
        oauth_path=oauth_path,
        device_key=device_key,
    ) if claude_path.is_file() else ["missing claude_desktop_config.json"]
    if config_problems:
        if not args.from_install:
            print("")
            print("Updating Claude Desktop config:")
            for item in config_problems:
                print(f"  - {item}")
        fix_claude_config(
            claude_path,
            server_name=server_name,
            mcp_url=mcp_url,
            device_key=device_key,
            oauth_path=oauth_path,
        )

    print("")
    print("Success! Saved OAuth tokens:")
    print(f"  {token_path}")
    print("")
    if args.from_install:
        print("Setup complete. Open Claude Desktop — it should connect without asking again.")
        print("If Claude opens Google sign-in anyway, quit Claude (Cmd+Q) and do NOT sign in")
        print("there; re-run install.sh instead.")
    else:
        print("Open Claude Desktop — it should connect WITHOUT a browser prompt.")
        print("Do not add Aegis via Settings → Connectors (that bypasses saved tokens).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
