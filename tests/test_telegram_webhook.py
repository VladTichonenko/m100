import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from m100_bot.config import Settings
from m100_bot.store import Store
from m100_bot.telegram_webhook import PATH, consume_updates, register_webhook


@pytest.fixture
async def inbox(postgres_url):
    store = Store(postgres_url)
    settings = Settings(
        _env_file=None,
        telegram_mode="webhook",
        telegram_webhook_secret="a" * 48,
        web_base_url="https://m100.example.com",
    )
    reports = SimpleNamespace(settings=settings, store=store, writer_ready=True)
    app = web.Application()
    register_webhook(app, reports)
    client = TestClient(TestServer(app))
    await client.start_server()
    yield reports, client
    await client.close()
    store.close()


async def test_webhook_authentication_validation_and_durable_deduplication(inbox):
    reports, client = inbox
    payload = {"update_id": 42}
    for secret in ("", "wrong", "я"):
        assert (
            await client.post(
                PATH,
                json=payload,
                headers={
                    "X-Telegram-Bot-Api-Secret-Token": secret,
                },
            )
        ).status == 401
    assert reports.store.pending_update() is None
    headers = {"X-Telegram-Bot-Api-Secret-Token": "a" * 48}
    assert (await client.post(PATH, json={}, headers=headers)).status == 400
    reports.writer_ready = False
    assert (await client.post(PATH, json=payload, headers=headers)).status == 503
    reports.writer_ready = True
    for _ in range(2):
        assert (await client.post(PATH, json=payload, headers=headers)).status == 200
    assert reports.store.pending_update()[:2] == (42, payload)
    reports.store.finish_update(42)
    assert (await client.post(PATH, json=payload, headers=headers)).status == 200
    assert reports.store.pending_update() is None
    assert reports.store.db.execute(
        "SELECT count(*),min(payload::text) FROM telegram_updates"
    ).fetchone() == (1, "{}")


async def test_worker_retries_failed_update_and_processes_it_after_restart(inbox):
    reports, _ = inbox
    reports.store.enqueue_update({"update_id": 99})
    first_attempt = asyncio.Event()

    async def fail(*args):
        first_attempt.set()
        raise RuntimeError("temporary failure")

    dispatcher = SimpleNamespace(feed_raw_update=AsyncMock(side_effect=fail))
    worker = asyncio.create_task(consume_updates(reports, dispatcher, object()))
    await asyncio.wait_for(first_attempt.wait(), timeout=2)
    worker.cancel()
    with pytest.raises(asyncio.CancelledError):
        await worker
    row = reports.store.db.execute(
        "SELECT attempts,completed_at FROM telegram_updates WHERE update_id=99"
    ).fetchone()
    assert row == (1, None)
    reports.store.db.execute("UPDATE telegram_updates SET retry_at=0")
    dispatcher.feed_raw_update = AsyncMock()
    worker = asyncio.create_task(consume_updates(reports, dispatcher, object()))
    try:
        async with asyncio.timeout(2):
            while reports.store.pending_update():
                await asyncio.sleep(0.01)
    finally:
        worker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await worker
    dispatcher.feed_raw_update.assert_awaited_once()


def test_webhook_requires_https_server_and_strong_secret():
    settings = Settings(_env_file=None, telegram_mode="webhook")
    assert any("HTTPS" in item for item in settings.problems())
    assert any("TELEGRAM_WEBHOOK_SECRET" in item for item in settings.problems())
