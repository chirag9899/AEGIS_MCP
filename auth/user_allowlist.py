"""Optional email allowlist for OAuth users."""

from __future__ import annotations

import logging
import os
from functools import lru_cache
from typing import FrozenSet, Optional

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def get_allowed_user_emails() -> Optional[FrozenSet[str]]:
    """Return normalized allowlisted emails, or None when unrestricted."""
    raw = os.getenv("WORKSPACE_MCP_ALLOWED_USER_EMAILS", "").strip()
    if not raw:
        return None
    emails = frozenset(email.strip().lower() for email in raw.split(",") if email.strip())
    return emails or None


def is_user_email_allowed(email: Optional[str]) -> bool:
    """Return True when the email is allowed to use the MCP server."""
    allowed = get_allowed_user_emails()
    if allowed is None:
        return True
    if not email:
        return False
    return email.strip().lower() in allowed


def check_user_email_allowed(email: Optional[str]) -> Optional[str]:
    """Return a denial reason when blocked, otherwise None."""
    allowed = get_allowed_user_emails()
    if allowed is None:
        return None

    normalized = (email or "").strip().lower()
    if not normalized:
        return "Access denied: authenticated user email is required"

    if normalized not in allowed:
        allowed_list = ", ".join(sorted(allowed))
        return (
            f"Access denied: {email} is not authorized to use this MCP server. "
            f"Allowed users: {allowed_list}"
        )

    return None


def log_allowlist_configuration() -> None:
    """Log the active allowlist once at startup."""
    allowed = get_allowed_user_emails()
    if allowed is None:
        logger.info("OAuth user allowlist disabled (WORKSPACE_MCP_ALLOWED_USER_EMAILS not set)")
        return
    logger.info("OAuth user allowlist enabled for: %s", ", ".join(sorted(allowed)))
    logger.info("Device key enforcement handles per-device isolation (see WORKSPACE_MCP_DEVICE_KEY_ENFORCEMENT)")
