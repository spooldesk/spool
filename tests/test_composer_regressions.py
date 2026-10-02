"""Three regressions found by an operator, not by this suite.

Worth their own file, because they share a shape: something was made
conditional or cancelled, and code that assumed otherwise failed silently.
Silently is the important word — no exception reached a log, no test went
red, and the only symptom was a button that did nothing.

The composer's behaviour lives in a <script> in the template, so these check
the rendered page rather than a Python function. That is a weaker test than
driving a browser, and it is the one that would have caught all three.
"""

import re
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

import src.channels as channels
from src.channels.base import Capabilities
from src.dashboard.app import app
from src.database.db import db_session
from src.database.models import Message, Team, Ticket
from tests.conftest import RecordingChannel, next_tg_id


class _NoFiles(RecordingChannel):
    """A channel that carries no files — the widget, or Slack without the
    scope. The composer has to survive it."""

    name = "nofiles"

    def __init__(self):
        super().__init__(name="nofiles",
                         caps=Capabilities(unsend=True, files=False, buttons=True))


def _login(c: TestClient) -> str:
    c.get("/login")
    token = c.cookies.get("supbot_csrf")
    r = c.post("/login", data={"email": "admin@local", "password": "test-password-123",
                               "next": "/", "csrf_token": token}, follow_redirects=False)
    assert r.status_code == 303
    return c.cookies.get("supbot_csrf") or token


async def _ticket_on(channel_name: str) -> int:
    from src.channels.identity import create_customer

    async with db_session() as session:
        team = Team(name=f"cr-{next_tg_id()}")
        session.add(team)
        await session.flush()
        # Telegram's address is its routing key and has to be a number;
        # every other channel takes a string.
        address = (str(next_tg_id()) if channel_name == "telegram"
                   else f"addr-{next_tg_id()}")
        user = await create_customer(session, channel=channel_name,
                                     address=address, team_id=team.id)
        ticket = Ticket(user_id=user.id, team_id=team.id, category="Other",
                        status="active")
        session.add(ticket)
        await session.flush()
        session.add(Message(ticket_id=ticket.id, sender_type="user",
                            sender_id=user.tg_id, text="hello"))
        await session.flush()
        tid = ticket.id
        await session.commit()
    return tid


def _page(tid: int) -> str:
    with TestClient(app, base_url="http://testserver") as c:
        _login(c)
        return c.get(f"/tickets/{tid}").text


@pytest.mark.parametrize("enabled,provider,key,visible", [
    (False, "anthropic", "test-key", False),
    (True, "anthropic", None, False),
    (True, "anthropic", "test-key", False),
    (True, "ollama", None, False),
])
async def test_manual_ai_button_is_absent_regardless_of_provider(
    initialized_db, monkeypatch, enabled, provider, key, visible,
):
    from src.services import settings_service

    monkeypatch.setattr(settings_service, "get_ai_enabled", AsyncMock(return_value=enabled))
    monkeypatch.setattr(settings_service, "get_ai_provider", AsyncMock(return_value=provider))
    monkeypatch.setattr(settings_service, "get_ai_key", AsyncMock(return_value=key))
    monkeypatch.setattr(settings_service, "is_category_ai_blacklisted", AsyncMock(return_value=False))
    channels.register(_NoFiles())
    try:
        body = _page(await _ticket_on("nofiles"))
    finally:
        channels.reset()
    assert ('id="ai-btn"' in body) is visible
    assert 'id="file-input"' in body  # Removing AI must not break the composer.


# ------------------------------------------------- the composer stays alive


async def test_the_file_input_exists_even_where_files_cannot_be_sent(initialized_db):
    """The composer's script reads this element on load. Making the element
    itself conditional killed every behaviour registered after that line —
    the reply/note toggle, Ctrl+Enter, paste-a-screenshot — on any channel
    that carries no files, with no error anyone would notice.

    The invariant, not a null-check: a null-check has to be remembered by
    every line added afterwards.
    """
    channels.register(_NoFiles())
    try:
        page = _page(await _ticket_on("nofiles"))
    finally:
        channels.reset()

    assert 'id="file-input"' in page, "the element the script reads must always exist"
    assert "disabled" in page.split('id="file-input"')[1][:200], \
        "it should be there but inert"
    # And the affordance is gone, which was the point of the change.
    assert 'for="file-input"' not in page, "offered a button for something it cannot do"


async def test_the_button_is_offered_where_files_do_work(initialized_db, channel):
    page = _page(await _ticket_on("telegram"))
    assert 'id="file-input"' in page
    assert 'for="file-input"' in page, "a channel that carries files should say so"
    assert "disabled" not in page.split('id="file-input"')[1][:200]


async def test_nothing_in_the_composer_script_runs_before_it_is_guarded(initialized_db):
    """A structural check, because the failure was structural: the script
    dereferenced `fileInput` on the line after looking it up, so anything
    below that line never ran when the lookup returned null."""
    channels.register(_NoFiles())
    try:
        page = _page(await _ticket_on("nofiles"))
    finally:
        channels.reset()

    # Everything the operator needs must still be wired below the file code.
    for wired in ("mode-note", "reply-text", "composer-submit"):
        assert f"getElementById('{wired}')" in page, f"{wired} is no longer wired up"


# ------------------------------------------------- the one-submit guard


def test_a_cancelled_confirmation_does_not_leave_a_dead_button():
    """The guard ran on the capture phase, before a form's own
    `onsubmit="return confirm(...)"`. Saying no left the button disabled for
    ten seconds with nothing having happened — most visible when closing
    tickets one after another, which is the rhythm the queue mode encourages.
    """
    from pathlib import Path

    base = Path("src/dashboard/templates/app_base.html").read_text(encoding="utf-8")
    guard = base[base.index("One submit per click"):]

    assert "e.defaultPrevented" in guard, \
        "the guard still commits to submits that were cancelled"
    # The listener must not be on capture, or it runs before the confirmation.
    listener = guard[guard.index("addEventListener('submit'"):]
    assert re.search(r"\}, false\);", listener), \
        "the guard is back on the capture phase, ahead of onsubmit"


def test_the_close_form_still_asks_before_closing():
    """The fix above only makes sense while there is a confirmation to
    cancel; without one, nothing would ever set defaultPrevented and the
    check would be dead code that looks load-bearing."""
    from pathlib import Path

    page = Path("src/dashboard/templates/ticket_detail.html").read_text(encoding="utf-8")
    close = page[page.index('action="/tickets/{{ t.id }}/close"'):][:400]
    assert "onsubmit" in close and "confirm(" in close


# ------------------------------------------------- the file reference


async def test_a_delivered_file_is_recorded_with_something_to_fetch_it_by(
    initialized_db, channel
):
    """The reference used to travel in `Delivery.error_detail` — the field
    named for failures. Telegram happened to fill it; Slack did not, so an
    operator's own attachment was stored with no reference at all and the
    ticket page showed no link."""
    tid = await _ticket_on("telegram")
    with TestClient(app, base_url="http://testserver") as c:
        token = _login(c)
        r = c.post(f"/tickets/{tid}/reply",
                   data={"text": "the log", "csrf_token": token},
                   files=[("attachments", ("app.log", b"contents", "text/plain"))],
                   follow_redirects=False)
    assert r.status_code == 303 and "err=" not in r.headers["location"]

    async with db_session() as session:
        from sqlalchemy import select
        row = (await session.execute(
            select(Message).where(Message.ticket_id == tid,
                                  Message.file_name == "app.log")
        )).scalar_one()
    assert row.file_id, "the attachment was recorded with no way to fetch it back"


@pytest.mark.parametrize("adapter_name", ["telegram", "slack"])
async def test_success_never_travels_in_the_failure_field(adapter_name, monkeypatch):
    """Both adapters return a file reference on success. Neither should be
    putting it in `error_detail`, which is what the caller reads when
    something went wrong."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    import httpx
    from src.channels import slack, telegram

    if adapter_name == "telegram":
        bot = SimpleNamespace(send_document=AsyncMock(return_value=SimpleNamespace(
            message_id=42, document=SimpleNamespace(file_id="stored-file"))))
        adapter = telegram.TelegramChannel(bot)
    else:
        def transport(request):
            if str(request.url).endswith("files.getUploadURLExternal"):
                return httpx.Response(200, json={"ok": True, "upload_url": "https://files.slack.test/upload", "file_id": "stored-file"})
            if str(request.url).endswith("files.completeUploadExternal"):
                return httpx.Response(200, json={"ok": True, "files": [{"id": "stored-file", "timestamp": "42"}]})
            return httpx.Response(200)
        real_client = httpx.AsyncClient
        monkeypatch.setattr(httpx, "AsyncClient", lambda *args, **kwargs:
                            real_client(*args, **{**kwargs, "transport": httpx.MockTransport(transport)}))
        adapter = slack.SlackChannel(token="test-only")
    result = await adapter.send_file("12345", filename="log.txt", content=b"log")
    assert result.ok and result.file_ref == "stored-file" and result.ref == "42"
    assert result.error_detail is None and result.error_key is None
