import asyncio
import logging
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
from .web import start_web


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
