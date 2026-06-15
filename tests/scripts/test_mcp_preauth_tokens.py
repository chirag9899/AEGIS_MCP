"""Tests for mcp-remote token normalization in mcp_preauth."""

import pytest


def test_normalize_requires_refresh_token():
    from scripts.mcp_preauth import normalize_mcp_remote_tokens

    with pytest.raises(ValueError, match="refresh_token"):
        normalize_mcp_remote_tokens({"access_token": "at"})


def test_normalize_adds_bearer_and_expires_in():
    from scripts.mcp_preauth import normalize_mcp_remote_tokens

    out = normalize_mcp_remote_tokens(
        {
            "access_token": "at",
            "refresh_token": "rt",
            "expires_in": "3600",
        }
    )
    assert out["access_token"] == "at"
    assert out["refresh_token"] == "rt"
    assert out["token_type"] == "Bearer"
    assert out["expires_in"] == 3600
