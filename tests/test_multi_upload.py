"""Attaching several files to one reply.

The rule the tests exist to hold: nothing is sent until everything has been
checked. An operator who attaches five files and gets an error on the fourth
cannot un-send the three the customer already has.
"""

from fastapi.testclient import TestClient
from sqlalchemy import select

from src.dashboard.app import app
from src.dashboard.routes import tickets as tickets_route
from src.database.db import db_session
from src.database.models import Message, Team, Ticket, User
from tests.conftest import next_tg_id


def _login(c: TestClient) -> str:
    c.get("/login")
    token = c.cookies.get("supbot_csrf")
    r = c.post("/login", data={"email": "admin@local", "password": "test-password-123",
                               "next": "/", "csrf_token": token}, follow_redirects=False)
    assert r.status_code == 303
    return c.cookies.get("supbot_csrf") or token


class _Doc:
    def __init__(self, file_id):
        self.file_id = file_id


class _Sent:
    def __init__(self, file_id):
        self.document = _Doc(file_id)


class _Bot:
    """Records what reached the customer, and can fail on the Nth send."""

    def __init__(self, fail_on: int | None = None):
        self.sent: list[dict] = []
        self.fail_on = fail_on

    async def send_document(self, chat_id, document, caption=None, **kw):
        # Mirrors into the admin chat pass a file_id string, not an upload.
        is_upload = hasattr(document, "filename")
        if is_upload and self.fail_on is not None and len(self.to_user) + 1 == self.fail_on:
            raise RuntimeError("Telegram said no")
        self.sent.append({"chat_id": chat_id, "caption": caption,
                          "filename": getattr(document, "filename", None),
                          "upload": is_upload})
        return _Sent(f"fid-{len(self.sent)}")

    @property
    def to_user(self):
        return [s for s in self.sent if s["upload"]]


async def _ticket() -> int:
    async with db_session() as session:
        team = Team(name=f"up-{next_tg_id()}")
        session.add(team)
        await session.flush()
        u = User(tg_id=next_tg_id(), username="up_user", team_id=team.id,
                 is_verified=True, is_active=True)
        session.add(u)
        await session.flush()
        t = Ticket(user_id=u.id, team_id=team.id, category="Network", status="active")
        session.add(t)
        await session.flush()
        session.add(Message(ticket_id=t.id, sender_type="user", sender_id=u.tg_id,
                            text="here is the problem"))
        await session.commit()
        return t.id


async def _files_on(ticket_id: int) -> list[tuple[str, str]]:
    async with db_session() as session:
        rows = (await session.execute(
            select(Message).where(Message.ticket_id == ticket_id,
                                  Message.sender_type == "admin")
            .order_by(Message.id.asc())
        )).scalars().all()
        return [(m.file_name, m.text) for m in rows if m.file_name]


def _post(c, tid, token, files, text=""):
    return c.post(f"/tickets/{tid}/reply",
                  data={"text": text, "csrf_token": token},
                  files=[("attachments", f) for f in files],
                  follow_redirects=False)


async def test_every_attached_file_reaches_the_customer(initialized_db, monkeypatch):
    bot = _Bot()
    monkeypatch.setattr("src.dashboard.bot_client.get_bot", lambda: bot)
    tid = await _ticket()

    with TestClient(app, base_url="http://testserver") as c:
        token = _login(c)
        r = _post(c, tid, token, [
            ("a.png", b"one", "image/png"),
            ("b.png", b"two", "image/png"),
            ("c.pdf", b"three", "application/pdf"),
        ], text="all three logs")

    assert r.status_code == 303 and "sent=1" in r.headers["location"]
    assert [s["filename"] for s in bot.to_user] == ["a.png", "b.png", "c.pdf"]
    assert [n for n, _ in await _files_on(tid)] == ["a.png", "b.png", "c.pdf"]


async def test_the_text_is_a_caption_once_not_on_every_file(initialized_db, monkeypatch):
    """Repeating it under each attachment would read as three separate replies."""
    bot = _Bot()
    monkeypatch.setattr("src.dashboard.bot_client.get_bot", lambda: bot)
    tid = await _ticket()

    with TestClient(app, base_url="http://testserver") as c:
        token = _login(c)
        _post(c, tid, token, [("a.png", b"x", "image/png"),
                              ("b.png", b"y", "image/png")], text="see attached")

    captions = [s["caption"] for s in bot.to_user]
    assert captions == ["see attached", None]


async def test_one_file_still_behaves_exactly_as_before(initialized_db, monkeypatch):
    bot = _Bot()
    monkeypatch.setattr("src.dashboard.bot_client.get_bot", lambda: bot)
    tid = await _ticket()

    with TestClient(app, base_url="http://testserver") as c:
        token = _login(c)
        r = _post(c, tid, token, [("solo.txt", b"hello", "text/plain")], text="here")

    assert "sent=1" in r.headers["location"]
    assert len(bot.to_user) == 1
    assert await _files_on(tid) == [("solo.txt", "here")]


async def test_an_oversized_file_stops_the_whole_batch(initialized_db, monkeypatch):
    """The point of checking up front: the small ones must not go on ahead."""
    bot = _Bot()
    monkeypatch.setattr("src.dashboard.bot_client.get_bot", lambda: bot)
    monkeypatch.setattr(tickets_route, "_MAX_UPLOAD_BYTES", 10)
    tid = await _ticket()

    with TestClient(app, base_url="http://testserver") as c:
        token = _login(c)
        r = _post(c, tid, token, [
            ("small.txt", b"tiny", "text/plain"),
            ("huge.bin", b"x" * 50, "application/octet-stream"),
        ])

    assert "err=" in r.headers["location"]
    assert bot.sent == [], "nothing may be sent when any file is rejected"
    assert await _files_on(tid) == []


async def test_too_many_files_is_refused_before_sending(initialized_db, monkeypatch):
    bot = _Bot()
    monkeypatch.setattr("src.dashboard.bot_client.get_bot", lambda: bot)
    monkeypatch.setattr(tickets_route, "_MAX_UPLOAD_FILES", 2)
    tid = await _ticket()

    with TestClient(app, base_url="http://testserver") as c:
        token = _login(c)
        r = _post(c, tid, token, [(f"f{i}.txt", b"x", "text/plain") for i in range(3)])

    assert "err=" in r.headers["location"]
    assert bot.sent == []


async def test_a_failure_midway_reports_how_far_it_got(initialized_db, monkeypatch):
    """Three of five delivered is not a failure to retry blindly."""
    bot = _Bot(fail_on=3)
    monkeypatch.setattr("src.dashboard.bot_client.get_bot", lambda: bot)
    tid = await _ticket()

    with TestClient(app, base_url="http://testserver") as c:
        token = _login(c)
        r = _post(c, tid, token, [(f"f{i}.txt", b"x", "text/plain") for i in range(5)])

    assert "err=" in r.headers["location"]
    assert len(bot.to_user) == 2, "it must stop, not push on through"
    # What did leave is recorded, or the operator would resend it.
    assert [n for n, _ in await _files_on(tid)] == ["f0.txt", "f1.txt"]


async def test_a_text_reply_survives_the_empty_file_part(initialized_db, monkeypatch):
    """A browser posts the file field even when nothing was picked, and a part
    with no filename arrives as a string. Typing list[UploadFile] rejects that
    with a 422 — which would break every ordinary text reply, not just uploads.
    """
    bot = _Bot()
    monkeypatch.setattr("src.dashboard.bot_client.get_bot", lambda: bot)
    tid = await _ticket()

    with TestClient(app, base_url="http://testserver") as c:
        token = _login(c)
        r = c.post(f"/tickets/{tid}/reply",
                   data={"text": "no attachment here", "csrf_token": token},
                   files=[("attachments", ("", b"", "application/octet-stream"))],
                   follow_redirects=False)
        empty = c.post(f"/tickets/{tid}/reply",
                       data={"text": "", "csrf_token": token},
                       files=[("attachments", ("", b"", "application/octet-stream"))],
                       follow_redirects=False)

    assert r.status_code == 303, "an empty file part must not fail validation"
    assert bot.to_user == [], "an empty part is not an attachment"
    assert "err=" in empty.headers["location"], "no text and no file is an empty reply"


async def test_the_composer_advertises_the_same_limits(initialized_db):
    """A pick the browser accepts and the route rejects wastes the upload."""
    tid = await _ticket()
    with TestClient(app, base_url="http://testserver") as c:
        _login(c)
        html = c.get(f"/tickets/{tid}").text

    assert f'data-max-files="{tickets_route._MAX_UPLOAD_FILES}"' in html
    assert f'data-max-bytes="{tickets_route._MAX_UPLOAD_BYTES}"' in html
    assert 'name="attachments" multiple' in html
