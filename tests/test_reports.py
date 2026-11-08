from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from m100_bot.config import Settings
from m100_bot.domain import Line, Position, UserError, number, validate_line
from m100_bot.service import Reports
from m100_bot.sheets import Snapshot
from m100_bot.store import Store


def position(key="cable", name="Кабель 3х2,5", unit="м"):
    return Position(id=key, row=5, name=name, section="ЭМ3", unit=unit, planned=100, completed=20)


class FakeSheets:
    url = "https://docs.google.com/spreadsheets/d/test/edit"

    def __init__(self):
        self.positions = {"cable": position()}
        self.quantities = {"cable": Decimal(5)}
        self.receipts = set()
        self.writes = 0
        self.lose_response = False

    async def snapshot(self, day):
        return Snapshot(0, 80, 702, self.positions, self.quantities.copy(), 9, 10, 1)

    async def apply(self, op):
        if op.id in self.receipts:
            return
        for change in op.changes:
            assert self.quantities[change.position_id] == change.before
            self.quantities[change.position_id] = change.after
        self.receipts.add(op.id)
        self.writes += 1
        if self.lose_response:
            self.lose_response = False
            raise UserError("Ответ Google потерян после успешной записи")


@pytest.fixture
def system(tmp_path, postgres_url):
    settings = Settings(_env_file=None, bitrix_enabled=True, database_url=postgres_url)
    store = Store(settings.database_dsn)
    sheets = FakeSheets()
    llm = SimpleNamespace(
        extract=AsyncMock(
            return_value=[
                Line(phrase="кабель 10 метров", position_id="cable", quantity=10, unit="м")
            ]
        )
    )
    bitrix = SimpleNamespace(apply=AsyncMock())
    reports = Reports(settings, store, sheets, llm, bitrix)
    reports.today = lambda: date(2026, 11, 2)
    yield reports
    store.close()


async def test_no_write_until_confirmation_and_double_click_is_idempotent(system):
    draft = await system.create(7, 7, "кабель 10 метров")
    assert system.sheets.writes == 0
    assert system.changes(draft, await system.sheets.snapshot(draft.day))[0].after == 15
    await system.confirm(draft)
    await system.confirm(draft)
    assert system.sheets.quantities["cable"] == 15
    assert system.sheets.writes == 1
    system.bitrix.apply.assert_awaited_once()


async def test_partial_failure_retries_only_bitrix_and_blocks_next_operation(system):
    system.bitrix.apply.side_effect = [UserError("temporary"), None]
    draft = await system.create(7, 7, "кабель 10 метров")
    with pytest.raises(UserError):
        await system.confirm(draft)
    saved = system.store.operation_for(draft.id)
    assert saved.sheets_done and not saved.bitrix_done
    another = await system.create(8, 8, "кабель 10 метров")
    with pytest.raises(UserError, match="предыдущую"):
        await system.confirm(another)
    await system.confirm(draft)
    assert system.sheets.writes == 1
    assert system.store.get_draft(draft.id, 7).status == "done"


async def test_google_committed_but_response_lost_then_process_restarts(system):
    system.sheets.lose_response = True
    draft = await system.create(7, 7, "кабель 10 метров")
    with pytest.raises(UserError):
        await system.confirm(draft)
    path = system.settings.database_dsn
    system.store.close()
    system.store = Store(path)
    restored = system.store.active_draft(7)
    await system.confirm(restored)
    assert system.sheets.writes == 1
    assert system.sheets.quantities["cable"] == 15


async def test_today_replaces_quantity_instead_of_adding(system):
    draft = await system.create(7, 7)
    draft.lines[0].quantity = Decimal(2)
    system.save_edit(draft)
    await system.confirm(draft)
    assert system.sheets.quantities["cable"] == 2


async def test_clearing_saved_line_persists_zero(system):
    draft = await system.create(7, 7)
    draft.lines[0].quantity = Decimal(0)
    system.save_edit(draft)
    await system.confirm(draft)
    assert system.sheets.quantities["cable"] == 0


async def test_changed_day_cell_rejects_stale_confirmation(system):
    draft = await system.create(7, 7, "кабель 10 метров")
    system.sheets.quantities["cable"] = Decimal(8)
    with pytest.raises(UserError, match="изменились"):
        await system.confirm(draft)
    assert system.sheets.writes == 0
    assert system.store.operation_for(draft.id) is None


async def test_ambiguous_line_cannot_be_committed(system):
    system.llm.extract.return_value = [Line(phrase="кабель", quantity=10, unit="м")]
    draft = await system.create(7, 7, "кабель")
    with pytest.raises(UserError, match="уточните"):
        await system.confirm(draft)
    assert system.sheets.writes == 0


async def test_cancel_and_previous_day_cancel(system):
    draft = await system.create(7, 7, "кабель 10 метров")
    system.today = lambda: date(2026, 11, 3)
    with pytest.raises(UserError, match="новый день"):
        system.editable(draft)
    system.cancel(draft)
    assert system.store.active_draft(7) is None
    assert system.sheets.writes == 0


async def test_previous_day_unsubmitted_draft_does_not_block_new_voice_report(system):
    old = await system.create(7, 7, "кабель 10 метров")
    system.today = lambda: date(2026, 11, 3)
    current = await system.create(7, 7, "кабель 20 метров")
    assert current.id != old.id
    assert system.store.get_draft(old.id, 7).status == "cancelled"
    assert system.sheets.writes == 0


async def test_previous_day_partial_save_is_never_abandoned(system):
    system.bitrix.apply.side_effect = UserError("temporary")
    old = await system.create(7, 7, "кабель 10 метров")
    with pytest.raises(UserError):
        await system.confirm(old)
    system.today = lambda: date(2026, 11, 3)
    assert system.active_draft(7).id == old.id
    with pytest.raises(UserError, match="незавершённый"):
        await system.create(7, 7, "кабель 20 метров")


async def test_draft_cannot_be_read_by_another_user(system):
    draft = await system.create(7, 7, "кабель 10 метров")
    with pytest.raises(UserError):
        system.store.get_draft(draft.id, 8)


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity"])
def test_nonfinite_amount_rejected(value):
    with pytest.raises(UserError):
        number(value)


def test_comma_decimal_and_negative_validation():
    assert number("2,5") == Decimal("2.5")
    with pytest.raises(ValidationError):
        Line(phrase="x", quantity=-1)


def test_mismatched_units_and_fractional_pieces_require_correction():
    catalog = {"cable": position()}
    line = validate_line(Line(phrase="x", position_id="cable", quantity=2, unit="кг"), catalog)
    assert line.quantity is None
    catalog = {"piece": position("piece", "Хомут", "шт")}
    line = validate_line(Line(phrase="x", position_id="piece", quantity="2.5", unit="шт"), catalog)
    assert line.quantity is None


def test_duplicate_names_require_explicit_user_choice():
    catalog = {"a": position("a", "Сверление"), "b": position("b", "Сверление")}
    line = validate_line(Line(phrase="x", position_id="a", quantity=2, unit="м"), catalog)
    assert line.position_id is None
    assert line.candidates == ["a", "b"]
    line.position_id = "a"
    line.confirmed_position = True
    assert validate_line(line, catalog).position_id == "a"


async def test_voice_edit_targets_only_selected_line_and_requires_confirmation(system):
    from m100_bot.llm import VoiceCommand

    draft = await system.create(7, 7, "кабель 10 метров")
    draft.lines.append(Line(phrase="вторая работа", position_id="cable", quantity=3, unit="м"))
    system.save_edit(draft)
    command = VoiceCommand(
        action="edit",
        edits=[
            {
                "line_number": 1,
                "quantity": 20,
                "position_id": None,
                "remove": False,
            }
        ],
        additions="",
        question="",
    )
    updated = await system.execute_command(draft, command, "Исправь первый пункт на 20 метров")
    assert [line.quantity for line in updated.lines] == [20, 3]
    assert system.sheets.writes == 0
    await system.confirm(updated)
    assert system.sheets.quantities["cable"] == 28


async def test_voice_edit_after_save_replaces_daily_total_instead_of_adding(system):
    from m100_bot.llm import VoiceCommand

    first = await system.create(7, 7, "кабель 10 метров")
    await system.confirm(first)
    command = VoiceCommand(
        action="edit",
        edits=[
            {
                "line_number": 1,
                "quantity": 12,
                "position_id": None,
                "remove": False,
            }
        ],
        additions="",
        question="",
    )
    system.llm.command = AsyncMock(return_value=command)
    plan, target = await system.plan_command(7, 7, "Исправь кабель на 12 метров")
    updated = await system.execute_command(target, plan, "Исправь кабель на 12 метров")
    assert updated.mode == "replace"
    assert system.sheets.quantities["cable"] == 15
    await system.confirm(updated)
    assert system.sheets.quantities["cable"] == 12


async def test_spoken_delete_of_recorded_work_explicitly_zeroes_it(system):
    from m100_bot.llm import VoiceCommand

    draft = await system.create(7, 7)
    command = VoiceCommand(
        action="edit",
        edits=[
            {
                "line_number": 1,
                "quantity": None,
                "position_id": None,
                "remove": True,
            }
        ],
        additions="",
        question="",
    )
    updated = await system.execute_command(draft, command, "Удали кабель")
    assert updated.lines[0].deleted and updated.lines[0].quantity == 0
    await system.confirm(updated)
    assert system.sheets.quantities["cable"] == 0


async def test_invalid_multi_line_voice_edit_is_atomic(system):
    from m100_bot.llm import VoiceCommand

    draft = await system.create(7, 7, "кабель 10 метров")
    command = VoiceCommand(
        action="edit",
        edits=[
            {"line_number": 1, "quantity": 20, "position_id": None, "remove": False},
            {"line_number": 8, "quantity": 4, "position_id": None, "remove": False},
        ],
        additions="",
        question="",
    )
    with pytest.raises(UserError, match="однозначно"):
        await system.execute_command(draft, command, "Измени первую и восьмую строки")
    assert system.store.get_draft(draft.id, 7).lines[0].quantity == 10
    assert draft.lines[0].quantity == 10


async def test_voice_rerecord_replaces_draft_and_preserves_daily_mode(system):
    from m100_bot.llm import VoiceCommand

    draft = await system.create(7, 7)
    system.llm.extract.return_value = [
        Line(phrase="кабель 2 метра", position_id="cable", quantity=2, unit="м")
    ]
    updated = await system.execute_command(
        draft,
        VoiceCommand(
            action="rewrite",
            edits=[],
            additions="кабель 2 метра",
            question="",
        ),
        "Перезапиши весь отчёт: кабель 2 метра",
    )
    assert updated.mode == "replace"
    assert len(updated.lines) == 1 and updated.lines[0].quantity == 2
    await system.confirm(updated)
    assert system.sheets.quantities["cable"] == 2


async def test_ambiguous_command_does_not_create_or_change_draft(system):
    from m100_bot.llm import VoiceCommand

    system.llm.command = AsyncMock(
        return_value=VoiceCommand(
            action="clarify",
            edits=[],
            additions="",
            question="Какое сечение кабеля?",
        )
    )
    command, target = await system.plan_command(7, 7, "Исправь кабель")
    with pytest.raises(UserError, match="сечение"):
        await system.execute_command(target, command, "Исправь кабель")
    assert system.store.active_draft(7) is None
    assert system.sheets.writes == 0


def save_ready_command(**kwargs):
    from m100_bot.llm import VoiceCommand

    return VoiceCommand(action="save_ready", edits=[], additions="", question="", **kwargs)


async def mixed_draft(system, mode="add"):
    draft = await system.create(7, 7, "кабель 10 метров; трубу надо уточнить")
    draft.mode = mode
    draft.lines.append(
        Line(phrase="труба 8 метров", quantity=8, unit="м", question="Уточните диаметр трубы")
    )
    system.save_edit(draft)
    return draft


async def test_save_ready_yellow_preserves_unresolved_and_later_adds_without_repeating(system):
    draft = await mixed_draft(system)
    unresolved = draft.lines[1].model_dump()
    result = await system.execute_command(
        draft, save_ready_command(highlight="yellow"), "Сохрани уточнённое, пометь жёлтым"
    )
    op = system.store.operation_for(draft.id)
    assert result.status == "done" and len(result.lines) == 1
    assert op.highlight == "yellow" and op.changes[0].cell == "J5"
    assert "#gid=0&range=J5" in op.table_url
    assert system.sheets.quantities["cable"] == 15
    remainder = system.active_draft(7)
    assert remainder.id == op.continuation_id
    assert [line.model_dump() for line in remainder.lines] == [unresolved]
    assert remainder.before["cable"] == 15
    # Clarifying the remaining line to the same position must not add the first 10 again.
    remainder.lines[0] = Line(phrase="кабель", position_id="cable", quantity=8, unit="м")
    system.save_edit(remainder)
    await system.confirm(remainder)
    await system.confirm(draft)
    assert system.sheets.quantities["cable"] == 23
    assert system.sheets.writes == 2
    assert system.active_draft(7) is None


@pytest.mark.parametrize("failure", ["google", "bitrix"])
async def test_partial_save_retry_after_restart_keeps_color_and_exact_remainder(system, failure):
    draft = await mixed_draft(system)
    if failure == "google":
        system.sheets.lose_response = True
    else:
        system.bitrix.apply.side_effect = [UserError("temporary"), None]
    with pytest.raises(UserError):
        await system.execute_command(
            draft, save_ready_command(highlight="yellow"), "Сохрани готовое"
        )
    path = system.settings.database_dsn
    system.store.close()
    system.store = Store(path)
    resumed = system.active_draft(7)
    assert resumed.status == "saving"
    op = await system.confirm(resumed)
    remainder = system.active_draft(7)
    assert op.highlight == "yellow" and system.sheets.writes == 1
    assert len(remainder.lines) == 1 and remainder.lines[0].question == "Уточните диаметр трубы"
    remainder.lines[0].phrase = "правка после сохранения"
    system.save_edit(remainder)
    await system.confirm(resumed)
    assert system.active_draft(7).lines[0].phrase == "правка после сохранения"


async def test_partial_replace_never_zeroes_unresolved_saved_position(system):
    system.sheets.positions["pipe"] = position("pipe", "Труба")
    system.sheets.quantities["pipe"] = Decimal(9)
    draft = await system.create(7, 7)
    draft.lines[0].quantity = Decimal(2)
    draft.lines[1].quantity = None
    draft.lines[1].question = "Уточните объём трубы"
    system.save_edit(draft)
    await system.execute_command(draft, save_ready_command(highlight="yellow"), "Сохрани готовое")
    assert system.sheets.quantities == {"cable": 2, "pipe": 9}
    remainder = system.active_draft(7)
    assert remainder.mode == "replace" and remainder.lines[0].position_id == "pipe"
    remainder.lines[0].quantity = Decimal(4)
    remainder.lines[0].question = ""
    system.save_edit(remainder)
    await system.confirm(remainder)
    assert system.sheets.quantities == {"cable": 2, "pipe": 4}


@pytest.mark.parametrize(
    "problem",
    [
        Line(phrase="кабель", position_id="cable", unit="м"),
        Line(phrase="кабель", position_id="cable", quantity=2, unit="кг"),
        Line(
            phrase="кабель", position_id="cable", quantity=2, unit="м", question="Уточните данные"
        ),
    ],
)
async def test_unresolved_contribution_blocks_whole_cell(system, problem):
    draft = await system.create(7, 7, "кабель 10 метров")
    draft.lines.append(problem)
    system.save_edit(draft)
    with pytest.raises(UserError, match="Нет готовых"):
        await system.execute_command(
            draft, save_ready_command(highlight="yellow"), "Сохрани готовое"
        )
    assert system.sheets.writes == 0 and system.store.operation_for(draft.id) is None
    assert len(system.active_draft(7).lines) == 2


async def test_save_ready_can_select_only_named_lines(system):
    system.sheets.positions["pipe"] = position("pipe", "Труба")
    system.sheets.quantities["pipe"] = Decimal(9)
    draft = await system.create(7, 7)
    await system.execute_command(
        draft,
        save_ready_command(line_numbers=[2], highlight="yellow"),
        "Сохрани вторую строку и пометь жёлтым",
    )
    op = system.store.operation_for(draft.id)
    assert [change.position_id for change in op.changes] == ["pipe"]
    assert system.active_draft(7).lines[0].position_id == "cable"
    assert system.sheets.quantities["cable"] == 5


async def test_unknown_line_in_save_command_cannot_write(system):
    draft = await system.create(7, 7, "кабель")
    with pytest.raises(UserError, match="Строка не найдена"):
        await system.execute_command(draft, save_ready_command(line_numbers=[8]), "Сохрани восьмую")
    assert system.sheets.writes == 0


async def test_voice_editing_remainder_keeps_previously_saved_daily_totals(system):
    from m100_bot.llm import VoiceCommand

    system.sheets.positions["pipe"] = position("pipe", "Труба")
    system.sheets.quantities["pipe"] = Decimal(9)
    draft = await system.create(7, 7)
    draft.lines[0].quantity = Decimal(2)
    draft.lines[1].quantity = None
    system.save_edit(draft)
    await system.execute_command(draft, save_ready_command(), "Сохрани готовое")
    remainder = system.active_draft(7)
    updated = await system.execute_command(
        remainder,
        VoiceCommand(
            action="edit",
            edits=[{"line_number": 1, "quantity": 4, "position_id": None, "remove": False}],
            additions="",
            question="",
        ),
        "Уточни трубу: четыре метра",
    )
    assert [line.position_id for line in updated.lines] == ["pipe"]
    await system.confirm(updated)
    assert system.sheets.quantities == {"cable": 2, "pipe": 4}


def agent_command(action="format", **kwargs):
    from m100_bot.llm import VoiceCommand

    return VoiceCommand(action=action, edits=[], additions="", question="", **kwargs)


async def test_immediate_add_saves_in_same_command_with_arbitrary_color(system):
    from m100_bot.domain import Draft

    draft = Draft(user_id=7, chat_id=7, day=system.today(), before=system.sheets.quantities)
    result = await system.execute_command(
        draft,
        agent_command("add", save_immediately=True, highlight="#A8DADC"),
        "кабель 10 метров, сразу сохрани",
    )
    assert result.status == "done"
    assert system.sheets.quantities["cable"] == 15
    assert system.store.operation_for(result.id).highlight == "#A8DADC"
    assert system.active_draft(7) is None


async def test_immediate_save_intent_survives_clarification_and_restart(system):
    from m100_bot.domain import Draft
    from m100_bot.llm import VoiceCommand

    system.llm.extract.return_value = [
        Line(phrase="кабель 8 метров", quantity=8, unit="м", question="Какое сечение кабеля?")
    ]
    target = Draft(user_id=7, chat_id=7, day=system.today())
    pending = await system.execute_command(
        target, agent_command("add", save_immediately=True), "кабель 8 метров, сразу сохрани"
    )
    assert pending.auto_save and system.sheets.writes == 0
    path = system.settings.database_dsn
    system.store.close()
    system.store = Store(path)
    pending = system.active_draft(7)
    saved = await system.execute_command(
        pending,
        VoiceCommand(
            action="edit",
            additions="",
            question="",
            edits=[{"line_number": 1, "quantity": None, "position_id": "cable", "remove": False}],
        ),
        "три на два с половиной",
    )
    assert saved.status == "done"
    assert system.sheets.quantities["cable"] == 13
    assert len(saved.lines) == 1


async def test_wait_cancels_automatic_save_for_later_clarifications(system):
    from m100_bot.llm import VoiceCommand

    draft = await system.create(7, 7, "кабель")
    draft.auto_save = True
    system.save_edit(draft)
    await system.execute_command(
        draft, agent_command("wait", save_immediately=False), "пока не сохраняй"
    )
    pending = system.active_draft(7)
    updated = await system.execute_command(
        pending,
        VoiceCommand(
            action="edit",
            additions="",
            question="",
            edits=[{"line_number": 1, "quantity": 3, "position_id": None, "remove": False}],
        ),
        "исправь на три метра",
    )
    assert not updated.auto_save and updated.status == "draft"
    assert system.sheets.writes == 0


async def test_format_today_uses_saved_operations_and_does_not_touch_pending_work(system):
    first = await system.create(7, 7, "кабель 10 метров")
    await system.confirm(first)
    system.sheets.positions["pipe"] = position("pipe", "Труба")
    system.sheets.positions["pipe"].row = 6
    system.sheets.quantities["pipe"] = Decimal(0)
    system.llm.extract.return_value = [
        Line(phrase="труба", position_id="pipe", quantity=8, unit="м")
    ]
    second = await system.create(7, 7, "труба 8 м")
    await system.confirm(second)
    system.llm.extract.return_value = [
        Line(phrase="наружный блок", quantity=5, question="Какая модель наружного блока?")
    ]
    pending = await system.create(7, 7, "пять наружных блоков")
    before = system.sheets.quantities.copy()
    op = await system.format_cells(
        7, 7, agent_command(highlight="#FFFF00", target_scope="today_added")
    )
    assert op.kind == "format" and set(op.cells) == {"J5", "J6"}
    assert not op.changes and op.sheets_done and op.bitrix_done
    assert system.sheets.quantities == before
    assert system.active_draft(7) == pending
    assert system.bitrix.apply.await_count == 2


async def test_last_selection_survives_restart_and_midnight(system):
    op = await system.format_cells(
        7, 7, agent_command(highlight="#00AAAA", target_scope="cells", cells=["B5:D6"])
    )
    path = system.settings.database_dsn
    system.store.close()
    system.store = Store(path)
    system.today = lambda: date(2026, 11, 3)
    recolored = await system.format_cells(7, 7, agent_command(highlight="#CCAAFF"))
    assert recolored.cells == op.cells and recolored.day == op.day
    assert not recolored.changes
    assert system.sheets.quantities["cable"] == 5


async def test_format_can_target_specific_fields_by_position(system):
    op = await system.format_cells(
        7,
        7,
        agent_command(
            highlight="#FE09AD",
            target_scope="positions",
            position_ids=["cable"],
            fields=["name", "planned", "unit_pay"],
        ),
    )
    assert op.cells == ["B5", "D5", "E5"]
    assert system.sheets.quantities["cable"] == 5


@pytest.mark.parametrize("cells", [["Other!B5"], ["A0"], ["Z9999"], ["A1:ZZ5000"]])
async def test_format_rejects_invalid_or_excessive_ranges_without_creating_operation(system, cells):
    with pytest.raises(UserError):
        await system.format_cells(
            7, 7, agent_command(highlight="#008000", target_scope="cells", cells=cells)
        )
    assert not system.store.user_operations(7, 7, system.settings.google_spreadsheet_id)
    assert system.sheets.writes == 0


async def test_format_retry_after_lost_response_is_idempotent(system):
    system.sheets.lose_response = True
    with pytest.raises(UserError):
        await system.format_cells(
            7, 7, agent_command(highlight="#AABBCC", target_scope="cells", cells=["B5:D5"])
        )
    op = await system.retry(7, 7)
    assert op.sheets_done and op.highlight == "#AABBCC" and system.sheets.writes == 1
    assert system.sheets.quantities["cable"] == 5


async def test_agent_context_remembers_history_and_recorded_operations_with_user_scope(system):
    draft = await system.create(7, 7, "кабель 10 метров")
    await system.confirm(draft)
    system.store.remember(7, 7, "user", "кабель сохрани")
    system.store.remember(7, 7, "assistant", "сохранил кабель")
    system.store.remember(8, 8, "user", "чужое сообщение")
    system.llm.command = AsyncMock(
        return_value=agent_command(highlight="#0000FF", target_scope="today_added")
    )
    await system.plan_command(7, 7, "выдели сегодня добавленное")
    context = system.llm.command.call_args.kwargs["context"]
    assert context["history"][-1]["content"] == "сохранил кабель"
    assert context["recent_operations"][0]["changes"][0]["after"] == "15"
    assert "чужое" not in str(context)
    assert not system.store.user_operations(8, 8, system.settings.google_spreadsheet_id)


async def test_saved_work_edit_leaves_unresolved_draft_in_place(system):
    from m100_bot.llm import VoiceCommand

    original = await system.create(7, 7, "кабель 10 метров")
    await system.confirm(original)
    system.llm.extract.return_value = [
        Line(phrase="наружный блок", quantity=5, question="Какая модель наружного блока?")
    ]
    unresolved = await system.create(7, 7, "пять наружных блоков")
    system.llm.command = AsyncMock(
        return_value=VoiceCommand(
            action="edit",
            edits=[{"line_number": 1, "quantity": 20, "position_id": None, "remove": False}],
            additions="",
            question="",
            target_scope="positions",
            position_ids=["cable"],
            save_immediately=True,
        )
    )
    command, target = await system.plan_command(7, 7, "исправь записанный кабель на20 и сохрани")
    await system.execute_command(target, command, "исправь записанный кабель на20 и сохрани")
    assert system.sheets.quantities["cable"] == 20
    assert system.active_draft(7) == unresolved


async def test_format_selected_names_after_recoloring_uses_same_rows(system):
    await system.format_cells(
        7, 7, agent_command(highlight="#0000FF", target_scope="cells", cells=["J5"])
    )
    op = await system.format_cells(7, 7, agent_command(highlight="#40E0D0", fields=["name"]))
    assert op.cells == ["B5"]


async def test_today_added_does_not_include_noop_report_cells(system):
    draft = await system.create(7, 7)
    await system.confirm(draft)
    with pytest.raises(UserError, match="не менял"):
        await system.format_cells(
            7, 7, agent_command(highlight="#FF0000", target_scope="today_added")
        )
    assert system.sheets.writes == 1


async def test_replacing_saved_position_keeps_both_zeroed_source_and_new_target(system):
    from m100_bot.llm import VoiceCommand

    system.sheets.positions["pipe"] = position("pipe", "Труба")
    system.sheets.positions["pipe"].row = 6
    system.sheets.quantities["pipe"] = Decimal(0)
    system.llm.command = AsyncMock(
        return_value=VoiceCommand(
            action="edit",
            edits=[{"line_number": 1, "quantity": 7, "position_id": "pipe", "remove": False}],
            additions="",
            question="",
            target_scope="positions",
            position_ids=["cable"],
            save_immediately=True,
        )
    )
    command, target = await system.plan_command(
        7, 7, "замени записанный кабель трубой 7м и сохрани"
    )
    await system.execute_command(target, command, "замени записанный кабель трубой 7м и сохрани")
    assert system.sheets.quantities == {"cable": 0, "pipe": 7}


async def test_immediate_save_with_new_excess_stays_draft_until_master_reviews(system):
    system.sheets.positions["cable"].completed = Decimal(95)
    draft = await system.create(7, 7, "кабель десять метров")
    result = await system.maybe_save(draft, agent_command(action="add", save_immediately=True))
    assert result.status == "draft" and not result.auto_save
    assert system.store.operation_for(draft.id) is None
