"""End-to-end smoke: the dashboard boots, auth + CSRF actually gate requests.

Uses TestClient as a context manager so startup handlers run (init_db, seeds,
session secret). This is the test that fails loudly if the app can't start.
"""

import pytest
from fastapi.testclient import TestClient

from src.dashboard.app import app


@pytest.fixture(scope="module")
def client():
    with TestClient(app, base_url="http://testserver") as c:
        yield c


def _csrf(client: TestClient) -> str:
    """GET /login to ensure the CSRF cookie is set, return its value."""
    client.get("/login")
    token = client.cookies.get("supbot_csrf")
    assert token, "CSRF cookie was not issued"
    return token


def test_healthz(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_login_page_renders(client):
    r = client.get("/login")
    assert r.status_code == 200
    assert "Sign in" in r.text


def test_unauthenticated_redirects_to_login(client):
    r = client.get("/tickets", follow_redirects=False)
    assert r.status_code == 307
    assert r.headers["location"].startswith("/login")


def test_login_without_csrf_rejected(client):
    r = client.post(
        "/login",
        data={"email": "admin@local", "password": "test-password-123", "next": "/"},
        follow_redirects=False,
    )
    assert r.status_code == 403


def test_login_wrong_password_rejected(client):
    token = _csrf(client)
    r = client.post(
        "/login",
        data={"email": "admin@local", "password": "nope", "next": "/",
              "csrf_token": token},
        follow_redirects=False,
    )
    assert r.status_code == 401


def test_login_and_browse(client):
    token = _csrf(client)
    r = client.post(
        "/login",
        data={"email": "admin@local", "password": "test-password-123",
              "next": "/tickets", "csrf_token": token},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert client.cookies.get("supbot_dash"), "session cookie missing after login"

    for path, marker in [
        ("/tickets", "Tickets"),          # EN is the product default UI language
        ("/overview", "Overview"),
        ("/manage/admins", "Dashboard operators"),
        ("/manage/settings", "Settings"),
        ("/users", "Users"),
    ]:
        r = client.get(path)
        assert r.status_code == 200, f"{path} -> {r.status_code}"
        if marker:
            assert marker in r.text, f"{path} missing marker {marker!r}"


def test_404_renders_error_page(client):
    r = client.get("/definitely-not-a-page")
    assert r.status_code == 404
    assert "This page does not exist" in r.text


def test_queue_status_endpoint(client):
    # Authenticated (session cookie from test_login_and_browse)
    r = client.get("/api/queue-status")
    assert r.status_code == 200
    data = r.json()
    assert set(data) == {"awaiting", "latest_ticket_id"}
    assert isinstance(data["awaiting"], int)


def test_authenticated_post_without_csrf_rejected(client):
    # Session cookie is present from test_login_and_browse, but no CSRF token.
    r = client.post("/manage/admins/new",
                    data={"email": "evil@x.dev", "password": "hackhack123"},
                    headers={"x-csrf-token": "wrong-token"},
                    follow_redirects=False)
    assert r.status_code == 403
