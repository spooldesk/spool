import pytest
from sqlalchemy.engine import make_url

from src.database.connection_url import database_url


@pytest.mark.parametrize("password", ["plain-password", "p@ss/word:#%+?", " leading and trailing "])
def test_split_postgres_credentials_roundtrip_reserved_characters(password):
    resolved = make_url(database_url({
        "DATABASE_URL": "sqlite+aiosqlite:///wrong.db", "POSTGRES_HOST": "postgres",
        "POSTGRES_USER": "operator@team", "POSTGRES_DB": "support/desk",
        "POSTGRES_PASSWORD": password,
    }))
    assert (resolved.host, resolved.port, resolved.username, resolved.password, resolved.database) == (
        "postgres", 5432, "operator@team", password, "support/desk")


@pytest.mark.parametrize("url", ["sqlite+aiosqlite:///local.db", "postgresql+asyncpg://u:p@remote/db"])
def test_explicit_database_urls_do_not_require_compose_credentials(url):
    assert database_url({"DATABASE_URL": url, "POSTGRES_PASSWORD": "unused"}) == url


@pytest.mark.parametrize("port", ["0", "65536", "not-a-port"])
def test_invalid_split_credentials_fail_before_connecting_without_echoing_password(port):
    with pytest.raises(ValueError) as exc:
        database_url({"POSTGRES_HOST": "postgres", "POSTGRES_PORT": port,
                      "POSTGRES_PASSWORD": "PRIVATE-PASSWORD"})
    assert "PRIVATE-PASSWORD" not in str(exc.value)


def test_host_requires_password():
    with pytest.raises(ValueError, match="POSTGRES_PASSWORD"):
        database_url({"POSTGRES_HOST": "postgres"})


def test_application_uses_the_same_resolved_database_url(monkeypatch):
    from src.config import load_settings
    monkeypatch.setenv("POSTGRES_HOST", "postgres")
    monkeypatch.setenv("POSTGRES_PASSWORD", "p@ss/w#rd")
    assert make_url(load_settings().database_url).password == "p@ss/w#rd"
