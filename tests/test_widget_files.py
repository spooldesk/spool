"""Attachments on the one channel that has to keep its own.

Every other channel hands back a reference and holds the bytes for us. The
widget has a browser, so this instance is the storage — which makes the
upload endpoint the most exposed thing in the codebase and the download
endpoint the one that must never serve the wrong person's file.

The tests worth having here are therefore mostly refusals: too large, not
yours, not a real reference, not in your transcript. The happy path is one
test, and it is the least interesting one.
"""

import io
import json
import os
import tempfile

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

import src.dashboard.routes.widget as widget_routes
from src.channels.widget import NAME as WIDGET, address_for
from src.dashboard.app import app
from src.database.db import db_session
from src.database.models import Message, User
from src.services import file_store, intake_service, settings_service

# A real PNG, eight bytes of signature and a minimal IHDR. Not a text file
# with a .png name: the point of some of these assertions is that the bytes
# come back unchanged.
PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
    b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


@pytest.fixture(autouse=True)
def _own_store(tmp_path, monkeypatch):
    """Point the store at a directory this test owns.

    Without this the suite writes attachments into the working tree and
    leaves them there, which is the sort of thing nobody notices until the
    repository is a gigabyte.
    """
    monkeypatch.setenv("ATTACHMENTS_DIR", str(tmp_path / "attachments"))


@pytest.fixture(autouse=True)
async def _clean_limits(initialized_db):
    widget_routes._hits.clear()
    yield
    widget_routes._hits.clear()


@pytest.fixture()
async def widget_on(initialized_db):
    await settings_service.set_widget_enabled(True, updated_by=0)
    await settings_service.set_widget_origins("", updated_by=0)
    yield
    await settings_service.set_widget_enabled(False, updated_by=0)


def _upload(c: TestClient, name: str, content: bytes, token: str | None = None,
            text: str = "", mime: str = "application/octet-stream"):
    headers = {"origin": "https://shop.example"}
    if token:
        headers["x-spooldesk-session"] = token
    return c.post("/widget/api/upload", headers=headers,
                  files={"file": (name, io.BytesIO(content), mime)},
                  data={"text": text})


def _say(c: TestClient, text: str, token: str | None = None, **body):
    headers = {"origin": "https://shop.example"}
    if token:
        headers["x-spooldesk-session"] = token
    return c.post("/widget/api/message", headers=headers,
                  content=json.dumps({"text": text, **body}))


def _get_file(c: TestClient, message_id: int, token: str | None = None):
    headers = {"origin": "https://shop.example"}
    if token:
        headers["x-spooldesk-session"] = token
    return c.get(f"/widget/api/file/{message_id}", headers=headers)


# ---------------------------------------------------------------- the store


def test_the_store_never_builds_a_path_from_its_caller(tmp_path, monkeypatch):
    """The oldest bug on the internet, and the way not to have it is to make
    the reference something this module invented and nothing else."""
    monkeypatch.setenv("ATTACHMENTS_DIR", str(tmp_path))

    ref = file_store.put(b"real content")
    assert file_store.get(ref) == b"real content"

    for hostile in ("../../etc/passwd", "..", "/etc/passwd", "a" * 32,
                    "", "  ", "0123456789abcdef0123456789abcdeZ"):
        assert file_store.get(hostile) is None, hostile
        assert file_store.delete(hostile) is False, hostile

    # And the real one still works after all that.
    assert file_store.get(ref) == b"real content"


def test_a_reference_survives_a_restart(tmp_path, monkeypatch):
    """Which is the whole reason this is on disk and not in a dict."""
    monkeypatch.setenv("ATTACHMENTS_DIR", str(tmp_path))
    ref = file_store.put(PNG)

    # A fresh import is as close to a restart as a test gets; the store keeps
    # no state between calls, so the reference is all that has to carry over.
    import importlib

    import src.services.file_store as fresh
    importlib.reload(fresh)
    assert fresh.get(ref) == PNG


# --------------------------------------------------------------- uploading


async def test_a_first_contact_can_be_a_screenshot(widget_on):
    """A file is a message. Someone whose whole problem is a screenshot
    should not have to send an empty text message first to get a ticket."""
    with TestClient(app, base_url="http://testserver") as c:
        r = _upload(c, "screenshot.png", PNG, mime="image/png")

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ticket"], "no ticket was opened"
    assert body["session"], "no session was issued"

    files = [m for m in body["messages"] if m.get("file")]
    assert len(files) == 1
    assert files[0]["file"]["name"] == "screenshot.png"
    assert files[0]["file"]["image"] is True
    assert files[0]["file"]["size"] == len(PNG)


async def test_an_upload_carries_its_caption(widget_on):
    with TestClient(app, base_url="http://testserver") as c:
        body = _upload(c, "log.txt", b"stack trace",
                       text="this is what the console said").json()

    said = [m["text"] for m in body["messages"]]
    assert "this is what the console said" in said


async def test_an_oversized_file_is_refused(widget_on, monkeypatch):
    """Refused, not accepted-then-deleted. The cap is enforced while reading
    because Content-Length is a claim and memory is finite."""
    monkeypatch.setattr(widget_routes, "_MAX_UPLOAD_BYTES", 1024)

    with TestClient(app, base_url="http://testserver") as c:
        r = _upload(c, "big.bin", b"x" * 4096)

    assert r.status_code == 413
    assert r.json()["error"] == "too_large"


async def test_an_empty_file_opens_nothing(widget_on):
    with TestClient(app, base_url="http://testserver") as c:
        r = _upload(c, "nothing.txt", b"")
    assert r.status_code == 400


async def test_uploads_are_rate_limited_per_session(widget_on, monkeypatch):
    monkeypatch.setattr(widget_routes, "_UPLOADS_PER_SESSION", 2)

    with TestClient(app, base_url="http://testserver") as c:
        token = _upload(c, "a.txt", b"one").json()["session"]
        assert _upload(c, "b.txt", b"two", token=token).status_code == 200
        assert _upload(c, "c.txt", b"three", token=token).status_code == 200
        assert _upload(c, "d.txt", b"four", token=token).status_code == 429


async def test_a_blocked_visitor_leaves_nothing_on_disk(widget_on):
    """The refusal happens after the bytes are stored — intake is what knows
    the customer is blocked — so the store has to be cleaned up, or blocking
    someone becomes a way to fill a disk."""
    with TestClient(app, base_url="http://testserver") as c:
        token = _upload(c, "first.txt", b"hello").json()["session"]

        async with db_session() as session:
            user = (await session.execute(
                select(User).where(User.channel == WIDGET,
                                   User.channel_user_id == address_for(token))
            )).scalar_one()
            user.is_active = False
            await session.commit()

        before = _count_stored()
        r = _upload(c, "second.txt", b"and again", token=token)

    assert r.status_code == 403
    assert _count_stored() == before, "a refused upload was left on disk"


def _count_stored() -> int:
    root = file_store.root()
    if not root.exists():
        return 0
    return sum(1 for p in root.rglob("*") if p.is_file())


# ------------------------------------------------------------- downloading


async def test_a_visitor_gets_their_own_file_back_byte_for_byte(widget_on):
    with TestClient(app, base_url="http://testserver") as c:
        body = _upload(c, "screenshot.png", PNG, mime="image/png").json()
        token = body["session"]
        msg_id = [m for m in body["messages"] if m.get("file")][0]["id"]

        r = _get_file(c, msg_id, token)

    assert r.status_code == 200
    assert r.content == PNG
    assert r.headers["content-type"] == "image/png"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["content-disposition"].startswith("inline")


async def test_the_content_type_comes_from_us_not_from_the_uploader(widget_on):
    """A stored file that comes back as text/html is stored XSS on our own
    origin. What the uploader called it is not evidence."""
    with TestClient(app, base_url="http://testserver") as c:
        body = _upload(c, "payload.html", b"<script>alert(1)</script>",
                       mime="text/html").json()
        token = body["session"]
        msg_id = [m for m in body["messages"] if m.get("file")][0]["id"]

        r = _get_file(c, msg_id, token)

    assert r.status_code == 200
    assert "html" not in r.headers["content-type"], r.headers["content-type"]
    assert r.headers["content-disposition"].startswith("attachment")


async def test_svg_is_downloaded_rather_than_shown(widget_on):
    """An SVG is an image that can contain script. Rendering one inline from
    our own origin would run it on the page an operator is reading."""
    with TestClient(app, base_url="http://testserver") as c:
        body = _upload(c, "logo.svg", b"<svg xmlns='http://www.w3.org/2000/svg'/>").json()
        token = body["session"]
        entry = [m for m in body["messages"] if m.get("file")][0]

        r = _get_file(c, entry["id"], token)

    assert entry["file"]["image"] is False, "an SVG must not be rendered inline"
    assert r.headers["content-disposition"].startswith("attachment")


async def test_someone_elses_file_is_not_yours(widget_on):
    """Ticket and message ids are small integers. Without an ownership check
    this endpoint is a way to read strangers' support conversations by
    counting."""
    with TestClient(app, base_url="http://testserver") as c:
        theirs = _upload(c, "private.txt", b"my card number is").json()
        msg_id = [m for m in theirs["messages"] if m.get("file")][0]["id"]

        mine = _say(c, "unrelated question").json()

        assert _get_file(c, msg_id, mine["session"]).status_code == 404
        assert _get_file(c, msg_id, None).status_code == 401
        assert _get_file(c, msg_id, "not-a-real-token").status_code == 401


async def test_an_internal_note_is_not_fetchable(widget_on):
    """The one that would actually hurt: a file an operator attached to a
    note about the customer, fetched by the customer."""
    from src.database.models import Message as MessageRecord

    with TestClient(app, base_url="http://testserver") as c:
        opened = _upload(c, "theirs.txt", b"visible").json()
        token, ticket_id = opened["session"], opened["ticket"]

        ref = file_store.put(b"what we really think")
        async with db_session() as session:
            note = MessageRecord(
                ticket_id=ticket_id, sender_type="admin", sender_id=0,
                text="internal", is_internal=True,
                file_id=ref, file_type="document", file_name="notes.txt",
                file_size=20,
            )
            session.add(note)
            await session.commit()
            note_id = note.id

        r = _get_file(c, note_id, token)
        poll = c.get("/widget/api/poll", headers={"x-spooldesk-session": token}).json()

    assert r.status_code == 404
    assert "internal" not in [m["text"] for m in poll["messages"]]


# ------------------------------------------------- the operator's direction


async def test_an_operator_attachment_reaches_the_visitor(widget_on):
    """Through the same reply route Telegram uses, not a shortcut."""
    with TestClient(app, base_url="http://testserver") as c:
        opened = _say(c, "the invoice is wrong").json()
        token, ticket_id = opened["session"], opened["ticket"]

        c.get("/login")
        csrf = c.cookies.get("supbot_csrf")
        signed_in = c.post("/login", data={
            "email": "admin@local", "password": "test-password-123",
            "next": "/", "csrf_token": csrf,
        }, follow_redirects=False)
        assert signed_in.status_code == 303, "could not sign in as an operator"

        sent = c.post(
            f"/tickets/{ticket_id}/reply",
            data={"text": "Here is the corrected one.", "csrf_token": csrf},
            files={"attachments": ("invoice.pdf", io.BytesIO(b"%PDF-1.4 fixed"),
                                   "application/pdf")},
            follow_redirects=False,
        )
        assert sent.status_code == 303, sent.text

        seen = c.get("/widget/api/poll",
                     headers={"x-spooldesk-session": token}).json()
        arrived = [m for m in seen["messages"] if m.get("file")]
        assert arrived, "the operator's file never reached the visitor"
        assert arrived[0]["file"]["name"] == "invoice.pdf"

        got = _get_file(c, arrived[0]["id"], token)

    assert got.status_code == 200
    assert got.content == b"%PDF-1.4 fixed"


async def test_the_channel_now_admits_it_can_take_files(widget_on):
    """The reply route asks before reading anything, so a widget that says no
    here refuses every operator attachment with an error."""
    from src.channels import get as get_channel

    assert get_channel(WIDGET).caps.files is True


# --------------------------------------------------------- the panel's view


async def test_the_conversation_list_is_the_visitors_own(widget_on):
    with TestClient(app, base_url="http://testserver") as c:
        mine = _say(c, "where is my order").json()
        _say(c, "a different person entirely")

        listed = c.get("/widget/api/conversations",
                       headers={"x-spooldesk-session": mine["session"]}).json()

    assert [c_["id"] for c_ in listed["conversations"]] == [mine["ticket"]]
    assert listed["conversations"][0]["title"] == "where is my order"
    assert listed["conversations"][0]["open"] is True


async def test_a_stranger_asking_for_a_thread_is_told_nothing(widget_on):
    with TestClient(app, base_url="http://testserver") as c:
        theirs = _say(c, "confidential").json()
        mine = _say(c, "mine").json()

        r = c.get(f"/widget/api/thread/{theirs['ticket']}",
                  headers={"x-spooldesk-session": mine["session"]})
        anon = c.get(f"/widget/api/thread/{theirs['ticket']}")

    assert r.status_code == 404
    assert anon.status_code == 401


async def test_a_visitor_with_no_history_gets_an_empty_list_not_an_error(widget_on):
    """A first-time visitor has done nothing wrong, and an error screen is a
    poor way to tell them so."""
    with TestClient(app, base_url="http://testserver") as c:
        r = c.get("/widget/api/conversations")

    assert r.status_code == 200
    assert r.json()["conversations"] == []


async def test_the_panel_is_told_the_paperclip_is_safe_to_show(widget_on):
    with TestClient(app, base_url="http://testserver") as c:
        info = c.get("/widget/api/hello").json()

    assert info["files"] is True
    assert info["max_file_bytes"] == widget_routes._MAX_UPLOAD_BYTES
    for key in ("attach", "download", "file_too_large", "file_failed"):
        assert info.get(key), f"the panel has no wording for {key}"
