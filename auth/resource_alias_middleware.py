"""Rewrite canonical google-mcp OAuth URLs for the /google alias,
and inject required OIDC discovery fields into authorization-server metadata."""

import json as _json
import logging

from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger(__name__)

# These are the byte patterns that appear inside WWW-Authenticate and Location headers
# when the canonical /google-mcp resource is advertised. We replace them with the
# per-user prefix detected from X-Forwarded-Prefix.
_PATTERNS = [
    b"/google-mcp/mcp",           # in resource_metadata path
    b"/google-mcp/.well-known",   # in openid-configuration path
    b"/google-mcp/authorize",     # in authorization_endpoint
    b"/google-mcp/token",         # in token_endpoint
    b"/google-mcp/register",      # in registration_endpoint
    b'"google-mcp"',              # in JSON issuer claims
    b"/google-mcp",               # catch-all (last)
]


class ResourceAliasMiddleware:
    """ASGI middleware that rewrites OAuth metadata using X-Forwarded-Prefix."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        prefix = _header_value(scope, b"x-forwarded-prefix").strip(b"/")
        if not prefix or prefix == b"google-mcp":
            await self.app(scope, receive, send)
            return

        async def send_with_alias(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = []
                for name, value in message.get("headers", []):
                    name_lower = name.lower()
                    if name_lower in (b"www-authenticate", b"location") and b"google-mcp" in value:
                        original = value
                        for pattern in _PATTERNS:
                            replacement = pattern.replace(b"google-mcp", prefix)
                            value = value.replace(pattern, replacement)
                        if value != original:
                            logger.debug(
                                "Rewrote %s header for prefix /%s",
                                name.decode(),
                                prefix.decode(),
                            )
                    headers.append((name, value))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_alias)


class OIDCFieldsMiddleware:
    """Inject required OIDC discovery fields into /.well-known/oauth-authorization-server responses.

    mcp-remote 0.1.38+ validates the auth-server metadata with a Zod schema that requires
    jwks_uri, subject_types_supported, and id_token_signing_alg_values_supported.  FastMCP's
    OAuthMetadata model does not include these fields, so we inject them here.  Nginx's
    sub_filter will subsequently rewrite any canonical google-mcp URLs to per-alias URLs.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not scope.get("path", "").endswith("oauth-authorization-server"):
            await self.app(scope, receive, send)
            return

        status_code = 200
        resp_headers: list[tuple[bytes, bytes]] = []
        body_chunks: list[bytes] = []

        async def capture(message: Message) -> None:
            nonlocal status_code, resp_headers
            if message["type"] == "http.response.start":
                status_code = message.get("status", 200)
                resp_headers = list(message.get("headers", []))
            elif message["type"] == "http.response.body":
                body_chunks.append(message.get("body", b""))

        await self.app(scope, receive, capture)

        body = b"".join(body_chunks)
        if status_code == 200:
            try:
                data = _json.loads(body)
                issuer = str(data.get("issuer", ""))
                # Inject missing OIDC fields (setdefault = only if absent)
                data.setdefault("jwks_uri", f"{issuer}/jwks")
                data.setdefault("subject_types_supported", ["public"])
                data.setdefault("id_token_signing_alg_values_supported", ["RS256"])
                body = _json.dumps(data).encode()
                # If an alias prefix was forwarded, rewrite canonical google-mcp URLs in body
                prefix = _header_value(scope, b"x-forwarded-prefix").strip(b"/")
                if prefix and prefix != b"google-mcp":
                    body = body.replace(b"/google-mcp", b"/" + prefix)
                logger.debug("Injected OIDC fields into oauth-authorization-server response")
            except Exception as exc:
                logger.warning("OIDCFieldsMiddleware: failed to inject fields: %s", exc)

        # Rebuild headers with correct content-length
        new_headers = [(k, v) for k, v in resp_headers if k.lower() != b"content-length"]
        new_headers.append((b"content-length", str(len(body)).encode()))

        await send({"type": "http.response.start", "status": status_code, "headers": new_headers})
        await send({"type": "http.response.body", "body": body, "more_body": False})


def _header_value(scope: Scope, name: bytes) -> bytes:
    for header_name, header_value in scope.get("headers", []):
        if header_name.lower() == name:
            return header_value
    return b""
