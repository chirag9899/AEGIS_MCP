"""Tests for fingerprint-based device key reuse."""

import json


def test_register_pending_device_reuses_same_fingerprint(monkeypatch, tmp_path):
    registry = tmp_path / "device_key_bindings.json"
    monkeypatch.setenv("WORKSPACE_MCP_CREDENTIALS_DIR", str(tmp_path))

    from auth import device_key_registry as dkr

    first = dkr.register_pending_device("fp-sam-mac", "Sam-Mac")
    second = dkr.register_pending_device("fp-sam-mac", "Sam-Mac")

    assert first["key"] == second["key"]
    assert second.get("reused") is True

    saved = json.loads(registry.read_text(encoding="utf-8"))
    pending = saved["_pending"]
    matching = [p for p in pending if p.get("fingerprint") == "fp-sam-mac"]
    assert len(matching) == 1
