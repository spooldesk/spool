"""Resolve database settings without loading dotenv or opening a connection."""
from collections.abc import Mapping

from sqlalchemy.engine import URL


def database_url(config: Mapping[str, str | None]) -> str:
    # Compose passes credentials separately: interpolation into a URL breaks
    # valid passwords containing @, /, # or %. SQLAlchemy escapes these for us.
    host = (config.get("POSTGRES_HOST") or "").strip()
    if host:
        password = config.get("POSTGRES_PASSWORD")
        if not password:
            raise ValueError("POSTGRES_PASSWORD is required with POSTGRES_HOST")
        raw_port = config.get("POSTGRES_PORT") or "5432"
        if not raw_port.isdigit() or not 0 < int(raw_port) < 65536:
            raise ValueError("POSTGRES_PORT must be a valid TCP port")
        return URL.create(
            "postgresql+asyncpg", host=host, port=int(raw_port),
            username=config.get("POSTGRES_USER") or "spooldesk",
            password=password, database=config.get("POSTGRES_DB") or "spooldesk",
        ).render_as_string(hide_password=False)
    url = config.get("DATABASE_URL")
    if not url:
        raise ValueError("Environment variable 'DATABASE_URL' is required but missing.")
    return url
