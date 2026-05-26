from types import SimpleNamespace

import pytest
from mcp.server.auth.provider import TokenError

from auth.user_allowlist import get_allowed_user_emails
from auth.workspace_google_provider import WorkspaceGoogleProvider


@pytest.fixture(autouse=True)
def clear_allowlist_cache():
    get_allowed_user_emails.cache_clear()
    yield
    get_allowed_user_emails.cache_clear()


@pytest.mark.asyncio
async def test_exchange_authorization_code_rejects_non_allowlisted_user(monkeypatch):
    monkeypatch.setenv(
        "WORKSPACE_MCP_ALLOWED_USER_EMAILS",
        "jitin@infrasingularity.com",
    )
    get_allowed_user_emails.cache_clear()

    provider = WorkspaceGoogleProvider.__new__(WorkspaceGoogleProvider)
    async def fake_get(key):
        return SimpleNamespace(idp_tokens={"access_token": "ya29.test-token"})

    provider._code_store = SimpleNamespace(get=fake_get)
    provider._token_validator = SimpleNamespace(
        _inner=SimpleNamespace(verify_token=_verify_chirag_token)
    )

    with pytest.raises(TokenError) as exc_info:
        await provider.exchange_authorization_code(
            client=SimpleNamespace(client_id="client-1"),
            authorization_code=SimpleNamespace(code="auth-code"),
        )

    assert exc_info.value.error == "invalid_grant"
    assert "chirag@infrasingularity.com" in str(exc_info.value.error_description)


async def _verify_chirag_token(token: str):
    return SimpleNamespace(
        email="chirag@infrasingularity.com",
        claims={"email": "chirag@infrasingularity.com"},
    )
