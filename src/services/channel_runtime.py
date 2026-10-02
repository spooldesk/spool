"""Apply saved connections without shell access or changing process environment."""
import asyncio
import hashlib
import logging
import os
import secrets
from src.config import load_settings
from src.services import integrations, worker_lease

logger = logging.getLogger(__name__)
_telegram_signature = None
_slack_signature = None
_dispatcher = None


async def refresh() -> None:
    global _telegram_signature, _slack_signature
    await integrations.refresh()
    # Tests install fake adapters; never replace them with network clients.
    if os.getenv("TESTING") == "1":
        return
    from src.channels import register, unregister, ensure
    from src.channels.email import EmailChannel, is_configured as email_ready
    from src.channels.slack import SlackChannel, is_configured as slack_ready, bot_token, signing_secret
    from src.channels.widget import WidgetChannel
    from src.channels.telegram import TelegramChannel
    from src.dashboard.bot_client import close_bot, get_bot
    settings = load_settings()
    signature = (settings.bot_token, settings.admin_chat_id)
    if signature != _telegram_signature:
        await close_bot()
        unregister("telegram")
        if settings.has_telegram:
            register(TelegramChannel(get_bot()))
        _telegram_signature = signature
    if email_ready():
        ensure(EmailChannel())
    else:
        unregister('email')
    signature = (bot_token(), signing_secret())
    if signature != _slack_signature:
        unregister('slack')
        if slack_ready():
            register(SlackChannel())
        _slack_signature = signature
    ensure(WidgetChannel())


async def refresh_loop() -> None:
    while True:
        try:
            await refresh()
        except Exception:
            # Never include a credential or a decrypted record in an error.
            logger.warning("Could not refresh saved channel configuration")
        await asyncio.sleep(5)


def dispatcher():
    global _dispatcher
    if _dispatcher is None:
        from aiogram import Dispatcher
        from src.handlers.user_handlers import user_router
        from src.handlers.retired_operator_actions import retired_operator_router
        _dispatcher = Dispatcher()
        _dispatcher.include_router(retired_operator_router)
        _dispatcher.include_router(user_router)
    return _dispatcher


async def telegram_loop() -> None:
    """One poller per DB/token; stop polling before losing its renewable lease.

The optional legacy bot process uses the same lease as dashboard processes.
Changing credentials stops the old poller before starting the replacement.
"""
    from aiogram import Bot
    owner = secrets.token_hex(12)
    while True:
        try:
            await integrations.refresh()
            token = load_settings().bot_token
            key = 'telegram-poll-' + hashlib.sha256(token.encode()).hexdigest()[:24]
            acquired = bool(token) and await worker_lease.acquire(key, owner, 90)
        except Exception:
            logger.warning('Telegram receiver could not refresh its configuration or lease')
            acquired = False
        if not acquired:
            await asyncio.sleep(5)
            continue
        bot, poll, guard = None, None, None
        try:
            bot = Bot(token=token)
            # Do not silently replace a webhook managed by another installation.
            webhook = await bot.get_webhook_info()
            if webhook.url:
                logger.warning("Telegram polling blocked: remove the existing webhook before enabling polling")
                await asyncio.sleep(15)
                continue
            async def maintain():
                while True:
                    await asyncio.sleep(5)
                    await integrations.refresh()
                    if load_settings().bot_token != token or not await worker_lease.renew(key, owner, 90):
                        return
            async def receive():
                # Own the receiving task directly: cancelling start_polling()
                # in aiogram can leave its internally spawned poller alive.
                # feed_update is the public dispatcher API; handlers stay the same.
                offset = None
                dp = dispatcher()
                while True:
                    updates = await bot.get_updates(offset=offset, timeout=20,
                        allowed_updates=dp.resolve_used_update_types(), request_timeout=30)
                    for update in updates:
                        try:
                            await dp.feed_update(bot, update)
                        except Exception:
                            logger.warning('Telegram update handler failed')
                        offset = update.update_id + 1
            poll = asyncio.create_task(receive())
            guard = asyncio.create_task(maintain())
            await asyncio.wait({poll, guard}, return_when=asyncio.FIRST_COMPLETED)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("Telegram receiver could not start; check the saved connection")
            await asyncio.sleep(5)
        finally:
            for task in (poll, guard):
                if task:
                    task.cancel()
            await asyncio.gather(*(t for t in (poll, guard) if t), return_exceptions=True)
            if bot:
                await bot.session.close()
            try:
                await worker_lease.release(key, owner)
            except Exception:
                logger.warning('Telegram receiver lease release failed; it will expire')
        await asyncio.sleep(1)
