"""Every button that reaches the admin chat must find a handler.

This is the guard for retiring the bot's menu. The failure it prevents is
silent: an operator taps [AI] or [Close] under a message, aiogram finds nothing
to run, and nothing at all happens — no error, no reply, just a dead button.

The callbacks are built the same way the surviving code builds them, so if
somebody renames an action on one side the pairing breaks here first.
"""

import pytest
from aiogram.types import CallbackQuery, User as TgUser

from src.handlers import ticket_actions
from src.utils.callback_data import build_callback_data


def _callback_handlers():
    return ticket_actions.ticket_actions_router.observers["callback_query"].handlers


def _message_handlers():
    return ticket_actions.ticket_actions_router.observers["message"].handlers


def _fake_callback(data: str) -> CallbackQuery:
    """A real CallbackQuery — magic filters like F.data.startswith() need one."""
    return CallbackQuery.model_construct(
        id="1",
        from_user=TgUser(id=1, is_bot=False, first_name="op"),
        chat_instance="x",
        data=data,
    )


async def _match_in(router, data: str) -> str | None:
    """First handler in `router` that would run for this callback.

    Mirrors aiogram: handlers are tried in registration order, ALL filters on a
    handler must pass, and a handler with no filters matches everything — which
    is exactly how the catch-all is registered.
    """
    cb = _fake_callback(data)
    for handler in router.observers["callback_query"].handlers:
        passed = True
        for f in handler.filters or []:
            try:
                if not await f.call(cb):
                    passed = False
                    break
            except Exception:
                passed = False
                break
        if passed:
            return handler.callback.__name__
    return None


async def _resolves_to(data: str) -> str | None:
    return await _match_in(ticket_actions.ticket_actions_router, data)


# What user_handlers puts under every user message in the admin chat,
# what notifier.py puts under an AI suggestion, and what greets a new user.
EMITTED = {
    "admin:templates:pick": build_callback_data("admin", "templates", "pick", {"t": 7}),
    "admin:templates:apply": build_callback_data("admin", "templates", "apply", {"t": 7, "x": 1}),
    "admin:templates:dismiss": build_callback_data("admin", "templates", "dismiss", {"t": 7}),
    "admin:ai:ask": build_callback_data("admin", "ai", "ask", {"t": 7}),
    "admin:ai:accept": build_callback_data("admin", "ai", "accept", {"t": 7, "c": 1}),
    "admin:ai:accept_text": build_callback_data("admin", "ai", "accept_text", {"t": 7, "c": 1}),
    "admin:ai:edit_text": build_callback_data("admin", "ai", "edit_text", {"t": 7, "c": 1}),
    "admin:ai:dismiss": build_callback_data("admin", "ai", "dismiss", {"t": 7, "c": 1}),
    "admin:ticket:close": build_callback_data("admin", "ticket", "close", {"i": 7}),
    "admin:team:assign": build_callback_data("admin", "team", "assign", {"u": 3, "n": "Alpha"}),
}


@pytest.mark.parametrize("name,data", sorted(EMITTED.items()))
async def test_every_emitted_button_has_a_handler(name, data):
    handler = await _resolves_to(data)
    assert handler is not None, f"{name} would be a dead button"
    assert handler != "fallback_callback_handler", (
        f"{name} fell through to the catch-all instead of its own handler"
    )


# Buttons the ticket flow generates for itself, one step in.
INTERNAL = {
    "category_toggle": build_callback_data(
        "admin", "ticket", "category_toggle", {"i": 7, "c": "1"}),
    "close_confirm": build_callback_data("admin", "ticket", "close_confirm", {"i": 7}),
    "ai_edit_cancel": build_callback_data("admin", "ai", "edit_cancel", {"t": 7}),
    "noop": build_callback_data("admin", "noop", "noop"),
}


@pytest.mark.parametrize("name,data", sorted(INTERNAL.items()))
async def test_second_step_buttons_have_handlers(name, data):
    handler = await _resolves_to(data)
    assert handler is not None and handler != "fallback_callback_handler", name


async def test_unknown_callback_is_caught_not_ignored():
    """A leftover menu button in the chat history should say something."""
    assert await _resolves_to("admin:menu:tickets") == "fallback_callback_handler"


def test_the_two_typing_flows_are_registered():
    """Closing with a free-text reason, and editing an AI draft."""
    names = {h.callback.__name__ for h in _message_handlers()}
    assert names == {"handle_custom_reason_input", "handle_ai_draft_edited"}


def test_only_ticket_scoped_states_remain():
    """The menu's navigation states went with it."""
    states = {n for n in vars(ticket_actions.AdminStates) if n.isupper()}
    assert states == {"TICKET_CLOSE_CUSTOM_REASON", "AI_DRAFT_EDIT"}


# ---------- router order ----------

async def test_live_dispatcher_excludes_retired_operator_mutations():
    """Old group buttons only explain where to work; customer callbacks survive."""
    from src.services.channel_runtime import dispatcher
    from src.handlers.admin_handlers import admin_router
    from src.handlers.retired_operator_actions import retired_operator_router
    from src.handlers.user_handlers import user_router
    registrations = dispatcher().sub_routers
    assert registrations == [retired_operator_router, user_router]
    assert ticket_actions.ticket_actions_router not in registrations
    assert admin_router not in registrations
