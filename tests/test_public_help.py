"""The help centre, on the open web.

tests/test_articles.py covers the same rule from the widget's side: a draft
is not reachable by anybody. This file asks it again of a surface where the
answer matters more, because there is no origin check and no token in front
of these pages — a search engine can walk them.

The rest is about the pages being worth having. A help centre exists so an
operator can send somebody a link instead of writing the answer for the
fortieth time, so the tests that matter are the ones about the link: that it
points at the page rather than at the JSON the widget reads, that it survives
the article being edited, and that it stops existing when the operator turns
the public site off.
"""

import pytest
from fastapi.testclient import TestClient

import src.dashboard.routes.widget as widget_routes
from src.dashboard.app import app
from src.services import article_service, settings_service


@pytest.fixture(autouse=True)
async def _clean(initialized_db):
    widget_routes._hits.clear()
    await settings_service.set_help_public(True, updated_by=0)
    yield
    widget_routes._hits.clear()
    await settings_service.set_help_public(True, updated_by=0)
    # Shared table: a leftover published article changes whether these pages
    # answer at all, for this file and for every other one.
    for a in await article_service.for_operator():
        await article_service.delete(a.id)


async def _published(title="Reset your password", body="Open the login page.", **kw):
    a = await article_service.create(title=title, body=body, **kw)
    await article_service.update(a.id, is_published=True)
    return await article_service.by_id(a.id)


# ------------------------------------------------------------- the one rule


async def test_a_draft_is_not_reachable_on_the_open_web():
    """The same rule as the widget's, asked of the surface with no door on it."""
    await _published(body="published")
    draft = await article_service.create(title="Internal runbook",
                                         body="the root password is")

    with TestClient(app) as c:
        assert c.get(f"/help/{draft.slug}").status_code == 404
        index = c.get("/help")
        assert "Internal runbook" not in index.text
        assert "root password" not in index.text


async def test_a_search_cannot_surface_a_draft():
    """Search is a second way in, and it reads a different code path."""
    await article_service.create(title="Unpublished secret plan",
                                 body="secret", summary="secret")
    await _published(body="something else")

    with TestClient(app) as c:
        r = c.get("/help", params={"q": "secret"})
        assert r.status_code == 200
        assert "Unpublished" not in r.text


# ----------------------------------------------------------- does it answer


async def test_nothing_published_means_no_help_centre():
    """An empty help centre is not a page saying it is empty. A crawler that
    finds one indexes it, and the first thing anyone searching for the brand
    then sees is a promise of documentation that is not there."""
    with TestClient(app) as c:
        assert c.get("/help").status_code == 404


async def test_the_operator_can_switch_the_public_site_off():
    a = await _published()
    await settings_service.set_help_public(False, updated_by=0)

    with TestClient(app) as c:
        assert c.get("/help").status_code == 404
        # And specifically 404, not 403: an instance with the setting off
        # should look like an instance that never had a help centre.
        assert c.get(f"/help/{a.slug}").status_code == 404


async def test_switching_it_off_leaves_the_widget_working():
    """The setting is about the public pages. Turning it off must not take
    the Help tab out of the widget, which is a different audience."""
    a = await _published()
    await settings_service.set_help_public(False, updated_by=0)
    await settings_service.set_widget_enabled(True, updated_by=0)
    await settings_service.set_widget_origins("", updated_by=0)
    try:
        with TestClient(app) as c:
            r = c.get(f"/widget/api/help/{a.slug}",
                      headers={"origin": "https://shop.example"})
            assert r.status_code == 200
            assert r.json()["title"] == a.title
    finally:
        await settings_service.set_widget_enabled(False, updated_by=0)


# ------------------------------------------------------------------ the link


async def test_the_canonical_link_points_at_the_page_not_the_json():
    """The bug this test was written for.

    Starlette resolves url_for by endpoint name, and the widget's JSON routes
    were also called help_index and help_article. Every canonical link on
    these pages pointed at /widget/api/help/<slug> — a URL that returns JSON,
    which is what a search engine would then have indexed as the article.
    """
    a = await _published()
    with TestClient(app) as c:
        body = c.get(f"/help/{a.slug}").text
        assert f'rel="canonical" href="http://testserver/help/{a.slug}"' in body
        assert "/widget/api/help" not in body

        index = c.get("/help").text
        assert 'rel="canonical" href="http://testserver/help"' in index


async def test_the_link_survives_the_title_being_rewritten():
    """A published slug is frozen, and this is the surface that makes that
    matter: the link may already be in an email somebody sent last month."""
    a = await _published(title="Reseting your pasword")
    original = a.slug

    await article_service.update(a.id, title="Resetting your password")

    with TestClient(app) as c:
        r = c.get(f"/help/{original}")
        assert r.status_code == 200
        assert "Resetting your password" in r.text


# ------------------------------------------------------------- what it shows


async def test_an_article_shows_its_text_and_keeps_the_line_breaks():
    a = await _published(body="1. Open the login page\n2. Check your email",
                         summary="Three steps.")
    with TestClient(app) as c:
        r = c.get(f"/help/{a.slug}")
        assert r.status_code == 200
        assert "1. Open the login page" in r.text
        # pre-wrap is what turns the operator's newline into a line break;
        # without it the whole article renders as one paragraph.
        assert "pre-wrap" in r.text


async def test_html_in_an_article_is_shown_not_run():
    """An operator is one paste away from carrying a customer's HTML into an
    article, and this page is served to strangers from the instance's own
    origin — the one the dashboard's session cookie belongs to."""
    a = await _published(body="Use the <script>alert(1)</script> button")
    with TestClient(app) as c:
        body = c.get(f"/help/{a.slug}").text
        assert "<script>alert(1)</script>" not in body
        assert "&lt;script&gt;" in body


async def test_the_index_lists_what_is_published():
    await _published(title="First article", summary="About the first thing")
    await _published(title="Second article")
    with TestClient(app) as c:
        body = c.get("/help").text
        assert "First article" in body
        assert "Second article" in body
        assert "About the first thing" in body


async def test_a_search_that_finds_nothing_says_so_and_offers_the_way_back():
    await _published()
    with TestClient(app) as c:
        body = c.get("/help", params={"q": "zzzzzzz"}).text
        assert "Nothing matches" in body
        assert 'href="/help"' in body


async def test_reading_the_page_counts_as_a_read():
    """The view count is the only evidence an instance collects about whether
    writing the article was worth the afternoon, and a read from the web is
    the same evidence as a read from the widget."""
    a = await _published()
    assert a.view_count == 0
    with TestClient(app) as c:
        c.get(f"/help/{a.slug}")
    assert (await article_service.by_id(a.id)).view_count == 1


# --------------------------------------------------------------- robots.txt


async def test_robots_opens_the_help_centre_and_nothing_else():
    await _published()
    with TestClient(app) as c:
        body = c.get("/robots.txt").text
    assert "Allow: /help" in body
    assert "Disallow: /" in body


async def test_robots_closes_everything_when_there_is_no_help_centre():
    """Nothing published: the dashboard is all that is left to find, and a
    crawler spending its budget on a login page helps nobody."""
    with TestClient(app) as c:
        body = c.get("/robots.txt").text
    assert "Allow:" not in body
    assert "Disallow: /" in body


# ------------------------------------------------------------------ the cap


async def test_an_article_body_has_a_ceiling():
    """The column is Text and has no limit of its own, so without the cap the
    only bound is what a browser will post — into a page served to the web."""
    a = await _published(body="x" * (article_service.MAX_BODY + 5_000))
    assert len(a.body) == article_service.MAX_BODY

    await article_service.update(a.id, body="y" * (article_service.MAX_BODY + 5_000))
    assert len((await article_service.by_id(a.id)).body) == article_service.MAX_BODY


# ------------------------------------------------- what the operator sees


def _login(c: TestClient) -> str:
    c.get("/login")
    token = c.cookies.get("supbot_csrf")
    r = c.post("/login", data={"email": "admin@local", "password": "test-password-123",
                               "next": "/", "csrf_token": token}, follow_redirects=False)
    assert r.status_code == 303
    return token


async def test_the_article_list_links_the_slug_only_when_it_leads_somewhere():
    """The list is where an operator copies a link from, so the link has to be
    real. With the public site off there is no page at that address, and a
    link into a 404 is worse than showing no link at all."""
    a = await _published()
    with TestClient(app) as c:
        _login(c)

        on = c.get("/manage/articles")
        assert on.status_code == 200
        assert f'href="/help/{a.slug}"' in on.text

        await settings_service.set_help_public(False, updated_by=0)
        off = c.get("/manage/articles")
        assert off.status_code == 200
        assert f'href="/help/{a.slug}"' not in off.text
        # ...but the slug is still shown, because it is still how the article
        # is addressed in the widget.
        assert a.slug in off.text
