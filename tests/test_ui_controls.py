"""The styled select and date picker.

They are progressive enhancement: the native controls stay in the DOM and hold
the value, so most of what matters is testable only in a browser. What is
pinned here is the contract that broke during development — several filters are
wired as onchange="this.requestSubmit()", and a widget that sets the value
without announcing it turns those into controls that do nothing when used.
"""

from fastapi.testclient import TestClient

from src.dashboard.app import app


def _login(c: TestClient) -> str:
    c.get("/login")
    token = c.cookies.get("supbot_csrf")
    r = c.post("/login", data={"email": "admin@local", "password": "test-password-123",
                               "next": "/", "csrf_token": token}, follow_redirects=False)
    assert r.status_code == 303
    return c.cookies.get("supbot_csrf") or token


def _ui_js() -> str:
    with TestClient(app, base_url="http://testserver") as c:
        r = c.get("/static/ui.js")
    assert r.status_code == 200
    return r.text


def test_picking_an_option_announces_the_change():
    js = _ui_js()
    assert "if (changed) fire(select);" in js, (
        "selecting an option must dispatch change — filters auto-submit on it"
    )
    assert "new Event('change', { bubbles: true })" in js, (
        "the event has to bubble to reach a handler on the form"
    )


def test_the_native_controls_are_kept():
    """With JavaScript off the page must still filter, so the real <select>
    and date input stay in the DOM and keep submitting."""
    js = _ui_js()
    assert "root.append(select, button, panel)" in js
    assert "root.append(input, button, panel)" in js
    assert "select.remove()" not in js and "input.remove()" not in js


def test_the_widgets_are_actually_wired_up():
    with TestClient(app, base_url="http://testserver") as c:
        _login(c)
        page = c.get("/tickets").text
    assert "/static/ui.js" in page, "the enhancement script is not loaded"
    assert "window.UI_WEEKDAYS" in page, "the calendar has no localised weekdays"


def test_calendar_labels_come_from_the_catalog():
    """ui.js is a static file, outside Jinja — it cannot translate itself."""
    with TestClient(app, base_url="http://testserver") as c:
        _login(c)
        page = c.get("/tickets").text
    for name in ("UI_WEEKDAYS", "UI_TODAY", "UI_CLEAR"):
        assert f"window.{name} = " in page
    assert "Undefined" not in page


# ---------- navigation affordances ----------

async def test_back_link_is_addressable_and_defaults_to_the_list(initialized_db):
    """ui.js rewrites this to wherever the ticket was opened from; without
    JavaScript it still has to be a working link."""
    from src.dashboard.app import app
    from src.database.db import db_session
    from src.database.models import Team, Ticket, User
    from tests.conftest import next_tg_id

    async with db_session() as session:
        team = Team(name=f"back-{next_tg_id()}")
        session.add(team)
        await session.flush()
        u = User(tg_id=next_tg_id(), username="back_user", team_id=team.id,
                 is_verified=True, is_active=True)
        session.add(u)
        await session.flush()
        t = Ticket(user_id=u.id, team_id=team.id, category="Network", status="active")
        session.add(t)
        await session.flush()
        tid = t.id
        await session.commit()

    with TestClient(app, base_url="http://testserver") as c:
        c.get("/login")
        token = c.cookies.get("supbot_csrf")
        c.post("/login", data={"email": "admin@local", "password": "test-password-123",
                               "next": "/", "csrf_token": token}, follow_redirects=False)
        html = c.get(f"/tickets/{tid}").text

    assert 'id="back-link"' in html
    assert 'href="/tickets"' in html


def test_ui_js_defines_the_back_helpers_before_it_runs_them():
    """The script is deferred, so enhanceAll() fires the moment it parses.
    Anything it calls must already be initialised — a `var` assigned further
    down the file is still undefined at that point, and the whole enhancement
    pass dies on the TypeError."""
    from pathlib import Path

    src = Path("src/dashboard/static/ui.js").read_text(encoding="utf-8")
    assert src.index("var LIST_PAGES") < src.index("function enhanceAll")
    assert src.index("function rememberList") < src.index("enhanceAll();")


def test_board_cards_open_on_a_plain_click():
    from pathlib import Path

    src = Path("src/dashboard/templates/board.html").read_text(encoding="utf-8")
    # Real controls keep their own behaviour...
    assert "e.target.closest('a, button')" in src
    # ...and a pointer that travelled was a drag, not a click.
    assert "pressAt" in src


def test_widgets_take_their_height_from_the_control_they_replace():
    """The widget's own padding classes cannot be the answer: the same select
    is styled px-2 py-1.5 on one page and px-3 py-2 on another, and a widget
    that ignores that ends up shorter than the inputs beside it."""
    from pathlib import Path

    src = Path("src/dashboard/static/ui.js").read_text(encoding="utf-8")
    assert "function matchHeight" in src
    # Measured before the original is hidden — afterwards it is 1px tall.
    assert src.index("matchHeight(button, select)") < src.index("select.classList.add('sr-only-select')")
    assert src.index("matchHeight(button, input)") < src.index("input.classList.add('sr-only-select')")


async def test_every_page_gets_the_same_left_gutter(initialized_db):
    """Page padding used to be each template's own business and had drifted:
    most had p-6, the board had 4px and Overview, AI, Templates and Broadcast
    had none, so their headings sat flush against the sidebar. It lives on
    <main> now; only the pages that fill the viewport themselves opt out.
    """
    from src.dashboard.app import app

    padded = ["/tickets", "/board", "/users", "/overview", "/ai", "/templates",
              "/reports", "/manage/templates", "/manage/categories",
              "/manage/close-reasons", "/manage/teams", "/manage/tags",
              "/manage/settings", "/manage/ai", "/manage/admins", "/broadcast"]

    with TestClient(app, base_url="http://testserver") as c:
        c.get("/login")
        token = c.cookies.get("supbot_csrf")
        c.post("/login", data={"email": "admin@local", "password": "test-password-123",
                               "next": "/", "csrf_token": token}, follow_redirects=False)
        for path in padded:
            r = c.get(path)
            assert r.status_code == 200, f"{path} -> {r.status_code}"
            main = r.text[r.text.index("<main"):r.text.index(">", r.text.index("<main"))]
            assert "p-6" in main, f"{path} lost its gutter"
            # A leftover wrapper would double it back to 48px.
            assert '<div class="p-6' not in r.text, f"{path} pads twice"


async def test_an_id_wider_than_the_database_is_not_found_not_a_crash(initialized_db):
    """FastAPI parses any number of digits into a Python int and the driver
    only objects when it binds it, so an oversized id used to surface as a 500
    from inside the query. Every int path parameter had it.
    """
    from src.dashboard.app import app

    huge = "9" * 25
    with TestClient(app, base_url="http://testserver",
                    raise_server_exceptions=False) as c:
        c.get("/login")
        token = c.cookies.get("supbot_csrf")
        c.post("/login", data={"email": "admin@local", "password": "test-password-123",
                               "next": "/", "csrf_token": token}, follow_redirects=False)

        for path in (f"/tickets/{huge}", f"/users/{huge}", f"/tickets/-{huge}"):
            r = c.get(path, follow_redirects=False)
            assert r.status_code == 404, f"{path} -> {r.status_code}"

        # Filters carry the same risk but must not turn the page into a 404 —
        # an unusable value is simply not a filter.
        for path in (f"/tickets?assigned={huge}", f"/tickets?team_id={huge}",
                     f"/tickets?page={huge}", f"/board?assigned={huge}"):
            r = c.get(path, follow_redirects=False)
            assert r.status_code == 200, f"{path} -> {r.status_code}"

        r = c.get(f"/tickets/12/messages.json?after_id={huge}", follow_redirects=False)
        assert r.status_code in (200, 404), f"poll -> {r.status_code}"
