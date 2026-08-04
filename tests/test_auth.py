"""Session cookies, signing secret, login rate limiting, and browser caching."""

from fastapi.testclient import TestClient
from starlette.requests import Request

from src.dashboard import auth
from src.dashboard.app import app


def _fake_request(ip: str = "10.0.0.1") -> Request:
    return Request({
        "type": "http",
        "method": "POST",
        "path": "/login",
        "headers": [],
        "query_string": b"",
        "client": (ip, 12345),
    })


def test_session_cookie_roundtrip():
    auth.set_secret_key("unit-test-secret")
    cookie = auth.make_session_cookie(42)
    assert auth.session_user_id(cookie) == 42
    assert auth.is_valid_session(cookie)


def test_tampered_cookie_rejected():
    auth.set_secret_key("unit-test-secret")
    cookie = auth.make_session_cookie(42)
    assert auth.session_user_id(cookie + "x") is None
    assert auth.session_user_id("garbage") is None
    assert auth.session_user_id(None) is None
    assert auth.session_user_id("") is None


def test_secret_rotation_invalidates_sessions():
    auth.set_secret_key("secret-one")
    cookie = auth.make_session_cookie(7)
    auth.set_secret_key("secret-two")
    assert auth.session_user_id(cookie) is None


def test_rate_limiter_blocks_after_failures():
    req = _fake_request("10.9.9.9")
    assert not auth.login_rate_limited(req)
    for _ in range(5):
        auth.record_login_failure(req)
    assert auth.login_rate_limited(req)
    # Success clears the counter
    auth.clear_login_failures(req)
    assert not auth.login_rate_limited(req)


def test_rate_limiter_is_per_ip():
    bad = _fake_request("10.8.8.8")
    good = _fake_request("10.7.7.7")
    for _ in range(5):
        auth.record_login_failure(bad)
    assert auth.login_rate_limited(bad)
    assert not auth.login_rate_limited(good)


def test_shared_password_verify():
    assert auth.verify_shared_password("test-password-123")
    assert not auth.verify_shared_password("wrong")
    assert not auth.verify_shared_password("")


def test_pages_are_never_cached_by_the_browser():
    """Logging out has to actually remove the conversation from the screen.

    Without no-store the back-forward cache re-renders the last page after
    logout — session cookie gone, customer messages still readable by whoever
    uses the machine next. The header is the only thing preventing that; the
    server never sees the request at all.
    """
    with TestClient(app, base_url="http://testserver") as c:
        c.get("/login")
        token = c.cookies.get("supbot_csrf")
        c.post("/login", data={"email": "admin@local", "password": "test-password-123",
                               "next": "/", "csrf_token": token}, follow_redirects=False)

        for path in ("/login", "/tickets", "/users", "/board", "/overview"):
            r = c.get(path, follow_redirects=True)
            cache = r.headers.get("cache-control", "")
            assert "no-store" in cache, f"{path} may be cached: {cache!r}"


def test_static_assets_stay_cacheable():
    """The stylesheet is versioned and reloaded on every page — caching it is
    the difference between a snappy dashboard and a flash of unstyled text."""
    with TestClient(app, base_url="http://testserver") as c:
        r = c.get("/static/tw.css")
    assert r.status_code == 200
    assert "no-store" not in r.headers.get("cache-control", "")
