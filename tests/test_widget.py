"""The website widget: the only part of this product a stranger can reach.

Everything else is behind a login, so these are the tests that matter most
per line. Roughly half of them are about what the endpoints refuse.

Note what is deliberately *not* asserted anywhere below: that a blocked
visitor is told they are blocked, or that an unknown session token is
distinguished from a missing one. Both would be more helpful, and both turn
a public endpoint into an oracle.
"""

import json

import pytest
from fastapi.testclient import TestClient

import src.dashboard.routes.widget as widget_routes
from src.channels.widget import NAME as WIDGET, address_for
from src.dashboard.app import app
from src.database.db import db_session
from src.database.models import Message, Ticket
from src.services import settings_service
from tests.conftest import next_tg_id


@pytest.fixture(autouse=True)
async def _clean_limits(initialized_db):
    """Rate-limit state is process-wide, so a test that trips it would
    otherwise fail whichever test happens to run next."""
    widget_routes._hits.clear()
    yield
    widget_routes._hits.clear()


@pytest.fixture()
async def widget_on(initialized_db):
    await settings_service.set_widget_enabled(True, updated_by=0)
    await settings_service.set_widget_origins("", updated_by=0)
    yield
    await settings_service.set_widget_enabled(False, updated_by=0)


def _say(c: TestClient, text: str, token: str | None = None, **body):
    headers = {"origin": "https://shop.example"}
    if token:
        headers["x-spool-session"] = token
    return c.post("/widget/api/message", headers=headers,
                  content=json.dumps({"text": text, **body}))


# ---------------------------------------------------------------- the gate


async def test_everything_is_shut_until_someone_turns_it_on(initialized_db):
    """A public endpoint that appears by itself is not a door anyone chose."""
    await settings_service.set_widget_enabled(False, updated_by=0)
    with TestClient(app, base_url="http://testserver") as c:
        assert _say(c, "hello?").status_code == 404
        assert c.get("/widget/api/hello").status_code == 404
        assert c.get("/widget/frame").status_code == 404
        assert c.get("/widget/embed.js").status_code == 404


async def test_turning_it_off_takes_effect_immediately(widget_on):
    """Not at the next restart. An operator switching this off is usually
    switching it off for a reason that will not wait."""
    with TestClient(app, base_url="http://testserver") as c:
        first = _say(c, "before")
        assert first.status_code == 200
        token = first.json()["session"]

        await settings_service.set_widget_enabled(False, updated_by=0)
        assert _say(c, "after", token).status_code == 404
        assert c.get("/widget/api/poll",
                     headers={"x-spool-session": token}).status_code == 404


# ---------------------------------------------------------------- limits


async def test_opening_conversations_from_one_address_is_capped(widget_on):
    """The expensive action: each one creates a customer, a ticket and a card
    on somebody's board."""
    with TestClient(app, base_url="http://testserver") as c:
        codes = [_say(c, f"visitor {i}").status_code
                 for i in range(widget_routes._NEW_SESSIONS_PER_IP + 2)]

    assert codes[:widget_routes._NEW_SESSIONS_PER_IP] == \
        [200] * widget_routes._NEW_SESSIONS_PER_IP
    assert codes[-1] == 429, "an unlimited public ticket factory"


async def test_a_session_that_floods_is_slowed_down(widget_on):
    with TestClient(app, base_url="http://testserver") as c:
        token = _say(c, "first").json()["session"]
        codes = [_say(c, f"m{i}", token).status_code
                 for i in range(widget_routes._MESSAGES_PER_SESSION + 2)]
    assert 429 in codes


async def test_holding_a_session_does_not_spend_the_ip_budget(widget_on):
    """The two limits count different things. If a follow-up spent the
    new-conversation budget, one talkative visitor would lock out an office
    behind the same address."""
    with TestClient(app, base_url="http://testserver") as c:
        token = _say(c, "opening").json()["session"]
        for i in range(4):
            assert _say(c, f"more {i}", token).status_code == 200
        # A colleague on the same network can still start their own.
        assert _say(c, "someone else entirely").status_code == 200


async def test_a_paste_bomb_is_trimmed_not_stored(widget_on):
    with TestClient(app, base_url="http://testserver") as c:
        r = _say(c, "x" * 50_000)
    assert r.status_code == 200
    async with db_session() as session:
        msg = await session.get(Message, r.json()["messages"][0]["id"])
    assert len(msg.text) == widget_routes._MAX_MESSAGE_CHARS


async def test_an_empty_message_is_refused_before_anything_is_created(widget_on):
    from sqlalchemy import func, select

    from src.database.models import User

    async def visitors() -> int:
        async with db_session() as session:
            return (await session.execute(
                select(func.count()).select_from(User).where(User.channel == WIDGET)
            )).scalar()

    # Stated as a difference, not a total: every test in this file shares one
    # database, so an absolute count is really a claim about running order.
    before = await visitors()
    with TestClient(app, base_url="http://testserver") as c:
        assert _say(c, "   ").status_code == 400
        assert _say(c, "").status_code == 400
    assert await visitors() == before, "an empty POST created a customer"


# ---------------------------------------------------------------- session


async def test_the_token_is_never_stored_in_a_form_anyone_could_use(widget_on):
    """A session token is a bearer credential. A database holding them in the
    clear is a list of live logins for whoever reads it."""
    from sqlalchemy import select
    from src.database.models import User

    with TestClient(app, base_url="http://testserver") as c:
        token = _say(c, "hello").json()["session"]

    async with db_session() as session:
        stored = (await session.execute(
            select(User.channel_user_id).where(User.channel == WIDGET)
        )).scalars().all()

    assert token not in stored, "the raw session token was written to the database"
    assert address_for(token) in stored


async def test_the_token_is_handed_over_exactly_once(widget_on):
    """On creation and never again. Repeating it in every response would put
    a credential into far more logs than it needs to be in."""
    with TestClient(app, base_url="http://testserver") as c:
        first = _say(c, "hello").json()
        assert "session" in first
        again = _say(c, "still here", first["session"]).json()
        assert "session" not in again


async def test_a_returning_visitor_continues_the_same_conversation(widget_on):
    with TestClient(app, base_url="http://testserver") as c:
        first = _say(c, "my order is late").json()
        token = first["session"]
        second = _say(c, "any news?", token).json()

    assert second["ticket"] == first["ticket"]
    assert [m["text"] for m in second["messages"]] == ["my order is late", "any news?"]


async def test_a_stranger_cannot_read_a_conversation_by_guessing(widget_on):
    with TestClient(app, base_url="http://testserver") as c:
        _say(c, "something private")
        r = c.get("/widget/api/poll", headers={"x-spool-session": "not-a-real-token"})
    assert r.status_code == 401
    assert "private" not in r.text


async def test_an_unknown_token_answers_exactly_like_a_missing_one(widget_on):
    """Otherwise the endpoint tells an attacker when a guess was close."""
    with TestClient(app, base_url="http://testserver") as c:
        missing = c.get("/widget/api/poll")
        unknown = c.get("/widget/api/poll", headers={"x-spool-session": "nope"})
    assert missing.status_code == unknown.status_code == 401
    assert missing.json() == unknown.json()


async def test_the_session_never_travels_in_a_url(widget_on):
    """A query string ends up in access logs, browser history and referrer
    headers. This one is a credential, so it goes in a header."""
    import inspect

    signature = inspect.signature(widget_routes.poll)
    assert "x_spool_session" in signature.parameters, "the header parameter is gone"
    for name, param in signature.parameters.items():
        if "session" in name or "token" in name:
            assert "Header" in repr(param.default), \
                f"{name} is not read from a header"


# ---------------------------------------------------------------- privacy


async def test_a_visitor_never_sees_an_internal_note(widget_on):
    """The endpoint where getting this wrong shows a customer what an
    operator wrote about them."""
    with TestClient(app, base_url="http://testserver") as c:
        opened = _say(c, "is this thing on?").json()
        async with db_session() as session:
            session.add(Message(ticket_id=opened["ticket"], sender_type="admin",
                                sender_id=1, text="third time this week",
                                is_internal=True))
            session.add(Message(ticket_id=opened["ticket"], sender_type="admin",
                                sender_id=1, text="Yes! How can we help?"))
            await session.commit()

        r = c.get("/widget/api/poll", headers={"x-spool-session": opened["session"]})

    texts = [m["text"] for m in r.json()["messages"]]
    assert "Yes! How can we help?" in texts
    assert "third time this week" not in texts
    assert "third time this week" not in r.text


async def test_a_withdrawn_reply_disappears_from_the_visitors_view(widget_on):
    """The widget is the one channel where an unsend genuinely unsends: there
    is no copy in anyone's chat history to outlive the row."""
    from src.services import message_service

    with TestClient(app, base_url="http://testserver") as c:
        opened = _say(c, "hello").json()
        async with db_session() as session:
            reply = Message(ticket_id=opened["ticket"], sender_type="admin",
                            sender_id=1, text="Sent to the wrong person, sorry")
            session.add(reply)
            await session.commit()
            reply_id = reply.id

        before = c.get("/widget/api/poll", headers={"x-spool-session": opened["session"]})
        assert "wrong person" in before.text

        result = await message_service.delete_for_user(None, reply_id)
        assert result.ok, result.error_key

        after = c.get("/widget/api/poll", headers={"x-spool-session": opened["session"]})

    assert "wrong person" not in after.text


async def test_the_unsend_button_is_offered_on_a_widget_ticket(widget_on):
    """It was not, at first. The check looked for a message id the platform
    hands back, and this platform has none — so the one channel that could
    unsend best was the one refused."""
    from src.dashboard import queries

    with TestClient(app, base_url="http://testserver") as c:
        opened = _say(c, "hello").json()
    async with db_session() as session:
        session.add(Message(ticket_id=opened["ticket"], sender_type="admin",
                            sender_id=1, text="On it."))
        await session.commit()

    detail = await queries.ticket_detail(opened["ticket"])
    reply = next(m for m in detail["messages"] if m["sender_type"] == "admin")
    assert reply["can_delete"] is True
    assert detail["channel"] == WIDGET


# ---------------------------------------------------------------- embedding


async def test_the_snippet_points_at_the_host_that_served_it(widget_on):
    """Pointing an embed at the wrong host is the commonest way one fails,
    and it fails invisibly: the bubble appears and nothing behind it works."""
    with TestClient(app, base_url="http://example-help.test") as c:
        js = c.get("/widget/embed.js").text
    assert 'var BASE = "http://example-help.test"' in js
    assert "__SPOOL_BASE__" not in js, "the placeholder was served as-is"


async def test_the_panel_refuses_to_be_framed_by_an_unlisted_site(widget_on):
    """Otherwise any page can put it in an invisible overlay and read what a
    visitor types into what they believe is their own site."""
    await settings_service.set_widget_origins("https://shop.example", updated_by=0)
    with TestClient(app, base_url="http://testserver") as c:
        r = c.get("/widget/frame")
    assert r.headers["content-security-policy"] == "frame-ancestors https://shop.example"


async def test_cors_answers_only_the_sites_that_were_listed(widget_on):
    await settings_service.set_widget_origins("https://shop.example", updated_by=0)
    with TestClient(app, base_url="http://testserver") as c:
        allowed = c.get("/widget/api/hello", headers={"origin": "https://shop.example"})
        stranger = c.get("/widget/api/hello", headers={"origin": "https://evil.test"})

    assert allowed.headers.get("access-control-allow-origin") == "https://shop.example"
    assert "access-control-allow-origin" not in stranger.headers


async def test_an_empty_allowlist_means_any_site(widget_on):
    """The right default for something that has to be pasted into a page and
    just work — given that the whole feature is off until switched on."""
    with TestClient(app, base_url="http://testserver") as c:
        r = c.get("/widget/api/hello", headers={"origin": "https://anywhere.test"})
    assert r.headers.get("access-control-allow-origin") == "https://anywhere.test"


async def test_the_panel_asks_for_nothing_from_anywhere_else(widget_on):
    """A widget that pulls a font or a script from a CDN fails on any page
    with a strict CSP — which is exactly the sort of page worth having as a
    customer."""
    from src.dashboard.routes.widget_asset import frame_html

    html = frame_html()
    for scheme in ("http://", "https://", "//cdn", "src=\"/static"):
        assert scheme not in html, f"the panel reaches out to {scheme}"


async def test_the_loader_only_acts_on_messages_from_its_own_instance():
    """Anyone can postMessage at a window, and everything the loader does on
    receipt touches the host page."""
    from src.dashboard.routes.widget_asset import loader_js

    js = loader_js("https://help.example")
    assert "e.origin !== BASE" in js


# ---------------------------------------------------------------- the ticket


async def test_a_widget_message_becomes_an_ordinary_ticket(widget_on):
    """The whole promise of the channel layer. Nothing about this ticket is
    special: it sorts, filters, assigns and reports like any other."""
    from src.dashboard import queries

    marker = f"the checkout page hangs {next_tg_id()}"
    with TestClient(app, base_url="http://testserver") as c:
        opened = _say(c, marker, name="Dana").json()

    async with db_session() as session:
        ticket = await session.get(Ticket, opened["ticket"])
        assert ticket.status == "active" and ticket.stage == "new"

    # Searched rather than listed: a board column shows the first fifty cards,
    # and by the time the whole suite has run there are more than fifty new
    # tickets. That cap is the board working, not a bug to assert around.
    board = await queries.board_columns(q=marker)
    ids = [card["id"] for column in board.values() for card in column["cards"]]
    assert ids == [opened["ticket"]], "a widget ticket never reached the board"


async def test_an_operators_reply_reaches_the_visitor(widget_on):
    """End to end, through the same reply path Telegram uses."""
    from src.services import template_service

    with TestClient(app, base_url="http://testserver") as c:
        opened = _say(c, "help").json()

        async with db_session() as session:
            result = await template_service.apply_text(
                session, None, ticket_id=opened["ticket"],
                text="Try a hard refresh.", admin_tg_id=1, admin_chat_id=-100,
            )
            await session.commit()
        assert result.success, result.error

        r = c.get("/widget/api/poll", headers={"x-spool-session": opened["session"]})

    assert "Try a hard refresh." in [m["text"] for m in r.json()["messages"]]
