"""Every channel's ticket reaches the operators' group.

The product is bought on one sentence: your agents answer from Telegram
without learning another inbox. That was true only for tickets that arrived
through Telegram — the forum header was built inside the bot's own intake
handler, so a widget visitor or a Slack conversation opened a ticket that
existed on the board and nowhere else.

These tests are about the promise, not the plumbing: a stranger writes on any
channel, and an operator who only ever looks at Telegram sees them.
"""

import json
import asyncio
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from aiogram.types import Chat, ChatFullInfo, ForumTopic, Message as TelegramMessage

import src.channels as channels
from src.dashboard.app import app
from src.database.db import db_session
from src.database.models import Message, Team, Ticket
from src.services import announce_service, intake_service
from tests.conftest import next_tg_id


class FakeBot:
    """API-shaped responses: only createForumTopic creates a new topic."""

    def __init__(self, *, forum: bool = True, fail: bool = False):
        self.forum = forum
        self.fail = fail
        self.sent: list[dict] = []
        self._next_id = 1000
        self.topics: list[int] = []

    async def get_chat(self, chat_id):
        return ChatFullInfo(id=chat_id, type="supergroup", is_forum=self.forum,
                            accent_color_id=0, max_reaction_count=11,
                            accepted_gift_types={
                                "unlimited_gifts": False, "limited_gifts": False,
                                "unique_gifts": False, "premium_subscription": False,
                                "gifts_from_channels": False,
                            })

    async def create_forum_topic(self, chat_id, name):
        self._next_id += 1
        self.topics.append(self._next_id)
        return ForumTopic(message_thread_id=self._next_id, name=name, icon_color=0x6FB9F0)

    async def delete_forum_topic(self, chat_id, message_thread_id):
        self.topics.remove(message_thread_id)
        return True

    async def send_message(self, chat_id, text, **kw):
        if self.fail:
            raise RuntimeError("admin chat unreachable")
        self._next_id += 1
        thread = kw.get("message_thread_id")
        if thread is None and self.forum:
            thread = 1  # General; sending text does not create a topic.
        self.sent.append({"chat_id": chat_id, "text": text,
                          "thread": kw.get("message_thread_id"),
                          "reply_to": kw.get("reply_to_message_id"),
                          "markup": kw.get("reply_markup")})
        return TelegramMessage(message_id=self._next_id,
                               date=datetime.now(timezone.utc),
                               chat=Chat(id=chat_id, type="supergroup"),
                               text=text, message_thread_id=thread)

    async def send_document(self, document, caption, **kw):
        return await self.send_message(text=caption, **kw)

    @property
    def texts(self) -> list[str]:
        return [m["text"] for m in self.sent]


async def _ticket_from(channel: str, address: str, text: str,
                       *, speaker: str | None = None) -> int:
    from src.channels.identity import create_customer  # noqa: F401

    async with db_session() as session:
        team = Team(name=f"ann-{next_tg_id()}")
        session.add(team)
        await session.flush()
        result = await intake_service.receive(session, intake_service.Inbound(
            channel=channel, address=address, text=text, speaker=speaker,
        ))
        ticket = await session.get(Ticket, result.ticket_id)
        ticket.team_id = team.id
        await session.commit()
    return result.ticket_id


# ---------------------------------------------------------------- the promise


@pytest.mark.parametrize("channel,address", [
    ("widget", "visitor-abc"),
    ("slack", "C0ACME"),
])
async def test_a_ticket_from_any_channel_reaches_the_operators(
    initialized_db, channel, address
):
    """The gap this closes. Before it, only Telegram tickets were visible to
    an operator who never opens the dashboard."""
    tid = await _ticket_from(channel, f"{address}-{next_tg_id()}",
                             "our exports stopped overnight")
    bot = FakeBot()
    assert await announce_service.announce_ticket(bot, tid) is True

    header = bot.texts[0]
    assert f"Ticket ID: {tid}" in header
    assert "Channel:" in header, "an operator cannot tell where to reply"
    # And the customer's actual words, not just a notification that they exist.
    assert any("our exports stopped overnight" in t for t in bot.texts)


async def test_operator_group_receives_read_only_copies(initialized_db):
    tid = await _ticket_from("widget", f"btn-{next_tg_id()}", "help please")
    bot = FakeBot()
    await announce_service.announce_ticket(bot, tid)
    assert bot.sent and all(m['markup'] is None for m in bot.sent)


async def test_a_topic_is_opened_per_ticket_not_per_channel(initialized_db):
    """Two actual createForumTopic responses address two distinct tickets."""
    first = await _ticket_from("widget", f"t1-{next_tg_id()}", "one")
    second = await _ticket_from("widget", f"t2-{next_tg_id()}", "two")
    bot = FakeBot()
    await announce_service.announce_ticket(bot, first)
    await announce_service.announce_ticket(bot, second)

    async with db_session() as session:
        a = await session.get(Ticket, first)
        b = await session.get(Ticket, second)

    assert a.thread_id and b.thread_id
    assert a.thread_id != b.thread_id, "two tickets landed in one topic"
    assert a.header_message_id and b.header_message_id


async def test_the_header_is_sent_into_the_created_topic(initialized_db):
    """The createForumTopic result must be supplied to sendMessage."""
    tid = await _ticket_from("slack", f"C0{next_tg_id()}", "hello")
    bot = FakeBot()
    await announce_service.announce_ticket(bot, tid)
    assert len(bot.topics) == 1
    assert bot.sent[0]["thread"] == bot.topics[0]


async def test_an_ordinary_group_still_works(initialized_db):
    """Not every instance uses a forum. Without topics there is no thread id,
    and the ticket's messages hang off the header as replies instead."""
    tid = await _ticket_from("widget", f"plain-{next_tg_id()}", "hello")
    bot = FakeBot(forum=False)
    assert await announce_service.announce_ticket(bot, tid) is True

    async with db_session() as session:
        ticket = await session.get(Ticket, tid)
    assert ticket.header_message_id is not None
    assert any(m["reply_to"] == ticket.header_message_id for m in bot.sent[1:]), \
        "without a topic the message must quote the header"


# ---------------------------------------------------------------- safety


async def test_announcing_twice_does_not_open_two_topics(initialized_db):
    """Slack resends any event it does not get a prompt 2xx for, and a widget
    visitor can double-submit. Either would otherwise produce two topics for
    one conversation."""
    tid = await _ticket_from("slack", f"C0dup{next_tg_id()}", "my build is stuck")
    bot = FakeBot()
    await announce_service.announce_ticket(bot, tid)
    first_count = len(bot.sent)

    assert await announce_service.announce_ticket(bot, tid) is True
    assert len(bot.sent) == first_count, "a retry opened a second topic"


async def test_missing_topic_permission_falls_back_without_storing_general(initialized_db):
    tid = await _ticket_from("widget", f"fallback-{next_tg_id()}", "help")

    class NoTopicPermission(FakeBot):
        async def create_forum_topic(self, **kwargs):
            raise RuntimeError("not enough rights to create a topic")

    bot = NoTopicPermission()
    assert await announce_service.announce_ticket(bot, tid)
    async with db_session() as session:
        ticket = await session.get(Ticket, tid)
    assert ticket.thread_id is None
    assert ticket.header_message_id is not None
    assert all(m["thread"] is None for m in bot.sent)
    assert bot.sent[1]["reply_to"] == ticket.header_message_id


async def test_concurrent_announcements_share_one_header_and_one_mirror(initialized_db):
    tid = await _ticket_from("widget", f"parallel-{next_tg_id()}", "only once")
    async with db_session() as session:
        mid = (await session.execute(select(Message.id).where(Message.ticket_id == tid))).scalar_one()
    bot = FakeBot()
    assert all(await asyncio.gather(*[
        announce_service.announce_message(bot, tid, mid) for _ in range(3)
    ]))
    assert len(bot.topics) == 1
    assert len(bot.sent) == 2
    async with db_session() as session:
        row = await session.get(Message, mid)
    assert row.admin_message_id == bot._next_id


async def test_followups_are_mirrored_by_exact_id_without_replaying_upgrade_history(initialized_db):
    tid = await _ticket_from("widget", f"followup-{next_tg_id()}", "old message")
    header = next_tg_id()
    async with db_session() as session:
        ticket = await session.get(Ticket, tid)
        ticket.header_message_id = header
        ticket.thread_id = 1  # Stored by the old, incorrect topic implementation.
        followup = Message(ticket_id=tid, sender_type="user", sender_id=1, text="new message")
        session.add(followup)
        await session.commit()
    bot = FakeBot()
    assert await announce_service.announce_ticket(bot, tid)
    assert bot.sent == []
    assert await announce_service.announce_message(bot, tid, followup.id)
    assert await announce_service.announce_message(bot, tid, followup.id)
    assert len(bot.sent) == 1
    assert bot.sent[0]["text"] == "new message"
    assert bot.sent[0]["thread"] is None
    assert bot.sent[0]["reply_to"] == header


async def test_long_followup_is_bounded_without_losing_the_source(initialized_db):
    tid = await _ticket_from("widget", f"long-{next_tg_id()}", "first")
    text = "a" * 8000
    async with db_session() as session:
        followup = Message(ticket_id=tid, sender_type="user", sender_id=1, text=text)
        session.add(followup)
        await session.commit()
    bot = FakeBot()
    assert await announce_service.announce_message(bot, tid, followup.id)
    assert len(bot.texts[-1]) == 4096
    async with db_session() as session:
        assert (await session.get(Message, followup.id)).text == text


async def test_an_inbound_file_uses_its_channel_fetch_api(initialized_db):
    from tests.conftest import RecordingChannel
    pipe = RecordingChannel(name="relay")
    pipe.fetched["provider-ref"] = b"file-bytes"
    channels.register(pipe)
    try:
        tid = await _ticket_from("relay", f"file-{next_tg_id()}", "first")
        async with db_session() as session:
            row = Message(ticket_id=tid, sender_type="user", sender_id=1, text="caption",
                          file_id="provider-ref", file_name="notes.txt", file_type="document")
            session.add(row)
            await session.commit()

        class FileBot(FakeBot):
            async def send_document(self, document, caption, **kwargs):
                assert document.data == b"file-bytes"
                assert document.filename == "notes.txt"
                return await super().send_document(document, caption, **kwargs)

        bot = FileBot()
        assert await announce_service.announce_message(bot, tid, row.id)
        assert bot.texts[-1] == "caption"
    finally:
        channels.reset()


@pytest.mark.parametrize("status,visible", [("delivered", True), ("failed", False),
                                         ("pending", False), ("uncertain", False)])
async def test_outgoing_mirror_only_shows_confirmed_deliveries(initialized_db, status, visible):
    tid = await _ticket_from("widget", f"outgoing-{next_tg_id()}", "question")
    async with db_session() as session:
        row = Message(ticket_id=tid, sender_type="admin", sender_id=1, text="answer",
                      delivery_status=status)
        session.add(row)
        await session.commit()
    bot = FakeBot()
    assert await announce_service.announce_message(bot, tid, row.id) is visible
    assert any("📤 answer" in text for text in bot.texts) is visible


async def test_pending_operator_file_mirrors_from_durable_bytes_after_delivery(
    initialized_db, tmp_path, monkeypatch,
):
    from src.services import outbound
    from tests.conftest import RecordingChannel
    from src.channels.identity import address_of
    from src.database.models import User
    monkeypatch.setenv("ATTACHMENTS_DIR", str(tmp_path))
    pipe = RecordingChannel(name="relay")
    channels.register(pipe)
    try:
        tid = await _ticket_from("relay", f"retained-{next_tg_id()}", "question")
        async with db_session() as session:
            ticket = await session.get(Ticket, tid)
            user = await session.get(User, ticket.user_id)
            job = await outbound.enqueue(session, ticket_id=tid, channel="relay", address=address_of(user),
                                         text="file reply", filename="report.txt", content=b"saved-report")
            await session.commit()
        assert (await outbound.deliver(job.id)).ok

        class FileBot(FakeBot):
            async def send_document(self, document, caption, **kwargs):
                assert document.data == b"saved-report"
                return await super().send_document(document, caption, **kwargs)

        # The provider's recorder cannot download its opaque fid-1 ref. The
        # original bytes on the durable job must still reach the operator view.
        bot = FileBot()
        assert await announce_service.announce_message(bot, tid, job.message_id)
        assert bot.texts[-1] == "📤 file reply"
    finally:
        channels.reset()


async def test_a_customer_is_never_refused_because_the_admin_chat_is_down(
    initialized_db,
):
    """The visitor's message is already recorded. Failing their request
    because the *operators'* chat was unreachable has the priorities
    backwards — the ticket is on the board either way."""
    tid = await _ticket_from("widget", f"down-{next_tg_id()}", "anyone there?")
    assert await announce_service.announce_ticket(FakeBot(fail=True), tid) is False

    async with db_session() as session:
        ticket = await session.get(Ticket, tid)
        rows = (await session.execute(
            select(Message).where(Message.ticket_id == tid)
        )).scalars().all()
    assert ticket is not None and ticket.status == "active"
    assert len(rows) == 1, "the customer's message was lost with the announcement"


async def test_a_ticket_with_no_category_tags_nobody(initialized_db):
    """The widget and Slack cannot ask which category a message belongs to, so
    they arrive with none. Looking up mentions for an empty category must not
    tag whoever happens to be first in the settings."""
    tid = await _ticket_from("widget", f"cat-{next_tg_id()}", "no category here")
    bot = FakeBot()
    await announce_service.announce_ticket(bot, tid)
    assert "@" not in bot.texts[0].split("#ticket")[0]


# ---------------------------------------------------------------- end to end


async def test_a_widget_visitor_becomes_a_thread_an_operator_can_answer(
    initialized_db,
):
    """The whole loop, through the real endpoints: someone writes in the
    bubble, a topic opens in the operators' group, and an answer typed there
    comes back to the bubble.

    The last step is checked by polling rather than by watching a channel
    send: on the widget the transcript *is* the delivery, so "did it send"
    has no meaning and "can the visitor see it" is the only real question.
    """
    from src.services import settings_service, template_service

    await settings_service.set_widget_enabled(True, updated_by=0)
    bot = FakeBot()
    try:
        import src.dashboard.routes.widget as widget_routes

        widget_routes._hits.clear()
        original = widget_routes._announce

        async def announce_with_fake(ticket_id: int) -> None:
            await announce_service.announce_ticket(bot, ticket_id)

        widget_routes._announce = announce_with_fake
        try:
            with TestClient(app, base_url="http://testserver") as c:
                opened = c.post("/widget/api/message",
                                headers={"origin": "https://shop.example"},
                                content=json.dumps({"text": "checkout is broken"}))
                assert opened.status_code == 200
                session_token = opened.json()["session"]
                ticket_id = opened.json()["ticket"]

                # The operators saw it, in their own group, with the question.
                assert any(f"Ticket ID: {ticket_id}" in t for t in bot.texts)
                assert any("checkout is broken" in t for t in bot.texts)

                async with db_session() as session:
                    result = await template_service.apply_text(
                        session, None, ticket_id=ticket_id,
                        text="Fixed — try again.",
                        admin_tg_id=1, admin_chat_id=-100,
                    )
                    await session.commit()
                assert result.success, result.error

                back = c.get("/widget/api/poll",
                             headers={"x-spooldesk-session": session_token})
        finally:
            widget_routes._announce = original

        assert back.status_code == 200
        said = [m["text"] for m in back.json()["messages"]]
        assert "Fixed — try again." in said, "the visitor never saw the answer"
    finally:
        await settings_service.set_widget_enabled(False, updated_by=0)
