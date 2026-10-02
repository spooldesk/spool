"""Application configuration utilities."""

from dataclasses import dataclass
import logging
import os
from pathlib import Path

from dotenv import load_dotenv
from src.database.connection_url import database_url
from src.config.channel_values import get as channel_value


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

    # ---- Email channel --------------------------------------------------
    #: The support mailbox: the From address, and usually the login too.
    email_address: str = ""
    email_password: str = ""
    email_imap_host: str = ""
    email_imap_port: int = 993
    email_smtp_host: str = ""
    email_smtp_port: int = 587
    #: When the login differs from the address, which some hosts insist on.
    email_login: str = ""
    #: What a customer sees in their inbox next to the message.
    email_from_name: str = "Support"
    email_subject: str = "Re: your support request"

    @property
    def has_email(self) -> bool:
        """All four load-bearing parts, or none of it.

        Half a mailbox is the failure worth designing against: SMTP without
        IMAP answers mail nobody can send it, and IMAP without SMTP collects
        tickets it cannot reply to. Both look configured from the outside.
        """
        return bool(
            self.email_address and self.email_password
            and self.email_imap_host and self.email_smtp_host
        )

    @property
    def email_user(self) -> str:
        """What to log in as. The address, unless the host wants otherwise."""
        return self.email_login or self.email_address

    @property
    def has_telegram(self) -> bool:
        """A bot token enables Telegram; operators may work entirely in the dashboard."""
        return bool(self.bot_token)


class _LiveSettings:
    """Legacy handlers read current channel settings without freezing import-time values."""
    def __getattr__(self, name):
        return getattr(load_settings(), name)


runtime_settings = _LiveSettings()


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

    try:
        resolved_database_url = database_url(os.environ)
    except ValueError as exc:
        raise RuntimeError(str(exc)) from None
    access_password = _require_env("ACCESS_PASSWORD")

    try:
        hops = max(0, int((os.getenv("TRUSTED_PROXY_HOPS") or "0").strip() or 0))
    except ValueError:
        # A typo here would otherwise fail open, and failing open on this
        # setting means the rate limits quietly stop working.
        logger.warning("TRUSTED_PROXY_HOPS is not a number; assuming no proxy")
        hops = 0

    bot_token = channel_value("BOT_TOKEN").strip()
    raw_chat = channel_value("ADMIN_CHAT_ID").strip()
    try:
        admin_chat_id = int(raw_chat) if raw_chat else 0
    except ValueError:
        # A malformed id is a typo, not a decision to run without Telegram —
        # but failing to start over it would take the dashboard down too.
        logger.warning("ADMIN_CHAT_ID is not a number; operator-group mirroring is off")
        admin_chat_id = 0

    def _port(name: str, fallback: int) -> int:
        raw = channel_value(name).strip()
        try:
            return int(raw) if raw else fallback
        except ValueError:
            logger.warning(f"{name} is not a number ({raw!r}); using {fallback}")
            return fallback

    email_address = channel_value("EMAIL_ADDRESS").strip()
    email_password = channel_value("EMAIL_PASSWORD")
    email_imap_host = channel_value("EMAIL_IMAP_HOST").strip()
    email_smtp_host = channel_value("EMAIL_SMTP_HOST").strip()

    # Said once, at load, rather than discovered when the first customer
    # writes in and nothing happens.
    if email_address and not (email_password and email_imap_host and email_smtp_host):
        logger.warning(
            "EMAIL_ADDRESS is set but the mailbox is incomplete — email stays "
            "off. It needs EMAIL_PASSWORD, EMAIL_IMAP_HOST and EMAIL_SMTP_HOST."
        )

    gemini_api_key = os.getenv("GEMINI_API_KEY") or None
    gemini_model = os.getenv("GEMINI_MODEL") or "gemini-2.5-flash"

    return Settings(
        database_url=resolved_database_url,
        access_password=access_password,
        bot_token=bot_token,
        admin_chat_id=admin_chat_id,
        gemini_api_key=gemini_api_key,
        gemini_model=gemini_model,
        trusted_proxy_hops=hops,
        email_address=email_address,
        email_password=email_password,
        email_imap_host=email_imap_host,
        email_imap_port=_port("EMAIL_IMAP_PORT", 993),
        email_smtp_host=email_smtp_host,
        email_smtp_port=_port("EMAIL_SMTP_PORT", 587),
        email_login=channel_value("EMAIL_LOGIN").strip(),
        email_from_name=channel_value("EMAIL_FROM_NAME", "Support").strip(),
        email_subject=channel_value("EMAIL_SUBJECT", "Re: your support request").strip(),
    )
