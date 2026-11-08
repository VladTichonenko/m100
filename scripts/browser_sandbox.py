"""Local-only acceptance sandbox: real PostgreSQL, in-memory external systems.

Run: uv run python scripts/browser_sandbox.py
Open the URL in artifacts/browser-sandbox-url.txt. Ctrl+C cleans up its database schema.
No production configuration, Google, Bitrix or Telegram credentials are loaded.
"""

import asyncio
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiohttp import web
from pg_sandbox import sandbox_store

from m100_bot.config import Settings
from m100_bot.domain import Line, Position, UserError
from m100_bot.service import Reports
from m100_bot.sheets import Snapshot
from m100_bot.web import create_app


async def main():
    with sandbox_store() as store:
        position = Position(
            id="QA-001",
            row=5,
            name="Кабель: проверка превышения",
            section="Проверка",
            unit="м",
            planned=100,
            completed=95,
            unit_pay=2,
            planned_pay=200,
            completed_pay=190,
        )
        replacement = Position(
            id="QA-002",
            row=6,
            name="Кабель для замены",
            section="Проверка",
            unit="м",
            planned=100,
            completed=10,
            unit_pay=3,
            planned_pay=300,
            completed_pay=30,
        )
        other = Position(
            id="QA-003",
            row=7,
            name="Другая сохранённая работа",
            section="Проверка",
            unit="шт",
            planned=20,
            completed=2,
            unit_pay=1,
            planned_pay=20,
            completed_pay=2,
        )
        positions = {p.id: p for p in (position, replacement, other)}
        values = {position.id: Decimal(0), replacement.id: Decimal(0), other.id: Decimal(2)}
        applied = set()

        async def snapshot(day):
            return Snapshot(0, 80, 702, positions, values.copy(), 9, 10, 1)

        async def apply(op):
            if op.id not in applied:
                for change in op.changes:
                    positions[change.position_id].completed += change.after - change.before
                    values[change.position_id] = change.after
                applied.add(op.id)

        bitrix_calls = 0

        async def bitrix_apply(*args, **kwargs):
            nonlocal bitrix_calls
            bitrix_calls += 1
            if bitrix_calls == 1:
                raise UserError(
                    "Проверочный сбой Битрикс24. Google уже сохранил объём; повторите отправку."
                )

        settings = Settings(
            _env_file=None, allowed_user_ids="7", bitrix_enabled=True, web_auto_open=False
        )
        sheets = SimpleNamespace(
            url="https://example.invalid/test-sheet", snapshot=snapshot, apply=apply
        )
        llm = SimpleNamespace(
            extract=AsyncMock(
                return_value=[
                    Line(phrase=position.name, position_id=position.id, unit="м", quantity=8)
                ]
            )
        )
        reports = Reports(settings, store, sheets, llm, SimpleNamespace(apply=bitrix_apply))
        reports.today = lambda: date(2026, 11, 8)
        draft = await reports.create(7, 7, "проверочный кабель 8 метров")
        token = store.web_token(draft)
        path = Path("artifacts/browser-sandbox-url.txt")
        path.parent.mkdir(exist_ok=True)
        path.write_text(f"http://127.0.0.1:8766/report/{draft.id}#token={token}")
        path.chmod(0o600)
        runner = web.AppRunner(create_app(reports, Path("web/dist")))
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", 8766).start()
        print(
            "Sandbox ready. Open artifacts/browser-sandbox-url.txt. "
            "First Bitrix save fails; retry succeeds.",
            flush=True,
        )
        try:
            await asyncio.Event().wait()
        finally:
            await runner.cleanup()
            path.unlink(missing_ok=True)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
