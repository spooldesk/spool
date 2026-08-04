"""RBAC enforcement: owner / admin / agent access matrix.

Logs in as each role through the real login flow and checks the routers'
role gates. 403s must render (or JSON) — never redirect to /login (that
would mean the auth layer, not RBAC, rejected the request).
"""

import pytest
from fastapi.testclient import TestClient

from src.dashboard.app import app


@pytest.fixture(scope="module")
def client():
    with TestClient(app, base_url="http://testserver") as c:
        # Accounts for each role (module-scoped: created once)
        import asyncio
        from src.services import admin_user_service as svc

        async def _mk():
            for email, role in [("rbac-admin@t.dev", "admin"),
                                ("rbac-agent@t.dev", "agent")]:
                try:
                    await svc.create_admin(email=email, password="rbacpass123",
                                           name=email.split("@")[0], role=role)
                except ValueError:
                    pass  # already exists from a previous module in this session
        asyncio.run(_mk())
        yield c


def _login(client: TestClient, email: str, password: str) -> None:
    client.get("/login")  # ensure CSRF cookie
    token = client.cookies.get("supbot_csrf")
    r = client.post("/login", data={
        "email": email, "password": password, "next": "/", "csrf_token": token,
    }, follow_redirects=False)
    assert r.status_code == 303, f"login failed for {email}: {r.status_code}"


def test_agent_access_matrix(client):
    _login(client, "rbac-agent@t.dev", "rbacpass123")
    # Allowed: tickets, users, analytics
    for path in ("/tickets", "/users", "/overview", "/ai"):
        assert client.get(path).status_code == 200, path
    # Forbidden: Configure section, AI config, operators, managerial reports
    for path in ("/manage/settings", "/manage/templates", "/manage/categories",
                 "/manage/ai", "/manage/admins", "/reports"):
        r = client.get(path)
        assert r.status_code == 403, f"{path} -> {r.status_code}"
    # Forbidden on POST too (router-level gate covers all methods)
    token = client.cookies.get("supbot_csrf")
    r = client.post("/manage/settings/brand", data={"brand_name": "X", "csrf_token": token},
                    follow_redirects=False)
    assert r.status_code == 403


def test_admin_access_matrix(client):
    _login(client, "rbac-admin@t.dev", "rbacpass123")
    for path in ("/tickets", "/manage/settings", "/manage/templates", "/manage/ai",
                 "/reports"):
        assert client.get(path).status_code == 200, path
    # Report export streams a real xlsx
    r = client.get("/reports/export.xlsx?preset=30d")
    assert r.status_code == 200
    assert "spreadsheetml" in r.headers["content-type"]
    # Operators page is owner-only
    assert client.get("/manage/admins").status_code == 403


def test_owner_access_matrix(client):
    _login(client, "admin@local", "test-password-123")  # seeded owner
    for path in ("/tickets", "/manage/settings", "/manage/admins"):
        assert client.get(path).status_code == 200, path


def test_agent_403_renders_error_page(client):
    _login(client, "rbac-agent@t.dev", "rbacpass123")
    r = client.get("/manage/settings")
    assert r.status_code == 403
    assert "Access denied" in r.text
