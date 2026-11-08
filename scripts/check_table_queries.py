"""Live LLM table questions, with simulated Sheets and Telegram. No real table writes."""

import asyncio
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import httpx
from check_agent_commands import MemorySheet
from pg_sandbox import sandbox_store

from m100_bot.config import Settings
from m100_bot.domain import Line
from m100_bot.llm import LLM
from m100_bot.service import Reports
from m100_bot.sheets import Snapshot


class QuerySheet(MemorySheet):
    def __init__(self):
        super().__init__()
        self.values.update(cable=Decimal(5), outdoor1=Decimal(2))
        self.positions["cable"].completed = Decimal(20)
        self.positions["cable"].unit_pay = Decimal(2)
        self.positions["cable"].completed_pay = Decimal(40)
        self.positions["outdoor1"].completed = Decimal(4)

    async def snapshot(self, day):
        return Snapshot(
            0,
            80,
            702,
            self.positions,
            self.values.copy(),
            9,
            11,
            1,
            [(date(2026, 11, 7), 9), (date(2026, 11, 6), 10)],
        )

    async def period_values(self, start, end, snap):
        assert start == end == date(2026, 11, 6)
        return {"cable": Decimal(3), "outdoor1": Decimal(1), "outdoor2": Decimal(0)}, [start]

    async def read_cells(self, cells, snap):
        assert cells == ["B5:D5"]
        return [("B5", "Кабель 3х2,5"), ("C5", "м"), ("D5", "100")]


async def main():
    settings = Settings()
    settings.bitrix_enabled = False
    settings.bitrix_access_enabled = False
    settings.allowed_user_ids = "7"
    settings.web_auto_open = False
    sheet = QuerySheet()
    with sandbox_store() as store:
        async with httpx.AsyncClient() as http:
            reports = Reports(settings, store, sheet, LLM(settings, http), SimpleNamespace())
            reports.today = lambda: date(2026, 11, 7)
            # Keep unrelated unresolved work; questions must not save or disturb it.
            from m100_bot.domain import Draft

            pending = Draft(
                user_id=7,
                chat_id=7,
                day=reports.today(),
                lines=[Line(phrase="пять блоков", quantity=5, question="Какая модель блока?")],
            )
            store.save_draft(pending)

            async def ask(text, expected):
                store.remember(7, 7, "user", text)
                snap = await sheet.snapshot(reports.today())
                plan, _ = await reports.plan_command(7, 7, text, snapshot=snap)
                assert plan.action == "query", plan.model_dump_json()
                answer = await reports.read_query(7, 7, plan, snapshot=snap)
                for fragment in expected:
                    assert fragment in answer.text, (
                        f"{text}: {plan.model_dump_json()} => {answer.text}"
                    )
                store.remember(7, 7, "assistant", answer.text)
                assert reports.active_draft(7) == pending and not sheet.receipts
                print(f"PASS: {text}", flush=True)
                return answer

            await ask("Сколько всего выполнено кабеля три на два с половиной?", ["20 м"])
            await ask("А сколько его осталось?", ["80 м"])
            await ask("А какая по нему ставка за единицу?", ["2 ден. ед./м"])
            await ask("Что записано сегодня?", ["5 м", "2 шт"])
            await ask("А за вчера?", ["3 м", "1 шт"])
            await ask("Сколько заработали по этим работам за вчера?", ["6 ден. ед."])
            await ask("Какие работы превысили смету?", ["Подходящих записей нет"])
            await ask("Покажи B5:D5", ["Кабель 3х2,5", "100"])
            answer = await ask("Покажи две работы с самым большим остатком", ["80 м"])
            assert set(answer.selection["position_ids"]) == {"cable", "outdoor2"}
            store.remember(7, 7, "user", "Выдели их синим")
            plan, _ = await reports.plan_command(7, 7, "Выдели их синим")
            assert plan.action == "format", plan.model_dump_json()
            op = await reports.format_cells(7, 7, plan)
            assert set(op.cells) == set(answer.selection["cells"]) and not op.changes, (
                plan.model_dump_json(),
                op.cells,
                answer.selection,
            )
            assert sheet.values["cable"] == 5 and reports.active_draft(7) == pending
            print("PASS: Выдели их синим", flush=True)
            print(
                "10 live conversation steps passed; no real Sheets or Telegram writes.", flush=True
            )


if __name__ == "__main__":
    asyncio.run(main())
