"""Per-operator preferences.

The property worth pinning hardest is isolation: one person's choice must not
become everyone's. That is easy to get wrong in a way that only shows up
under load, because the obvious implementation — a module-level "current
language" — is shared by every request in the process and asyncio interleaves
them at every await.
"""

import asyncio

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from src.dashboard import i18n
from src.dashboard.app import app
from src.database.db import db_session
from src.database.models import AdminUser
from src.services import preferences_service


def _login(c: TestClient) -> str:
    c.get("/login")
    token = c.cookies.get("supbot_csrf")
    r = c.post("/login", data={"email": "admin@local", "password": "test-password-123",
                               "next": "/", "csrf_token": token}, follow_redirects=False)
    assert r.status_code == 303
    return c.cookies.get("supbot_csrf") or token


async def _owner_id() -> int:
    async with db_session() as session:
        return (await session.execute(
            select(AdminUser.id).where(AdminUser.email == "admin@local")
        )).scalar_one()


@pytest.fixture(autouse=True)
async def _clean(initialized_db):
    """Preferences persist on the shared account, so a test that sets one
    would otherwise be read by whichever test runs next."""
    yield
    async with db_session() as session:
        admin = await session.get(AdminUser, await _owner_id())
        if admin is not None:
            admin.preferences = None
            await session.commit()
    i18n.use_language(None)


# ---------------------------------------------------------------- defaults


async def test_an_operator_who_never_opened_the_page_gets_todays_behaviour(
    initialized_db,
):
    """Every default is what the dashboard already did, so the feature is
    invisible until someone wants it."""
    prefs = await preferences_service.for_operator(await _owner_id())
    assert prefs["language"] == preferences_service.FOLLOW_INSTANCE
    assert prefs["queue_advance"] is False
    assert prefs["signature"] == ""
    assert prefs["hotkeys"] is True


async def test_the_shared_password_has_no_row_and_is_not_an_error(initialized_db):
    """An instance can still be on the bootstrap password, where nobody is a
    row in admin_users yet."""
    assert await preferences_service.for_operator(None) == preferences_service.defaults()


@pytest.mark.parametrize("key,junk", [
    ("language", "klingon"),
    ("theme", "neon"),
    ("density", "microscopic"),
    ("landing", "/manage/settings"),
])
async def test_a_value_that_stopped_being_allowed_falls_back(initialized_db, key, junk):
    """Written to survive its own history: a preference saved by an older
    release must not reach a template that no longer expects it. `/landing`
    is the pointed one — a path an agent cannot open would 403 them every
    morning."""
    op = await _owner_id()
    async with db_session() as session:
        admin = await session.get(AdminUser, op)
        admin.preferences = {key: junk}
        await session.commit()

    prefs = await preferences_service.for_operator(op)
    assert prefs[key] == preferences_service.DEFAULTS[key]


async def test_an_unknown_key_is_dropped_rather_than_carried(initialized_db):
    op = await _owner_id()
    await preferences_service.save(op, {"theme": "light", "colour_of_the_sky": "blue"})
    prefs = await preferences_service.for_operator(op)
    assert prefs["theme"] == "light"
    assert "colour_of_the_sky" not in prefs


async def test_unchecking_a_box_actually_unchecks_it(initialized_db):
    """A cleared checkbox sends nothing at all. The route turns that absence
    into False explicitly — if it merged instead, nothing could ever be
    turned off once it had been turned on."""
    op = await _owner_id()
    with TestClient(app, base_url="http://testserver") as c:
        token = _login(c)
        c.post("/me", data={"hotkeys": "1", "csrf_token": token},
               follow_redirects=False)
        assert (await preferences_service.for_operator(op))["hotkeys"] is True

        # The same form with the box cleared: the field is simply not sent.
        c.post("/me", data={"csrf_token": token}, follow_redirects=False)
    assert (await preferences_service.for_operator(op))["hotkeys"] is False


# ---------------------------------------------------------------- language


async def test_one_operators_language_is_not_everybody_elses(initialized_db):
    """The failure this guards against is invisible in a single-user test and
    obvious in production: with a module-level global, two requests share the
    process and interleave at every await, so the second request's language
    lands in the middle of the first request's page."""
    i18n.set_active_language("en")
    seen: dict[str, str] = {}

    async def render_as(who: str, lang: str | None) -> None:
        i18n.use_language(lang)
        await asyncio.sleep(0)          # where the other request gets its turn
        seen[who] = i18n.t("save")

    await asyncio.gather(
        render_as("russian", "ru"),
        render_as("english", None),
    )

    assert seen["english"] == "Save"
    assert seen["russian"] != "Save", "one request's language leaked into another"


async def test_the_instance_language_is_what_you_get_without_a_preference(
    initialized_db,
):
    i18n.set_active_language("ru")
    try:
        i18n.use_language(None)
        assert i18n.active_language() == "ru"
    finally:
        i18n.set_active_language("en")


async def test_the_page_is_rendered_in_the_operators_own_language(initialized_db):
    op = await _owner_id()
    await preferences_service.save(op, {"language": "ru"})

    from src.dashboard.i18n import strings

    with TestClient(app, base_url="http://testserver") as c:
        _login(c)
        page = c.get("/tickets").text

    russian = strings("ru")
    english = strings("en")
    # Something on this page that differs between the two, so the assertion
    # cannot pass by accident on a word both languages share.
    assert russian["apply"] in page, "the page ignored the operator's language"
    assert english["apply"] not in page


# ---------------------------------------------------------------- the page


async def test_saving_from_the_page_takes_effect(initialized_db):
    with TestClient(app, base_url="http://testserver") as c:
        token = _login(c)
        r = c.post("/me", data={
            "language": "ru", "theme": "light", "density": "compact",
            "landing": "/board", "signature": "— Dana, support",
            "hotkeys": "1", "csrf_token": token,
        }, follow_redirects=False)
        assert r.status_code == 303

    prefs = await preferences_service.for_operator(await _owner_id())
    assert prefs["theme"] == "light"
    assert prefs["density"] == "compact"
    assert prefs["landing"] == "/board"
    assert prefs["signature"] == "— Dana, support"
    assert prefs["queue_advance"] is False, "a box nobody ticked was turned on"


async def test_the_page_needs_no_role(initialized_db):
    """Nothing here affects anyone else, so an agent may set their own
    language without being an admin."""
    from src.dashboard import app as app_module

    source = open("src/dashboard/app.py", encoding="utf-8").read()
    line = next(l for l in source.splitlines() if "preferences_routes.router" in l)
    assert "_ANY_ROLE" in line, "personal settings grew a role check"
    assert app_module is not None


async def test_an_overlong_signature_is_trimmed(initialized_db):
    op = await _owner_id()
    await preferences_service.save(op, {"signature": "x" * 5000})
    prefs = await preferences_service.for_operator(op)
    assert len(prefs["signature"]) == preferences_service.MAX_SIGNATURE


# ---------------------------------------------------------------- signature


def test_a_signature_is_added_once_not_twice():
    """Templates that already end in a sign-off are common, and getting it
    twice looks careless in front of a customer."""
    sig = "— Dana, support"
    assert preferences_service.apply_signature("Fixed.", sig) == f"Fixed.\n\n{sig}"
    assert preferences_service.apply_signature(f"Fixed.\n\n{sig}", sig) == f"Fixed.\n\n{sig}"


def test_an_empty_reply_does_not_become_a_bare_signature():
    assert preferences_service.apply_signature("", "— Dana") == ""
    assert preferences_service.apply_signature("Fixed.", "") == "Fixed."


# ---------------------------------------------------------------- shortcuts


def test_the_cheat_sheet_and_the_keys_are_one_source():
    """A list that has drifted from the keys it describes is worse than no
    list. Both the overlay and the preferences page render the same partial."""
    from pathlib import Path

    base = Path("src/dashboard/templates/app_base.html").read_text(encoding="utf-8")
    page = Path("src/dashboard/templates/preferences.html").read_text(encoding="utf-8")
    assert '{% include "_shortcuts.html" %}' in base
    assert '{% include "_shortcuts.html" %}' in page

    keys = Path("src/dashboard/templates/_shortcuts.html").read_text(encoding="utf-8")
    script = base[base.index("Keyboard shortcuts."):]

    # Every key the script binds has to be written down, or the sheet is a
    # lie that grows with every release.
    for letter in ("g", "?", "/", "r", "n", "j"):
        assert f"case '{letter}':" in script, f"the script no longer binds {letter}"
        assert f"'{letter}'" in keys, f"the sheet does not mention {letter}"
    # And the pairs that 'g' opens.
    for second in ("t", "b", "p"):
        assert f"'{second}'" in keys, f"the sheet does not mention g {second}"


def test_shortcuts_do_nothing_while_someone_is_typing():
    """A support reply contains the letters r, n, j and g rather a lot."""
    from pathlib import Path

    base = Path("src/dashboard/templates/app_base.html").read_text(encoding="utf-8")
    script = base[base.index("Keyboard shortcuts."):]
    assert "function typing(el)" in script
    assert "if (typing(document.activeElement)) return;" in script
    # Escape is the exception, because it is the way *out* of a field.
    escape = script[script.index("e.key === 'Escape'"):][:400]
    assert "blur()" in escape


def test_shortcuts_can_be_turned_off():
    from pathlib import Path

    base = Path("src/dashboard/templates/app_base.html").read_text(encoding="utf-8")
    script = base[base.index("Keyboard shortcuts."):]
    assert "prefs.hotkeys" in script and "return;" in script


# ---------------------------------------------------------------- effect


async def test_a_signature_reaches_the_customer(initialized_db, channel):
    """A settings page whose switches change nothing is worse than no
    settings page: it teaches an operator that the controls are decorative."""
    from src.channels.identity import create_customer
    from src.database.models import Team, Ticket
    from tests.conftest import next_tg_id

    op = await _owner_id()
    await preferences_service.save(op, {"signature": "— Dana, support"})

    async with db_session() as session:
        team = Team(name=f"sig-{next_tg_id()}")
        session.add(team)
        await session.flush()
        user = await create_customer(session, channel="telegram",
                                     address=str(next_tg_id()), team_id=team.id)
        ticket = Ticket(user_id=user.id, team_id=team.id, category="Other",
                        status="active")
        session.add(ticket)
        await session.flush()
        tid = ticket.id
        await session.commit()

    with TestClient(app, base_url="http://testserver") as c:
        token = _login(c)
        c.post(f"/tickets/{tid}/reply",
               data={"text": "All sorted.", "csrf_token": token},
               follow_redirects=False)

    assert channel.texts, "nothing was sent"
    _address, sent = channel.texts[-1]
    assert sent == "All sorted.\n\n— Dana, support"


async def test_a_signature_never_lands_on_an_internal_note(initialized_db, channel):
    """A note is not addressed to anybody, so signing it is nonsense — and
    notes go down a different route, which is why this is worth pinning."""
    from src.channels.identity import create_customer
    from src.database.models import Message, Team, Ticket
    from tests.conftest import next_tg_id

    op = await _owner_id()
    await preferences_service.save(op, {"signature": "— Dana, support"})

    async with db_session() as session:
        team = Team(name=f"nsig-{next_tg_id()}")
        session.add(team)
        await session.flush()
        user = await create_customer(session, channel="telegram",
                                     address=str(next_tg_id()), team_id=team.id)
        ticket = Ticket(user_id=user.id, team_id=team.id, category="Other",
                        status="active")
        session.add(ticket)
        await session.flush()
        tid = ticket.id
        await session.commit()

    with TestClient(app, base_url="http://testserver") as c:
        token = _login(c)
        c.post(f"/tickets/{tid}/note",
               data={"text": "third time this week", "csrf_token": token},
               follow_redirects=False)

    async with db_session() as session:
        from sqlalchemy import select
        note = (await session.execute(
            select(Message).where(Message.ticket_id == tid,
                                  Message.is_internal == True)  # noqa: E712
        )).scalar_one()
    assert note.text == "third time this week"


async def test_the_landing_page_follows_the_preference(initialized_db):
    op = await _owner_id()
    await preferences_service.save(op, {"landing": "/board"})
    with TestClient(app, base_url="http://testserver") as c:
        _login(c)
        r = c.get("/", follow_redirects=False)
    assert r.headers["location"] == "/board"


async def test_without_a_preference_the_logo_still_goes_to_tickets(initialized_db):
    with TestClient(app, base_url="http://testserver") as c:
        _login(c)
        r = c.get("/", follow_redirects=False)
    assert r.headers["location"] == "/tickets"


async def test_the_standing_queue_only_stands_in_where_nothing_was_asked(
    initialized_db, channel
):
    """The form field means "keep going for this click". The preference means
    "imply that click every time" — and only where the field is empty, so a
    plain Send from a ticket opened out of the blue still leaves the operator
    where they were."""
    import src.dashboard.routes.tickets as tickets_route

    class _Req:
        state = type("S", (), {"prefs": {"queue_advance": True}})()

    class _ReqOff:
        state = type("S", (), {"prefs": {"queue_advance": False}})()

    assert tickets_route._standing_queue(_Req()) == "open"
    assert tickets_route._standing_queue(_ReqOff()) == ""
