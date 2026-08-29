"""Help articles: the writing side and the reading side.

Two audiences with opposite defaults. An operator writing one wants to see
drafts, reorder them, and change their minds; a customer searching must never
see anything nobody decided to publish. So every function here says which of
the two it is for, and the public ones filter on `is_published` themselves
rather than trusting a caller to remember.

The search is the same case-folding problem the ticket search has, and it is
solved the same way — through src/database/dialect.py, so PostgreSQL uses
ILIKE and SQLite uses the registered function. A help centre whose search
cannot find "Ökonomie" from "ökonomie" is a help centre people stop using
after the second try.
"""

from __future__ import annotations

import logging
import re
import unicodedata

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.database.db import db_session, settings as _db_settings
from src.database.dialect import contains_ci
from src.database.models import Article

logger = logging.getLogger(__name__)

#: Long enough for a real title, short enough to stay a readable URL.
_MAX_SLUG = 120


def _contains(column, needle: str):
    return contains_ci(column, needle, url=_db_settings.database_url)


def slugify(title: str) -> str:
    """A URL-safe stem from a title, in any alphabet.

    Transliteration is deliberately not attempted. "Как сбросить пароль"
    becoming "kak-sbrosit-parol" requires a table per language and gets
    Ukrainian subtly wrong; percent-encoded Cyrillic in a URL is ugly and
    correct, and browsers render it back as the words. What matters is that
    the slug is stable and unique, not that an English speaker can read it.
    """
    text = unicodedata.normalize("NFKC", (title or "").strip().lower())
    text = re.sub(r"[\s_]+", "-", text)
    # Anything that would need escaping in a path segment, and the characters
    # that make a slug ambiguous when it is read aloud.
    text = re.sub(r"[^\w\-]+", "", text, flags=re.UNICODE)
    text = re.sub(r"-{2,}", "-", text).strip("-")
    return text[:_MAX_SLUG] or "article"


async def _free_slug(session: AsyncSession, wanted: str, *, exclude: int | None = None) -> str:
    """`wanted`, or `wanted-2`, `wanted-3`… until nothing else has it.

    The unique index is the real guard; this is what stops an operator being
    shown a database error because two articles happened to be called
    "Billing".
    """
    stem = wanted
    for attempt in range(1, 50):
        candidate = stem if attempt == 1 else f"{stem[:_MAX_SLUG - 4]}-{attempt}"
        clash = select(Article.id).where(Article.slug == candidate)
        if exclude is not None:
            clash = clash.where(Article.id != exclude)
        if (await session.execute(clash)).first() is None:
            return candidate
    # Fifty articles with one title is not a naming collision, it is a script.
    raise RuntimeError(f"could not find a free slug for {wanted!r}")


# ---------------------------------------------------------------- writing


async def create(*, title: str, body: str, summary: str = "",
                 category: str | None = None, is_published: bool = False,
                 created_by: int = 0) -> Article:
    async with db_session() as session:
        article = Article(
            slug=await _free_slug(session, slugify(title)),
            title=(title or "").strip()[:200],
            summary=(summary or "").strip()[:300],
            body=body or "",
            category=(category or None),
            is_published=bool(is_published),
            created_by=created_by,
        )
        session.add(article)
        await session.flush()
        await session.commit()
        logger.info(f"Article #{article.id} created ({article.slug})")
        return article


async def update(article_id: int, **fields) -> bool:
    """Change an article. The slug follows the title, but only while nobody
    could have linked to it yet.

    A published article's slug is frozen: it may be in a customer's bookmarks
    or pasted into a reply somebody sent last month, and quietly breaking
    those to keep a URL tidy is the wrong trade. Drafts have no such history.
    """
    async with db_session() as session:
        article = await session.get(Article, article_id)
        if article is None:
            return False

        was_published = article.is_published
        title = fields.get("title")
        if title is not None:
            article.title = title.strip()[:200]
            if not was_published:
                article.slug = await _free_slug(
                    session, slugify(article.title), exclude=article.id
                )
        for name, cap in (("summary", 300), ("body", None), ("category", 50)):
            if name in fields and fields[name] is not None:
                value = fields[name]
                setattr(article, name, value[:cap] if cap else value)
        if "is_published" in fields:
            article.is_published = bool(fields["is_published"])
        if "sort_order" in fields and fields["sort_order"] is not None:
            article.sort_order = int(fields["sort_order"])

        await session.commit()
        return True


async def delete(article_id: int) -> bool:
    async with db_session() as session:
        article = await session.get(Article, article_id)
        if article is None:
            return False
        await session.delete(article)
        await session.commit()
        logger.info(f"Article #{article_id} deleted")
        return True


async def for_operator(*, q: str = "") -> list[Article]:
    """Everything, drafts included. The writing side."""
    async with db_session() as session:
        stmt = select(Article)
        if q.strip():
            stmt = stmt.where(or_(
                _contains(Article.title, q.strip()),
                _contains(Article.summary, q.strip()),
                _contains(Article.body, q.strip()),
            ))
        stmt = stmt.order_by(Article.sort_order, Article.title)
        return list((await session.execute(stmt)).scalars().all())


async def by_id(article_id: int) -> Article | None:
    async with db_session() as session:
        return await session.get(Article, article_id)


# ---------------------------------------------------------------- reading


async def published(*, limit: int = 20) -> list[Article]:
    """What a customer sees before they search for anything."""
    async with db_session() as session:
        return list((await session.execute(
            select(Article)
            .where(Article.is_published == True)  # noqa: E712 — SQL, not Python
            .order_by(Article.sort_order, Article.title)
            .limit(limit)
        )).scalars().all())


async def search(q: str, *, limit: int = 12) -> list[Article]:
    """Published articles matching `q`.

    Title matches first: somebody typing "refund" wants the article called
    Refunds above one that mentions the word in its ninth paragraph. Two
    queries rather than one clever ranking expression, because the clever
    expression would have to be written twice — once per engine — and this
    does not.
    """
    q = (q or "").strip()
    if not q:
        return []

    async with db_session() as session:
        by_title = list((await session.execute(
            select(Article)
            .where(Article.is_published == True,  # noqa: E712
                   or_(_contains(Article.title, q), _contains(Article.summary, q)))
            .order_by(Article.sort_order, Article.title)
            .limit(limit)
        )).scalars().all())

        if len(by_title) >= limit:
            return by_title

        seen = {a.id for a in by_title}
        by_body = (await session.execute(
            select(Article)
            .where(Article.is_published == True,  # noqa: E712
                   _contains(Article.body, q))
            .order_by(Article.sort_order, Article.title)
            .limit(limit)
        )).scalars().all()
        return by_title + [a for a in by_body if a.id not in seen][:limit - len(by_title)]


async def read(slug: str) -> Article | None:
    """One published article, and the fact that somebody read it.

    The view count is the only evidence an instance ever collects about
    whether writing an article was worth the afternoon, so it is incremented
    here rather than left to a caller who might forget. Counted with an
    UPDATE rather than read-modify-write: two customers opening the same
    article at once should be two.
    """
    async with db_session() as session:
        article = (await session.execute(
            select(Article).where(
                Article.slug == (slug or "").strip(),
                Article.is_published == True,  # noqa: E712
            )
        )).scalar_one_or_none()
        if article is None:
            return None
        await session.execute(
            Article.__table__.update()
            .where(Article.id == article.id)
            .values(view_count=Article.view_count + 1)
        )
        await session.commit()
        # The row was loaded before the UPDATE, so the object in hand is one
        # behind what the database now holds. Anything showing the count would
        # have shown zero on the first read, forever.
        await session.refresh(article)
        return article


#: Words this short carry no signal and match half the help centre.
_MIN_TERM = 4
#: More than this and the search costs more than the answer is worth.
_MAX_TERMS = 8


def _terms(question: str) -> list[str]:
    """The words worth looking up out of a customer's message."""
    seen: list[str] = []
    for word in re.findall(r"\w{%d,}" % _MIN_TERM, (question or "").lower(), re.UNICODE):
        if word not in seen:
            seen.append(word)
        if len(seen) >= _MAX_TERMS:
            break
    return seen


async def candidates_for(question: str, *, category: str | None = None,
                         limit: int = 4) -> list[Article]:
    """Published articles that might answer this, best first.

    Ranked by how many of the question's words each one matched, with the
    ticket's category breaking ties. Nothing here decides anything — the
    model reads them and judges — so a rough ranking that surfaces the right
    four is the whole requirement.
    """
    terms = _terms(question)
    if not terms:
        return []

    hits: dict[int, int] = {}
    found: dict[int, Article] = {}

    async with db_session() as session:
        for term in terms:
            rows = (await session.execute(
                select(Article)
                .where(Article.is_published == True,  # noqa: E712
                       or_(_contains(Article.title, term),
                           _contains(Article.summary, term),
                           _contains(Article.body, term)))
                .limit(limit * 3)
            )).scalars().all()
            for article in rows:
                found[article.id] = article
                hits[article.id] = hits.get(article.id, 0) + 1

    ranked = sorted(
        found.values(),
        key=lambda a: (
            -hits[a.id],
            # A ticket filed under Billing is more likely answered by a
            # Billing article, all else being equal.
            a.category != category,
            a.sort_order,
        ),
    )
    return ranked[:limit]


async def credit_deflection(article_id: int) -> None:
    """Record that an answer built from this article ended a conversation.

    Only ever called when the person who asked said it solved their problem.
    Counting offers instead would make every article look useful, which is
    the same as measuring nothing.
    """
    if not article_id:
        return
    async with db_session() as session:
        await session.execute(
            Article.__table__.update()
            .where(Article.id == article_id)
            .values(deflected_count=Article.deflected_count + 1)
        )
        await session.commit()


async def count_published() -> int:
    """Whether the help centre has anything in it.

    The widget asks before drawing a Help tab: a tab leading to an empty
    search box is worse than no tab, because it looks like the search is
    broken rather than like nothing has been written yet.
    """
    async with db_session() as session:
        return int((await session.execute(
            select(func.count(Article.id)).where(Article.is_published == True)  # noqa: E712
        )).scalar() or 0)
