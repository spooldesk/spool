"""Who the visitor says they are, and who they can prove they are.

A widget runs inside a page the visitor controls. Every name, address and
customer id below arrives from there, which means the interesting tests are
not "does the name reach the operator" — it does, trivially — but what stops
a stranger from reaching the operator as somebody else.

Three properties carry the feature, and each has a test that would fail
loudly if it broke:

  * an unsigned claim is stored, and stored as unverified;
  * an unsigned claim can never overwrite what is already known;
  * a signature made with anything but this instance's secret is worth
    exactly as much as no signature at all.

The middle one is the subtle one. Forgery is not the only attack — quietly
*rewriting* an identity mid-conversation is the cheaper one, because by then
an operator has stopped looking at the sidebar.
"""

import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

import src.dashboard.routes.widget as widget_routes
from src.channels.widget import (
    NAME as WIDGET,
    address_for,
    identity_subject,
    looks_like_email,
    sign_identity,
    sign_identity_v2,
    verified_identity_fields,
    verify_identity,
)
from src.dashboard.app import app
from src.database.db import db_session
from src.database.models import User
from src.services import settings_service
from tests.conftest import next_tg_id

SECRET = "a-shared-secret-only-two-servers-know"


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
    await settings_service.set_widget_identity_secret("", updated_by=0)
    await settings_service.set_widget_ask_identity(False, updated_by=0)


def _say(c: TestClient, text: str, token: str | None = None, **body):
    headers = {"origin": "https://shop.example"}
    if token:
        headers["x-spooldesk-session"] = token
    return c.post("/widget/api/message", headers=headers,
                  content=json.dumps({"text": text, **body}))


async def _customer(token: str) -> User:
    async with db_session() as session:
        return (await session.execute(
            select(User).where(
                User.channel == WIDGET,
                User.channel_user_id == address_for(token),
            )
        )).scalar_one()


# ------------------------------------------------------------ the primitives


def test_a_signature_is_over_the_identifier_not_the_name():
    """So a site can fix a typo in someone's name without re-issuing it.

    If the name were signed, correcting "Jon" to "John" would invalidate
    every stored signature, and the site's likeliest response to that is to
    stop signing anything.
    """
    assert identity_subject("ann@shop.example", "cust_7") == "cust_7"
    assert identity_subject("Ann@Shop.Example", None) == "ann@shop.example"
    assert identity_subject(None, None) == ""


def test_only_this_instances_secret_produces_a_valid_signature():
    good = sign_identity(SECRET, "cust_7")
    assert verify_identity(SECRET, good, "cust_7")
    assert not verify_identity(SECRET, good, "cust_8"), "signature moved subjects"
    assert not verify_identity("some-other-secret", good, "cust_7")
    assert not verify_identity(SECRET, "", "cust_7")
    assert not verify_identity(SECRET, "0" * 64, "cust_7")


def test_no_secret_configured_means_nobody_is_verified():
    """The failure that would matter: "we are not using this" reading as
    "everyone is verified"."""
    assert not verify_identity("", sign_identity(SECRET, "cust_7"), "cust_7")
    assert not verify_identity("", "anything", "cust_7")


@pytest.mark.parametrize("value,ok", [
    ("ann@shop.example", True),
    ("ann+tickets@shop.co.uk", True),
    ("ann@shop", False),          # no dot in the domain
    ("please call me on 555", False),
    ("ann @shop.example", False),
    ("@shop.example", False),
    ("ann@.example", False),
    ("", False),
    ("x" * 400 + "@shop.example", False),
])
def test_the_email_field_holds_something_shaped_like_an_email(value, ok):
    """A shape check, not a validity check. It exists so an operator does not
    read a sentence as an address and click it."""
    assert looks_like_email(value) is ok


# ------------------------------------------------------------ over the wire


async def test_an_unsigned_identity_is_kept_and_marked_as_unproved(widget_on):
    with TestClient(app, base_url="http://testserver") as c:
        opened = _say(c, "my order never arrived",
                      name="Ann", email="ann@shop.example",
                      external_id="cust_7").json()

    user = await _customer(opened["session"])
    assert user.username == "Ann"
    assert user.email == "ann@shop.example"
    assert user.external_id == "cust_7"
    assert user.identity_verified is False, (
        "a page typed this; nothing proved it"
    )


async def test_a_signed_identity_is_marked_as_proved(widget_on):
    await settings_service.set_widget_identity_secret(SECRET, updated_by=0)

    with TestClient(app, base_url="http://testserver") as c:
        opened = _say(c, "my order never arrived",
                      name="Ann", email="ann@shop.example", external_id="cust_7",
                      hash=sign_identity(SECRET, "cust_7")).json()

    user = await _customer(opened["session"])
    assert user.identity_verified is True


async def test_a_signature_from_somewhere_else_proves_nothing(widget_on):
    """The whole feature in one assertion: an attacker who can sign with
    *their own* secret still cannot be verified here."""
    await settings_service.set_widget_identity_secret(SECRET, updated_by=0)

    with TestClient(app, base_url="http://testserver") as c:
        opened = _say(c, "reset my password please",
                      email="boss@shop.example", external_id="cust_1",
                      hash=sign_identity("the-attackers-own-secret", "cust_1")).json()

    user = await _customer(opened["session"])
    assert user.external_id == "cust_1", "the claim is still recorded"
    assert user.identity_verified is False, "but it was never proved"


async def test_an_unsigned_claim_cannot_rewrite_an_established_identity(widget_on):
    """The cheap attack, and the one an operator would not notice.

    Write in as yourself, wait until somebody is helping you, then change the
    name at the top of the ticket. By that point nobody is reading the
    sidebar again.
    """
    with TestClient(app, base_url="http://testserver") as c:
        opened = _say(c, "hello", name="Ann", email="ann@shop.example").json()
        token = opened["session"]

        _say(c, "actually, one more thing", token=token,
             name="Head of Support", email="admin@shop.example",
             external_id="cust_1")

    user = await _customer(token)
    assert user.username == "Ann"
    assert user.email == "ann@shop.example"
    # A field that was *blank* is still fillable — that is how a channel
    # learns things late, and it takes nothing away from anyone.
    assert user.external_id == "cust_1"


async def test_a_signed_identity_may_correct_what_it_signed_for(widget_on):
    """The host's own server is allowed to say the email changed."""
    await settings_service.set_widget_identity_secret(SECRET, updated_by=0)

    with TestClient(app, base_url="http://testserver") as c:
        opened = _say(c, "hi", name="Ann", email="old@shop.example").json()
        token = opened["session"]

        _say(c, "I changed my address", token=token,
             name="Ann Smith", email="new@shop.example", external_id="cust_7",
             hash=sign_identity_v2(SECRET, name="Ann Smith", email="new@shop.example",
                                   external_id="cust_7"))

    user = await _customer(token)
    assert user.email == "new@shop.example"
    assert user.username == "Ann Smith"
    assert user.identity_verified is True


async def test_legacy_identifier_proof_cannot_rewrite_unsigned_profile_fields(widget_on):
    await settings_service.set_widget_identity_secret(SECRET, updated_by=0)
    with TestClient(app, base_url="http://testserver") as c:
        opened = _say(c, "hello", name="Ann", email="old@shop.example").json()
        _say(c, "forged neighbors", token=opened["session"], name="Boss",
             email="boss@shop.example", external_id="cust_7",
             hash=sign_identity(SECRET, "cust_7"))
    user = await _customer(opened["session"])
    assert user.username == "Ann" and user.email == "old@shop.example"
    assert user.identity_verified_fields == ["external_id"]


@pytest.mark.parametrize("field", ["name", "email", "external_id"])
def test_v2_proof_binds_every_supplied_identity_field(field):
    identity = dict(name="Анна", email="ann@shop.example", external_id="cust_7")
    signature = sign_identity_v2(SECRET, **identity)
    assert set(verified_identity_fields(SECRET, signature, **identity)) == set(identity)
    identity[field] += "changed"
    assert verified_identity_fields(SECRET, signature, **identity) == ()


@pytest.mark.parametrize("signature", ["я" * 64, "v2:" + "я" * 64, "v2:bad", "bad"])
def test_malformed_identity_signature_fails_closed(signature):
    assert verified_identity_fields(SECRET, signature, name="Ann", email=None,
                                    external_id="cust_7") == ()


async def test_once_proved_an_unsigned_claim_can_no_longer_touch_it(widget_on):
    await settings_service.set_widget_identity_secret(SECRET, updated_by=0)

    with TestClient(app, base_url="http://testserver") as c:
        opened = _say(c, "hi", name="Ann", email="ann@shop.example",
                      external_id="cust_7",
                      hash=sign_identity(SECRET, "cust_7")).json()
        token = opened["session"]

        _say(c, "and another thing", token=token,
             name="Somebody Else", email="else@shop.example")

    user = await _customer(token)
    assert user.username == "Ann"
    assert user.email == "ann@shop.example"
    assert user.identity_verified is True


async def test_a_sentence_in_the_email_field_is_dropped_not_stored(widget_on):
    with TestClient(app, base_url="http://testserver") as c:
        opened = _say(c, "help", name="Ann",
                      email="call me on 555-0100").json()

    user = await _customer(opened["session"])
    assert user.email is None
    assert user.username == "Ann", "the rest of the identity still lands"


async def test_identity_is_optional_and_its_absence_changes_nothing(widget_on):
    """The ordinary case. Most visitors are not logged into anything."""
    with TestClient(app, base_url="http://testserver") as c:
        opened = _say(c, "is this thing on?")
    assert opened.status_code == 200

    user = await _customer(opened.json()["session"])
    assert user.email is None and user.external_id is None
    assert user.identity_verified is False


# ------------------------------------------------------------ the panel


async def test_the_panel_is_told_whether_to_ask(widget_on):
    """Read on every page view rather than baked into the asset: an operator
    switches this on and it takes effect now, not at the next release."""
    with TestClient(app, base_url="http://testserver") as c:
        assert c.get("/widget/api/hello").json()["ask_identity"] is False

        await settings_service.set_widget_ask_identity(True, updated_by=0)
        info = c.get("/widget/api/hello").json()

    assert info["ask_identity"] is True
    # Every string the form needs arrives with it. The panel has no second
    # chance to ask, and a form with blank buttons is worse than no form.
    for key in ("ask_intro", "ask_name", "ask_email", "ask_start", "ask_skip",
                "ask_bad_email"):
        assert info.get(key), f"the pre-chat form has no {key}"


async def test_the_secret_never_leaves_the_server(widget_on):
    """It is shared between two servers, not published to every visitor."""
    await settings_service.set_widget_identity_secret(SECRET, updated_by=0)

    with TestClient(app, base_url="http://testserver") as c:
        hello = c.get("/widget/api/hello")
        frame = c.get("/widget/frame")
        embed = c.get("/widget/embed.js")

    for response in (hello, frame, embed):
        assert SECRET not in response.text, f"{response.url} leaks the secret"


# ------------------------------------------------------------ the operator


async def test_the_ticket_page_says_which_kind_of_identity_it_is(widget_on):
    """An operator glancing at a sidebar is exactly who forgery is aimed at,
    so the two cases must not render the same."""
    from src.dashboard import queries

    await settings_service.set_widget_identity_secret(SECRET, updated_by=0)

    with TestClient(app, base_url="http://testserver") as c:
        claimed = _say(c, "one", name="Ann", email="ann@shop.example").json()
        proved = _say(c, "two", name="Bob", email="bob@shop.example",
                      external_id="cust_9",
                      hash=sign_identity(SECRET, "cust_9")).json()

    a = await queries.ticket_detail(claimed["ticket"])
    b = await queries.ticket_detail(proved["ticket"])

    assert a["user_email"] == "ann@shop.example"
    assert a["user_identity_verified"] is False
    assert b["user_email"] == "bob@shop.example"
    assert b["user_external_id"] == "cust_9"
    assert b["user_identity_verified"] is True


async def test_telegram_tickets_carry_no_identity_caveat(initialized_db):
    """The warning is scoped by having nothing to warn about: on Telegram the
    account *is* the identity, and an amber flag on every ticket would be
    noise that teaches operators to ignore amber flags."""
    from src.dashboard import queries
    from src.services import intake_service

    async with db_session() as session:
        result = await intake_service.receive(session, intake_service.Inbound(
            channel="telegram", address=str(next_tg_id()),
            text="the printer is on fire", display_name="ann",
        ))
        await session.commit()

    detail = await queries.ticket_detail(result.ticket_id)
    assert detail["channel"] == "telegram"
    assert detail["user_email"] is None
    assert detail["user_external_id"] is None
    assert detail["user_identity_verified"] is False
