"""Screenshot fixtures cannot inherit credentials or replace a user's database."""

import os
import sqlite3
import sys
from unittest.mock import Mock

import pytest

from scripts import demo_environment, run_demo_dashboard


def test_demo_environment_discards_inherited_integrations_and_paths(tmp_path, monkeypatch):
    environment = {name: "inherited-sentinel" for name in (
        "BOT_TOKEN", "ADMIN_CHAT_ID", "ACCESS_PASSWORD", "DATABASE_URL",
        "EMAIL_ADDRESS", "EMAIL_PASSWORD", "EMAIL_IMAP_HOST", "EMAIL_SMTP_HOST",
        "SLACK_BOT_TOKEN", "SLACK_SIGNING_SECRET", "GEMINI_API_KEY", "GEMINI_MODEL",
        "OPENAI_API_KEY", "OPENAI_BASE_URL", "ANTHROPIC_API_KEY", "OLLAMA_HOST",
        "DASH_SECRET_KEY", "SECRETS_KEY", "ATTACHMENTS_DIR", "DASH_COOKIE_SECURE",
        "TRUSTED_PROXY_HOPS", "POSTGRES_HOST", "POSTGRES_PASSWORD", "POSTGRES_USER",
    )}
    monkeypatch.setattr(os, "environ", environment)
    demo_environment.configure(tmp_path / "demo.db")
    assert "inherited-sentinel" not in environment.values()
    assert environment["PYTHON_DOTENV_DISABLED"] == "1"
    assert environment["BOT_TOKEN"] == "" and environment["ADMIN_CHAT_ID"] == "0"
    assert environment["ACCESS_PASSWORD"] == "admin"
    assert environment["DASH_COOKIE_SECURE"] == "false"
    assert environment["TRUSTED_PROXY_HOPS"] == "0"
    assert environment["DATABASE_URL"].endswith("demo.db")
    assert environment["POSTGRES_HOST"] == "" and "POSTGRES_PASSWORD" not in environment
    assert environment["ATTACHMENTS_DIR"] == str(tmp_path / "data" / "demo-demo")
    assert not any(name.startswith(("EMAIL_", "SLACK_", "GEMINI_", "OPENAI_",
                                    "ANTHROPIC_", "OLLAMA_")) for name in environment)
    from dotenv import load_dotenv
    dotenv_file = tmp_path / ".env"
    dotenv_file.write_text("EMAIL_PASSWORD=must-not-load\n", encoding="utf-8")
    assert load_dotenv(dotenv_file, override=True) is False
    assert "EMAIL_PASSWORD" not in environment


def test_seed_refuses_existing_custom_database_and_production_filename(tmp_path):
    existing = tmp_path / "customer.sqlite"
    existing.write_bytes(b"do not replace")
    for path in (existing, tmp_path / "SUPPORT.DB", tmp_path / "support.db?mode=.db"):
        with pytest.raises(ValueError):
            demo_environment.database_path(str(path), rebuild=True)
    assert existing.read_bytes() == b"do not replace"
    assert not (tmp_path / "SUPPORT.DB").exists()
    assert demo_environment.database_path(str(tmp_path / "new.db"), rebuild=True) == tmp_path / "new.db"


def _fixture_database(path, *, marked):
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE app_settings(key TEXT PRIMARY KEY,value TEXT,updated_at TEXT)")
        conn.execute("INSERT INTO app_settings VALUES('ai_key_openai','old-demo-key',NULL)")
        if marked:
            conn.execute("INSERT INTO app_settings VALUES('demo_fixture','1',NULL)")


def test_demo_launcher_pins_offline_settings_before_server_import(tmp_path, monkeypatch):
    import uvicorn

    path = tmp_path / "demo.db"
    _fixture_database(path, marked=True)
    monkeypatch.setattr(os, "environ", {"BOT_TOKEN": "inherited-sentinel"})
    monkeypatch.setattr(sys, "argv", ["run_demo_dashboard.py", "--database", str(path)])
    run = Mock()
    monkeypatch.setattr(uvicorn, "run", run)
    run_demo_dashboard.main()
    run.assert_called_once_with("src.dashboard.app:app", host="127.0.0.1", port=8010)
    assert os.environ["BOT_TOKEN"] == "" and os.environ["PYTHON_DOTENV_DISABLED"] == "1"
    with sqlite3.connect(path) as conn:
        values = dict(conn.execute("SELECT key,value FROM app_settings"))
    assert "ai_key_openai" not in values
    assert values["ai_enabled"] == "0" and values["ai_host"] == ""
    assert values["widget_enabled"] == "1"


def test_demo_launcher_refuses_unmarked_custom_database_without_mutating_it(tmp_path, monkeypatch):
    path = tmp_path / "customer.db"
    _fixture_database(path, marked=False)
    before = path.read_bytes()
    monkeypatch.setattr(sys, "argv", ["run_demo_dashboard.py", "--database", str(path)])
    with pytest.raises(SystemExit):
        run_demo_dashboard.main()
    assert path.read_bytes() == before
