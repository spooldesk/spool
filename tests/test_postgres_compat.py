"""What can be checked about PostgreSQL without a PostgreSQL.

The real proof is the CI job that runs this whole suite a second time against
a live server; nothing here replaces it, and it is not pretending to. What
these tests do is catch the two kinds of breakage that are otherwise found
only by starting a server — a column type that will not render, and a query
that silently keeps its SQLite-only form — on any machine, in the normal run,
without anyone having to install anything.

SQLAlchemy can compile DDL and statements for a dialect it is not connected
to. That is the whole trick.
"""

import re
from pathlib import Path

import pytest
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.schema import CreateTable

from src.database import models
from src.database.dialect import (
    _as_pattern,
    contains_ci,
    is_postgres,
    is_sqlite,
    name_of,
)

PG = "postgresql+asyncpg://u:p@localhost:5432/spooldesk"
LITE = "sqlite+aiosqlite:///./support.db"


# --------------------------------------------------------------- the schema


@pytest.mark.parametrize(
    "table", list(models.Base.metadata.tables.values()), ids=lambda t: t.name
)
def test_every_table_renders_for_postgres(table):
    """A column type SQLAlchemy cannot express in PostgreSQL fails here rather
    than on somebody's first `docker compose up`."""
    ddl = str(CreateTable(table).compile(dialect=postgresql.dialect()))
    assert ddl.startswith("\nCREATE TABLE") or ddl.startswith("CREATE TABLE")
    assert table.name in ddl


def test_the_schema_still_renders_for_sqlite_too():
    """Support for a second engine that quietly drops the first is not
    support for two engines."""
    for table in models.Base.metadata.tables.values():
        assert "CREATE TABLE" in str(
            CreateTable(table).compile(dialect=sqlite.dialect())
        )


# ---------------------------------------------------------------- the search


def test_search_uses_ilike_on_postgres_and_the_function_on_sqlite():
    """The one query in the application that cannot be written once.

    SQLite's LIKE folds ASCII only, so "принтер" would not find "Принтер" —
    which for this product's first users is the feature, not an edge case.
    SQLite gets a Python function; PostgreSQL has ILIKE and needs none.
    """
    on_pg = str(contains_ci(models.User.username, "Принтер", url=PG).compile(
        dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
    assert "ILIKE" in on_pg.upper()
    assert "ci_contains" not in on_pg

    on_lite = str(contains_ci(models.User.username, "Принтер", url=LITE).compile(
        dialect=sqlite.dialect(), compile_kwargs={"literal_binds": True}))
    assert "ci_contains" in on_lite


@pytest.mark.parametrize("needle,pattern", [
    ("50%", r"%50\%%"),
    ("a_b", r"%a\_b%"),
    ("back\\slash", r"%back\\slash%"),
    ("plain", "%plain%"),
])
def test_wildcards_in_a_search_are_escaped(needle, pattern):
    """A customer searching for `50%` must not match every ticket they have.

    The search box says "contains", so a `%` typed into it is a per cent sign
    and `_` is an underscore. Asserted on the pattern rather than on rendered
    SQL, because the renderer doubles both `%` and backslashes for the
    paramstyle and the result says more about SQLAlchemy than about us.

    This only matters on PostgreSQL: the SQLite path hands the needle to a
    Python function, where it was never a pattern to begin with.
    """
    assert _as_pattern(needle) == pattern


def test_the_escape_character_is_declared_in_the_sql():
    """An escape that the database has not been told about is not an escape.
    PostgreSQL's default happens to be backslash, but that depends on
    `standard_conforming_strings`, and depending on it silently is how this
    would break on somebody else's server."""
    rendered = str(contains_ci(models.User.username, "50%", url=PG).compile(
        dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
    assert "ESCAPE" in rendered.upper()


# ------------------------------------------------------------ the URL itself


@pytest.mark.parametrize("url,dialect", [
    ("postgresql+asyncpg://u:p@h/db", "postgresql"),
    ("postgresql://u:p@h/db", "postgresql"),
    # Both spellings are in the wild and mean the same engine; a deployment
    # that used the short one should not silently get the SQLite branches.
    ("postgres://u:p@h/db", "postgresql"),
    ("sqlite+aiosqlite:///./x.db", "sqlite"),
    ("sqlite:///./x.db", "sqlite"),
])
def test_the_url_names_the_engine(url, dialect):
    assert name_of(url) == dialect
    assert is_postgres(url) is (dialect == "postgresql")
    assert is_sqlite(url) is (dialect == "sqlite")


# ------------------------------------------------------------- the seed SQL


def test_no_sqlite_only_sql_is_left_in_the_seeds():
    """The seeds run on a brand-new database of either kind, and they are
    wrapped in `except` clauses.

    That combination is why this test exists rather than a comment: with
    `datetime('now')` in them they raised on PostgreSQL and said nothing, so a
    fresh install came up with no categories, no close reasons and nobody able
    to sign in — looking, from the outside, exactly like it had worked.
    """
    source = Path("src/database/db.py").read_text(encoding="utf-8")
    seeds = source[source.index("def _init_teams"):]

    assert "datetime('now')" not in seeds, (
        "a SQLite date function in a seed; bind a Python datetime instead"
    )
    # `VALUES (..., 1, ...)` for a boolean column: PostgreSQL will not take an
    # integer there. Bound Python booleans render correctly in both.
    for statement in re.findall(r"VALUES \(([^)]*)\)", seeds):
        assert not re.search(r"(^|,)\s*[01]\s*(,|$)", statement), (
            f"integer literal in a seed VALUES list: {statement!r} — "
            f"PostgreSQL needs a real boolean"
        )
