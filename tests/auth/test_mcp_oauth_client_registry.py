"""Tests for stable MCP OAuth client registry."""

import json

import pytest


def test_get_or_create_stable_oauth_client_reuses_same_fingerprint(monkeypatch, tmp_path):
    registry = tmp_path / "device_key_bindings.json"
    monkeypatch.setenv("WORKSPACE_MCP_CREDENTIALS_DIR", str(tmp_path))

    from auth.mcp_oauth_client_registry import get_or_create_stable_oauth_client

    first = get_or_create_stable_oauth_client("fp-abc", label="test-mac")
    second = get_or_create_stable_oauth_client("fp-abc", label="test-mac")

    assert first["client_id"] == second["client_id"]
    assert first["client_secret"] == second["client_secret"]

    saved = json.loads(registry.read_text(encoding="utf-8"))
    assert saved["_oauth_clients"]["fp-abc"]["client_id"] == first["client_id"]
