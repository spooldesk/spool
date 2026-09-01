"""Choosing which LLM answers, and the credential that lets it.

The provider used to be a deployment decision: one branch in a factory and a
key in .env. Making it a setting moved three things into the database that
were not there before — a provider id, a model name, and an API key — and
each of them has a way of going quietly wrong.

The key is the one that matters. It is encrypted because `make backup`
produces an archive that leaves the machine, so the tests that earn their
keep here are the ones asserting that the plaintext is not in the row, that a
database restored without its secrets key degrades instead of raising, and
that nothing puts the key on a page.
"""

import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from src.ai import client as ai_client
from src.ai import registry
from src.ai.client import LLMUnavailable
from src.ai.providers import rest
from src.dashboard.app import app
from src.database.db import db_session
from src.database.models import AppSetting
from src.services import secret_store, settings_service

KEY = "sk-ant-not-a-real-key-0000-abcd"


@pytest.fixture(autouse=True)
async def _clean(initialized_db):
    ai_client.reset_cache()
    yield
    ai_client.reset_cache()
    # Shared settings table: a provider left switched changes what every
    # other test file's AI path does.
    await settings_service.set_ai_engine(
        provider=registry.DEFAULT_PROVIDER, model=registry.DEFAULT_MODEL,
        updated_by=0)
    for p in registry.PROVIDERS:
        await settings_service.set_ai_key(p.id, "", updated_by=0)


async def _stored(key: str) -> str:
    async with db_session() as session:
        row = (await session.execute(
            select(AppSetting).where(AppSetting.key == key))).scalar_one_or_none()
    return row.value if row else ""


# ------------------------------------------------------------ the credential


async def test_the_key_is_not_in_the_row_it_is_stored_in():
    """The whole reason this is encrypted.

    `make backup` archives the database and nothing else. That archive is the
    copy that travels — to a storage box, to S3, to somebody's laptop — so
    what matters is not that the column is called something private but that
    the key is not legible in it.
    """
    await settings_service.set_ai_key("anthropic", KEY, updated_by=0)

    raw = await _stored("ai_key_anthropic")
    assert KEY not in raw
    assert raw.startswith("gAAAAA")  # a Fernet token, not the key
    assert await settings_service.get_ai_key("anthropic") == KEY


async def test_a_database_restored_without_its_secrets_key_degrades():
    """Restore the archive on a fresh machine and the ciphertext is opaque.

    This must be "AI is off, and here is why", never an exception on a page
    about something else — so get_ai_key answers None and get_client answers
    None, while ai_key_is_stored still says there is one, because "broken"
    and "never set" need different fixes.
    """
    await settings_service.set_ai_key("anthropic", KEY, updated_by=0)
    async with db_session() as session:
        row = (await session.execute(
            select(AppSetting).where(AppSetting.key == "ai_key_anthropic"))).scalar_one()
        row.value = "gAAAAABmZZZZ-written-by-some-other-instance"
        await session.commit()

    assert await settings_service.get_ai_key("anthropic") is None
    assert await settings_service.ai_key_is_stored("anthropic") is True

    await settings_service.set_ai_engine(provider="anthropic",
                                         model="claude-sonnet-5", updated_by=0)
    assert await ai_client.get_client() is None


async def test_an_empty_key_stays_empty_rather_than_becoming_ciphertext():
    """Otherwise "unset" round-trips into "set to an encrypted empty string",
    and the dashboard reports a key that is not there."""
    await settings_service.set_ai_key("openai", "", updated_by=0)
    assert await _stored("ai_key_openai") == ""
    assert await settings_service.ai_key_is_stored("openai") is False


async def test_the_environment_still_works_for_an_instance_that_predates_this(
        monkeypatch):
    """Nobody's working AI should stop working because this feature shipped."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-from-the-environment")
    assert await settings_service.get_ai_key("openai") == "sk-from-the-environment"


async def test_a_stored_key_wins_over_the_environment(monkeypatch):
    """The dashboard is the newer, more specific answer. If it did not win,
    an operator changing the key would see no effect and no explanation."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-from-the-environment")
    await settings_service.set_ai_key("openai", "sk-from-the-dashboard", updated_by=0)
    assert await settings_service.get_ai_key("openai") == "sk-from-the-dashboard"


@pytest.mark.parametrize("secret", ["short", KEY, "x" * 200])
def test_masking_never_shows_enough_to_use(secret):
    shown = secret_store.masked(secret)
    assert secret not in shown
    assert len(shown) <= len(secret) + 4


# ------------------------------------------------------- which client is built


@pytest.mark.parametrize("provider_id,model,expected", [
    ("anthropic", "claude-sonnet-5", rest.AnthropicClient),
    ("openai", "gpt-4o-mini", rest.OpenAIClient),
])
async def test_the_chosen_provider_is_the_one_built(provider_id, model, expected):
    await settings_service.set_ai_engine(provider=provider_id, model=model,
                                         updated_by=0)
    await settings_service.set_ai_key(provider_id, KEY, updated_by=0)

    built = await ai_client.get_client()
    assert isinstance(built, expected)
    assert built._model_name == model


async def test_ollama_needs_no_key_at_all():
    """A model on the instance's own hardware authenticates with nothing.
    Requiring a key would make the one private option the hardest to turn on."""
    await settings_service.set_ai_engine(provider="ollama", model="llama3.1:8b",
                                         host="http://box:11434", updated_by=0)
    built = await ai_client.get_client()
    assert isinstance(built, rest.OllamaClient)
    assert built._base_url == "http://box:11434"


async def test_no_key_means_no_client_rather_than_an_error():
    await settings_service.set_ai_engine(provider="openai", model="gpt-4o-mini",
                                         updated_by=0)
    assert await ai_client.get_client() is None


async def test_changing_the_setting_changes_the_client():
    """The bug this cache was rewritten for.

    A single cached instance meant switching provider in the dashboard saved
    the choice and went on answering from the old one — which looks exactly
    like the setting not working, and takes a restart to notice.
    """
    await settings_service.set_ai_key("anthropic", KEY, updated_by=0)
    await settings_service.set_ai_key("openai", KEY, updated_by=0)

    await settings_service.set_ai_engine(provider="anthropic",
                                         model="claude-sonnet-5", updated_by=0)
    first = await ai_client.get_client()

    await settings_service.set_ai_engine(provider="openai", model="gpt-4o-mini",
                                         updated_by=0)
    second = await ai_client.get_client()

    assert isinstance(first, rest.AnthropicClient)
    assert isinstance(second, rest.OpenAIClient)


async def test_the_same_settings_reuse_one_client():
    """The cache still has to be a cache. A new HTTP client per suggestion
    would be a connection pool per suggestion."""
    await settings_service.set_ai_engine(provider="anthropic",
                                         model="claude-sonnet-5", updated_by=0)
    await settings_service.set_ai_key("anthropic", KEY, updated_by=0)
    assert await ai_client.get_client() is await ai_client.get_client()


# ------------------------------------------------------------- the price rule


async def test_the_price_follows_the_model():
    """The savings report is a document somebody shows their manager. Left
    alone, it billed Gemini's rates for Claude's tokens — a number that is
    wrong and looks entirely reasonable."""
    await settings_service.set_ai_engine(provider="anthropic",
                                         model="claude-sonnet-5", updated_by=0)
    assert (await settings_service.get_ai_pricing()
            == registry.prices_for("anthropic", "claude-sonnet-5"))


async def test_a_price_the_operator_typed_survives_saving_the_form_again():
    await settings_service.set_ai_engine(provider="openai", model="gpt-4o",
                                         updated_by=0)
    await settings_service.set_ai_pricing(price_in=9.99, price_out=1.11, updated_by=0)

    await settings_service.set_ai_engine(provider="openai", model="gpt-4o",
                                         updated_by=0)
    assert await settings_service.get_ai_pricing() == (9.99, 1.11)


async def test_an_unlisted_model_leaves_the_prices_alone():
    """Guessing a price for a model released after this release would put a
    plausible, invented number on that report."""
    await settings_service.set_ai_pricing(price_in=7.0, price_out=8.0, updated_by=0)
    await settings_service.set_ai_engine(provider="anthropic",
                                         model="claude-released-next-year",
                                         updated_by=0)
    assert await settings_service.get_ai_pricing() == (7.0, 8.0)


async def test_an_unknown_provider_is_refused_not_stored():
    with pytest.raises(ValueError):
        await settings_service.set_ai_engine(provider="skynet", model="x",
                                             updated_by=0)


async def test_a_provider_id_from_a_dropped_release_falls_back():
    """A settings row can outlive the release that wrote it. The dashboard
    must still render."""
    await settings_service.set_value("ai_provider", "some-old-provider",
                                     updated_by=0)
    assert await settings_service.get_ai_provider() == registry.DEFAULT_PROVIDER


# ------------------------------------------------------------ the REST calls


class _Response:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text or json.dumps(self._payload)

    def json(self):
        return self._payload


class _FakeHTTP:
    """Just enough httpx.AsyncClient to record one POST and answer it."""

    sent: dict = {}

    def __init__(self, response):
        self._response = response

    def __call__(self, *args, **kwargs):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, headers=None, json=None):
        _FakeHTTP.sent = {"url": url, "headers": headers or {}, "body": json or {}}
        return self._response


@pytest.fixture()
def http(monkeypatch):
    def _install(response):
        monkeypatch.setattr(rest.httpx, "AsyncClient", _FakeHTTP(response))
        return _FakeHTTP
    return _install


async def test_anthropic_sends_the_system_prompt_as_its_own_field(http):
    """Not as a pseudo-message. The API has a first-class field for it, and
    folding it into the conversation changes how the model weighs it."""
    seen = http(_Response(payload={
        "content": [{"type": "text", "text": "42"}],
        "usage": {"input_tokens": 11, "output_tokens": 2},
    }))
    out = await rest.AnthropicClient(api_key=KEY, model="claude-sonnet-5").complete(
        system="you classify", user="which template")

    assert out.text == "42"
    assert out.tokens_in == 11 and out.tokens_out == 2
    assert seen.sent["body"]["system"] == "you classify"
    assert seen.sent["headers"]["x-api-key"] == KEY
    # Pinned: the response shape is versioned by this header, so a floating
    # one means a future change arrives unannounced on somebody's instance.
    assert seen.sent["headers"]["anthropic-version"] == "2023-06-01"


async def test_openai_reads_its_own_token_field_names(http):
    seen = http(_Response(payload={
        "choices": [{"message": {"content": "17"}}],
        "usage": {"prompt_tokens": 9, "completion_tokens": 1},
    }))
    out = await rest.OpenAIClient(api_key=KEY, model="gpt-4o-mini").complete(
        system="s", user="u")

    assert out.text == "17"
    assert (out.tokens_in, out.tokens_out) == (9, 1)
    assert seen.sent["headers"]["Authorization"] == f"Bearer {KEY}"


async def test_ollama_asks_for_one_answer_not_a_stream(http):
    """Nothing here renders tokens as they arrive; a stream would just be a
    response this code has to reassemble."""
    seen = http(_Response(payload={
        "message": {"content": "ok"}, "prompt_eval_count": 5, "eval_count": 3,
    }))
    out = await rest.OllamaClient(model="llama3.1:8b").complete(system="s", user="u")

    assert out.text == "ok"
    assert (out.tokens_in, out.tokens_out) == (5, 3)
    assert seen.sent["body"]["stream"] is False


async def test_the_providers_own_words_reach_the_log(http):
    """A wrong model name, an expired key and an exhausted quota all arrive
    as an HTTP error, and the body is the only thing telling them apart."""
    http(_Response(status_code=400, payload={}, text='{"error":"model not found"}'))
    with pytest.raises(LLMUnavailable, match="model not found"):
        await rest.AnthropicClient(api_key=KEY, model="claude-typo").complete(
            system="s", user="u")


async def test_an_empty_answer_is_unavailable_not_an_empty_suggestion(http):
    """Callers write what comes back into a ticket. A blank draft is worse
    than no draft."""
    http(_Response(payload={"content": [], "usage": {}}))
    with pytest.raises(LLMUnavailable):
        await rest.AnthropicClient(api_key=KEY, model="claude-sonnet-5").complete(
            system="s", user="u")


async def test_a_shape_we_do_not_recognise_is_unavailable_not_a_crash(http):
    """A provider that changed its response, or a proxy answering with its
    own JSON. Either way this is a background task and must not raise."""
    http(_Response(payload={"unexpected": True}))
    with pytest.raises(LLMUnavailable):
        await rest.OpenAIClient(api_key=KEY, model="gpt-4o").complete(
            system="s", user="u")


# ------------------------------------------------------------- the dashboard


def _login(c: TestClient) -> str:
    c.get("/login")
    token = c.cookies.get("supbot_csrf")
    r = c.post("/login", data={"email": "admin@local", "password": "test-password-123",
                               "next": "/", "csrf_token": token}, follow_redirects=False)
    assert r.status_code == 303
    return token


async def test_the_page_never_carries_the_key():
    await settings_service.set_ai_engine(provider="anthropic",
                                         model="claude-sonnet-5", updated_by=0)
    await settings_service.set_ai_key("anthropic", KEY, updated_by=0)

    with TestClient(app) as c:
        _login(c)
        body = c.get("/manage/ai").text

    assert KEY not in body
    # The last four are shown so somebody with two accounts can tell which is
    # in there. Four is useless to anyone else.
    assert KEY[-4:] in body


async def test_saving_with_the_key_field_blank_keeps_the_key():
    """The field is drawn empty every time, because the key is never sent to
    the page. A blank meaning "clear" would delete a working credential every
    time somebody changed the model."""
    await settings_service.set_ai_key("anthropic", KEY, updated_by=0)

    with TestClient(app) as c:
        token = _login(c)
        r = c.post("/manage/ai/engine",
                   data={"provider": "anthropic", "model": "claude-opus-5",
                         "api_key": "", "csrf_token": token},
                   follow_redirects=False)
        assert r.status_code == 303

    assert await settings_service.get_ai_key("anthropic") == KEY
    assert await settings_service.get_ai_model() == "claude-opus-5"


async def test_clearing_is_a_deliberate_checkbox():
    await settings_service.set_ai_key("anthropic", KEY, updated_by=0)

    with TestClient(app) as c:
        token = _login(c)
        c.post("/manage/ai/engine",
               data={"provider": "anthropic", "model": "claude-sonnet-5",
                     "api_key": "", "clear_key": "1", "csrf_token": token},
               follow_redirects=False)

    assert await settings_service.get_ai_key("anthropic") is None


async def test_the_route_refuses_a_provider_it_does_not_know():
    before = await settings_service.get_ai_provider()
    with TestClient(app) as c:
        token = _login(c)
        r = c.post("/manage/ai/engine",
                   data={"provider": "skynet", "model": "hal",
                         "csrf_token": token}, follow_redirects=False)
        assert r.status_code == 303
        assert "err=provider" in r.headers["location"]
    assert await settings_service.get_ai_provider() == before


async def test_every_provider_offers_a_model_and_somewhere_to_get_a_key():
    """A provider in the dropdown with no models is a dead end, and one with
    no console link is an operator searching the web instead of turning the
    feature on."""
    for p in registry.PROVIDERS:
        assert p.models, f"{p.id} lists no models"
        assert p.console_url.startswith("https://"), f"{p.id} has no console link"
        assert registry.default_model_for(p.id) == p.models[0].name
