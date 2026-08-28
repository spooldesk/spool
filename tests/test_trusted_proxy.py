"""What to believe from a reverse proxy, and what happened when two layers
both had an opinion.

Every rate limit in this application is keyed on the client address: the
login throttle, and the widget's cap on how many conversations one address
may open. So "who is this request from" is not a detail — it is the whole of
those two defences, and an implementation that lets the caller choose their
own answer does not weaken them, it removes them.

That is what was happening. This application read `X-Forwarded-For`, and so
did uvicorn's own middleware, which is enabled by default and trusts anything
arriving from 127.0.0.1. Locally the widget's "five new conversations per
address per hour" therefore did nothing at all: uvicorn had already rewritten
`scope["client"]` from a header the caller sent, so fifteen requests in a row
looked like fifteen visitors. Found by running it, not by reading it — the
unit tests of the parsing function passed the whole time.

Now uvicorn's is off and `src/dashboard/proxy.py` is the only reader. These
tests cover both headers it acts on, because the second one is easy to forget
and its failure is silent: without `X-Forwarded-Proto` the embed snippet
comes out as `http://`, and a browser on an HTTPS page refuses to load it.
"""

import json

import pytest
from fastapi.testclient import TestClient

import src.dashboard.routes.widget as widget_routes
from src.dashboard.app import app
from src.dashboard.auth import _client_ip
from src.dashboard.proxy import ProxyHeaders, _host
from src.services import settings_service


@pytest.fixture(autouse=True)
def _no_proxy(monkeypatch):
    """The default every test starts from, stated rather than assumed."""
    monkeypatch.delenv("TRUSTED_PROXY_HOPS", raising=False)


def _echo_app():
    """An ASGI app that reports what the middleware left in the scope."""

    async def inner(scope, receive, send):
        if scope["type"] == "lifespan":
            # TestClient's context manager runs the lifespan protocol, and a
            # bare ASGI app that answers it with an HTTP response fails in a
            # way that says nothing about the middleware under test.
            while True:
                message = await receive()
                if message["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif message["type"] == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
                    return

        client = scope.get("client")
        payload = json.dumps({
            "client": client[0] if client else None,
            "scheme": scope.get("scheme"),
        }).encode()
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": payload})

    return inner


def _seen(hops: str | None, monkeypatch, **headers):
    if hops is None:
        monkeypatch.delenv("TRUSTED_PROXY_HOPS", raising=False)
    else:
        monkeypatch.setenv("TRUSTED_PROXY_HOPS", hops)
    # Constructed here, not at import: the middleware reads the setting once,
    # because it is a fact about the deployment and cannot change under a
    # running process.
    with TestClient(ProxyHeaders(_echo_app()), base_url="http://testserver") as c:
        return c.get("/", headers=headers).json()


# ------------------------------------------------------------ the middleware


def test_with_nothing_in_front_the_headers_are_not_evidence(monkeypatch):
    """They are strings a stranger chose. Untouched scope is the only honest
    outcome, and it is the default because most installs have no proxy."""
    seen = _seen(None, monkeypatch,
                 **{"x-forwarded-for": "1.2.3.4", "x-forwarded-proto": "https"})
    assert seen["client"] == "testclient"
    assert seen["scheme"] == "http"


def test_behind_one_proxy_the_last_entry_is_the_trustworthy_one(monkeypatch):
    """Proxies append rather than replace, so behind one the header reads

        <whatever the client sent>, <what our proxy observed>

    and only the right-hand entry was written by something we control.
    """
    assert _seen("1", monkeypatch,
                 **{"x-forwarded-for": "203.0.113.7"})["client"] == "203.0.113.7"
    assert _seen("1", monkeypatch,
                 **{"x-forwarded-for": "1.2.3.4, 203.0.113.7"})["client"] == "203.0.113.7"
    assert _seen("1", monkeypatch,
                 **{"x-forwarded-for": "evil, worse, 203.0.113.7"})["client"] == "203.0.113.7"


def test_two_proxies_means_the_second_from_the_right(monkeypatch):
    seen = _seen("2", monkeypatch,
                 **{"x-forwarded-for": "1.2.3.4, 203.0.113.7, 10.1.1.1"})
    assert seen["client"] == "203.0.113.7"


def test_a_chain_shorter_than_the_proxy_count_is_left_alone(monkeypatch):
    """A request that did not come through the proxies at all. Promoting the
    one entry it carries would be promoting a forged one."""
    assert _seen("2", monkeypatch)["client"] == "testclient"
    assert _seen("2", monkeypatch,
                 **{"x-forwarded-for": "1.2.3.4"})["client"] == "testclient"


def test_a_typo_in_the_setting_fails_closed(monkeypatch):
    """Failing open means the rate limits silently stop working, which nobody
    notices until it matters."""
    assert _seen("yes please", monkeypatch,
                 **{"x-forwarded-for": "1.2.3.4"})["client"] == "testclient"
    assert _seen("-3", monkeypatch,
                 **{"x-forwarded-for": "1.2.3.4"})["client"] == "testclient"


def test_the_scheme_is_taken_from_the_proxy_too(monkeypatch):
    """Not cosmetic. See the embed test below for what breaks without it."""
    assert _seen("1", monkeypatch, **{
        "x-forwarded-for": "203.0.113.7", "x-forwarded-proto": "https",
    })["scheme"] == "https"

    # Anything that is not a scheme we recognise is ignored rather than
    # copied into the scope.
    assert _seen("1", monkeypatch, **{
        "x-forwarded-for": "203.0.113.7", "x-forwarded-proto": "gopher",
    })["scheme"] == "http"


@pytest.mark.parametrize("entry,expected", [
    ("203.0.113.7", "203.0.113.7"),
    ("203.0.113.7:41234", "203.0.113.7"),
    ("[2001:db8::1]:41234", "2001:db8::1"),
    ("2001:db8::1", "2001:db8::1"),
])
def test_a_port_does_not_split_one_visitor_into_many(entry, expected):
    """Keeping the port would give one person as many buckets as they have
    source ports, which is the same as having no limit."""
    assert _host(entry) == expected


def test_the_address_is_read_from_the_scope_and_nowhere_else():
    """One reader. This used to parse the header itself, next to uvicorn
    doing the same thing differently."""
    import inspect

    from src.dashboard import auth

    source = inspect.getsource(auth._client_ip)
    assert "forwarded" not in source.lower(), (
        "_client_ip is reading proxy headers again; that decision belongs to "
        "src/dashboard/proxy.py alone"
    )


# ------------------------------------------------------ what it protects


@pytest.fixture(autouse=True)
def _clean_limits(initialized_db):
    widget_routes._hits.clear()
    yield
    widget_routes._hits.clear()


@pytest.fixture()
async def widget_on(initialized_db):
    await settings_service.set_widget_enabled(True, updated_by=0)
    await settings_service.set_widget_origins("", updated_by=0)
    yield
    await settings_service.set_widget_enabled(False, updated_by=0)


def _open_conversation(c: TestClient, forged: str | None = None):
    headers = {"origin": "https://shop.example"}
    if forged:
        headers["x-forwarded-for"] = forged
    return c.post("/widget/api/message", headers=headers,
                  content=json.dumps({"text": "hello"}))


async def test_a_forged_header_does_not_buy_a_fresh_allowance(widget_on, monkeypatch):
    """The bug this file exists for. Opening a conversation costs a customer
    row, a ticket and a card on someone's board, which is why it is the
    tightest limit here — and it was bypassable by incrementing a header."""
    monkeypatch.setattr(widget_routes, "_NEW_SESSIONS_PER_IP", 3)

    with TestClient(app, base_url="http://testserver") as c:
        codes = [_open_conversation(c, f"203.0.113.{n}").status_code
                 for n in range(1, 7)]

    assert codes[:3] == [200, 200, 200]
    assert codes[3:] == [429, 429, 429], (
        "a new address per request bought a new allowance"
    )


async def test_behind_a_proxy_genuinely_different_visitors_stay_separate(
    widget_on, monkeypatch
):
    """The other half: the fix must not collapse a real deployment into one
    bucket, or the first person to hit a limit locks out everyone behind it.

    Wrapped by hand because the middleware inside `app` was built with the
    default and a running process cannot change what is a fact about its
    deployment. The inner one is a no-op at 0 hops, so the outer one decides.
    """
    monkeypatch.setenv("TRUSTED_PROXY_HOPS", "1")
    monkeypatch.setattr(widget_routes, "_NEW_SESSIONS_PER_IP", 2)

    with TestClient(ProxyHeaders(app), base_url="http://testserver") as c:
        assert _open_conversation(c, "203.0.113.1").status_code == 200
        assert _open_conversation(c, "203.0.113.1").status_code == 200
        assert _open_conversation(c, "203.0.113.1").status_code == 429

        # A different visitor, seen by our proxy as themselves.
        assert _open_conversation(c, "203.0.113.2").status_code == 200

        # And the first cannot escape their bucket by prepending someone
        # else's address to the chain.
        assert _open_conversation(c, "10.9.9.9, 203.0.113.1").status_code == 429


async def test_the_embed_snippet_is_https_behind_tls(widget_on, monkeypatch):
    """Why the scheme half of this middleware exists.

    The one line a customer pastes into their site is built from the request.
    Behind TLS with the scheme unset it comes out as `http://`, every browser
    on an HTTPS page refuses to load it as mixed content, and the widget
    silently does not appear — on the customer's site, not ours, which is
    where nobody is looking at logs.
    """
    monkeypatch.setenv("TRUSTED_PROXY_HOPS", "1")

    with TestClient(ProxyHeaders(app), base_url="http://help.example.com") as c:
        served = c.get("/widget/embed.js", headers={
            "x-forwarded-for": "203.0.113.7",
            "x-forwarded-proto": "https",
        })

    assert served.status_code == 200
    assert "https://help.example.com" in served.text
    assert "http://help.example.com" not in served.text
