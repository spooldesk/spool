"""Acknowledge obsolete buttons without executing ticket mutations."""
from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery

retired_operator_router = Router(name="retired_operator_actions")


@retired_operator_router.callback_query(F.message.chat.type != ChatType.PRIVATE)
async def retired_operator_action(callback: CallbackQuery) -> None:
    from src.dashboard.i18n import t
    await callback.answer(t("operator_dashboard_only"), show_alert=True)
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        pass  # An inaccessible message cannot be changed; no action ran.
