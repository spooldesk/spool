"""The bot has two audiences and they do not share a language setting.

Text a customer reads follows the language THEY picked. Text posted into the
admin chat follows the instance's UI language — the one the operators set in
the dashboard. Getting these backwards is not a cosmetic bug: it means either
telling a customer something in a language they never chose, or an English
support team reading Russian buttons.
"""

import pytest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from src.dashboard.i18n import set_active_language
from src.handlers import admin_handlers, ticket_actions, user_handlers
from src.utils.i18n import get_text


@pytest.fixture(autouse=True)
def _restore_language():
    yield
    set_active_language("en")


# ---------- operator side: follows the instance ----------

@pytest.mark.parametrize('language,word', [('en', 'dashboard'), ('ru', 'кабинет')])
async def test_retired_thread_buttons_explain_dashboard_in_operator_language(language, word):
    from src.handlers.retired_operator_actions import retired_operator_action
    set_active_language(language)
    callback = SimpleNamespace(answer=AsyncMock(), message=SimpleNamespace(edit_reply_markup=AsyncMock()))
    await retired_operator_action(callback)
    assert word in callback.answer.call_args.args[0].lower()
    assert callback.answer.call_args.kwargs['show_alert'] is True
    callback.message.edit_reply_markup.assert_awaited_once_with(reply_markup=None)


@pytest.mark.parametrize("module", [ticket_actions, admin_handlers, user_handlers])
def test_every_handler_module_resolves_operator_text(module):
    set_active_language("en")
    assert module._op("op_error") == "⚠️ Something went wrong"
    set_active_language("ru")
    assert module._op("op_error") == "⚠️ Что-то пошло не так"


def test_operator_messages_interpolate():
    set_active_language("en")
    assert ticket_actions._op("op_ticket_closed", id=42) == "✅ Ticket #42 closed."
    assert "#42" in ticket_actions._op("op_templates_for", id=42)
    assert admin_handlers._op("op_closed", id=7) == "Ticket #7 closed."


def test_retired_menu_points_somewhere_useful():
    """A leftover button from the deleted menu must say where things went."""
    set_active_language("en")
    msg = ticket_actions._op("op_retired_menu")
    assert "dashboard" in msg.lower()


def test_templates_hint_no_longer_points_at_the_deleted_menu():
    """The old copy told operators to use "🛠 Menu → Templates", which is gone."""
    for lang in ("en", "ru"):
        set_active_language(lang)
        hint = ticket_actions._op("op_templates_none")
        assert "Меню" not in hint and "Menu" not in hint
        assert "dashboard" in hint.lower() or "дашборд" in hint.lower()


# ---------- customer side: follows the customer ----------

def test_customer_text_ignores_the_instance_language():
    """An English dashboard must not switch a Ukrainian user's messages."""
    set_active_language("en")
    assert get_text("account_blocked", "uk").startswith("Ваш обліковий")
    assert get_text("account_blocked", "ru").startswith("Ваш аккаунт")
    assert get_text("account_blocked", "en").startswith("Your account")


@pytest.mark.parametrize("key", [
    "account_approved", "category_unavailable",
    "invalid_language", "attachment_only",
])
def test_customer_keys_exist_in_every_language(key):
    from src.utils.i18n import TRANSLATIONS

    for lang in ("ru", "uk", "en"):
        assert key in TRANSLATIONS[lang], f"{key} missing from {lang}"
        assert TRANSLATIONS[lang][key].strip(), f"{key} empty in {lang}"


def test_english_customer_text_has_no_cyrillic():
    """The whole point: an English-speaking customer sees English."""
    import re

    from src.utils.i18n import TRANSLATIONS

    cyrillic = re.compile(r"[Ѐ-ӿ]")
    leaks = {k: v for k, v in TRANSLATIONS["en"].items() if cyrillic.search(v)}
    assert not leaks, f"Cyrillic in the English customer catalog: {leaks}"
