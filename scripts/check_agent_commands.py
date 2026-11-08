"""Live LLM conversation smoke test. Google Sheets and Telegram are simulated.

Run explicitly: uv run python scripts/check_agent_commands.py
Uses the configured LLM credentials; never writes to the real spreadsheet.
"""

import asyncio
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
from pg_sandbox import sandbox_store

from m100_bot.config import Settings
from m100_bot.domain import Position
from m100_bot.llm import LLM
from m100_bot.service import Reports
from m100_bot.sheets import Snapshot
from m100_bot.ui import show_draft, show_saved


class MemorySheet:
    url = "https://docs.google.com/spreadsheets/d/smoke-only/edit"

    def __init__(self):
        self.positions = {
            "cable": Position(
                id="cable",
                row=5,
                name="Кабель 3х2,5",
                section="ЭМ3",
                unit="м",
                planned=100,
                completed=0,
            ),
            "outdoor1": Position(
                id="outdoor1",
                row=6,
                name="Наружный блок MDOC-18HFN8",
                section="ОВ3",
                unit="шт",
                planned=10,
                completed=0,
            ),
            "outdoor2": Position(
                id="outdoor2",
                row=7,
                name="Наружный блок MDOC-07HDN8",
                section="ОВ3",
                unit="шт",
                planned=10,
                completed=0,
            ),
        }
        self.values = dict.fromkeys(self.positions, Decimal(0))
        self.colors = {}
        self.receipts = set()

    async def snapshot(self, day):
        return Snapshot(0, 80, 702, self.positions, self.values.copy(), 9, 10, 1)

    async def apply(self, op):
        if op.id in self.receipts:
            return
        for change in op.changes:
            assert self.values[change.position_id] == change.before
            self.values[change.position_id] = change.after
            if op.highlight:
                self.colors[change.cell] = op.highlight
        if op.kind == "format":
            for cell in op.cells:
                self.colors[cell] = op.highlight
        self.receipts.add(op.id)


async def main():
    settings = Settings()
    settings.bitrix_enabled = False
    settings.bitrix_access_enabled = False
    settings.allowed_user_ids = "7"
    settings.web_auto_open = False
    sheet = MemorySheet()
    with sandbox_store() as store:
        async with httpx.AsyncClient() as http:
            reports = Reports(settings, store, sheet, LLM(settings, http), SimpleNamespace())
            reports.today = lambda: date(2026, 11, 7)

            async def command(text, expected):
                store.remember(7, 7, "user", text)
                plan, target = await reports.plan_command(7, 7, text)
                assert plan.action == expected, f"{text}: {plan.model_dump_json()}"
                reply = SimpleNamespace(answer=AsyncMock())
                if plan.action == "format":
                    op = await reports.format_cells(7, 7, plan)
                    await show_saved(reply, op, reports)
                else:
                    draft = await reports.execute_command(target, plan, text)
                    if draft.status == "done":
                        await show_saved(reply, store.operation_for(draft.id), reports)
                    else:
                        await show_draft(reply, draft, reports)
                reply.answer.assert_awaited_once()
                response = reply.answer.call_args.args[0]
                store.remember(7, 7, "assistant", response)
                print(f"PASS {plan.action}: {text}", flush=True)

            await command(
                "Добавь кабель три на два с половиной десять метров и сразу сохрани", "add"
            )
            assert sheet.values["cable"] == 10 and reports.active_draft(7) is None
            await command("Добавь пять наружных блоков", "add")
            pending = reports.active_draft(7)
            assert len(pending.lines) == 1 and pending.lines[0].position_id is None
            await command("Выделили мне всё, что ты сегодня добавлял жёлтым цветом", "format")
            assert sheet.colors["J5"] in {"yellow", "#FFFF00"}
            assert reports.active_draft(7) == pending
            await command("Теперь их синим", "format")
            assert sheet.colors["J5"].upper() == "#0000FF"
            await command("А названия этих работ — бирюзовым", "format")
            assert "B5" in sheet.colors and sheet.values["cable"] == 10
            await command("Уточни блок: MDOC-18HFN8. И сразу сохрани", "edit")
            assert sheet.values["outdoor1"] == 5 and sheet.values["cable"] == 10
            assert reports.active_draft(7) is None
            await command("Выдели всё, что ты сегодня добавлял, светло-зелёным", "format")
            assert sheet.colors["J5"] == sheet.colors["J6"]
            await command("Сними с них выделение", "format")
            assert sheet.colors["J5"] == sheet.colors["J6"] == "clear"
            await command("Исправь уже записанный кабель на 12 метров и сохрани", "edit")
            assert sheet.values["cable"] == 12 and sheet.values["outdoor1"] == 5
            await command("Выдели B5:D5 цветом #A8DADC", "format")
            assert all(sheet.colors[cell].upper() == "#A8DADC" for cell in ["B5", "C5", "D5"])
            print("10 conversation steps passed; no real Sheets or Telegram writes.", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
