"""The channel layer.

These tests exist to answer one question: if a second channel were plugged in
tomorrow, would anything above the seam have to change? Most of them work by
installing a channel that is emphatically not Telegram and checking that the
reply path, the intake path and the dashboard all carry on regardless.

The one that matters most is the conformance test at the bottom. A fake that
drifts out of step with the protocol makes every other test in this file agree
with a lie, and that has already happened once during this feature.
"""

import inspect
from types import SimpleNamespace

import pytest
from sqlalchemy import select

import src.channels as channels
from src.channels.base import Capabilities, Channel, Delivery, Option
from src.channels.identity import (
    address_of,
    allocate_routing_key,
    channel_of,
    create_customer,
    display_address,
    find_customer,
)
from src.channels.telegram import TelegramChannel
from src.database.db import db_session
from src.database.models import Message, Team, Ticket, User
from src.services import intake_service, template_service
from tests.conftest import RecordingChannel, next_tg_id


class Carrier(RecordingChannel):
    """A channel that has nothing to do with Telegram.

    No unsend, no files, and an address that is not a number — which is the
    combination most likely to break code that still assumes otherwise.
    """

    name = "carrier"

    def __init__(self):
        super().__init__(name="carrier",
                         caps=Capabilities(unsend=False, files=False, buttons=True))


@pytest.fixture()
def carrier():
    c = Carrier()
    channels.register(c)
    yield c
    channels.reset()


# ---------------------------------------------------------------- identity


async def test_a_customer_off_telegram_gets_a_key_that_cannot_collide(initialized_db):
    """Telegram ids are positive; ours are negative. That is the whole trick.

    It is what let `tg_id` stay non-null and unique — and therefore let fifty
    call sites that read it as a plain integer stay exactly as they were.
    """
    async with db_session() as session:
        user = await create_customer(
            session, channel="carrier", address="visitor-abc123",
        )
        await session.commit()

        assert user.tg_id < 0, "an off-Telegram key must not land in Telegram's range"
        assert user.channel == "carrier"
        assert address_of(user) == "visitor-abc123"


async def test_two_customers_arriving_together_do_not_share_a_key(initialized_db):
    async with db_session() as session:
        keys = set()
        for i in range(5):
            u = await create_customer(session, channel="carrier", address=f"v-{i}")
            keys.add(u.tg_id)
        await session.commit()
    assert len(keys) == 5, "routing keys were reused"


async def test_a_telegram_customer_keeps_the_id_they_always_had(initialized_db):
    """The migration has to be a no-op for everyone who already exists."""
    tg = next_tg_id()
    async with db_session() as session:
        user = await create_customer(session, channel="telegram", address=str(tg))
        await session.commit()
        assert user.tg_id == tg
        assert address_of(user) == str(tg)


async def test_customers_from_before_the_columns_are_still_found(initialized_db):
    """Rows written before `channel_user_id` existed have it empty, and their
    address was always the routing key. Looking them up must still work, or
    the first message after an upgrade opens a duplicate customer."""
    tg = next_tg_id()
    async with db_session() as session:
        session.add(User(tg_id=tg, username="legacy", is_verified=True, is_active=True))
        await session.commit()

    async with db_session() as session:
        found = await find_customer(session, channel="telegram", address=str(tg))
        assert found is not None and found.username == "legacy"
        assert channel_of(found) == "telegram", "the default has to be Telegram"


async def test_an_operator_is_never_shown_a_synthetic_key(initialized_db):
    """A negative number in a field labelled "tg_id" reads as corruption."""
    async with db_session() as session:
        user = await create_customer(session, channel="carrier", address="ops@example.com")
        await session.commit()
        shown = display_address(user)
    assert shown == "ops@example.com"
    assert "-" not in shown.split("@")[0]


async def test_the_first_off_telegram_customer_still_gets_a_negative_key(initialized_db):
    """`min()` over an empty-ish table must not hand out zero or a positive."""
    async with db_session() as session:
        key = await allocate_routing_key(session)
    assert key < 0


# ---------------------------------------------------------------- outbound


async def test_a_reply_reaches_a_customer_who_has_no_telegram_account(
    initialized_db, carrier
):
    """The point of the whole layer, in one test.

    Nothing in `template_service` knows what a carrier is. If this passes, the
    reply path is genuinely channel-agnostic; if it fails, it never was.
    """
    async with db_session() as session:
        team = Team(name=f"ch-{next_tg_id()}")
        session.add(team)
        await session.flush()
        user = await create_customer(
            session, channel="carrier", address="visitor-xyz", team_id=team.id,
        )
        ticket = Ticket(user_id=user.id, team_id=team.id, category="Network",
                        status="active")
        session.add(ticket)
        await session.flush()
        tid = ticket.id
        await session.commit()

    async with db_session() as session:
        result = await template_service.apply_text(
            session, None, ticket_id=tid, text="We are on it.",
            admin_tg_id=1, admin_chat_id=-100,
        )
        await session.commit()

    assert result.success, result.error
    assert carrier.texts == [("visitor-xyz", "We are on it.")]


async def test_a_failed_delivery_is_reported_in_the_operators_language(
    initialized_db, carrier
):
    """The adapter says which failure; the caller says it in words.

    An i18n key rather than a sentence, because the same failure has to be
    readable to an operator running the dashboard in either locale.
    """
    async with db_session() as session:
        team = Team(name=f"chf-{next_tg_id()}")
        session.add(team)
        await session.flush()
        user = await create_customer(session, channel="carrier", address="gone",
                                     team_id=team.id)
        ticket = Ticket(user_id=user.id, team_id=team.id, category="Network",
                        status="active")
        session.add(ticket)
        await session.flush()
        tid = ticket.id
        await session.commit()

    carrier.fail_on = 1
    async with db_session() as session:
        result = await template_service.apply_text(
            session, None, ticket_id=tid, text="hello",
            admin_tg_id=1, admin_chat_id=-100,
        )

    assert not result.success
    assert "e_send_failed" not in (result.error or ""), \
        "an i18n key leaked into the operator's screen instead of the text"
    assert result.error, "a failure with nothing to show the operator is worse than none"


async def test_nothing_is_recorded_as_sent_when_the_send_failed(
    initialized_db, carrier
):
    """A transcript that shows a reply the customer never got is worse than a
    visible error: the next operator reads it and assumes it was answered."""
    async with db_session() as session:
        team = Team(name=f"chn-{next_tg_id()}")
        session.add(team)
        await session.flush()
        user = await create_customer(session, channel="carrier", address="nope",
                                     team_id=team.id)
        ticket = Ticket(user_id=user.id, team_id=team.id, category="Network",
                        status="active")
        session.add(ticket)
        await session.flush()
        tid = ticket.id
        await session.commit()

    carrier.fail_on = 1
    async with db_session() as session:
        await template_service.apply_text(
            session, None, ticket_id=tid, text="never arrived",
            admin_tg_id=1, admin_chat_id=-100,
        )
        await session.commit()

    async with db_session() as session:
        rows = (await session.execute(
            select(Message).where(Message.ticket_id == tid,
                                  Message.sender_type == "admin")
        )).scalars().all()
    assert rows == [], "a reply that never left was written into the transcript"


async def test_a_channel_reports_what_it_cannot_do_rather_than_failing_at_it(
    initialized_db, carrier
):
    assert channels.caps("carrier").files is False
    assert channels.caps("carrier").unsend is False
    # And the default is still Telegram's, for every row that names no channel.
    assert channels.caps(None).unsend is True


async def test_an_unknown_channel_is_refused_rather_than_guessed(initialized_db):
    """Sending someone's reply down the wrong pipe is worse than not sending."""
    channels.reset()
    with pytest.raises(LookupError):
        channels.get("carrier-pigeon")


async def test_startup_does_not_replace_a_channel_that_is_already_installed(
    initialized_db, carrier
):
    """`ensure` is what process startup calls, and it must lose to `register`.

    The dashboard registers Telegram on every startup, including the one a
    test triggers — quietly swapping a fake for a real client would make a
    test pass by talking to the network.
    """
    replacement = Carrier()
    channels.ensure(replacement)
    assert channels.get("carrier") is carrier


# ---------------------------------------------------------------- inbound


async def test_a_message_from_a_new_address_opens_a_ticket(initialized_db):
    async with db_session() as session:
        result = await intake_service.receive(session, intake_service.Inbound(
            channel="carrier", address="new-visitor", text="my login is broken",
            display_name="Sam",
        ))
        await session.commit()

    assert result.opened_ticket and result.new_customer
    async with db_session() as session:
        ticket = await session.get(Ticket, result.ticket_id)
        assert ticket.status == "active" and ticket.stage == "new"
        msg = await session.get(Message, result.message_id)
        assert msg.sender_type == "user" and msg.text == "my login is broken"


async def test_the_second_message_continues_the_same_ticket(initialized_db):
    """Otherwise every follow-up becomes a new ticket and the board fills with
    fragments of one conversation."""
    async with db_session() as session:
        first = await intake_service.receive(session, intake_service.Inbound(
            channel="carrier", address="chatty", text="hello",
        ))
        await session.commit()
    async with db_session() as session:
        second = await intake_service.receive(session, intake_service.Inbound(
            channel="carrier", address="chatty", text="still there?",
        ))
        await session.commit()

    assert second.ticket_id == first.ticket_id
    assert not second.opened_ticket and not second.new_customer


async def test_the_same_address_on_two_channels_is_two_customers(initialized_db):
    """A widget session token and a Telegram id could collide as strings, and
    treating them as one person would show a stranger someone else's ticket."""
    async with db_session() as session:
        a = await intake_service.receive(session, intake_service.Inbound(
            channel="carrier", address="12345", text="from the widget",
        ))
        await session.commit()
    async with db_session() as session:
        b = await intake_service.receive(session, intake_service.Inbound(
            channel="courier", address="12345", text="from somewhere else",
        ))
        await session.commit()

    assert a.ticket_id != b.ticket_id


async def test_a_blocked_customer_is_refused_on_every_channel(initialized_db):
    """Blocking that only works on Telegram is not blocking."""
    async with db_session() as session:
        user = await create_customer(session, channel="carrier", address="banned")
        user.is_active = False
        await session.commit()

    async with db_session() as session:
        result = await intake_service.receive(session, intake_service.Inbound(
            channel="carrier", address="banned", text="let me in",
        ))
        await session.commit()

    assert result.rejected == "blocked" and result.ticket_id == 0


async def test_an_empty_message_opens_nothing(initialized_db):
    async with db_session() as session:
        result = await intake_service.receive(session, intake_service.Inbound(
            channel="carrier", address="silent", text="   ",
        ))
    assert result.rejected == "empty"


async def test_an_attachment_alone_is_a_message(initialized_db):
    """People send a screenshot and nothing else, constantly."""
    async with db_session() as session:
        result = await intake_service.receive(session, intake_service.Inbound(
            channel="carrier", address="screenshotter", text="",
            attachment=intake_service.Attachment(
                ref="obj-1", name="error.png", mime_type="image/png", size=2048,
            ),
        ))
        await session.commit()

    assert result.opened_ticket
    async with db_session() as session:
        msg = await session.get(Message, result.message_id)
        assert msg.file_name == "error.png" and msg.file_id == "obj-1"


async def test_a_name_learned_late_fills_a_blank_but_never_overwrites(initialized_db):
    """Channels learn who someone is at odd moments. Filling a blank is help;
    replacing a name an operator may have corrected is not."""
    async with db_session() as session:
        await intake_service.receive(session, intake_service.Inbound(
            channel="carrier", address="anon", text="hi",
        ))
        await session.commit()
    async with db_session() as session:
        await intake_service.receive(session, intake_service.Inbound(
            channel="carrier", address="anon", text="I'm Dana", display_name="Dana",
        ))
        await session.commit()
    async with db_session() as session:
        await intake_service.receive(session, intake_service.Inbound(
            channel="carrier", address="anon", text="again", display_name="Someone Else",
        ))
        await session.commit()

    async with db_session() as session:
        user = await find_customer(session, channel="carrier", address="anon")
        assert user.username == "Dana"


async def test_the_customer_transcript_never_contains_an_internal_note(initialized_db):
    """A channel that renders the conversation back — the widget does, an email
    thread does — must not be the thing that leaks what an operator wrote
    about someone to that someone."""
    async with db_session() as session:
        result = await intake_service.receive(session, intake_service.Inbound(
            channel="carrier", address="reader", text="what is happening",
        ))
        session.add(Message(ticket_id=result.ticket_id, sender_type="admin",
                            sender_id=1, text="this one is a time-waster",
                            is_internal=True))
        session.add(Message(ticket_id=result.ticket_id, sender_type="admin",
                            sender_id=1, text="Looking into it now."))
        await session.commit()

    async with db_session() as session:
        visible = await intake_service.conversation(session, result.ticket_id)
    texts = [m.text for m in visible]
    assert "Looking into it now." in texts
    assert "this one is a time-waster" not in texts


# ---------------------------------------------------------------- conformance


@pytest.mark.parametrize("adapter", [TelegramChannel, RecordingChannel, Carrier])
def test_every_adapter_matches_the_protocol(adapter):
    """Signatures, not just method names.

    `runtime_checkable` only checks that the names exist, so a fake whose
    `send_text` refuses the `options` argument satisfies `isinstance` and then
    fails at the one call site that passes one. That is exactly how it failed
    the first time, and it looked like a bug in the feature rather than in the
    fake.
    """
    for name in ("send_text", "send_file", "fetch_file", "unsend", "typing"):
        expected = inspect.signature(getattr(Channel, name))
        actual = inspect.signature(getattr(adapter, name))
        assert list(actual.parameters) == list(expected.parameters), (
            f"{adapter.__name__}.{name} does not match the protocol"
        )

    assert isinstance(getattr(adapter, "name"), str)
    assert isinstance(getattr(adapter, "caps", None) or Capabilities(), Capabilities)


def test_a_delivery_carries_enough_to_act_on():
    ok = Delivery.sent(42)
    assert ok.ok and ok.ref == "42", "the ref must be a string on every channel"

    bad = Delivery.failed("e_user_blocked_bot", "555", blocked=True)
    assert not bad.ok and bad.blocked and bad.error_key == "e_user_blocked_bot"

    slow = Delivery.failed("e_send_rate_limited", retry_after=0)
    assert slow.retry_after == 0, "zero is a delay, not the absence of one"


def test_an_option_survives_the_tightest_limit_any_channel_imposes():
    """Telegram caps callback_data at 64 bytes. Every payload we build has to
    fit, because a channel cannot negotiate that away."""
    from src.ai import deflect
    from src.services import csat_service

    payloads = [o.payload for o in csat_service.rating_options(999_999)]
    payloads += [o.payload for o in deflect._deflect_options(999_999, 999_999, "en")]
    for p in payloads:
        assert len(p.encode()) <= 64, f"{p!r} is too long for callback_data"


def test_options_reach_the_channel_rather_than_being_dropped():
    """A closing message that quietly loses its rating request looks fine and
    collects nothing."""
    assert len(Option("5⭐", "csat:1:5").payload) > 0
    opts = [Option(f"{n}", f"csat:1:{n}") for n in range(1, 6)]
    from src.channels.telegram import _keyboard

    kb = _keyboard(opts)
    assert kb is not None
    assert [b.callback_data for row in kb.inline_keyboard for b in row] == \
        [o.payload for o in opts]
    assert _keyboard(None) is None, "no options must mean no keyboard, not an empty one"


async def test_an_operator_replying_from_telegram_reaches_a_widget_customer(
    initialized_db, carrier
):
    """The trap this closes.

    An operator writing in the admin group used to send straight to
    `user.tg_id`. For anyone who did not arrive through Telegram that is a
    synthetic negative number and no chat answers to it, so the reply would
    have vanished with an error that looked like Telegram was down.
    """
    from src.handlers.admin_handlers import _forward_admin_message_to_user

    async with db_session() as session:
        user = await create_customer(session, channel="carrier", address="visitor-9")
        await session.commit()
        key = user.tg_id

    await _forward_admin_message_to_user(
        None, key, "Sorted — try again now.", None, ticket_id=1,
        channel="carrier", address="visitor-9",
    )
    assert carrier.texts == [("visitor-9", "Sorted — try again now.")]


async def test_an_attachment_is_refused_where_the_channel_cannot_carry_one(
    initialized_db, carrier
):
    """Forwarding by file_id is a Telegram trick. Somewhere it does not work,
    saying so beats an exception that reads like an outage."""
    from src.handlers.admin_handlers import _forward_admin_message_to_user

    attachment = SimpleNamespace(content_type="document", file_name="log.txt",
                                 file_id="fid-1", file_size=10, mime_type="text/plain")
    with pytest.raises(ValueError):
        await _forward_admin_message_to_user(
            None, -1, "here you go", attachment, ticket_id=1,
            channel="carrier", address="visitor-9",
        )
    assert carrier.files == []
    assert carrier.texts == [], "the caption must not go on ahead without its file"
