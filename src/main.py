"""Entry point for the Telegram support bot."""

import asyncio
import logging
import os
import traceback

from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramAPIError, TelegramNetworkError
from aiogram.types import Update, ErrorEvent, Message
from aiogram.filters import Command
from aiogram.dispatcher.middlewares.base import BaseMiddleware
from typing import Any, Awaitable, Callable, Dict

from src.config import load_settings
from src.database.db import init_db
from src.handlers.admin_handlers import admin_router
from src.handlers.ticket_actions import ticket_actions_router
from src.handlers.user_handlers import user_router
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
                f"text={event.text[:50] if event.text else (event.caption[:50] if event.caption else 'None')}"
            )
        
        # Continue processing - don't block handlers
        return await handler(event, data)



# How often the SLA policy looks. A minute is pointless — the thresholds are
# measured in hours — and an hour is late enough to miss the point of an alert.
SLA_CHECK_SECONDS = 5 * 60
_sla_task: "asyncio.Task | None" = None


async def _sla_loop(bot: Bot) -> None:
    """Check for newly breached tickets until cancelled.

    Every failure is swallowed and retried on the next pass. A background loop
    that dies quietly is worse than one that logs and keeps going: nobody
    notices the alerts stopped until the day they were needed.
    """
    from src.services import sla_service

    while True:
        try:
            await asyncio.sleep(SLA_CHECK_SECONDS)
            result = await sla_service.run_once(bot)
            if result["breached"]:
                logger.info(f"SLA policy: {result}")
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.warning(f"SLA policy pass failed, will retry: {e}")


async def on_startup(bot: Bot) -> None:
    """Prepare application resources before polling starts."""
    
    # Check and delete webhook if exists (critical for polling to work)
    try:
        webhook_info = await bot.get_webhook_info()
        if webhook_info.url:
            logger.warning(f"⚠️ Webhook detected: {webhook_info.url}. Deleting it to enable polling...")
            await bot.delete_webhook(drop_pending_updates=True)
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

    global _sla_task
    _sla_task = asyncio.create_task(_sla_loop(bot))

    logger.info("Bot started successfully.")


async def on_shutdown() -> None:
    """Cleanup resources on shutdown."""
    if _sla_task is not None:
        _sla_task.cancel()
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
    """Bootstrap configuration, bot, and dispatcher."""

    # Setup logging
    log_level = os.getenv("LOG_LEVEL", "INFO")
    setup_logging(log_level=log_level)
    
    logger.info("=" * 50)
    logger.info("Starting Spooldesk bot")
    logger.info("=" * 50)

    try:
        settings = load_settings()
        bot = Bot(token=settings.bot_token)

        # Register Telegram against *this* process's bot. Without it the
        # registry would lazily build a second client from the dashboard's
        # factory — harmless, since a token may send from many clients, but
        # two objects where one was meant is the kind of thing that only
        # shows up as a confusing log line months later.
        from src.channels import register as register_channel
        from src.channels.telegram import TelegramChannel
        register_channel(TelegramChannel(bot))
        from src.channels.widget import WidgetChannel
        register_channel(WidgetChannel())
        # Registered only when it has credentials: an adapter that
        # cannot authenticate would answer `get("slack")` and then fail
        # every send, which reads like an outage rather than a setting
        # nobody filled in.
        from src.channels.slack import SlackChannel, is_configured
        if is_configured():
            register_channel(SlackChannel())

        dp = Dispatcher()
        
        # Register error handler
        dp.errors.register(error_handler)
        
        # Register routers.
        #
        # Order matters: ticket_actions ends with a catch-all callback handler,
        # so anything that needs its own callbacks must come before it.
        # admin_router is last because its final handler treats any message in
        # the admin chat as a reply to a ticket.
        dp.include_router(user_router)
        dp.include_router(ticket_actions_router)
        dp.include_router(admin_router)
        
        # Register startup/shutdown handlers
        dp.startup.register(on_startup)
        dp.shutdown.register(on_shutdown)
        
        # Register logging middleware (doesn't block handlers, just logs)
        # Use outer_middleware to log before all handlers process the event
        dp.update.outer_middleware(LoggingMiddleware())

        # Start the long-polling loop.
        # Use allowed_updates=None to receive ALL update types (including replies)
        logger.info("Starting polling...")
        logger.info("⚠️ Using allowed_updates=None to ensure reply_to_message is received")
        await dp.start_polling(bot, allowed_updates=None)
    
    except Exception as e:
        import sys
        exc_info = sys.exc_info()
        logger.critical(f"Fatal error during bot startup: {e}", exc_info=True)
        # Try to send error notification if bot was created
        try:
            if 'bot' in locals():
                await send_error_to_admin(
                    bot=bot,
                    error=e,
                    context="Bot startup",
                    include_traceback=True,
                    exc_info=exc_info
                )
        except:
            pass
        raise


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped by user.")
    except Exception as e:
        logger.critical(f"Fatal error: {e}", exc_info=True)
        raise

