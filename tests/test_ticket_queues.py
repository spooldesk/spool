"""Separate live work from history without losing search or export scope."""

from datetime import datetime
from io import BytesIO
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient
from openpyxl import load_workbook
from sqlalchemy import delete

from src.dashboard import auth, i18n
from src.dashboard.app import app
from src.database.db import db_session
from src.database.models import Team, Ticket, User
from src.services import admin_user_service
from tests.conftest import next_tg_id


@pytest.fixture()
async def queue_client(initialized_db):
    owner = await admin_user_service.create_admin(
        email=f"queues-{next_tg_id()}@example.test", password="test-queues-password", role="owner")
    async with db_session() as session:
        team = Team(name=f"queues-{next_tg_id()}")
        session.add(team)
        await session.flush()
        user = User(tg_id=next_tg_id(), team_id=team.id)
        session.add(user)
        await session.flush()
        for i in range(30):
            session.add(Ticket(user_id=user.id, team_id=team.id,
                               conversation_key=f"queue-{i}", category="Queue marker",
                               status="active" if i < 2 else "closed", stage="new",
                               closed_at=None if i < 2 else datetime.utcnow()))
        await session.commit()
        team_id = team.id
    try:
        with TestClient(app) as client:
            client.cookies.set(auth.SESSION_COOKIE, auth.make_session_cookie(owner.id))
            yield client, team_id
    finally:
        # The suite shares a disposable DB: do not fill another test's board cap.
        async with db_session() as session:
            await session.execute(delete(Ticket).where(Ticket.team_id == team_id))
            await session.commit()


@pytest.mark.parametrize("status", [None, "", "all", "invalid", "active"])
async def test_default_and_legacy_all_views_show_only_live_work(queue_client, status):
    client, team = queue_client
    params = {"team_id": team}
    if status is not None:
        params["status"] = status
    page = client.get("/tickets", params=params)
    assert page.status_code == 200
    assert page.context["total"] == 2
    assert {r["status"] for r in page.context["rows"]} == {"active"}
    assert page.context["status_counts"] == {"active": 2, "closed": 28}
    assert 'name="status" value="active"' in page.text
    assert '<select name="status"' not in page.text


async def test_archive_switch_reset_pagination_and_export_keep_their_scope(queue_client):
    client, team = queue_client
    page = client.get("/tickets", params={"status": "closed", "team_id": team,
                     "stage": "waiting", "q": "Queue marker", "page": 1})
    assert page.status_code == 200
    assert page.context["total"] == 28
    assert len(page.context["rows"]) == 3
    assert {r["status"] for r in page.context["rows"]} == {"closed"}
    assert page.context["filters"]["stage"] == ""
    assert 'name="stage"' not in page.text
    assert page.context["reset_url"] == "/tickets?status=closed"
    switch = parse_qs(urlsplit(page.context["status_urls"]["active"]).query)
    assert switch == {"team_id": [str(team)], "q": ["Queue marker"], "status": ["active"]}
    previous = parse_qs(urlsplit(page.context["prev_url"]).query)
    assert previous["status"] == ["closed"]
    exported = client.get(page.context["export_url"])
    book = load_workbook(BytesIO(exported.content), read_only=True)
    records = list(book.active.values)[1:]
    assert len(records) == 28
    assert {row[1] for row in records} == {"closed"}
    ticket = client.get(f'/tickets/{page.context["rows"][0]["id"]}')
    assert 'href="/tickets?status=closed" id="back-link"' in ticket.text


async def test_ticket_survives_an_older_translation_catalog(queue_client, monkeypatch):
    client, team = queue_client
    page = client.get("/tickets", params={"team_id": team})
    tid = page.context["rows"][0]["id"]
    original = i18n.strings
    def stale(language):
        return {k: v for k, v in original(language).items()
                if k not in {"composer_uncertain", "session_expired"}}
    monkeypatch.setattr(i18n, "strings", stale)
    result = client.get(f"/tickets/{tid}")
    assert result.status_code == 200
    assert "SpComposerSubmit(" in result.text
