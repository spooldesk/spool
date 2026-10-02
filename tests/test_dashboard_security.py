"""Security boundaries exercised through real public and authenticated routes."""

import json
import re
import shutil
import subprocess
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, quote, urlsplit

import pytest
from fastapi.testclient import TestClient

import src.channels as channels
from src.channels.widget import WidgetChannel
from src.dashboard import auth
from src.dashboard.app import TEMPLATES, app
from src.dashboard.i18n import strings
from src.dashboard.routes import widget
from src.services import admin_user_service, settings_service
from tests.conftest import next_tg_id


@pytest.fixture()
async def security_client(initialized_db, monkeypatch, tmp_path):
    """No lifespan/network work; all data and uploaded bytes belong to tests."""
    monkeypatch.setenv("ATTACHMENTS_DIR", str(tmp_path))
    monkeypatch.setattr(widget, "_announce", AsyncMock())
    monkeypatch.setattr(widget, "_hits", {})
    monkeypatch.setattr(auth, "_fail_log", {})
    monkeypatch.setitem(channels._registry, "widget", WidgetChannel())
    monkeypatch.setitem(TEMPLATES.env.globals, "T", strings("en"))
    was_enabled = await settings_service.get_widget_enabled()
    await settings_service.set_widget_enabled(True, updated_by=0)
    admin = await admin_user_service.create_admin(
        email=f"security-{next_tg_id()}@example.test", password="security-password",
        role="owner",
    )
    client = TestClient(app, base_url="http://testserver")
    client.cookies.set(auth.SESSION_COOKIE, auth.make_session_cookie(admin.id))
    yield client, admin
    client.close()
    await settings_service.set_widget_enabled(was_enabled, updated_by=0)


async def test_public_visitor_names_cannot_close_dashboard_script_elements(security_client, monkeypatch):
    from src.dashboard import queries
    real_users = queries.users_with_ticket_stats
    async def include_fixture(*args, **kwargs):
        # Other modules populate the shared disposable DB. Ensure the hostile
        # fixture reaches the chart instead of falling outside its top five.
        kwargs['limit'] = 10000
        return await real_users(*args, **kwargs)
    monkeypatch.setattr(queries, 'users_with_ticket_stats', include_fixture)
    client, _ = security_client
    payload = "</script><script>window.AUDIT_XSS=1</script>"
    r = client.post("/widget/api/message", json={"name": payload, "text": "Question"})
    assert r.status_code == 200

    for path in ("/users", "/overview"):
        page = client.get(path)
        assert page.status_code == 200
        assert payload not in page.text
        assert r"\u003c/script\u003e" in page.text

    # JavaScript still receives arrays of actual data, not doubly-encoded JSON.
    match = re.search(r"const USERS = (.+);", client.get("/users").text)
    users = json.loads(match.group(1))
    assert any(u["label"] == payload for u in users)  # Widget names have no Telegram prefix.


async def test_brand_is_escaped_inside_the_formatted_settings_hint(security_client, monkeypatch):
    client, _ = security_client
    payload = '<img src=x onerror="window.AUDIT_XSS=1">'
    monkeypatch.setattr(settings_service, "get_brand_name", AsyncMock(return_value=payload))
    page = client.get("/manage/settings")
    assert page.status_code == 200
    assert payload not in page.text
    assert "&lt;img" in page.text


async def test_query_toasts_are_inserted_as_text(security_client):
    client, _ = security_client
    payload = '<img src=x onerror="window.AUDIT_XSS=1"> 50% complete'
    page = client.get("/tickets", params={"toast": payload})
    assert page.status_code == 200
    source = re.search(r"window\.showToast = function.*?\n  };", page.text, re.S).group()
    assert "decodeURIComponent(msg)" not in page.text
    # The Python-only CI image still verifies the rendered text sink. When
    # Node is available, also execute the actual renderer against a tiny DOM.
    assert "textContent = msg" in source
    node = shutil.which("node")
    if node:
        script = """
const assert = require('node:assert/strict');
const window = {};
const span = {textContent: ''};
const element = {innerHTML: '', classList: {add(){}, remove(){}},
                 querySelector(){return span;}};
const document = {createElement(){return element;}};
const container = {appendChild(){}};
const _colors = {success: 'success'};
const _icons = {success: 'check'};
const requestAnimationFrame = fn => fn();
const setTimeout = () => {};
""" + source + "\nconst payload = " + json.dumps(payload) + """;
window.showToast(payload, 'success');
assert.equal(span.textContent, payload);
assert.equal(element.innerHTML.includes(payload), false);
assert.equal(element.innerHTML.includes('<img'), false);
"""
        subprocess.run([node, "-e", script], check=True, capture_output=True, text=True)


@pytest.mark.parametrize("upload", [False, True])
async def test_unknown_widget_tokens_spend_the_new_visitor_ip_budget(security_client, upload):
    client, _ = security_client
    for i in range(widget._NEW_SESSIONS_PER_IP + 1):
        chosen = f"caller-chosen-token-{i}"
        headers = {"x-spooldesk-session": chosen}
        if upload:
            r = client.post("/widget/api/upload", headers=headers,
                            files={"file": ("note.txt", b"Question", "text/plain")})
        else:
            r = client.post("/widget/api/message", headers=headers, json={"text": "Question"})
        if i < widget._NEW_SESSIONS_PER_IP:
            assert r.status_code == 200
            issued = r.json()["session"]
            assert issued != chosen
            assert client.get("/widget/api/poll", headers=headers).status_code == 401
            assert client.get("/widget/api/poll", headers={
                "x-spooldesk-session": issued,
            }).status_code == 200
        else:
            assert r.status_code == 429


@pytest.mark.parametrize("name,mime", [
    ("payload.svg", "image/svg+xml"), ("payload.html", "text/html"),
    ("report.txt", "text/html"), ("звіт.txt", "text/plain"),
])
async def test_active_attachments_download_safely_on_both_surfaces(security_client, name, mime):
    client, _ = security_client
    payload = b"<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>"
    uploaded = client.post("/widget/api/upload", files={"file": (name, payload, mime)})
    assert uploaded.status_code == 200
    data = uploaded.json()
    mid = data["messages"][-1]["id"]
    for path in (f"/tickets/attachment/{mid}", f"/widget/api/file/{mid}"):
        r = client.get(path, headers={"x-spooldesk-session": data["session"]})
        assert r.status_code == 200
        assert r.content == payload
        assert r.headers["content-disposition"].startswith("attachment;")
        assert "filename*=UTF-8''" + quote(name, safe="") in r.headers["content-disposition"]
        assert r.headers["x-content-type-options"] == "nosniff"
        assert r.headers["content-security-policy"] == "sandbox"
        assert r.headers["content-type"] not in ("text/html", "image/svg+xml")


@pytest.mark.parametrize("next_path", [
    "https://example.invalid/", "//example.invalid/", "/\\example.invalid/",
    "/\n/example.invalid/",
])
async def test_login_redirects_reject_external_destinations(security_client, next_path):
    client, admin = security_client
    r = client.get("/login", params={"next": next_path}, follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/"

    client.cookies.delete(auth.SESSION_COOKIE)
    client.get("/login")
    r = client.post("/login", data={
        "email": admin.email, "password": "security-password", "next": next_path,
        "csrf_token": client.cookies.get("supbot_csrf"),
    }, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/"


async def test_login_preserves_a_local_destination_with_filters(security_client):
    client, _ = security_client
    target = "/tickets?assigned=me&status=active"
    r = client.get("/login", params={"next": target}, follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == target


async def test_expired_navigation_retains_all_filters(security_client):
    client, _ = security_client
    client.cookies.delete(auth.SESSION_COOKIE)
    target = "/tickets?assigned=me&status=active&search=hello%20world"
    response = client.get(target, follow_redirects=False)
    assert response.status_code == 307
    assert parse_qs(urlsplit(response.headers["location"]).query)["next"] == [target]


@pytest.mark.parametrize("referer,expected", [
    ("http://testserver/tickets/83?queue=mine&status=active", "/tickets/83?queue=mine&status=active"),
    ("https://outside.invalid/", "/tickets"),
    ("http://[", "/tickets"),
    ("", "/tickets"),
])
async def test_expired_form_does_not_replay_its_post_into_login(security_client, referer, expected):
    client, _ = security_client
    client.cookies.delete(auth.SESSION_COOKIE)
    response = client.post("/tickets/83/note", data={"text": "unsent"},
                           headers={"referer": referer})
    assert response.status_code == 200  # GET login, never POST login -> 422
    assert response.history[0].status_code == 303
    assert response.request.method == "GET"
    assert parse_qs(response.request.url.query.decode())["next"] == [expected]


@pytest.mark.parametrize("headers", [
    {"X-Requested-With": "Spooldesk"}, {"Accept": "application/json"},
])
async def test_expired_ajax_is_a_json_failure_without_following_login(security_client, headers):
    client, _ = security_client
    client.cookies.delete(auth.SESSION_COOKIE)
    response = client.post("/tickets/83/note", data={"text": "unsent"}, headers=headers)
    assert response.status_code == 401
    assert not response.history
    assert response.json()["ok"] is False


async def test_navigation_only_offers_configured_telegram_tools(security_client, monkeypatch):
    client, _ = security_client
    monkeypatch.delenv("BOT_TOKEN", raising=False)
    monkeypatch.delenv("ADMIN_CHAT_ID", raising=False)
    body = client.get("/tickets").text
    assert 'href="/broadcast"' not in body
    assert 'href="/manage/tags"' not in body
    assert 'href="/manage/articles"' in body
    monkeypatch.setenv("BOT_TOKEN", "1000000:TEST")
    monkeypatch.setenv("ADMIN_CHAT_ID", "-1001234567890")
    body = client.get("/tickets").text
    assert 'href="/broadcast"' in body
    assert 'href="/manage/tags"' not in body


@pytest.mark.parametrize("field", ["price_in", "price_out", "assist_minutes"])
@pytest.mark.parametrize("value", ["nan", "inf", "-inf"])
async def test_ai_report_rejects_nonfinite_assumptions(security_client, field, value):
    client, _ = security_client
    client.get("/manage/ai")
    before = (await settings_service.get_ai_pricing(), await settings_service.get_ai_assist_minutes())
    response = client.post("/manage/ai/savings", data={
        field: value, "csrf_token": client.cookies.get("supbot_csrf"),
    })
    assert response.status_code == 422
    assert (await settings_service.get_ai_pricing(), await settings_service.get_ai_assist_minutes()) == before


async def test_ai_settings_audit_records_the_operator(security_client, monkeypatch):
    client, admin = security_client
    setter = AsyncMock()
    monkeypatch.setattr(settings_service, "set_ai_engine", setter)
    client.get("/manage/ai")
    response = client.post("/manage/ai/engine", data={
        "provider": "ollama", "model": "local-model", "host": "http://localhost:11434",
        "csrf_token": client.cookies.get("supbot_csrf"),
    }, follow_redirects=False)
    assert response.status_code == 303
    setter.assert_awaited_once_with(provider="ollama", model="local-model",
                                    host="http://localhost:11434", updated_by=admin.id)
