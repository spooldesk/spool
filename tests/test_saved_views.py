"""Named filter sets.

Small feature, but two of its properties are worth pinning: a view belongs to
one operator and nobody else can reach it, and a saved view is a filter rather
than a snapshot of where you happened to be standing.
"""

from fastapi.testclient import TestClient
from sqlalchemy import select

from src.dashboard.app import app
from src.database.db import db_session
from src.database.models import AdminUser, SavedView
from src.services import saved_view_service


def _login(c: TestClient) -> str:
    c.get("/login")
    token = c.cookies.get("supbot_csrf")
    r = c.post("/login", data={"email": "admin@local", "password": "test-password-123",
                               "next": "/", "csrf_token": token}, follow_redirects=False)
    assert r.status_code == 303
    return token


async def _owner_id() -> int:
    async with db_session() as session:
        return (await session.execute(
            select(AdminUser.id).where(AdminUser.email == "admin@local")
        )).scalar_one()


async def test_a_view_keeps_the_filters_and_drops_the_page(initialized_db):
    """A view is a filter, not a bookmark. Remembering it was saved on page 4
    sends the operator somewhere confusing every single time."""
    op = await _owner_id()
    view = await saved_view_service.save(
        op, name="Overdue mine", path="/tickets",
        query="assigned=me&stage=waiting&page=4",
    )
    assert view is not None
    assert "page" not in view.query
    assert "assigned=me" in view.query and "stage=waiting" in view.query


async def test_the_same_filters_in_another_order_are_the_same_view(initialized_db):
    op = await _owner_id()
    a = saved_view_service.normalise_query("stage=waiting&assigned=me")
    b = saved_view_service.normalise_query("assigned=me&stage=waiting")
    assert a == b


async def test_saving_the_same_name_updates_rather_than_refusing(initialized_db):
    """Typing the name again means "make this one that". Demanding a delete
    first to achieve it is a worse answer than just doing it."""
    op = await _owner_id()
    await saved_view_service.save(op, name="Hot", path="/tickets", query="stage=new")
    await saved_view_service.save(op, name="Hot", path="/tickets", query="stage=waiting")

    views = [v for v in await saved_view_service.list_for(op, "/tickets") if v.name == "Hot"]
    assert len(views) == 1, "a duplicate name produced a second view"
    assert "waiting" in views[0].query


async def test_a_view_belongs_to_one_operator(initialized_db):
    op = await _owner_id()
    await saved_view_service.save(op, name="Only mine", path="/tickets", query="stage=new")

    stranger = op + 9999
    assert await saved_view_service.list_for(stranger) == []


async def test_you_cannot_delete_someone_elses(initialized_db):
    """Scoped by owner, so an id guessed from elsewhere reaches nothing."""
    op = await _owner_id()
    view = await saved_view_service.save(op, name="Protected", path="/tickets",
                                         query="stage=new")

    assert await saved_view_service.remove(op + 9999, view.id) is False
    assert await saved_view_service.remove(op, view.id) is True


async def test_a_view_belongs_to_the_page_it_was_saved_from(initialized_db):
    """The same filters mean different things on the list and on the board."""
    op = await _owner_id()
    await saved_view_service.save(op, name="Board only", path="/board", query="assigned=none")

    assert "Board only" not in [v.name for v in await saved_view_service.list_for(op, "/tickets")]
    assert "Board only" in [v.name for v in await saved_view_service.list_for(op, "/board")]


async def test_junk_is_refused_rather_than_stored(initialized_db):
    op = await _owner_id()
    assert await saved_view_service.save(op, name="   ", path="/tickets", query="a=1") is None
    assert await saved_view_service.save(op, name="ok", path="/evil", query="a=1") is None


async def test_the_csrf_token_never_becomes_part_of_a_view(initialized_db):
    """It arrives on the form that saves the view, and storing it would put a
    stale token into a link the operator clicks for months."""
    op = await _owner_id()
    view = await saved_view_service.save(
        op, name="Clean", path="/tickets", query="stage=new&csrf_token=abc123",
    )
    assert "csrf_token" not in view.query


async def test_saving_and_applying_from_the_page(initialized_db):
    with TestClient(app, base_url="http://testserver") as c:
        token = _login(c)
        r = c.post("/views/save",
                   data={"name": "From the page", "path": "/tickets",
                         "query": "stage=new&assigned=me", "csrf_token": token},
                   follow_redirects=False)
        assert r.status_code == 303
        # It lands you on the filters you just named.
        assert "stage=new" in r.headers["location"]

        page = c.get("/tickets").text
        assert "From the page" in page, "the chip should appear on the list"


async def test_the_strip_only_offers_saving_when_there_is_something_to_save(initialized_db):
    """An empty filter set named "everything" is a chip that does nothing."""
    from pathlib import Path

    strip = Path("src/dashboard/templates/_saved_views.html").read_text(encoding="utf-8")
    assert "{% if current_query %}" in strip


async def test_there_is_a_limit(initialized_db):
    op = await _owner_id()
    for i in range(saved_view_service.MAX_PER_OPERATOR + 5):
        await saved_view_service.save(op, name=f"v{i}", path="/tickets", query=f"q={i}")

    assert len(await saved_view_service.list_for(op)) <= saved_view_service.MAX_PER_OPERATOR
