"""The help centre feeding the assistant, and getting the credit back.

Two things join here. The assistant used to answer from canned templates and
old ticket transcripts — a reply written for an operator to send, and a
recording of two people working something out. An article was written for the
person asking, which is exactly who is about to read the answer.

And the article gets told when it worked. `view_count` says how many people
opened one, which is a different and weaker question: somebody who read it and
opened a ticket anyway got nothing from it, and the read still counted. What
is recorded here is how often an answer built from it ended the conversation.

The assertion that matters most is none of that: the bookkeeping line the
model is asked for must never reach a customer. "SOURCE: 2" at the top of a
support answer is worse than losing every statistic in this file.
"""

import pytest

from src.ai import deflect
from src.ai.prompts import build_deflect_user_prompt
from src.database.db import db_session
from src.services import article_service


@pytest.fixture(autouse=True)
async def _clean(initialized_db):
    yield
    for a in await article_service.for_operator():
        await article_service.delete(a.id)


# ------------------------------------------------------ the customer's view


@pytest.mark.parametrize("raw,expected_n", [
    ("SOURCE: 2\nRefunds take five working days.", 2),
    ("source: 3\nRefunds take five working days.", 3),
    ("  SOURCE : 1  \nRefunds take five working days.", 1),
    ("**SOURCE: 2**\nRefunds take five working days.", 2),
    ("- SOURCE: [3]\nRefunds take five working days.", 3),
    ("SOURCE: 2.\nRefunds take five working days.", 2),
    ("SOURCE: none\nRefunds take five working days.", None),
    ("SOURCE: unknown\nRefunds take five working days.", None),
    ("Refunds take five working days.", None),
])
def test_the_bookkeeping_line_never_reaches_the_customer(raw, expected_n):
    """The model is asked for a marker and models are inventive about
    formatting. Every variation must be stripped, and the ones that cannot be
    understood must cost the attribution rather than the answer."""
    number, answer = deflect.split_source(raw)
    assert number == expected_n
    assert answer == "Refunds take five working days."
    assert "source" not in answer.lower()


def test_a_marker_the_model_repeated_is_still_removed():
    number, answer = deflect.split_source("SOURCE: 2\nSOURCE: 1\nThe answer.")
    assert answer == "The answer."
    assert number == 1


def test_a_source_word_inside_the_answer_is_left_alone():
    """Only a leading line is a marker. "The source of the problem is…" is
    somebody being helped."""
    _, answer = deflect.split_source("The source of the problem is your card.")
    assert answer == "The source of the problem is your card."


# ------------------------------------------------------------- the prompt


def test_articles_go_into_the_prompt_and_are_preferred():
    prompt = build_deflect_user_prompt(
        user_text="my refund has not arrived",
        category="Billing",
        templates=[{"title": "Refund macro", "body": "Tell them five days."}],
        examples=[],
        articles=[{"n": 1, "id": 7, "title": "Refunds",
                   "body": "Refunds reach the card in five working days."}],
    )
    assert "HELP ARTICLES (prefer these)" in prompt
    assert "--- Article 1: Refunds ---" in prompt
    assert "five working days" in prompt
    # And the older sources are still there — articles are an addition, not a
    # replacement, and an instance with no help centre must still deflect.
    assert "Refund macro" in prompt


def test_a_prompt_without_articles_is_unchanged_in_shape():
    prompt = build_deflect_user_prompt(
        user_text="hello", category="", templates=[], examples=[], articles=[])
    assert "HELP ARTICLES" not in prompt


# ------------------------------------------------------------- the search


async def test_candidates_come_from_the_words_in_the_question():
    """A customer sends a paragraph, not a search term. Looking up the whole
    paragraph matches nothing."""
    await article_service.create(
        title="Refunds and how long they take",
        body="Refunds go back to the card that paid.",
        category="Billing", is_published=True)
    await article_service.create(
        title="Printer offline", body="Restart the print spooler.",
        category="Hardware", is_published=True)

    found = await article_service.candidates_for(
        "Hello, I asked for a refund three weeks ago and nothing has arrived",
        category="Billing")

    assert [a.title for a in found] == ["Refunds and how long they take"]


async def test_a_draft_is_never_a_candidate():
    """The assistant answers customers. An unpublished article reaching one
    through the model is the same leak as one reaching them through the
    widget, with an extra step."""
    await article_service.create(
        title="Unannounced pricing", body="We are raising the price to 39.",
        is_published=False)

    found = await article_service.candidates_for("what is the pricing")
    assert found == []


async def test_the_ticket_category_breaks_a_tie():
    await article_service.create(
        title="Billing and invoices", body="invoice invoice",
        category="Billing", is_published=True)
    await article_service.create(
        title="Access and invoices", body="invoice invoice",
        category="Access", is_published=True)

    found = await article_service.candidates_for("invoice question", category="Access")
    assert found[0].category == "Access"


async def test_a_question_of_only_short_words_searches_for_nothing():
    """"is it up?" has no term worth looking up, and four random articles
    would be worse than none."""
    await article_service.create(title="Anything", body="…", is_published=True)
    assert await article_service.candidates_for("is it up?") == []


# ------------------------------------------------------------ the credit


async def test_an_article_is_credited_only_when_it_actually_solved_something():
    """Counting offers would make every article look useful, which is the
    same as measuring nothing."""
    from src.database.models import AICall

    article = await article_service.create(
        title="Refunds", body="Five working days.", is_published=True)

    async with db_session() as session:
        offered = AICall(use_case="deflect", model="test", article_id=article.id)
        dismissed = AICall(use_case="deflect", model="test", article_id=article.id)
        session.add_all([offered, dismissed])
        await session.flush()
        offered_id, dismissed_id = offered.id, dismissed.id
        await session.commit()

    await deflect.mark_outcome(dismissed_id, False)
    assert (await article_service.by_id(article.id)).deflected_count == 0

    await deflect.mark_outcome(offered_id, True)
    assert (await article_service.by_id(article.id)).deflected_count == 1


async def test_a_verdict_is_only_counted_once():
    """The button is in a chat message, and a chat message can be tapped
    twice."""
    from src.database.models import AICall

    article = await article_service.create(
        title="Refunds", body="Five working days.", is_published=True)

    async with db_session() as session:
        call = AICall(use_case="deflect", model="test", article_id=article.id)
        session.add(call)
        await session.flush()
        call_id = call.id
        await session.commit()

    await deflect.mark_outcome(call_id, True)
    await deflect.mark_outcome(call_id, True)
    assert (await article_service.by_id(article.id)).deflected_count == 1


async def test_a_deflection_from_templates_credits_nobody():
    """An answer assembled from canned replies is still a deflection. It is
    simply not one an article can take the credit for."""
    from src.database.models import AICall

    async with db_session() as session:
        call = AICall(use_case="deflect", model="test", article_id=None)
        session.add(call)
        await session.flush()
        call_id = call.id
        await session.commit()

    # The point is that this does not raise looking for an article to credit.
    await deflect.mark_outcome(call_id, True)


async def test_an_operator_sees_both_numbers_and_they_differ(initialized_db):
    """Reads and Solved answer different questions, and the page shows both
    because showing only the first flatters every article equally."""
    from fastapi.testclient import TestClient

    from src.dashboard.app import app

    article = await article_service.create(
        title="Refunds", summary="Five days.", body="Five working days.",
        is_published=True)
    await article_service.read(article.slug)
    await article_service.read(article.slug)
    await article_service.credit_deflection(article.id)

    with TestClient(app, base_url="http://testserver") as c:
        c.get("/login")
        csrf = c.cookies.get("supbot_csrf")
        c.post("/login", data={"email": "admin@local", "password": "test-password-123",
                               "next": "/", "csrf_token": csrf}, follow_redirects=False)
        page = c.get("/manage/articles")

    assert page.status_code == 200
    fresh = await article_service.by_id(article.id)
    assert fresh.view_count == 2
    assert fresh.deflected_count == 1
