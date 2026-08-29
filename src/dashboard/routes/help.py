"""The help centre, on the open web.

Everything here was already readable — the widget has fetched articles as
JSON since the help centre was built. What was missing was a URL. An article
that only exists inside a chat panel cannot be pasted into an email reply,
cannot be bookmarked, cannot be found by somebody who has not opened the
widget yet, and cannot be indexed. Deflection is mostly the act of sending
someone a link, so a help centre without links deflects almost nothing.

These pages are deliberately plain: server-rendered, no JavaScript, no
session, no cookie. A documentation page that needs a script to show its
text is a documentation page that fails for the person on a bad connection
who is already having a problem.

Nothing here can reach a draft — every read goes through the published-only
functions in src/services/article_service.py.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, Response

# The same limiter the widget uses, and deliberately the same buckets: one
# visitor hammering both surfaces is one visitor. `_client_ip` reads the
# forwarded headers the way src/dashboard/proxy.py normalised them, which is
# the only reason a per-IP count means anything behind Caddy.
from src.dashboard.auth import _client_ip
from src.dashboard.routes.widget import _HELP_PER_IP, _HELP_WINDOW, _too_many
from src.services import article_service, settings_service

router = APIRouter()

# The endpoint names are prefixed because the widget's JSON routes are called
# help_index and help_article too, and Starlette's url_for resolves a name to
# whichever route registered it first. Without the prefix every canonical link
# on these pages pointed at /widget/api/help/<slug>.

#: Longest search string worth running. Past this it is not a question.
_MAX_QUERY = 120


async def _enabled() -> bool:
    """Whether these pages should answer at all.

    Two conditions, and both are about not advertising an empty room: the
    operator has to have left the public help centre switched on, and there
    has to be something published in it.
    """
    if not await settings_service.get_help_public():
        return False
    return (await article_service.count_published()) > 0


def _gone() -> HTMLResponse:
    """404 for a help centre that is switched off.

    Not 403: a disabled help centre should be indistinguishable from an
    instance that never had one, rather than an announcement that there are
    articles here for whoever gets the setting changed.
    """
    return HTMLResponse("Not found", status_code=404)


def _slow_down() -> HTMLResponse:
    return HTMLResponse("Too many requests", status_code=429)


def _brand(request: Request) -> str:
    return request.app.state.templates.env.globals.get("g_brand_name") or "Support"


@router.get("/help", response_class=HTMLResponse)
async def public_help_index(request: Request, q: str = ""):
    """Every published article, or the ones matching a search."""
    if not await _enabled():
        return _gone()
    if _too_many(f"h:{_client_ip(request)}", _HELP_PER_IP, _HELP_WINDOW):
        return _slow_down()

    query = (q or "").strip()[:_MAX_QUERY]
    found = (await article_service.search(query) if query
             else await article_service.published(limit=200))

    templates = request.app.state.templates
    return templates.TemplateResponse(request, "help_index.html", {
        "brand": _brand(request),
        "lang": await settings_service.get_default_language(),
        "q": query,
        "articles": found,
        "canonical": str(request.url_for("public_help_index")),
    })


@router.get("/help/{slug}", response_class=HTMLResponse)
async def public_help_article(slug: str, request: Request):
    """One article.

    Addressed by slug for the same reason the widget's endpoint is: an id
    lets anyone walk the table by counting, and tells them how many drafts
    exist by how many numbers answer 404.
    """
    if not await _enabled():
        return _gone()
    if _too_many(f"h:{_client_ip(request)}", _HELP_PER_IP, _HELP_WINDOW):
        return _slow_down()

    # Published-only, and it counts the read. A page view here is the same
    # evidence as one from the widget, so it goes in the same column.
    article = await article_service.read(slug)
    if article is None:
        return _gone()

    templates = request.app.state.templates
    return templates.TemplateResponse(request, "help_article.html", {
        "brand": _brand(request),
        "lang": await settings_service.get_default_language(),
        "a": article,
        "canonical": str(request.url_for("public_help_article", slug=article.slug)),
    })


@router.get("/robots.txt", response_class=PlainTextResponse)
async def robots(request: Request) -> Response:
    """What a crawler may look at.

    The help centre is the only part of this application meant to be found
    by search, and everything else — the dashboard, the widget API, the
    attachment store — is either behind a login or pointless to index. Saying
    so explicitly keeps a crawler from spending its budget on redirects to a
    sign-in page.
    """
    if await _enabled():
        body = (
            "User-agent: *\n"
            "Allow: /help\n"
            "Disallow: /\n"
        )
    else:
        body = "User-agent: *\nDisallow: /\n"
    return PlainTextResponse(body, headers={"Cache-Control": "public, max-age=3600"})
