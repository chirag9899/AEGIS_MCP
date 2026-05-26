import os

from core.env_loader import load_project_dotenv


def test_load_project_dotenv_clears_optional_key_when_commented(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "WORKSPACE_MCP_PORT=8010\n"
        "# WORKSPACE_MCP_ALLOWED_USER_EMAILS=chirag@infrasingularity.com\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("WORKSPACE_MCP_ALLOWED_USER_EMAILS", "chirag@infrasingularity.com")

    load_project_dotenv(env_file)

    assert os.environ["WORKSPACE_MCP_PORT"] == "8010"
    assert "WORKSPACE_MCP_ALLOWED_USER_EMAILS" not in os.environ


def test_load_project_dotenv_applies_latest_value_from_file(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "WORKSPACE_MCP_ALLOWED_USER_EMAILS=jitin@infrasingularity.com\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("WORKSPACE_MCP_ALLOWED_USER_EMAILS", "chirag@infrasingularity.com")

    load_project_dotenv(env_file)

    assert os.environ["WORKSPACE_MCP_ALLOWED_USER_EMAILS"] == "jitin@infrasingularity.com"
