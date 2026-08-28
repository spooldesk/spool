"""The handful of places where SQLite and PostgreSQL are not the same thing.

Almost none of this application cares which database it is on: it is written
against the SQLAlchemy ORM, and there are three raw statements outside the
migrations. This module holds what is left, so that "which engine is this"
is a question asked in one file instead of scattered through queries.

Two differences do real work.

**Case-insensitive search.** SQLite's `LIKE` and `lower()` fold ASCII only,
so "принтер" would not match "Принтер" — which for a product whose first
users write in Russian is not an edge case, it is the feature. SQLite
therefore gets a Python function registered on each connection, doing a
proper Unicode casefold. PostgreSQL already has `ILIKE`, which folds Unicode
correctly, so it uses that and no function is registered.

**Migrations.** `_migrate_to_new_schema` in db.py is a long list of additive
`ALTER TABLE`s that bring a *legacy SQLite file* up to the current schema.
A PostgreSQL database has no legacy: `create_all` builds it correct on the
first run. So those statements are skipped there rather than translated,
which is both less work and more honest about what they are.

That leaves a real limitation worth naming: there is no migration framework
here. Schema changes have so far been additive columns applied to SQLite in
place. A PostgreSQL deployment that needs to move between two releases with
a schema change will need Alembic, and adding it is a separate piece of work
from making the engine run.
"""

from __future__ import annotations

from sqlalchemy import func


def name_of(url: str) -> str:
    """The dialect a URL names, without connecting to anything."""
    scheme = (url or "").split(":", 1)[0].lower()
    base = scheme.split("+", 1)[0]
    # Both spellings appear in the wild and mean the same engine; SQLAlchemy
    # itself accepts either.
    return "postgresql" if base in ("postgres", "postgresql") else base


def is_postgres(url: str) -> bool:
    return name_of(url) == "postgresql"


def is_sqlite(url: str) -> bool:
    return name_of(url) == "sqlite"


#: The escape character for LIKE/ILIKE patterns below. Backslash rather than
#: the default, because the default *is* backslash in PostgreSQL but relying
#: on that silently changes meaning under `standard_conforming_strings`.
_ESCAPE = "\\"


def _as_pattern(needle: str) -> str:
    """A literal substring, made safe to put inside an ILIKE pattern.

    Without this a customer searching for `50%` matches every ticket, and one
    searching for `a_b` matches `axb`. Wildcards in user input are a bug, not
    a feature: the search box promises "contains", so it has to mean it.
    """
    for special in (_ESCAPE, "%", "_"):
        needle = needle.replace(special, _ESCAPE + special)
    return f"%{needle}%"


def contains_ci(column, needle: str, *, url: str):
    """`column` contains `needle`, ignoring case, in either engine.

    Returns a SQLAlchemy expression. The two branches are not
    interchangeable implementations of one idea — they are the same promise
    kept by different means, and the SQLite one exists only because SQLite
    cannot keep it natively.
    """
    if is_postgres(url):
        return column.ilike(_as_pattern(needle), escape=_ESCAPE)
    # The registered function returns 1/0 rather than a boolean because
    # SQLite has no boolean type to return.
    return func.ci_contains(column, needle) == 1
