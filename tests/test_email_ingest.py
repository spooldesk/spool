"""Turning mail into tickets, and refusing to talk to a machine.

The interesting failure here is not a dropped message, it is a mail loop: our
reply lands back in the mailbox, an out-of-office answers it, that answer
opens a ticket, closing the ticket sends a message, the autoresponder answers
again. By the time anyone notices there are four hundred tickets and a
customer has received four hundred emails.

So most of this file is about what must *not* become a ticket. The rest is
the ordinary path, which is short.
"""

import email as email_lib
from email.message import EmailMessage

import pytest

from src.channels import email as email_channel
from src.database.db import db_session
from src.services import article_service  # noqa: F401 — keeps table creation ordered
from src.services import email_ingest, intake_service
from tests.conftest import next_tg_id


def _raw(*, frm="Ann Fowler <ann@shop.example>", subject="Card declined",
         text="My card is declined at checkout.", headers=None) -> EmailMessage:
    m = EmailMessage()
    m["From"] = frm
    m["To"] = "support@ours.example"
    m["Subject"] = subject
    m["Message-ID"] = f"<mail-{next_tg_id()}@shop.example>"
    for key, value in (headers or {}).items():
        m[key] = value
    m.set_content(text)
    return m


@pytest.fixture(autouse=True)
def _own_store(tmp_path, monkeypatch):
    monkeypatch.setenv("ATTACHMENTS_DIR", str(tmp_path / "attachments"))


OURS = "support@ours.example"


# ------------------------------------------------------------- the mail loop


@pytest.mark.parametrize("headers,why", [
    ({"Auto-Submitted": "auto-replied"}, "an out-of-office"),
    ({"Auto-Submitted": "auto-generated"}, "a notification system"),
    ({"Precedence": "bulk"}, "a mailing list"),
    ({"Precedence": "junk"}, "something that says so itself"),
    ({"List-Id": "<announce.example.com>"}, "a list"),
    ({"List-Unsubscribe": "<mailto:x@y>"}, "a newsletter"),
    ({"Return-Path": "<>"}, "a bounce"),
])
def test_machines_are_not_customers(headers, why):
    """Every one of these has caused a real mail loop somewhere."""
    assert email_ingest._looks_automated(_raw(headers=headers), OURS) is True, why


def test_our_own_message_coming_back_is_not_a_ticket():
    """Some hosts deliver a copy of what you sent into the same mailbox. Left
    unguarded, the helpdesk opens a ticket about its own reply."""
    assert email_ingest._looks_automated(_raw(frm=f"Support <{OURS}>"), OURS) is True
    # Capitalised differently, which is the version that gets through a
    # careless comparison.
    assert email_ingest._looks_automated(_raw(frm="Support@Ours.Example"), OURS) is True


def test_an_ordinary_customer_is_not_mistaken_for_one():
    """The guards must not be so eager that real mail stops arriving."""
    assert email_ingest._looks_automated(_raw(), OURS) is False
    assert email_ingest._looks_automated(
        _raw(headers={"Auto-Submitted": "no"}), OURS) is False


# ---------------------------------------------------------------- recording


async def _record(message: EmailMessage):
    from src.channels import email_parse

    return await email_ingest._record(email_parse.parse(message))


async def _transcript(ticket_id: int) -> list[str]:
    async with db_session() as session:
        rows = await intake_service.conversation(session, ticket_id)
        return [m.text or "" for m in rows]


async def test_an_email_becomes_a_ticket(initialized_db):
    ticket_id = await _record(_raw())
    assert ticket_id
    assert "My card is declined at checkout." in await _transcript(ticket_id)


async def test_the_sender_becomes_a_customer_with_their_address(initialized_db):
    from sqlalchemy import select

    from src.database.models import Ticket, User

    ticket_id = await _record(_raw())
    async with db_session() as session:
        ticket = await session.get(Ticket, ticket_id)
        user = (await session.execute(
            select(User).where(User.id == ticket.user_id)
        )).scalar_one()

    assert user.channel == "email"
    assert user.channel_user_id == "ann@shop.example"
    assert user.email == "ann@shop.example"
    assert user.username == "Ann Fowler"
    # An email customer has no Telegram account, so the routing key is the
    # synthetic negative one. See src/channels/identity.py.
    assert user.tg_id < 0


async def test_a_reply_continues_the_same_conversation(initialized_db):
    """The reply's parent header identifies its topic, not just the sender."""
    original = _raw(text="It fails at checkout.")
    first = await _record(original)
    second = await _record(_raw(subject="Re: Card declined",
                                text="Still failing this morning.",
                                headers={"In-Reply-To": original["Message-ID"]}))
    assert first == second
    assert "Still failing this morning." in await _transcript(first)


async def test_a_subject_carries_the_question_when_the_body_cannot(initialized_db):
    """"See screenshot" with a screenshot is a real support email, and a
    ticket that says nothing is not a useful one."""
    m = _raw(frm="Cara <cara@shop.example>",
             subject="Printer offline in room 4", text="")
    m.add_attachment(b"\x89PNG\r\n\x1a\n", maintype="image", subtype="png",
                     filename="shot.png")
    ticket_id = await _record(m)
    assert "Printer offline in room 4" in await _transcript(ticket_id)


async def test_several_attachments_arrive_as_several_messages(initialized_db):
    """One email with three files is three things to look at — which is also
    how a Telegram album arrives."""
    from sqlalchemy import select

    from src.database.models import Message as MessageRecord

    m = _raw(frm="Dana <dana@shop.example>", text="Everything you asked for.")
    for name in ("one.pdf", "two.pdf", "three.pdf"):
        m.add_attachment(b"%PDF-1.4", maintype="application", subtype="pdf",
                         filename=name)

    ticket_id = await _record(m)
    async with db_session() as session:
        files = (await session.execute(
            select(MessageRecord.file_name)
            .where(MessageRecord.ticket_id == ticket_id,
                   MessageRecord.file_id.is_not(None))
        )).scalars().all()

    assert sorted(files) == ["one.pdf", "three.pdf", "two.pdf"]


async def test_an_attachment_can_be_fetched_back(initialized_db):
    """The mail server will not hand the same MIME part over twice, so
    keeping it is the only way it survives to the ticket page."""
    from sqlalchemy import select

    from src.channels import get as get_channel
    from src.database.models import Message as MessageRecord

    m = _raw(frm="Erik <erik@shop.example>", text="Here it is.")
    m.add_attachment(b"%PDF-1.4 the invoice", maintype="application",
                     subtype="pdf", filename="invoice.pdf")
    ticket_id = await _record(m)

    async with db_session() as session:
        ref = (await session.execute(
            select(MessageRecord.file_id)
            .where(MessageRecord.ticket_id == ticket_id,
                   MessageRecord.file_id.is_not(None))
        )).scalars().first()

    import src.channels as channels
    channels.register(email_channel.EmailChannel())
    try:
        assert await get_channel("email").fetch_file(ref) == b"%PDF-1.4 the invoice"
    finally:
        channels.reset()


# ------------------------------------------------------------- the channel


def test_a_sent_email_cannot_be_unsent():
    """It is on somebody else's server the moment it is accepted, and a
    delete button that lies is worse than no delete button."""
    assert email_channel.EmailChannel().caps.unsend is False


def test_the_rating_request_is_spelled_out_because_there_are_no_buttons():
    class _Opt:
        def __init__(self, label):
            self.label = label

    text = email_channel.spell_options("How did we do?", [_Opt("1"), _Opt("5")])
    assert "How did we do?" in text
    assert "1" in text and "5" in text
    assert email_channel.EmailChannel().caps.buttons is False


def test_a_message_id_says_which_conversation_it_belongs_to():
    """Nothing routes on it — the sender's address already decides that — but
    a mailbox read months later still says what each message was about."""
    generated = email_channel.thread_id(4417, "help.example.com")
    assert email_channel.ticket_in(generated) == 4417
    assert generated.endswith("@help.example.com>")
    # Somebody else's Message-ID is not ours to interpret.
    assert email_channel.ticket_in("<CAF=abc@mail.gmail.com>") is None
    assert email_channel.ticket_in("") is None


def test_the_channel_stays_off_until_the_whole_mailbox_is_configured(monkeypatch):
    """Half a mailbox is the failure worth designing against: SMTP with no
    IMAP answers mail nobody can send it."""
    for name in ("EMAIL_ADDRESS", "EMAIL_PASSWORD",
                 "EMAIL_IMAP_HOST", "EMAIL_SMTP_HOST"):
        monkeypatch.delenv(name, raising=False)
    assert email_channel.is_configured() is False

    monkeypatch.setenv("EMAIL_ADDRESS", "support@ours.example")
    monkeypatch.setenv("EMAIL_PASSWORD", "secret")
    monkeypatch.setenv("EMAIL_IMAP_HOST", "imap.ours.example")
    assert email_channel.is_configured() is False, "no SMTP host, so no channel"

    monkeypatch.setenv("EMAIL_SMTP_HOST", "smtp.ours.example")
    assert email_channel.is_configured() is True


async def test_a_pass_over_an_unconfigured_mailbox_does_nothing(monkeypatch):
    """And in particular does not raise, because it runs on a timer in a
    process that has to keep serving pages."""
    for name in ("EMAIL_ADDRESS", "EMAIL_PASSWORD",
                 "EMAIL_IMAP_HOST", "EMAIL_SMTP_HOST"):
        monkeypatch.delenv(name, raising=False)
    assert await email_ingest.run_once() == {"fetched": 0, "opened": 0, "skipped": 0}


async def test_a_message_that_cannot_be_parsed_is_still_marked_read(
    initialized_db, monkeypatch
):
    """Otherwise it is a poison message the loop retries every minute
    forever, and the mailbox stops working entirely."""
    monkeypatch.setenv("EMAIL_ADDRESS", "support@ours.example")
    monkeypatch.setenv("EMAIL_PASSWORD", "secret")
    monkeypatch.setenv("EMAIL_IMAP_HOST", "imap.ours.example")
    monkeypatch.setenv("EMAIL_SMTP_HOST", "smtp.ours.example")

    marked: list[bytes] = []
    monkeypatch.setattr(email_ingest, "_fetch_unseen",
                        lambda settings: [email_ingest.FetchedMail(b"7", "123", b"this is not a mime message at all")])
    monkeypatch.setattr(email_ingest, "_mark_seen",
                        lambda settings, messages: marked.extend(m.uid for m in messages))

    result = await email_ingest.run_once()
    assert result["fetched"] == 1
    assert result["opened"] == 0
    assert marked == [b"7"], "the unparseable message would come back forever"


async def test_a_real_message_goes_all_the_way_through(initialized_db, monkeypatch):
    """The whole pass, with the network replaced and nothing else."""
    monkeypatch.setenv("EMAIL_ADDRESS", "support@ours.example")
    monkeypatch.setenv("EMAIL_PASSWORD", "secret")
    monkeypatch.setenv("EMAIL_IMAP_HOST", "imap.ours.example")
    monkeypatch.setenv("EMAIL_SMTP_HOST", "smtp.ours.example")

    raw = _raw(frm="Bob <bob@shop.example>", text="The invoice is wrong.").as_bytes()
    monkeypatch.setattr(email_ingest, "_fetch_unseen",
                        lambda settings: [email_ingest.FetchedMail(b"11", "123", raw)])
    monkeypatch.setattr(email_ingest, "_mark_seen", lambda settings, uids: None)

    result = await email_ingest.run_once()
    assert result == {"fetched": 1, "opened": 1, "skipped": 0}


async def test_an_autoresponder_in_the_mailbox_opens_nothing(
    initialized_db, monkeypatch
):
    monkeypatch.setenv("EMAIL_ADDRESS", "support@ours.example")
    monkeypatch.setenv("EMAIL_PASSWORD", "secret")
    monkeypatch.setenv("EMAIL_IMAP_HOST", "imap.ours.example")
    monkeypatch.setenv("EMAIL_SMTP_HOST", "smtp.ours.example")

    reply = _raw(subject="Out of office",
                 headers={"Auto-Submitted": "auto-replied"}).as_bytes()
    monkeypatch.setattr(email_ingest, "_fetch_unseen",
                        lambda settings: [email_ingest.FetchedMail(b"12", "123", reply)])
    monkeypatch.setattr(email_ingest, "_mark_seen", lambda settings, uids: None)

    result = await email_ingest.run_once()
    assert result["opened"] == 0
    assert result["skipped"] == 1
