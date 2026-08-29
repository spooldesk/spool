"""The help centre: the only table here whose rows are meant for strangers.

Which makes one property carry the whole feature — an article nobody
published must not be reachable by anyone, by any route, ever. Everything
else is convenience; that one is the difference between a help centre and an
accidental publication of whatever an operator was drafting.

The rest of these are about a help centre being *useful*: search that folds
case in more than ASCII, a slug that survives its title being edited, and a
Help tab that does not appear before there is anything to find.
"""

import json

import pytest
from fastapi.testclient import TestClient

import src.dashboard.routes.widget as widget_routes
from src.dashboard.app import app
from src.services import article_service, settings_service


@pytest.fixture(autouse=True)
async def _clean(initialized_db):
    widget_routes._hits.clear()
    yield
    widget_routes._hits.clear()
    # The table is shared with every other test file, and a stray published
    # article changes what `count_published` says about the Help tab.
    for a in await article_service.for_operator():
        await article_service.delete(a.id)


@pytest.fixture()
async def widget_on(initialized_db):
    await settings_service.set_widget_enabled(True, updated_by=0)
    await settings_service.set_widget_origins("", updated_by=0)
    yield
    await settings_service.set_widget_enabled(False, updated_by=0)


def _help(c: TestClient, q: str = ""):
    return c.get("/widget/api/help", params={"q": q},
                 headers={"origin": "https://shop.example"})


# ------------------------------------------------------------- the one rule


async def test_a_draft_is_not_reachable_by_anybody(widget_on):
    """The property the whole feature rests on.

    Checked through every door a stranger has: the list, the search, and the
    slug they would have if they somehow guessed it.
    """
    draft = await article_service.create(
        title="Pricing changes coming in March",
        summary="Not announced yet",
        body="We are raising the team plan to 39.",
        is_published=False,
    )

    with TestClient(app, base_url="http://testserver") as c:
        listed = _help(c).json()["articles"]
        searched = _help(c, "pricing").json()["articles"]
        direct = c.get(f"/widget/api/help/{draft.slug}",
                       headers={"origin": "https://shop.example"})

    assert listed == []
    assert searched == []
    assert direct.status_code == 404
    assert "39" not in direct.text, "the body leaked through the error"


async def test_unpublishing_takes_it_away_again(widget_on):
    article = await article_service.create(
        title="Temporary notice", body="The office is shut on Friday.",
        is_published=True,
    )

    with TestClient(app, base_url="http://testserver") as c:
        assert _help(c).json()["articles"], "a published article was not listed"

        await article_service.update(article.id, is_published=False)

        assert _help(c).json()["articles"] == []
        assert c.get(f"/widget/api/help/{article.slug}",
                     headers={"origin": "https://shop.example"}).status_code == 404


async def test_a_missing_slug_and_a_draft_slug_answer_identically(widget_on):
    """An article being written is not something a stranger should be able to
    detect by the shape of the refusal."""
    await article_service.create(title="Secret draft", body="…", is_published=False)

    with TestClient(app, base_url="http://testserver") as c:
        headers = {"origin": "https://shop.example"}
        draft = c.get("/widget/api/help/secret-draft", headers=headers)
        absent = c.get("/widget/api/help/no-such-article-at-all", headers=headers)

    assert draft.status_code == absent.status_code == 404
    assert draft.json() == absent.json()


# ------------------------------------------------------------------ slugs


async def test_two_articles_can_share_a_title():
    a = await article_service.create(title="Billing", body="one")
    b = await article_service.create(title="Billing", body="two")
    assert a.slug != b.slug
    assert b.slug.startswith("billing")


async def test_a_slug_is_made_from_any_alphabet():
    """Transliteration is not attempted on purpose — it needs a table per
    language and gets Ukrainian subtly wrong. Percent-encoded Cyrillic in a
    URL is ugly and correct."""
    assert article_service.slugify("How do I get a refund?") == "how-do-i-get-a-refund"
    assert article_service.slugify("Как сбросить пароль") == "как-сбросить-пароль"
    assert article_service.slugify("  Billing / VAT & invoices!!  ") == "billing-vat-invoices"
    assert article_service.slugify("!!!") == "article"


async def test_a_published_slug_is_frozen_when_the_title_changes():
    """It may be in a customer's bookmarks or pasted into a reply somebody
    sent last month. Tidying the URL is not worth breaking those."""
    article = await article_service.create(
        title="Refunds", body="…", is_published=True)
    original = article.slug

    await article_service.update(article.id, title="How refunds work")
    after = await article_service.by_id(article.id)

    assert after.title == "How refunds work"
    assert after.slug == original


async def test_a_draft_slug_still_follows_its_title():
    """Nothing can have linked to it yet, so there is nothing to protect."""
    article = await article_service.create(title="Frist draft", body="…")
    await article_service.update(article.id, title="First draft")
    after = await article_service.by_id(article.id)
    assert after.slug == "first-draft"


# ----------------------------------------------------------------- finding


async def test_search_folds_case_beyond_ascii(widget_on):
    """A plain SQL LIKE folds ASCII only, so this is the difference between a
    help centre people use and one they try twice."""
    await article_service.create(
        title="Ökonomie und Abrechnung", body="Etwas über Rechnungen.",
        is_published=True)

    with TestClient(app, base_url="http://testserver") as c:
        assert _help(c, "ökonomie").json()["articles"], "exact case missed"
        assert _help(c, "ÖKONOMIE").json()["articles"], "upper case missed"
        assert _help(c, "abrechnung").json()["articles"], "word inside the title missed"


async def test_a_title_match_outranks_a_body_match(widget_on):
    """Somebody typing "refund" wants the article called Refunds, not the one
    that mentions the word in its ninth paragraph."""
    await article_service.create(
        title="Shipping times", body="We do not offer a refund for late delivery.",
        is_published=True)
    await article_service.create(
        title="Refunds", body="Everything about getting money back.",
        is_published=True)

    with TestClient(app, base_url="http://testserver") as c:
        found = _help(c, "refund").json()["articles"]

    assert [a["title"] for a in found][0] == "Refunds"
    assert len(found) == 2, "the body match should still be offered, just second"


async def test_reading_an_article_is_counted(widget_on):
    """The only evidence an instance ever collects about whether writing it
    was worth the afternoon."""
    article = await article_service.create(
        title="Counted", body="…", is_published=True)

    with TestClient(app, base_url="http://testserver") as c:
        for _ in range(3):
            c.get(f"/widget/api/help/{article.slug}",
                  headers={"origin": "https://shop.example"})

    assert (await article_service.by_id(article.id)).view_count == 3


# -------------------------------------------------------------- the panel


async def test_the_help_tab_stays_away_until_there_is_something_to_find(widget_on):
    """An empty search box does not read as "nobody has written anything", it
    reads as "the search is broken"."""
    with TestClient(app, base_url="http://testserver") as c:
        assert c.get("/widget/api/hello").json()["help"] is False

        await article_service.create(title="Anything", body="…", is_published=True)
        assert c.get("/widget/api/hello").json()["help"] is True

        # A draft is not something to find.
        for a in await article_service.for_operator():
            await article_service.update(a.id, is_published=False)
        assert c.get("/widget/api/hello").json()["help"] is False


async def test_help_is_refused_while_the_widget_is_off(initialized_db):
    """Turning the widget off has to close every public door, not most."""
    await settings_service.set_widget_enabled(False, updated_by=0)
    await article_service.create(title="Public", body="…", is_published=True)

    with TestClient(app, base_url="http://testserver") as c:
        assert c.get("/widget/api/help").status_code == 404
        assert c.get("/widget/api/help/public").status_code == 404


# ------------------------------------------------------------ the operator


async def test_an_operator_writes_publishes_and_withdraws_one(widget_on):
    """The round trip through the admin pages, since that is the only way an
    article gets written in real life."""
    with TestClient(app, base_url="http://testserver") as c:
        c.get("/login")
        csrf = c.cookies.get("supbot_csrf")
        signed_in = c.post("/login", data={
            "email": "admin@local", "password": "test-password-123",
            "next": "/", "csrf_token": csrf,
        }, follow_redirects=False)
        assert signed_in.status_code == 303

        created = c.post("/manage/articles/new", data={
            "title": "Resetting your password",
            "summary": "Two minutes, no ticket needed",
            "body": "Open Settings and choose Reset.",
            "category": "", "is_published": "0", "csrf_token": csrf,
        }, follow_redirects=False)
        assert created.status_code == 303

        # A draft, so the customer sees nothing yet.
        assert _help(c, "password").json()["articles"] == []

        article = (await article_service.for_operator())[0]
        published = c.post(f"/manage/articles/{article.id}/publish",
                           data={"publish": "1", "csrf_token": csrf},
                           follow_redirects=False)
        assert published.status_code == 303

        found = _help(c, "password").json()["articles"]
        assert [a["title"] for a in found] == ["Resetting your password"]

        page = c.get("/manage/articles")

    assert page.status_code == 200
    assert "Resetting your password" in page.text
