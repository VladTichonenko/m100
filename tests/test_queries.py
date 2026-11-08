import csv
from datetime import date
from decimal import Decimal
from io import StringIO
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from m100_bot.config import Settings
from m100_bot.domain import Line, Position, UserError
from m100_bot.llm import VoiceCommand
from m100_bot.queries import TableQuery, csv_bytes
from m100_bot.service import Reports
from m100_bot.sheets import Snapshot
from m100_bot.store import Store


@pytest.fixture
async def query_system(tmp_path, postgres_url):
    positions = {
        "a": Position(
            id="a",
            row=5,
            name="Кабель <3х2,5>",
            section="ЭМ3",
            unit="м",
            planned=100,
            completed=20,
            unit_pay=2,
            planned_pay=200,
            completed_pay=40,
        ),
        "b": Position(
            id="b",
            row=6,
            name="Кабель 3х1,5",
            section="ЭМ3",
            unit="м",
            planned=50,
            completed=60,
            unit_pay=3,
            planned_pay=150,
            completed_pay=180,
        ),
        "c": Position(
            id="c", row=7, name="Блок", section="ОВ 3", unit="шт", planned=5, completed=0
        ),
    }
    snap = Snapshot(
        12,
        100,
        702,
        positions,
        {"a": Decimal(5), "b": Decimal(2), "c": Decimal(0)},
        10,
        11,
        1,
        [(date(2026, 11, 6), 9), (date(2026, 11, 7), 10)],
    )
    sheets = SimpleNamespace(
        url="https://docs.google.com/spreadsheets/d/test/edit",
        snapshot=AsyncMock(return_value=snap),
        apply=AsyncMock(),
        period_values=AsyncMock(
            return_value=({"a": Decimal(3), "b": Decimal(4), "c": Decimal(0)}, [date(2026, 11, 6)])
        ),
        read_cells=AsyncMock(
            return_value=[("B5", "<Название>"), ("C5", "м"), ("D5", "0"), ("E5", "")]
        ),
    )
    settings = Settings(_env_file=None, database_url=postgres_url)
    store = Store(settings.database_dsn)
    reports = Reports(
        settings,
        store,
        sheets,
        SimpleNamespace(command=AsyncMock(), extract=AsyncMock()),
        SimpleNamespace(apply=AsyncMock()),
    )
    reports.today = lambda: date(2026, 11, 7)
    yield SimpleNamespace(reports=reports, sheets=sheets, snapshot=snap)
    reports.store.close()


def command(**kwargs):
    ids = kwargs.pop("ids", [])
    cells = kwargs.pop("cells", [])
    return VoiceCommand(
        action="query",
        edits=[],
        additions="",
        question="",
        position_ids=ids,
        cells=cells,
        query=TableQuery(**kwargs),
    )


async def test_query_reads_current_values_without_saving_or_using_draft_numbers(query_system):
    q = query_system
    q.reports.llm.extract.return_value = [
        Line(phrase="кабель", position_id="a", quantity=999, unit="м")
    ]
    draft = await q.reports.create(7, 7, "кабель 999 м")
    result = await q.reports.read_query(
        7, 7, command(ids=["a"], scope="positions", metrics=["completed", "remaining"])
    )
    assert "20 м" in result.text and "80 м" in result.text and "999" not in result.text
    assert "&lt;3х2,5&gt;" in result.text and result.document is None
    assert q.reports.active_draft(7) == draft
    q.sheets.apply.assert_not_awaited()
    q.reports.bitrix.apply.assert_not_awaited()


async def test_today_reuses_snapshot_and_avoids_additional_google_reads(query_system):
    q = query_system
    result = await q.reports.read_query(
        7, 7, command(metrics=["daily"], status="recorded"), snapshot=q.snapshot
    )
    assert "5 м" in result.text and "2 м" in result.text and "Блок" not in result.text
    q.sheets.snapshot.assert_not_awaited()
    q.sheets.period_values.assert_not_awaited()
    q.sheets.apply.assert_not_awaited()


async def test_query_totals_keep_incompatible_units_separate(query_system):
    result = await query_system.reports.read_query(
        7, 7, command(metrics=["planned"], aggregate="sum")
    )
    assert "150 м" in result.text and "5 шт" in result.text
    assert "155" not in result.text


async def test_missing_money_is_not_reported_as_zero_or_a_complete_total(query_system):
    q = query_system
    result = await q.reports.read_query(7, 7, command(metrics=["completed_pay"], aggregate="sum"))
    assert "220 ден. ед." in result.text and "неполная сумма" in result.text
    result = await q.reports.read_query(
        7, 7, command(ids=["c"], metrics=["unit_pay", "completed_pay"])
    )
    assert "нет данных" in result.text and "0 ден." not in result.text


async def test_period_quantities_and_pay_are_computed_from_real_period_values(query_system):
    q = query_system
    result = await q.reports.read_query(
        7,
        7,
        command(
            metrics=["daily", "day_pay"],
            aggregate="sum",
            status="recorded",
            date_from="2026-11-06",
            date_to="2026-11-06",
        ),
    )
    assert "7 м" in result.text and "18 ден. ед." in result.text
    q.sheets.period_values.assert_awaited_once_with(
        date(2026, 11, 6), date(2026, 11, 6), q.snapshot
    )


async def test_absent_date_is_distinct_from_zero_volume(query_system):
    q = query_system
    q.sheets.period_values.return_value = ({}, [])
    result = await q.reports.read_query(7, 7, command(metrics=["daily"], date_from="2026-11-01"))
    assert "нет столбцов" in result.text and "0 м" not in result.text


@pytest.mark.parametrize(
    ("status", "ids"),
    [
        ("over_budget", ["b"]),
        ("not_started", ["c"]),
        ("remaining", ["a", "c"]),
        ("completed", ["b"]),
    ],
)
async def test_status_queries_select_correct_positions(query_system, status, ids):
    result = await query_system.reports.read_query(
        7, 7, command(status=status, metrics=["remaining"])
    )
    assert result.selection["position_ids"] == ids


async def test_query_section_and_top_with_missing_values(query_system):
    q = query_system
    result = await q.reports.read_query(
        7, 7, command(section="ЭМ 3", metrics=["completed"], sort_by="completed", limit=1)
    )
    assert result.selection["position_ids"] == ["b"]
    result = await q.reports.read_query(
        7, 7, command(metrics=["unit_pay"], sort_by="unit_pay", descending=False)
    )
    assert result.selection["position_ids"] == ["a", "b", "c"]


async def test_follow_up_query_and_highlight_use_last_read_selection(query_system):
    q = query_system
    await q.reports.read_query(7, 7, command(ids=["a"], metrics=["remaining"]))
    followup = await q.reports.read_query(
        7, 7, command(scope="last_selection", metrics=["unit_pay"])
    )
    assert followup.selection["position_ids"] == ["a"] and "2 ден. ед./м" in followup.text
    op = await q.reports.format_cells(
        7,
        7,
        VoiceCommand(action="format", edits=[], additions="", question="", highlight="#008000"),
    )
    assert op.cells == ["E5"] and not op.changes


async def test_query_selection_survives_restart_and_is_scoped_to_chat_and_table(query_system):
    q = query_system
    await q.reports.read_query(7, 7, command(ids=["a"], metrics=["completed"]))
    path = q.reports.settings.database_dsn
    q.reports.store.close()
    q.reports.store = Store(path)
    result = await q.reports.read_query(
        7, 7, command(scope="last_selection", metrics=["remaining"])
    )
    assert "80 м" in result.text
    with pytest.raises(UserError, match="Какие работы"):
        await q.reports.read_query(7, 8, command(scope="last_selection", metrics=["remaining"]))
    assert q.reports.store.selection(7, 7, "another-table", "Лист1") is None


async def test_raw_cells_preserve_zero_blank_and_escaped_text(query_system):
    result = await query_system.reports.read_query(7, 7, command(scope="cells", cells=["B5:E5"]))
    assert (
        "&lt;Название&gt;" in result.text
        and "<b>D5</b>: 0" in result.text
        and "<b>E5</b>: пусто" in result.text
    )


async def test_large_query_returns_one_csv_with_all_results(query_system):
    q = query_system
    for i in range(20):
        p = q.snapshot.positions["a"].model_copy(
            update={"id": f"x{i}", "row": i + 10, "name": f"Работа {i}"}
        )
        q.snapshot.positions[p.id] = p
    result = await q.reports.read_query(7, 7, command(metrics=["completed"]))
    assert result.document is not None and len(result.text) < 1024
    rows = list(csv.reader(StringIO(result.document.decode("utf-8-sig")), delimiter=";"))
    assert len(rows) == 24 and rows[-1][0] == "x19"
    assert "в файле" in result.text


@pytest.mark.parametrize(
    "dates",
    [
        {"date_from": "bad"},
        {"date_from": "2026-11-08", "date_to": "2026-11-01"},
        {"date_from": "2020-01-01", "date_to": "2026-11-07"},
    ],
)
async def test_invalid_periods_cannot_read_or_write_other_data(query_system, dates):
    q = query_system
    with pytest.raises(UserError):
        await q.reports.read_query(7, 7, command(metrics=["daily"], **dates))
    q.sheets.period_values.assert_not_awaited()
    q.sheets.apply.assert_not_awaited()


def test_csv_formula_content_is_neutralized_without_changing_negative_numbers():
    text = csv_bytes([["=HYPERLINK(1)", "@formula", "-10", "-test", "normal"]]).decode("utf-8-sig")
    row = next(csv.reader(StringIO(text), delimiter=";"))
    assert row == ["'=HYPERLINK(1)", "'@formula", "-10", "'-test", "normal"]


async def test_nested_query_selector_is_used_instead_of_the_whole_catalog(query_system):
    q = query_system
    plan = VoiceCommand(
        action="query",
        edits=[],
        additions="",
        question="",
        query=TableQuery(position_ids=["a"], scope="positions", metrics=["completed"]),
    )
    result = await q.reports.read_query(7, 7, plan)
    assert result.selection["position_ids"] == ["a"] and "20 м" in result.text


async def test_default_daily_list_excludes_zero_but_can_explicitly_include_it(query_system):
    q = query_system
    result = await q.reports.read_query(7, 7, command(metrics=["daily"]))
    assert result.selection["position_ids"] == ["a", "b"]
    result = await q.reports.read_query(7, 7, command(metrics=["daily"], include_zero=True))
    assert result.selection["position_ids"] == ["a", "b", "c"]


async def test_query_failure_keeps_previous_selection(query_system):
    q = query_system
    await q.reports.read_query(7, 7, command(ids=["a"], metrics=["completed"]))
    q.sheets.read_cells.side_effect = UserError("Google временно недоступен")
    with pytest.raises(UserError):
        await q.reports.read_query(7, 7, command(scope="cells", cells=["B5:D5"]))
    state = q.reports.store.selection(7, 7, q.reports.settings.google_spreadsheet_id, "Лист1")
    assert state["position_ids"] == ["a"]


async def test_text_fields_and_numeric_fields_can_be_returned_together(query_system):
    result = await query_system.reports.read_query(
        7, 7, command(ids=["a"], metrics=["name", "unit", "section", "remaining"])
    )
    assert "&lt;3х2,5&gt;" in result.text and "Единица: м" in result.text
    assert "Раздел: ЭМ3" in result.text and "80 м" in result.text
    assert result.selection["cells"] == ["B5", "C5", "G5"]


async def test_table_entries_question_includes_manual_entries_not_only_bot_operations(query_system):
    q = query_system
    q.reports.llm.command.return_value = VoiceCommand(
        action="query",
        edits=[],
        additions="",
        question="",
        query=TableQuery(scope="today_added", metrics=["daily"]),
    )
    plan, _ = await q.reports.plan_command(7, 7, "Что записано сегодня?", snapshot=q.snapshot)
    result = await q.reports.read_query(7, 7, plan, snapshot=q.snapshot)
    assert plan.query.scope == "all" and "5 м" in result.text and "2 м" in result.text
    assert not q.reports.store.user_operations(7, 7, q.reports.settings.google_spreadsheet_id)


async def test_explicit_bot_authorship_question_is_not_expanded_to_manual_entries(query_system):
    q = query_system
    q.reports.llm.command.return_value = VoiceCommand(
        action="query",
        edits=[],
        additions="",
        question="",
        query=TableQuery(scope="today_added", metrics=["daily"]),
    )
    plan, _ = await q.reports.plan_command(7, 7, "Что ты добавлял сегодня?", snapshot=q.snapshot)
    result = await q.reports.read_query(7, 7, plan, snapshot=q.snapshot)
    assert plan.query.scope == "today_added" and "Подходящих записей нет" in result.text
