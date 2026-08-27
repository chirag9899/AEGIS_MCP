"""Stable OAuth MCP client IDs for mcp-remote (avoids DCR per Cowork chat)."""

from __future__ import annotations

import logging
import secrets
import time
import uuid
from typing import Any, Dict, Optional

from auth.device_key_registry import _read_registry, _write_registry

logger = logging.getLogger(__name__)

# Valid URLs for OAuthClientInformationFull (wildcards like localhost:* fail Pydantic).
# Actual redirect validation uses allowed_client_redirect_uris patterns on the provider.
_DEFAULT_REDIRECT_URIS = (
    "http://localhost/oauth/callback",
    "http://127.0.0.1/oauth/callback",
)


def _oauth_clients_section(data: dict) -> dict:
    clients = data.get("_oauth_clients")
    if not isinstance(clients, dict):
        clients = {}
    return clients


def get_stable_oauth_client(fingerprint: str) -> Optional[Dict[str, str]]:
    """Return a previously issued stable OAuth client for this machine fingerprint."""
    if not fingerprint:
        return None
    data = _read_registry()
    entry = _oauth_clients_section(data).get(fingerprint)
    if not isinstance(entry, dict):
        return None
    client_id = entry.get("client_id")
    client_secret = entry.get("client_secret")
    if not client_id or not client_secret:
        return None
    return {"client_id": client_id, "client_secret": client_secret}


def get_or_create_stable_oauth_client(
    fingerprint: str,
    *,
    label: str,
    redirect_uris: Optional[list[str]] = None,
) -> Dict[str, str]:
    """
    Return a stable MCP OAuth client for this device fingerprint.

    Creates and registers a new client on first use; reuses the same client_id
    on subsequent install runs so mcp-remote can pass --static-oauth-client-info
    and reuse ~/.mcp-auth tokens across Cowork chat sessions.
    """
    existing = get_stable_oauth_client(fingerprint)
    if existing:
        return existing

    client_id = str(uuid.uuid4())
    client_secret = secrets.token_urlsafe(32)
    created = {
        "client_id": client_id,
        "client_secret": client_secret,
        "label": label,
        "created_at": int(time.time()),
    }

    data = _read_registry()
    clients = _oauth_clients_section(data)
    clients[fingerprint] = created
    data["_oauth_clients"] = clients
    _write_registry(data)

    logger.info(
        "Created stable MCP OAuth client %s... for fingerprint %s",
        client_id[:8],
        fingerprint[:20],
    )
    # client_secret_post so mcp-remote puts client_id in the token request BODY (the
    # MCP SDK reads it from the body only; the client_secret_basic default omits it →
    # "Missing client_id" loop). Written into ~/.aegis/mcp-oauth-client.json by install.
    return {
        "client_id": client_id,
        "client_secret": client_secret,
        "token_endpoint_auth_method": "client_secret_post",
    }


async def ensure_oauth_client_registered(
    auth_provider: Any,
    *,
    client_id: str,
    client_secret: str,
    redirect_uris: Optional[list[str]] = None,
    client_name: str = "aegis-mcp-remote",
) -> None:
    """Register or refresh the stable client in the OAuth provider client store."""
    if auth_provider is None:
        return

    from pydantic import AnyUrl

    from fastmcp.server.auth.oauth_proxy.models import ProxyDCRClient

    uris = redirect_uris or list(_DEFAULT_REDIRECT_URIS)
    allowed_patterns = getattr(auth_provider, "_allowed_client_redirect_uris", None)
    default_scope = getattr(auth_provider, "_default_scope_str", None)

    proxy_client = ProxyDCRClient(
        client_id=client_id,
        client_secret=client_secret or None,
        redirect_uris=[AnyUrl(u) for u in uris],
        grant_types=["authorization_code", "refresh_token"],
        scope=default_scope,
        token_endpoint_auth_method=(
            "client_secret_post" if client_secret else "none"
        ),
        allowed_redirect_uri_patterns=allowed_patterns,
        client_name=client_name,
    )

    client_store = getattr(auth_provider, "_client_store", None)
    if client_store is None:
        from mcp.shared.auth import OAuthClientInformationFull

        client_info = OAuthClientInformationFull(
            client_id=client_id,
            client_secret=client_secret,
            client_name=client_name,
            redirect_uris=uris,
            token_endpoint_auth_method=(
                "client_secret_post" if client_secret else "none"
            ),
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
        )
        await auth_provider.register_client(client_info)
        logger.info(
            "Registered stable MCP OAuth client %s... with auth provider",
            client_id[:8],
        )
        return

    existing = await auth_provider.get_client(client_id)
    await client_store.put(key=client_id, value=proxy_client)
    if existing is None:
        logger.info(
            "Registered stable MCP OAuth client %s... with auth provider",
            client_id[:8],
        )
    else:
        logger.info(
            "Refreshed stable MCP OAuth client %s... in auth provider store",
            client_id[:8],
        )
