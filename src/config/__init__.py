"""Application configuration utilities."""

from dataclasses import dataclass
import os
from pathlib import Path

from dotenv import load_dotenv


# Load environment variables from a .env file located in the project root.
# Find the project root (directory containing .env file)
# This works whether running as script or as module (python -m src.main)
# __file__ is src/config/__init__.py, so parent.parent.parent is project root
# (parent = src/config, parent.parent = src, parent.parent.parent = project root)
project_root = Path(__file__).parent.parent.parent
env_path = project_root / ".env"

# Load .env file with explicit path to ensure it's found regardless of working directory
if env_path.exists():
    load_dotenv(dotenv_path=env_path, override=False)
else:
    # Fallback: try to find .env file automatically (for backward compatibility)
    load_dotenv()


@dataclass
class Settings:
    """Typed container for environment-driven configuration."""

    bot_token: str
    admin_chat_id: int
    database_url: str
    access_password: str
    gemini_api_key: str | None = None
    gemini_model: str = "gemini-2.5-flash"


def _require_env(name: str) -> str:
    """Fetch a required environment variable or raise a descriptive error."""

    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Environment variable '{name}' is required but missing.")
    return value


def load_settings() -> Settings:
    """Create a Settings instance with validated environment variables.

    GEMINI_API_KEY is optional — if absent, AI features stay disabled and the bot
    keeps working as a plain ticket router.
    """

    bot_token = _require_env("BOT_TOKEN")
    admin_chat_id = int(_require_env("ADMIN_CHAT_ID"))
    database_url = _require_env("DATABASE_URL")
    access_password = _require_env("ACCESS_PASSWORD")
    gemini_api_key = os.getenv("GEMINI_API_KEY") or None
    gemini_model = os.getenv("GEMINI_MODEL") or "gemini-2.5-flash"

    return Settings(
        bot_token=bot_token,
        admin_chat_id=admin_chat_id,
        database_url=database_url,
        access_password=access_password,
        gemini_api_key=gemini_api_key,
        gemini_model=gemini_model,
    )
