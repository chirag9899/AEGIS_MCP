"""Load project .env as the authoritative configuration source."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import dotenv_values, load_dotenv

# Optional keys: when absent, commented out, or empty in .env, clear any stale
# value left over from `export` / `set -a && source .env` in the shell.
OPTIONAL_DOTENV_KEYS = (
    "WORKSPACE_MCP_ALLOWED_USER_EMAILS",
)


def load_project_dotenv(env_path: Path | None = None) -> Path:
    """Load .env into os.environ, overriding shell exports for defined keys."""
    if env_path is None:
        env_path = Path(__file__).resolve().parent.parent / ".env"

    file_vars = dotenv_values(env_path) if env_path.is_file() else {}

    load_dotenv(dotenv_path=env_path, override=True)

    for key in OPTIONAL_DOTENV_KEYS:
        raw = file_vars.get(key)
        if raw is None or not str(raw).strip():
            os.environ.pop(key, None)

    return env_path
