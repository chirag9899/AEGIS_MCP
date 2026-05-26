import pytest

from auth.user_allowlist import (
    check_user_email_allowed,
    get_allowed_user_emails,
    is_user_email_allowed,
)


@pytest.fixture(autouse=True)
def clear_allowlist_cache():
    get_allowed_user_emails.cache_clear()
    yield
    get_allowed_user_emails.cache_clear()


def test_get_allowed_user_emails_returns_none_when_unset(monkeypatch):
    monkeypatch.delenv("WORKSPACE_MCP_ALLOWED_USER_EMAILS", raising=False)
    assert get_allowed_user_emails() is None
    assert is_user_email_allowed("anyone@example.com") is True


def test_get_allowed_user_emails_parses_comma_separated_list(monkeypatch):
    monkeypatch.setenv(
        "WORKSPACE_MCP_ALLOWED_USER_EMAILS",
        " jitin@infrasingularity.com , admin@example.com ",
    )
    assert get_allowed_user_emails() == frozenset(
        {"jitin@infrasingularity.com", "admin@example.com"}
    )


def test_is_user_email_allowed_is_case_insensitive(monkeypatch):
    monkeypatch.setenv(
        "WORKSPACE_MCP_ALLOWED_USER_EMAILS",
        "jitin@infrasingularity.com",
    )
    assert is_user_email_allowed("Jitin@InfraSingularity.com") is True
    assert is_user_email_allowed("chirag@infrasingularity.com") is False


def test_check_user_email_allowed_returns_reason_for_blocked_user(monkeypatch):
    monkeypatch.setenv(
        "WORKSPACE_MCP_ALLOWED_USER_EMAILS",
        "jitin@infrasingularity.com",
    )
    reason = check_user_email_allowed("chirag@infrasingularity.com")
    assert reason is not None
    assert "chirag@infrasingularity.com" in reason
    assert "jitin@infrasingularity.com" in reason
