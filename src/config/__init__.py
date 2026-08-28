"""Application configuration utilities."""

from dataclasses import dataclass
import logging
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


logger = logging.getLogger(__name__)


@dataclass
class Settings:
    """Typed container for environment-driven configuration."""

    database_url: str
    access_password: str
    # Telegram is optional, like Gemini. Absent, the dashboard still runs and
    # the website widget still takes tickets — there is simply no Telegram
    # channel and no admin group to mirror into. The bot process is the one
    # thing that cannot start without it.
    bot_token: str = ""
    admin_chat_id: int = 0
    gemini_api_key: str | None = None
    gemini_model: str = "gemini-2.5-flash"
    #: How many reverse proxies sit in front of this process.
    #:
    #: Zero — the default — means X-Forwarded-For is ignored entirely, which
    #: is correct for anything reachable directly: the header is then just a
    #: string a stranger chose, and believing it hands them a new identity per
    #: request for every limit keyed on an address.
    #:
    #: Behind one proxy, set 1. Proxies *append* rather than replace, so the
    #: trustworthy entry is the Nth from the right — the one your own proxy
    #: wrote — and everything to the left of it is whatever the client sent.
    trusted_proxy_hops: int = 0

    @property
    def has_telegram(self) -> bool:
        """Both halves or neither.

        A token with no admin chat can send to customers but has nowhere to
        show operators a ticket, which is an instance that looks configured
        and quietly loses half its job.
        """
        return bool(self.bot_token and self.admin_chat_id)


def _require_env(name: str) -> str:
    """Fetch a required environment variable or raise a descriptive error."""

    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Environment variable '{name}' is required but missing.")
    return value


def load_settings() -> Settings:
    """Create a Settings instance with validated environment variables.

    Only the database and the first-login password are required. GEMINI_API_KEY
    absent means AI features stay off; BOT_TOKEN absent means there is no
    Telegram channel, and an instance serving only the website widget needs
    neither.
    """

    database_url = _require_env("DATABASE_URL")
    access_password = _require_env("ACCESS_PASSWORD")

    try:
        hops = max(0, int((os.getenv("TRUSTED_PROXY_HOPS") or "0").strip() or 0))
    except ValueError:
        # A typo here would otherwise fail open, and failing open on this
        # setting means the rate limits quietly stop working.
        logger.warning("TRUSTED_PROXY_HOPS is not a number; assuming no proxy")
        hops = 0

    bot_token = (os.getenv("BOT_TOKEN") or "").strip()
    raw_chat = (os.getenv("ADMIN_CHAT_ID") or "").strip()
    try:
        admin_chat_id = int(raw_chat) if raw_chat else 0
    except ValueError:
        # A malformed id is a typo, not a decision to run without Telegram —
        # but failing to start over it would take the dashboard down too.
        logger.warning(f"ADMIN_CHAT_ID is not a number ({raw_chat!r}); Telegram is off")
        admin_chat_id = 0

    if bot_token and not admin_chat_id:
        logger.warning(
            "BOT_TOKEN is set but ADMIN_CHAT_ID is not — Telegram stays off. "
            "Group ids are negative; copy it exactly."
        )

    gemini_api_key = os.getenv("GEMINI_API_KEY") or None
    gemini_model = os.getenv("GEMINI_MODEL") or "gemini-2.5-flash"

    return Settings(
        database_url=database_url,
        access_password=access_password,
        bot_token=bot_token,
        admin_chat_id=admin_chat_id,
        gemini_api_key=gemini_api_key,
        gemini_model=gemini_model,
        trusted_proxy_hops=hops,
    )
