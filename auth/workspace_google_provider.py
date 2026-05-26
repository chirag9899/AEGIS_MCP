"""Google OAuth provider with optional user email allowlist enforcement."""

from __future__ import annotations

import logging
from typing import Any

from fastmcp.server.auth.providers.google import GoogleProvider
from mcp.server.auth.provider import TokenError

from auth.allowlist_token_verifier import AllowlistTokenVerifier
from auth.user_allowlist import check_user_email_allowed, get_allowed_user_emails

logger = logging.getLogger(__name__)


class WorkspaceGoogleProvider(GoogleProvider):
    """GoogleProvider that optionally restricts OAuth users by email."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        if get_allowed_user_emails() is not None:
            self._token_validator = AllowlistTokenVerifier(self._token_validator)

    async def exchange_authorization_code(self, client, authorization_code):
        """Block token issuance during OAuth when the Google account is not allowed."""
        allowed = get_allowed_user_emails()
        if allowed is not None:
            code_model = await self._code_store.get(key=authorization_code.code)
            if not code_model:
                raise TokenError("invalid_grant", "Authorization code not found")

            access_token = code_model.idp_tokens.get("access_token")
            if not access_token:
                raise TokenError("invalid_grant", "Missing upstream access token")

            inner_verifier = getattr(self._token_validator, "_inner", self._token_validator)
            validated = await inner_verifier.verify_token(access_token)
            if not validated:
                raise TokenError("invalid_grant", "Could not verify upstream access token")

            email = getattr(validated, "email", None)
            claims = getattr(validated, "claims", None) or {}
            if not email and isinstance(claims, dict):
                email = claims.get("email")

            reason = check_user_email_allowed(email)
            if reason:
                logger.warning("Allowlist rejected OAuth exchange for %s", email)
                # Token endpoint only accepts RFC 6749 error codes; use invalid_grant.
                raise TokenError("invalid_grant", reason)

        return await super().exchange_authorization_code(client, authorization_code)
