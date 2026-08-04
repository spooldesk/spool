"""Dashboard UI i18n: defaults, fallbacks, live switching, key parity."""

from fastapi.testclient import TestClient

from src.dashboard import i18n as dash_i18n
from src.services import settings_service


def test_default_language_is_english():
    assert dash_i18n.DEFAULT_DASH_LANGUAGE == "en"
    assert dash_i18n.t("tickets_title", "en") == "Tickets"
    assert dash_i18n.t("tickets_title", "ru") == "Тикеты"


def test_unknown_key_and_language_fall_back():
    # Unknown language → English strings
    assert dash_i18n.t("tickets_title", "de") == "Tickets"
    # Unknown key → the key itself (never crashes a template)
    assert dash_i18n.t("no_such_key_xyz") == "no_such_key_xyz"


def test_format_placeholders():
    s = dash_i18n.t("page_of", "en", page=2, pages=5, shown=25, total=120)
    assert "2" in s and "5" in s and "120" in s


def test_locale_key_parity():
    """Every key must exist in BOTH locales — a missing key would silently
    fall back and ship mixed-language UI."""
    en = set(dash_i18n.strings("en"))
    ru = set(dash_i18n.strings("ru"))
    assert en == ru, f"locale drift: only-en={en - ru}, only-ru={ru - en}"


async def test_setting_roundtrip(initialized_db):
    await settings_service.set_dash_language("ru")
    assert await settings_service.get_dash_language() == "ru"
    await settings_service.set_dash_language("nope")  # rejected
    assert await settings_service.get_dash_language() == "ru"
    await settings_service.set_dash_language("en")
    assert await settings_service.get_dash_language() == "en"


def test_no_cyrillic_leaks_in_english_ui():
    """Every dashboard page must render pure English when the UI language is en.

    Guards the global launch: a single forgotten hardcoded Russian string would
    ship a half-translated product. Data (demo/user content) is not involved —
    the test DB only holds ASCII fixtures.
    """
    import re
    from src.dashboard.app import app

    CYRILLIC = re.compile(r"[А-Яа-яЁё]")
    with TestClient(app, base_url="http://testserver") as client:
        client.get("/login")
        token = client.cookies.get("supbot_csrf")
        client.post("/login", data={
            "email": "admin@local", "password": "test-password-123",
            "next": "/", "csrf_token": token,
        }, follow_redirects=False)

        pages = [
            "/tickets", "/users", "/overview", "/ai", "/templates", "/reports",
            "/manage/templates", "/manage/templates/new", "/manage/categories",
            "/manage/close-reasons", "/manage/teams", "/manage/tags",
            "/manage/settings", "/manage/ai", "/manage/admins",
            "/board", "/broadcast",
        ]
        # Operator-configured DATA may legitimately be Cyrillic even on an
        # English UI (brand, category names, operator names, language labels).
        # Strip it so only UI chrome is scanned.
        import asyncio
        from src.dashboard.i18n import SUPPORTED_DASH_LANGUAGES
        from src.services import (
            admin_user_service, catalog_service, close_reason_service,
            settings_service,
        )
        from src.database.db import db_session

        async def _data_strings() -> list[str]:
            out = [await settings_service.get_brand_name()]
            out += [a.name or "" for a in await admin_user_service.list_admins()]
            out += [a.email for a in await admin_user_service.list_admins()]
            async with db_session() as s:
                out += [c.name for c in await catalog_service.list_active(s)]
                out += [r.name for r in await close_reason_service.list_active(s)]
            out += list(SUPPORTED_DASH_LANGUAGES.values())
            from src.utils.i18n import SUPPORTED_LANGUAGES
            out += list(SUPPORTED_LANGUAGES.values())
            return [v for v in out if v]

        data_values = sorted(asyncio.run(_data_strings()), key=len, reverse=True)

        offenders = {}
        for path in pages:
            r = client.get(path)
            assert r.status_code == 200, f"{path} -> {r.status_code}"
            body = r.text
            # Avatar chips render the initials of a data name, so a Cyrillic
            # operator leaves one or two letters that stripping full values can
            # never reach. `uppercase` is what marks these chips; other
            # attributes may sit between the class and the text.
            body = re.sub(r"uppercase\"[^>]*>[А-Яа-яЁё]{1,2}</span>",
                          'uppercase">D</span>', body)
            for value in data_values:
                body = body.replace(value, "DATA")
            # Ignore ticket/user CONTENT: fixtures are ASCII, so any hit is UI text.
            found = CYRILLIC.findall(body)
            if found:
                # Report a readable snippet around the first hit
                idx = CYRILLIC.search(body).start()
                offenders[path] = body[max(0, idx - 60):idx + 60].replace("\n", " ")
        assert not offenders, "Cyrillic leaked into the English UI:\n" + "\n".join(
            f"  {p}: …{s}…" for p, s in offenders.items()
        )


def test_login_errors_are_localised():
    """Login failure paths are rendered outside the normal page scan — they
    shipped Russian text to English users until this was caught."""
    from src.dashboard.app import app
    with TestClient(app, base_url="http://testserver") as client:
        client.get("/login")
        token = client.cookies.get("supbot_csrf")
        r = client.post("/login", data={
            "email": "nobody@nowhere.dev", "password": "wrong-password",
            "next": "/", "csrf_token": token,
        }, follow_redirects=False)
        assert r.status_code == 401
        assert "Wrong email or password" in r.text
        import re
        assert not re.search(r"[А-Яа-яЁё]", r.text), "Cyrillic in the English login error page"


def test_live_switch_via_settings_route():
    from src.dashboard.app import app
    with TestClient(app, base_url="http://testserver") as client:
        # login as seeded owner
        client.get("/login")
        token = client.cookies.get("supbot_csrf")
        r = client.post("/login", data={
            "email": "admin@local", "password": "test-password-123",
            "next": "/", "csrf_token": token,
        }, follow_redirects=False)
        assert r.status_code == 303

        # Default EN: tickets page renders English chrome
        page = client.get("/tickets").text
        assert "Total matching filter:" in page

        # Switch to RU → applied immediately, no restart
        r = client.post("/manage/settings/dash-language",
                        data={"dash_language": "ru", "csrf_token": token},
                        follow_redirects=False)
        assert r.status_code == 303
        page = client.get("/tickets").text
        assert "Всего по фильтру:" in page

        # And back to EN for other tests
        client.post("/manage/settings/dash-language",
                    data={"dash_language": "en", "csrf_token": token},
                    follow_redirects=False)
        assert "Total matching filter:" in client.get("/tickets").text


def test_no_key_shadows_a_dict_method():
    """`{{ T.clear }}` silently rendered dict.clear, not the translation.

    Jinja resolves `T.foo` with getattr before getitem, so any key that shares
    a name with a dict method returns the bound method instead of the string.
    It fails at json/format time with a message that names neither the key nor
    the template, which is a long way to walk back from.
    """
    from src.dashboard.i18n import _STRINGS

    reserved = {name for name in dir(dict) if not name.startswith("_")}
    for lang, catalog in _STRINGS.items():
        clashes = sorted(set(catalog) & reserved)
        assert not clashes, (
            f"{lang}: these keys are dict methods and will not render — "
            f"rename them: {clashes}"
        )


async def test_language_defaults_survive_the_settings_form(initialized_db):
    """The default radio sits in the same form as the enable checkboxes, and a
    markup change that drops it silently repoints the bot's default language."""
    from src.dashboard.app import app
    from src.services import settings_service

    with TestClient(app, base_url="http://testserver") as c:
        c.get("/login")
        token = c.cookies.get("supbot_csrf")
        r = c.post("/login", data={"email": "admin@local", "password": "test-password-123",
                                   "next": "/", "csrf_token": token}, follow_redirects=False)
        assert r.status_code == 303

        page = c.get("/manage/settings").text
        # These forms carry no hidden field: app_base injects one into every
        # POST form from this meta tag on load. Read it the same way.
        import re as _re
        token = _re.search(r'name="csrf-token" content="([^"]+)"', page).group(1)
        assert 'name="default_language"' in page
        assert page.count('name="default_language"') == page.count('name="languages"'), \
            "one default choice per offered language"

        r = c.post("/manage/settings/languages",
                   data={"languages": ["ru", "en"], "default_language": "en",
                         "csrf_token": token},
                   follow_redirects=False)
        assert r.status_code == 303
        assert await settings_service.get_default_language() == "en"

        # A default outside the offered set must not be stored.
        r = c.post("/manage/settings/languages",
                   data={"languages": ["ru"], "default_language": "uk",
                         "csrf_token": token},
                   follow_redirects=False)
        assert r.status_code == 303
        assert await settings_service.get_default_language() == "ru"


def test_what_a_newcomer_reads_is_english():
    """The guard on src/ says nothing about the files someone meets first.

    DEPLOY.md was Russian while the README that links to it was English, and
    scripts/setup.py — the very first command in both DEPLOY.md and the
    docker-compose header — asked its questions in Russian too.
    """
    import re
    import subprocess
    from pathlib import Path

    cyrillic = re.compile(r"[А-Яа-яЁёІіЇїЄєҐґ]")
    # Byte sequences the setup wizard matches against, not text it prints:
    # a UTF-8 BOM mojibake'd through a legacy Windows code page.
    allowed_fragments = ("п»ї", "я╗┐")

    tracked = subprocess.run(["git", "ls-files"], capture_output=True,
                             text=True, encoding="utf-8").stdout.split()
    reader_facing = [
        f for f in tracked
        if f.endswith((".md", ".yml", ".yaml", ".cfg", ".ini", ".toml"))
        or f in {"Makefile", "Dockerfile", "docker-compose.yml", "scripts/setup.py"}
    ]
    assert len(reader_facing) > 8, "the file list stopped matching anything"

    offenders = {}
    for rel in reader_facing:
        try:
            text = Path(rel).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for fragment in allowed_fragments:
            text = text.replace(fragment, "")
        hit = cyrillic.search(text)
        if hit:
            start = max(0, hit.start() - 40)
            offenders[rel] = text[start:hit.start() + 40].replace("\n", " ")

    assert not offenders, "Russian in files a newcomer reads:\n" + "\n".join(
        f"  {f}: …{s}…" for f, s in offenders.items()
    )
