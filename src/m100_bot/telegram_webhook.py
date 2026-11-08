"""Authenticated Telegram webhook with a durable, sequential PostgreSQL inbox."""

import asyncio
import hmac
import logging

from aiogram import Bot, Dispatcher
from aiogram.types import Update
from aiohttp import web
from pydantic import ValidationError

from .service import Reports

PATH = "/telegram/webhook"
logger = logging.getLogger(__name__)


def register_webhook(app: web.Application, reports: Reports) -> None:
    secret = reports.settings.telegram_webhook_secret.get_secret_value()

    async def receive(request: web.Request) -> web.Response:
        supplied = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if not secret or not hmac.compare_digest(supplied.encode(), secret.encode()):
            raise web.HTTPUnauthorized()
        if not reports.writer_ready:
            raise web.HTTPServiceUnavailable()
        try:
            payload = await request.json()
            Update.model_validate(payload)
        except (ValueError, TypeError, ValidationError):
            raise web.HTTPBadRequest() from None
        # Acknowledge only after the commit; long LLM calls cannot cause Telegram timeouts.
        reports.store.enqueue_update(payload)
        return web.json_response({"ok": True})

    app.router.add_post(PATH, receive)


async def consume_updates(
    reports: Reports, dispatcher: Dispatcher, bot: Bot, stopped: asyncio.Event | None = None
) -> None:
    while stopped is None or not stopped.is_set():
        update = reports.store.pending_update()
        if not update:
            await asyncio.sleep(0.5)
            continue
        update_id, payload, attempts = update
        try:
            await dispatcher.feed_raw_update(bot, payload)
        except Exception as exc:
            # No payload, token or credential-bearing exception URLs in logs.
            logger.error("Telegram update %s failed: %s", update_id, type(exc).__name__)
            reports.store.retry_update(update_id, attempts)
        else:
            reports.store.finish_update(update_id)
