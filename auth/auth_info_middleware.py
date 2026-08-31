"""
Authentication middleware to populate context state with user information
"""

import logging
import time

from fastmcp.server.middleware import Middleware, MiddlewareContext
from fastmcp.server.dependencies import get_access_token
from fastmcp.server.dependencies import get_http_headers

from fastmcp.exceptions import AuthorizationError

from auth.external_oauth_provider import get_session_time
from auth.oauth21_session_store import ensure_session_from_access_token
from auth.oauth_types import WorkspaceAccessToken
from auth.user_allowlist import check_user_email_allowed
from auth.device_key_registry import (
    is_device_key_enforcement_enabled,
    validate_device_key,
    is_key_pending,
    bind_device_key,
)

# Configure logging
logger = logging.getLogger(__name__)


class AuthInfoMiddleware(Middleware):
    """
    Middleware to extract authentication information from JWT tokens
    and populate the FastMCP context state for use in tools and prompts.
    """

    def __init__(self):
        super().__init__()
        self.auth_provider_type = "GoogleProvider"

    def _enforce_user_allowlist(self, user_email: str, device_key: str | None = None) -> None:
        reason = check_user_email_allowed(user_email)
        if reason:
            logger.warning("Allowlist denied MCP access for %s", user_email)
            raise AuthorizationError(reason)

        if is_device_key_enforcement_enabled():
            if not device_key:
                raise AuthorizationError(
                    "Access denied: no device key provided. "
                    "Run the onboarding script: curl -fsSL https://aegis.infrasingularity.com/install.sh | bash"
                )

            if not validate_device_key(user_email, device_key):
                # Key not bound yet — check if it's a fresh pending key and auto-bind it
                if is_key_pending(device_key):
                    if bind_device_key(device_key, user_email):
                        logger.info(
                            "Auto-bound device key %s... to %s on first login",
                            device_key[:8], user_email,
                        )
                        # Key is now bound; fall through to normal flow
                    else:
                        raise AuthorizationError(
                            f"Access denied: could not bind device to {user_email}. "
                            "Ensure your email is on the allowed list."
                        )
                else:
                    logger.warning(
                        "Device key rejected for %s (key=%s...)",
                        user_email, device_key[:8],
                    )
                    raise AuthorizationError(
                        f"Access denied: unrecognized or expired device key for {user_email}. "
                        "Run the onboarding script again to register this device."
                    )

    async def _process_request_for_auth(self, context: MiddlewareContext):
        """Helper to extract, verify, and store auth info from a request."""
        if not context.fastmcp_context:
            logger.warning("No fastmcp_context available")
            return

        authenticated_user = None
        auth_via = None

        # First check if FastMCP has already validated an access token
        try:
            access_token = get_access_token()
            if access_token:
                logger.info("[AuthInfoMiddleware] FastMCP access_token found")
                user_email = getattr(access_token, "email", None)
                if not user_email and hasattr(access_token, "claims"):
                    user_email = access_token.claims.get("email")

                if user_email:
                    logger.info(
                        f"✓ Using FastMCP validated token for user: {user_email}"
                    )
                    await context.fastmcp_context.set_state(
                        "authenticated_user_email", user_email
                    )
                    await context.fastmcp_context.set_state(
                        "authenticated_via", "fastmcp_oauth"
                    )
                    await context.fastmcp_context.set_state(
                        "access_token", access_token, serializable=False
                    )
                    authenticated_user = user_email
                    auth_via = "fastmcp_oauth"
                else:
                    logger.warning(
                        f"FastMCP access_token found but no email. Type: {type(access_token).__name__}"
                    )
        except Exception as e:
            logger.debug(f"Could not get FastMCP access_token: {e}")

        # Try to get the HTTP request to extract Authorization header
        if not authenticated_user:
            try:
                headers = get_http_headers(include={"authorization"})
                if headers:
                    logger.debug("Processing HTTP headers for authentication")

                    # Get the Authorization header
                    auth_header = headers.get("authorization", "")
                    if auth_header.startswith("Bearer "):
                        token_str = auth_header[7:]  # Remove "Bearer " prefix
                        logger.info("Found Bearer token in request")

                        # For Google OAuth tokens (ya29.*), we need to verify them differently
                        if token_str.startswith("ya29."):
                            logger.debug("Detected Google OAuth access token format")

                            # Verify the token to get user info
                            from core.server import get_auth_provider

                            auth_provider = get_auth_provider()

                            if auth_provider:
                                try:
                                    # Verify the token
                                    verified_auth = await auth_provider.verify_token(
                                        token_str
                                    )
                                    if verified_auth:
                                        # Extract user email from verified token
                                        user_email = getattr(
                                            verified_auth, "email", None
                                        )
                                        if not user_email and hasattr(
                                            verified_auth, "claims"
                                        ):
                                            user_email = verified_auth.claims.get(
                                                "email"
                                            )

                                        if isinstance(
                                            verified_auth, WorkspaceAccessToken
                                        ):
                                            # ExternalOAuthProvider returns a fully-formed WorkspaceAccessToken
                                            access_token = verified_auth
                                        else:
                                            # Standard GoogleProvider returns a base AccessToken;
                                            # wrap it in WorkspaceAccessToken for identical downstream handling
                                            verified_expires = getattr(
                                                verified_auth, "expires_at", None
                                            )
                                            access_token = WorkspaceAccessToken(
                                                token=token_str,
                                                client_id=getattr(
                                                    verified_auth, "client_id", None
                                                )
                                                or "google",
                                                scopes=getattr(
                                                    verified_auth, "scopes", []
                                                )
                                                or [],
                                                session_id=f"google_oauth_{token_str[:8]}",
                                                expires_at=verified_expires
                                                if verified_expires is not None
                                                else int(time.time())
                                                + get_session_time(),
                                                claims=getattr(
                                                    verified_auth, "claims", {}
                                                )
                                                or {},
                                                sub=getattr(verified_auth, "sub", None)
                                                or user_email,
                                                email=user_email,
                                            )

                                        # Store in context state - this is the authoritative authentication state
                                        await context.fastmcp_context.set_state(
                                            "access_token",
                                            access_token,
                                            serializable=False,
                                        )
                                        mcp_session_id = getattr(
                                            context.fastmcp_context, "session_id", None
                                        )
                                        ensure_session_from_access_token(
                                            access_token,
                                            user_email,
                                            mcp_session_id,
                                        )
                                        await context.fastmcp_context.set_state(
                                            "auth_provider_type",
                                            self.auth_provider_type,
                                        )
                                        await context.fastmcp_context.set_state(
                                            "token_type", "google_oauth"
                                        )
                                        await context.fastmcp_context.set_state(
                                            "user_email", user_email
                                        )
                                        await context.fastmcp_context.set_state(
                                            "username", user_email
                                        )
                                        # Set the definitive authentication state
                                        await context.fastmcp_context.set_state(
                                            "authenticated_user_email", user_email
                                        )
                                        await context.fastmcp_context.set_state(
                                            "authenticated_via", "bearer_token"
                                        )
                                        authenticated_user = user_email
                                        auth_via = "bearer_token"
                                    else:
                                        logger.error(
                                            "Failed to verify Google OAuth token"
                                        )
                                except Exception as e:
                                    logger.error(
                                        f"Error verifying Google OAuth token: {e}"
                                    )
                            else:
                                logger.warning(
                                    "No auth provider available to verify Google token"
                                )

                        else:
                            # Non-Google JWT tokens require verification
                            # SECURITY: Never set authenticated_user_email from unverified tokens
                            logger.debug(
                                "Unverified JWT token rejected - only verified tokens accepted"
                            )
                    else:
                        logger.debug("No Bearer token in Authorization header")
                else:
                    logger.debug(
                        "No HTTP headers available (might be using stdio transport)"
                    )
            except Exception as e:
                logger.debug(f"Could not get HTTP request: {e}")

        # After trying HTTP headers, check for other authentication methods
        # This consolidates all authentication logic in the middleware
        if not authenticated_user:
            logger.debug(
                "No authentication found via bearer token, checking other methods"
            )

            # Check transport mode
            from core.config import get_transport_mode

            transport_mode = get_transport_mode()

            if transport_mode == "stdio":
                # In stdio mode, check if there's a session with credentials
                # This is ONLY safe in stdio mode because it's single-user
                logger.debug("Checking for stdio mode authentication")

                # Get the requested user from the context if available
                requested_user = None
                if hasattr(context, "request") and hasattr(context.request, "params"):
                    requested_user = context.request.params.get("user_google_email")
                elif hasattr(context, "arguments"):
                    # FastMCP may store arguments differently
                    requested_user = context.arguments.get("user_google_email")

                if requested_user:
                    try:
                        from auth.oauth21_session_store import get_oauth21_session_store

                        store = get_oauth21_session_store()

                        # Check if user has a recent session
                        if store.has_session(requested_user):
                            logger.debug(
                                f"Using recent stdio session for {requested_user}"
                            )
                            # In stdio mode, we can trust the user has authenticated recently
                            await context.fastmcp_context.set_state(
                                "authenticated_user_email", requested_user
                            )
                            await context.fastmcp_context.set_state(
                                "authenticated_via", "stdio_session"
                            )
                            await context.fastmcp_context.set_state(
                                "auth_provider_type", "oauth21_stdio"
                            )
                            authenticated_user = requested_user
                            auth_via = "stdio_session"
                    except Exception as e:
                        logger.debug(f"Error checking stdio session: {e}")

                # If no requested user was provided but exactly one session exists, assume it in stdio mode
                if not authenticated_user:
                    try:
                        from auth.oauth21_session_store import get_oauth21_session_store

                        store = get_oauth21_session_store()
                        single_user = store.get_single_user_email()
                        if single_user:
                            logger.debug(
                                f"Defaulting to single stdio OAuth session for {single_user}"
                            )
                            await context.fastmcp_context.set_state(
                                "authenticated_user_email", single_user
                            )
                            await context.fastmcp_context.set_state(
                                "authenticated_via", "stdio_single_session"
                            )
                            await context.fastmcp_context.set_state(
                                "auth_provider_type", "oauth21_stdio"
                            )
                            await context.fastmcp_context.set_state(
                                "user_email", single_user
                            )
                            await context.fastmcp_context.set_state(
                                "username", single_user
                            )
                            authenticated_user = single_user
                            auth_via = "stdio_single_session"
                    except Exception as e:
                        logger.debug(
                            f"Error determining stdio single-user session: {e}"
                        )

            # Check for MCP session binding
            if not authenticated_user and hasattr(
                context.fastmcp_context, "session_id"
            ):
                mcp_session_id = context.fastmcp_context.session_id
                if mcp_session_id:
                    try:
                        from auth.oauth21_session_store import get_oauth21_session_store

                        store = get_oauth21_session_store()

                        # Check if this MCP session is bound to a user
                        bound_user = store.get_user_by_mcp_session(mcp_session_id)
                        if bound_user:
                            logger.debug(f"MCP session bound to {bound_user}")
                            await context.fastmcp_context.set_state(
                                "authenticated_user_email", bound_user
                            )
                            await context.fastmcp_context.set_state(
                                "authenticated_via", "mcp_session_binding"
                            )
                            await context.fastmcp_context.set_state(
                                "auth_provider_type", "oauth21_session"
                            )
                            authenticated_user = bound_user
                            auth_via = "mcp_session_binding"
                    except Exception as e:
                        logger.debug(f"Error checking MCP session binding: {e}")

        # Single exit point with logging
        if authenticated_user:
            device_key: str | None = None
            try:
                # Desktop (mcp-remote) sends the device key as X-Device-Key. The
                # claude.ai cloud connector only allows an approved set of custom
                # header names, so it carries the key in X-Account-Key instead — we
                # accept either, preserving the full device-key gate on both paths.
                dk_headers = get_http_headers(
                    include={"x-device-key", "x-account-key"}
                )
                if dk_headers:
                    device_key = dk_headers.get("x-device-key") or dk_headers.get(
                        "x-account-key"
                    )
            except Exception:
                pass
            self._enforce_user_allowlist(authenticated_user, device_key=device_key)
            logger.info(f"✓ Authenticated via {auth_via}: {authenticated_user}")
            auth_email = await context.fastmcp_context.get_state(
                "authenticated_user_email"
            )
            logger.debug(
                f"Context state after auth: authenticated_user_email={auth_email}"
            )

    async def _handle_authenticated_request(self, context: MiddlewareContext, call_next):
        try:
            await self._process_request_for_auth(context)
            return await call_next(context)
        except AuthorizationError as e:
            logger.info(f"Authorization denied: {e}")
            raise
        except Exception as e:
            if "GoogleAuthenticationError" in str(
                type(e)
            ) or "Access denied: Cannot retrieve credentials" in str(e):
                logger.info(f"Authentication check failed: {e}")
            else:
                logger.error(
                    f"Error in auth middleware for {context.method}: {e}",
                    exc_info=True,
                )
            raise

    async def on_call_tool(self, context: MiddlewareContext, call_next):
        """Extract auth info from token and set in context state"""
        logger.debug("Processing tool call authentication")
        return await self._handle_authenticated_request(context, call_next)

    async def on_get_prompt(self, context: MiddlewareContext, call_next):
        """Extract auth info for prompt requests too"""
        logger.debug("Processing prompt authentication")
        return await self._handle_authenticated_request(context, call_next)

    async def on_list_tools(self, context: MiddlewareContext, call_next):
        """Extract auth info for tools/list requests too."""
        logger.debug("Processing tools/list authentication")
        return await self._handle_authenticated_request(context, call_next)
