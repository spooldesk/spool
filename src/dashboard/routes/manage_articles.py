"""Web admin: the help centre's writing side.

Everything a customer can read is created here, which makes publishing the
only irreversible-feeling action on the page — an article goes from something
one person is drafting to something the internet can fetch by slug. So it is
a deliberate switch rather than a side effect of saving, and the list says
plainly which state each article is in.

The reading side is src/services/article_service.py's published-only
functions, reached by the widget. Nothing here is public.
"""

from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from src.dashboard.i18n import t as tr
from src.database.db import db_session
from src.services import article_service

router = APIRouter()


async def _categories() -> list[str]:
    """The same list the bot offers, so a help centre and a queue agree about
    what the subjects are."""
    from src.services import catalog_service

    async with db_session() as session:
        return [""] + [c.name for c in await catalog_service.list_active(session)]


def _row(article) -> dict:
    return {
        "id": article.id,
        "slug": article.slug,
        "title": article.title,
        "summary": article.summary,
        "category": article.category or "",
        "is_published": article.is_published,
        "views": article.view_count,
        "deflected": article.deflected_count,
        "updated_at": article.updated_at,
    }


@router.get("/manage/articles", response_class=HTMLResponse)
async def articles_list(request: Request, q: str = "", sent: str | None = None):
    templates = request.app.state.templates
    rows = await article_service.for_operator(q=q)
    return templates.TemplateResponse(request, "articles_manage.html", {
        "active_page": "articles",
        "items": [_row(a) for a in rows],
        "q": q,
        "published_count": sum(1 for a in rows if a.is_published),
        "flash_sent": sent,
    })


@router.get("/manage/articles/new", response_class=HTMLResponse)
async def article_new(request: Request):
    templates = request.app.state.templates
    return templates.TemplateResponse(request, "article_form.html", {
        "active_page": "articles",
        "article": None,
        "categories": await _categories(),
    })


@router.post("/manage/articles/new")
async def article_create(
    title: str = Form(...),
    body: str = Form(...),
    summary: str = Form(""),
    category: str = Form(""),
    is_published: str = Form("0"),
):
    if not title.strip() or not body.strip():
        return RedirectResponse(
            f"/manage/articles/new?err={quote(tr('e_empty_message'))}", status_code=303)

    article = await article_service.create(
        title=title, body=body, summary=summary,
        category=category or None, is_published=is_published == "1",
    )
    return RedirectResponse(
        f"/manage/articles?sent=created#a{article.id}", status_code=303)


@router.get("/manage/articles/{article_id}/edit", response_class=HTMLResponse)
async def article_edit(article_id: int, request: Request):
    article = await article_service.by_id(article_id)
    if article is None:
        return RedirectResponse("/manage/articles", status_code=303)

    templates = request.app.state.templates
    return templates.TemplateResponse(request, "article_form.html", {
        "active_page": "articles",
        "article": _row(article) | {"body": article.body},
        "categories": await _categories(),
    })


@router.post("/manage/articles/{article_id}/edit")
async def article_save(
    article_id: int,
    title: str = Form(...),
    body: str = Form(...),
    summary: str = Form(""),
    category: str = Form(""),
    is_published: str = Form("0"),
):
    await article_service.update(
        article_id, title=title, body=body, summary=summary,
        category=category or None, is_published=is_published == "1",
    )
    return RedirectResponse(
        f"/manage/articles?sent=saved#a{article_id}", status_code=303)


@router.post("/manage/articles/{article_id}/publish")
async def article_publish(article_id: int, publish: str = Form("1")):
    """The one switch that changes who can see this.

    Its own endpoint rather than a checkbox on the edit form, so publishing
    from the list is one click and does not require re-submitting a body
    somebody may be halfway through rewriting.
    """
    await article_service.update(article_id, is_published=publish == "1")
    return RedirectResponse(
        f"/manage/articles?sent={'published' if publish == '1' else 'hidden'}"
        f"#a{article_id}", status_code=303)


@router.post("/manage/articles/{article_id}/delete")
async def article_delete(article_id: int):
    await article_service.delete(article_id)
    return RedirectResponse("/manage/articles?sent=deleted", status_code=303)
