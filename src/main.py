"""Entry point for the Telegram support bot."""

import asyncio
import os

from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramAPIError, TelegramNetworkError
from aiogram.types import ErrorEvent, Message
from aiogram.dispatcher.middlewares.base import BaseMiddleware
from typing import Any, Awaitable, Callable, Dict

from src.config import load_settings
from src.database.db import init_db
from src.utils.logger import setup_logging, send_error_to_admin, get_logger

logger = get_logger(__name__)


class LoggingMiddleware(BaseMiddleware):
    """Middleware for logging all incoming messages without blocking handlers."""
    
    async def __call__(
        self,
        handler: Callable[[Any, Dict[str, Any]], Awaitable[Any]],
        event: Any,
        data: Dict[str, Any],
    ) -> Any:
        # Log message if it's a Message object
        if isinstance(event, Message):
            logger.info(
                f"📨 UPDATE: chat_id={event.chat.id} | "
                f"reply={bool(event.reply_to_message)} | "
                f"reply_msg_id={event.reply_to_message.message_id if event.reply_to_message else None} | "
                f"thread_id={event.message_thread_id} | "
                f"content_type={event.content_type}"
            )
        
        # Continue processing - don't block handlers
        return await handler(event, data)



async def on_startup(bot: Bot) -> None:
    """Prepare application resources before polling starts."""
    
    # Check and delete webhook if exists (critical for polling to work)
    try:
        webhook_info = await bot.get_webhook_info()
        if webhook_info.url:
            logger.warning(f"⚠️ Webhook detected: {webhook_info.url}. Deleting it to enable polling...")
            await bot.delete_webhook(drop_pending_updates=False)
            logger.info("✅ Webhook deleted successfully. Polling can now work properly.")
        else:
            logger.info("✅ No webhook detected. Polling is ready.")
    except Exception as e:
        logger.error(f"❌ Failed to check/delete webhook: {e}. Polling may not work correctly.")
        # Don't fail startup, but log the error
    
    await init_db()
    logger.info("Database schema ensured.")

    # Apply the configured fallback language to this process (get_text default).
    try:
        from src.services import settings_service
        from src.utils.i18n import set_runtime_default_language
        set_runtime_default_language(await settings_service.get_default_language())
    except Exception as e:
        logger.warning(f"Could not apply configured default language: {e}")

    # No bot command menu and no reply keyboard. Both used to advertise a
    # /menu that the admin-panel handlers answered; those handlers are gone —
    # everything is managed from the dashboard now — so registering the
    # command only offered the operator a button that does nothing.

    logger.info("Bot started successfully.")


async def on_shutdown() -> None:
    """Cleanup resources on shutdown."""
    logger.info("Bot is shutting down...")


async def error_handler(event: ErrorEvent, bot: Bot) -> None:
    """
    Global error handler for unhandled exceptions.
    Sends critical errors to admin chat.
    """
    import sys
    exception = event.exception
    update = event.update
    
    # Get exception info for traceback
    exc_info = sys.exc_info()
    
    # Log error
    logger.error(
        f"Unhandled exception: {type(exception).__name__}: {exception}",
        exc_info=exception
    )
    
    # Build context
    context_parts = []
    if update:
        if update.message:
            context_parts.append(f"Message from user {update.message.from_user.id if update.message.from_user else 'unknown'}")
        if update.callback_query:
            context_parts.append(f"Callback query from user {update.callback_query.from_user.id if update.callback_query.from_user else 'unknown'}")
    
    context = " | ".join(context_parts) if context_parts else "Unknown context"
    
    # Send to admin if it's a critical error (but not network errors - they are temporary)
    # Network errors are logged but not sent as critical notifications
    if isinstance(exception, TelegramNetworkError):
        # Network errors are temporary - just log them, don't spam admins
        logger.warning(
            f"Network error (temporary): {exception}. "
            f"This is usually a temporary connection issue and will resolve automatically."
        )
        return
    
    # Send other critical errors to admin
    if isinstance(exception, (TelegramAPIError, Exception)):
        await send_error_to_admin(
            bot=bot,
            error=exception,
            context=context,
            include_traceback=True,
            exc_info=exc_info
        )


async def main() -> None:
    """Optional legacy receiver, sharing the dashboard's single-poller lease."""
    from src.database.db import init_db
    from src.services import channel_runtime
    from src.dashboard.bot_client import close_bot
    setup_logging(log_level=os.getenv('LOG_LEVEL', 'INFO'))
    await init_db()
    await channel_runtime.refresh()
    if not load_settings().has_telegram:
        logger.info('Telegram is optional and currently disabled; configure it in Integrations.')
        return
    refresh_task = asyncio.create_task(channel_runtime.refresh_loop())
    from src.ai import customer_agent
    ai_task = asyncio.create_task(customer_agent.poll_loop())
    try:
        await channel_runtime.telegram_loop()
    finally:
        refresh_task.cancel()
        ai_task.cancel()
        await asyncio.gather(refresh_task, ai_task, return_exceptions=True)
        await close_bot()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped by user.")
    except Exception as e:
        logger.critical(f"Fatal error: {e}", exc_info=True)
        raise

