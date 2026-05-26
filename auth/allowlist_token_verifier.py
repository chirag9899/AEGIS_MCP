"""Token verifier wrapper that enforces WORKSPACE_MCP_ALLOWED_USER_EMAILS."""

from __future__ import annotations

import logging
from typing import Any, Optional

from auth.user_allowlist import check_user_email_allowed, get_allowed_user_emails

logger = logging.getLogger(__name__)


class AllowlistTokenVerifier:
    """Reject upstream tokens whose user email is not on the allowlist."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    @property
    def required_scopes(self):
        return self._inner.required_scopes

    async def verify_token(self, token: str):
        result = await self._inner.verify_token(token)
        if result is None or get_allowed_user_emails() is None:
            return result

        email = getattr(result, "email", None)
        claims = getattr(result, "claims", None) or {}
        if not email and isinstance(claims, dict):
            email = claims.get("email")

        reason = check_user_email_allowed(email)
        if reason:
            logger.warning("Allowlist rejected token for %s", email)
            return None

        return result
