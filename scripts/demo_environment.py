"""Standalone demo configuration; deliberately independent of src.config/.env."""

import os
import secrets
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATABASE = PROJECT_ROOT / "demo_support.db"


def database_path(value: str | None, *, rebuild: bool = False) -> Path:
    candidate = Path(value).expanduser() if value else DEFAULT_DATABASE
    if candidate.is_symlink():
        raise ValueError("The demo database must not be a symbolic link")
    resolved = candidate.resolve()
    # DATABASE_URL must address exactly this file, never treat its tail as
    # URL parameters and accidentally open a differently named database.
    if any(char in str(resolved) for char in ("?", "#")):
        raise ValueError("The demo database path must not contain ? or #")
    if resolved.name.casefold() == "support.db":
        raise ValueError("Refusing to use support.db as a demo database")
    if resolved.suffix.lower() not in {".db", ".sqlite", ".sqlite3"}:
        raise ValueError("Use a SQLite file ending in .db, .sqlite or .sqlite3")
    if rebuild and value and resolved.exists():
        raise ValueError("--database must name a new file; existing databases are never replaced")
    return resolved


def configure(database: Path) -> None:
    """Do not inherit credentials, paths or dotenv from the developer's instance."""
    os.environ["PYTHON_DOTENV_DISABLED"] = "1"
    for name in tuple(os.environ):
        if name.startswith(("EMAIL_", "SLACK_", "GEMINI_", "ANTHROPIC_", "OPENAI_", "OLLAMA_", "POSTGRES_")):
            os.environ.pop(name, None)
    os.environ.update({
        "DATABASE_URL": f"sqlite+aiosqlite:///{database.as_posix()}",
        "POSTGRES_HOST": "",
        "BOT_TOKEN": "", "ADMIN_CHAT_ID": "0", "ACCESS_PASSWORD": "admin",
        "DASH_SECRET_KEY": secrets.token_urlsafe(48),
        "SECRETS_KEY": secrets.token_urlsafe(48),
        "ATTACHMENTS_DIR": str(database.parent / "data" / f"demo-{database.stem}"),
        "DEMO_MODE": "1",
        "DASH_COOKIE_SECURE": os.getenv("DEMO_DASH_COOKIE_SECURE", "false"),
        "TRUSTED_PROXY_HOPS": os.getenv("DEMO_TRUSTED_PROXY_HOPS", "0"),
    })


def pin_offline_settings(database: Path) -> None:
    """Clear inherited demo integrations and restore its public fixture settings."""
    import sqlite3

    with sqlite3.connect(database) as conn:
        conn.execute("DELETE FROM app_settings WHERE key LIKE 'ai_key_%'")
        for key, value in {
            "dash_language": "en", "widget_enabled": "1", "widget_origins": "",
            "ai_enabled": "0", "ai_deflection_enabled": "0", "ai_host": "",
            "widget_greeting": (
                "This is a live demo — write anything and it becomes a real ticket "
                "you can open in the dashboard. The instance is shared, so please "
                "don't type anything private."
            ),
        }.items():
            conn.execute(
                "INSERT INTO app_settings (key,value,updated_at) VALUES (?,?,datetime('now')) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value),
            )


def verify_seed_marker(database: Path) -> None:
    """Custom launch paths must be produced by the demo seed, never a user DB."""
    import sqlite3

    with sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True) as conn:
        row = conn.execute("SELECT value FROM app_settings WHERE key='demo_fixture'").fetchone()
    if row != ("1",):
        raise ValueError("This database is not marked as a demo seed")
