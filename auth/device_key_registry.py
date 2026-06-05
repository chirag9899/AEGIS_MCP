"""
Device key registry — maps email -> list of bound device keys.

Keys start as "pending" (no email) when the install script runs.
On the user's first successful Google OAuth login, the pending key is
automatically bound to their verified email. All future MCP requests
are then validated against that email.

Storage: device_key_bindings.json
  {
    "_pending": [{"key": "...", "label": "...", "fingerprint": "...", ...}],
    "chirag@example.com": [{"key": "...", "label": "...", "bound_at": ...}]
  }
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import secrets
import time
from pathlib import Path

logger = logging.getLogger(__name__)

_REGISTRY_LOCK = __import__("threading").Lock()
_PENDING_TTL_SECONDS = 7 * 24 * 3600  # pending keys expire after 7 days if never claimed


def _registry_path() -> Path:
    base = os.environ.get(
        "WORKSPACE_MCP_CREDENTIALS_DIR",
        os.path.expanduser("~/.google_workspace_mcp/credentials"),
    )
    return Path(base) / "device_key_bindings.json"


def _lock_file(fh):
    try:
        fcntl.flock(fh, fcntl.LOCK_EX)
    except OSError:
        pass


def _unlock_file(fh):
    try:
        fcntl.flock(fh, fcntl.LOCK_UN)
    except OSError:
        pass


def _read_registry() -> dict:
    path = _registry_path()
    if not path.exists():
        return {}
    try:
        raw = path.read_text(encoding="utf-8").strip()
        return json.loads(raw) if raw else {}
    except Exception:
        return {}


def _write_registry(data: dict) -> None:
    path = _registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(fd, "r+", encoding="utf-8") as fh:
        _lock_file(fh)
        try:
            fh.seek(0)
            fh.truncate()
            fh.write(json.dumps(data, indent=2, sort_keys=True))
            fh.flush()
            os.fsync(fh.fileno())
        finally:
            _unlock_file(fh)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def is_device_key_enforcement_enabled() -> bool:
    return os.environ.get("WORKSPACE_MCP_DEVICE_KEY_ENFORCEMENT", "").lower() in (
        "true", "1", "yes",
    )


# ── Registration ─────────────────────────────────────────────────────────────

def register_pending_device(fingerprint: str, label: str) -> dict:
    """
    Register a new device without requiring an email address.
    The key is stored as "pending" — it will be bound to an email on first OAuth login.
    Pending keys expire after 7 days if never claimed.
    """
    key = secrets.token_urlsafe(24)
    entry = {
        "key": key,
        "fingerprint": fingerprint,
        "label": label,
        "registered_at": int(time.time()),
        "expires_at": int(time.time()) + _PENDING_TTL_SECONDS,
    }

    with _REGISTRY_LOCK:
        data = _read_registry()
        pending = data.get("_pending", [])
        if not isinstance(pending, list):
            pending = []
        # Prune expired pending keys
        now = int(time.time())
        pending = [p for p in pending if isinstance(p, dict) and p.get("expires_at", 0) > now]
        pending.append(entry)
        data["_pending"] = pending
        _write_registry(data)

    logger.info("Registered pending device: label=%s key=%s...", label, key[:8])
    return entry


def register_device(email: str, fingerprint: str, label: str) -> dict:
    """
    Register a device directly bound to an email (used when email is already known).
    Kept for backwards compatibility with admin tooling.
    """
    from auth.user_allowlist import get_allowed_user_emails
    allowed = get_allowed_user_emails()
    if allowed is not None and email not in allowed:
        raise ValueError(f"{email} is not in the allowed user list")

    key = secrets.token_urlsafe(24)
    entry = {
        "key": key,
        "fingerprint": fingerprint,
        "label": label,
        "registered_at": int(time.time()),
    }

    with _REGISTRY_LOCK:
        data = _read_registry()
        if not isinstance(data, dict):
            data = {}
        devices = data.get(email, [])
        if not isinstance(devices, list):
            devices = []
        devices.append(entry)
        data[email] = devices
        _write_registry(data)

    logger.info("Registered device for %s: label=%s key=%s...", email, label, key[:8])
    return entry


# ── Pending → Bound binding ───────────────────────────────────────────────────

def is_key_pending(key: str) -> bool:
    """Return True if the key exists in the pending (unbound) list and has not expired."""
    if not key:
        return False
    data = _read_registry()
    now = int(time.time())
    for entry in data.get("_pending", []):
        if isinstance(entry, dict) and entry.get("key") == key:
            if entry.get("expires_at", 0) > now:
                return True
    return False


def bind_device_key(key: str, email: str) -> bool:
    """
    Bind a pending key to a verified email address.
    Called automatically on first successful Google OAuth login.
    Returns True if the key was found and bound, False otherwise.
    """
    from auth.user_allowlist import get_allowed_user_emails
    allowed = get_allowed_user_emails()
    if allowed is not None and email not in allowed:
        logger.warning("Refused to bind device key to unauthorized email: %s", email)
        return False

    with _REGISTRY_LOCK:
        data = _read_registry()
        now = int(time.time())
        matched = None
        remaining_pending = []

        for entry in data.get("_pending", []):
            if (isinstance(entry, dict)
                    and entry.get("key") == key
                    and entry.get("expires_at", 0) > now
                    and matched is None):
                matched = entry
            else:
                remaining_pending.append(entry)

        if not matched:
            return False

        bound_entry = {
            "key": matched["key"],
            "fingerprint": matched.get("fingerprint", ""),
            "label": matched.get("label", ""),
            "registered_at": matched.get("registered_at", int(time.time())),
            "bound_at": int(time.time()),
        }

        devices = data.get(email, [])
        if not isinstance(devices, list):
            devices = []
        devices.append(bound_entry)

        data[email] = devices
        data["_pending"] = remaining_pending
        _write_registry(data)

    logger.info("Bound device key %s... to %s on first login", key[:8], email)
    return True


# ── Validation ────────────────────────────────────────────────────────────────

def validate_device_key(email: str, key: str) -> bool:
    """Return True if the key is registered and bound to this email."""
    if not key:
        return False
    data = _read_registry()
    for entry in data.get(email, []):
        if isinstance(entry, dict) and entry.get("key") == key:
            return True
        if isinstance(entry, str) and entry == key:  # legacy plain-string format
            return True
    return False


# ── Admin helpers ─────────────────────────────────────────────────────────────

def list_devices(email: str) -> list:
    """Return all registered devices for an email (keys omitted)."""
    data = _read_registry()
    devices = data.get(email, [])
    if not isinstance(devices, list):
        return []
    return [
        {
            "label": d.get("label", "unknown") if isinstance(d, dict) else "unknown",
            "fingerprint": d.get("fingerprint", "") if isinstance(d, dict) else "",
            "registered_at": d.get("registered_at", 0) if isinstance(d, dict) else 0,
            "bound_at": d.get("bound_at") if isinstance(d, dict) else None,
        }
        for d in devices
    ]


def revoke_device_by_label(email: str, label: str) -> bool:
    """Remove all devices with the given label for this email. Returns True if any removed."""
    with _REGISTRY_LOCK:
        data = _read_registry()
        devices = data.get(email, [])
        before = len(devices)
        data[email] = [
            d for d in devices
            if not (isinstance(d, dict) and d.get("label") == label)
        ]
        if len(data[email]) < before:
            _write_registry(data)
            return True
    return False
