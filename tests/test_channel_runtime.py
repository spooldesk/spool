"""Runtime connection changes and singular, cancellable Telegram receipt."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from src.services import channel_runtime, worker_lease
from tests.conftest import next_tg_id


async def test_operator_group_reply_is_not_dispatched_to_legacy_handlers():
    from aiogram import Bot
    from aiogram.types import Update
    from datetime import datetime
    bot = Bot(token='123456:offline-test-only')
    bot.session.make_request = AsyncMock()
    try:
        dp = channel_runtime.dispatcher()
        assert 'ticket_actions' not in {r.name for r in dp.sub_routers}
        update = Update.model_validate({'update_id':1, 'message':{
            'message_id':12, 'date':datetime.now().timestamp(),
            'chat':{'id':-100123, 'type':'supergroup'},
            'from':{'id':1234, 'is_bot':False, 'first_name':'Operator'},
            'text':'This must not be forwarded to a customer', 'message_thread_id':33,
        }})
        await dp.feed_update(bot, update)
        bot.session.make_request.assert_not_called()
    finally:
        await bot.session.close()


async def test_old_operator_buttons_only_explain_and_remove_markup():
    from src.handlers.retired_operator_actions import retired_operator_action
    callback = SimpleNamespace(answer=AsyncMock(), message=SimpleNamespace(edit_reply_markup=AsyncMock()))
    await retired_operator_action(callback)
    callback.answer.assert_awaited_once()
    assert callback.answer.call_args.kwargs['show_alert'] is True
    callback.message.edit_reply_markup.assert_awaited_once_with(reply_markup=None)


async def test_slack_saved_token_replaces_existing_adapter(monkeypatch):
    import src.channels as channels
    from src.channels import slack
    monkeypatch.setenv('TESTING', '0')
    monkeypatch.setattr(channel_runtime.integrations, 'refresh', AsyncMock())
    monkeypatch.setattr(channel_runtime, 'load_settings', lambda: SimpleNamespace(bot_token='', admin_chat_id=0, has_telegram=False))
    monkeypatch.setattr(channel_runtime, '_slack_signature', None)
    monkeypatch.setattr(channel_runtime, '_telegram_signature', ('', 0))
    monkeypatch.setattr('src.channels.email.is_configured', lambda: False)
    monkeypatch.setattr(slack, 'bot_token', lambda: 'old-token')
    monkeypatch.setattr(slack, 'signing_secret', lambda: 'secret')
    try:
        await channel_runtime.refresh()
        old = channels.get('slack')
        monkeypatch.setattr(slack, 'bot_token', lambda: 'new-token')
        await channel_runtime.refresh()
        assert channels.get('slack') is not old
        assert channels.get('slack')._token == 'new-token'
        monkeypatch.setattr(slack, 'bot_token', lambda: '')
        await channel_runtime.refresh()
        with pytest.raises(LookupError):
            channels.get('slack')
    finally:
        channels.reset()


async def test_two_receivers_share_a_lease_and_cancel_owned_polling(initialized_db, monkeypatch):
    token = 'fake-' + str(next_tg_id())
    receiving, second_checked = asyncio.Event(), asyncio.Event()
    bots = []
    class FakeBot:
        def __init__(self, **kwargs):
            self.session = SimpleNamespace(close=AsyncMock())
            self.active = False
            bots.append(self)
        async def get_webhook_info(self):
            return SimpleNamespace(url='')
        async def get_updates(self, **kwargs):
            self.active = True
            receiving.set()
            try:
                await asyncio.Event().wait()
            finally:
                self.active = False
    real_acquire = worker_lease.acquire
    async def acquire(*args):
        result = await real_acquire(*args)
        if not result:
            second_checked.set()
        return result
    monkeypatch.setattr('aiogram.Bot', FakeBot)
    monkeypatch.setattr(worker_lease, 'acquire', acquire)
    monkeypatch.setattr(channel_runtime.integrations, 'refresh', AsyncMock())
    monkeypatch.setattr(channel_runtime, 'load_settings', lambda: SimpleNamespace(bot_token=token))
    monkeypatch.setattr(channel_runtime, 'dispatcher', lambda: SimpleNamespace(resolve_used_update_types=lambda: ['message'], feed_update=AsyncMock()))
    first = asyncio.create_task(channel_runtime.telegram_loop())
    second = None
    try:
        await asyncio.wait_for(receiving.wait(), 10)
        second = asyncio.create_task(channel_runtime.telegram_loop())
        await asyncio.wait_for(second_checked.wait(), 10)
        assert len(bots) == 1 and bots[0].active
    finally:
        tasks = [t for t in (first, second) if t]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    assert not bots[0].active
    bots[0].session.close.assert_awaited_once()
