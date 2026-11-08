import asyncio
import logging
import signal
import sys
from datetime import datetime

import httpx
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from .bitrix import Bitrix
from .branding import configure_menu
from .config import Settings
from .handlers import build_router
from .llm import LLM
from .service import Reports
from .sheets import Sheets
from .store import Store
from .telegram_webhook import PATH, consume_updates
from .web import start_web


async def run_webhook(reports: Reports, dispatcher: Dispatcher, bot: Bot) -> None:
    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stopped.set)
    worker = None
    waiter = None
    try:
        await dispatcher.emit_startup(bot=bot)
        await bot.set_webhook(
            reports.settings.web_base_url.rstrip("/") + PATH,
            secret_token=reports.settings.telegram_webhook_secret.get_secret_value(),
            allowed_updates=["message", "callback_query"],
            max_connections=1,
            drop_pending_updates=False,
        )
        logging.getLogger(__name__).info("Telegram webhook enabled")
        worker = asyncio.create_task(consume_updates(reports, dispatcher, bot, stopped))
        waiter = asyncio.create_task(stopped.wait())
        done, _ = await asyncio.wait([worker, waiter], return_when=asyncio.FIRST_COMPLETED)
        if worker in done:
            worker.result()
    finally:
        reports.writer_ready = False
        stopped.set()
        if worker and not worker.done():
            # Finish the current update before releasing the writer lease during a deploy.
            try:
                await asyncio.wait_for(asyncio.shield(worker), timeout=25)
            except Exception as exc:
                logging.getLogger(__name__).warning("Webhook drain: %s", type(exc).__name__)
        for task in (worker, waiter):
            if task:
                task.cancel()
        await asyncio.gather(*(task for task in (worker, waiter) if task), return_exceptions=True)
        await dispatcher.emit_shutdown(bot=bot)
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.remove_signal_handler(sig)


async def run(settings: Settings) -> None:
    store = Store(settings.database_dsn)
    try:
        async with httpx.AsyncClient(timeout=30) as http:
            sheets = Sheets(settings, http)
            await sheets.snapshot(datetime.now(settings.tz).date())
            reports = Reports(settings, store, sheets, LLM(settings, http), Bitrix(settings, http))
            reports.writer_ready = False
            dispatcher = Dispatcher()
            dispatcher.include_router(build_router(reports))
            async with Bot(
                token=settings.telegram_bot_token.get_secret_value(),
                default=DefaultBotProperties(parse_mode=ParseMode.HTML),
            ) as bot:
                me = await bot.me()
                reports.bot_url = f"https://t.me/{me.username}" if me.username else ""
                web_runner = await start_web(reports, bot) if settings.web_enabled else None
                try:
                    # New deployments can become healthy while the old consumer drains.
                    # Mutations stay blocked until this process owns the database lease.
                    for _ in range(180):
                        if store.try_acquire_writer(settings.google_spreadsheet_id):
                            break
                        await asyncio.sleep(1)
                    else:
                        raise RuntimeError("Writer lease did not become available")
                    reports.writer_ready = True
                    await configure_menu(bot)
                    if settings.telegram_mode == "webhook":
                        await run_webhook(reports, dispatcher, bot)
                    else:
                        if (await bot.get_webhook_info()).url:
                            from .domain import UserError

                            raise UserError(
                                "Для бота уже настроен вебхук. Локальный polling не запускается."
                            )
                        await dispatcher.start_polling(
                            bot,
                            handle_as_tasks=False,
                            allowed_updates=["message", "callback_query"],
                        )
                finally:
                    if web_runner:
                        await web_runner.cleanup()
    finally:
        store.close()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    try:
        settings = Settings()
        if problems := settings.problems():
            print("Заполните .env:\n- " + "\n- ".join(problems), file=sys.stderr)
            raise SystemExit(2)
        asyncio.run(run(settings))
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        # Do not expose credential-bearing request URLs in a default traceback.
        from .domain import UserError

        print(
            str(exc) if isinstance(exc, UserError) else f"Ошибка запуска: {type(exc).__name__}",
            file=sys.stderr,
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
