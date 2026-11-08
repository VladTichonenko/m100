from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace

from m100_bot.domain import Change, Operation, Position
from m100_bot.reporting import report_context


def test_estimate_controls_separate_excess_and_finishing_from_warehouse_stock():
    positions = {
        key: Position(id=key, row=i, name=key, section="ЭМ", unit="м", planned=100, completed=done)
        for i, (key, done) in enumerate(
            [("excess", 120), ("near", 95), ("done", 100), ("normal", 10)]
        )
    }
    snap = SimpleNamespace(positions=positions, day_values={k: Decimal(1) for k in positions})
    op = Operation(id="op", draft_id="d", day=date(2026, 11, 8), changes=[], sheets_done=True)
    result = report_context(op, snap, [], "Объект")
    assert result["excess_count"] == 1 and result["finishing_count"] == 1
    assert result["positions"] == 4 and "остатки материалов" in result["warnings"]
    assert "превышение 20 м" in result["warnings"]


def test_history_keeps_each_author_and_exact_changes_without_duplicate_on_retry():
    snap = SimpleNamespace(positions={}, day_values={})
    first = Operation(
        id="op1",
        draft_id="d1",
        day=date(2026, 11, 8),
        spreadsheet_id="table",
        actor_name="Иван",
        created_at=datetime(2026, 11, 8, 9, tzinfo=UTC),
        sheets_done=True,
        changes=[Change(position_id="A", name="Кабель", unit="м", before=0, after=15)],
    )
    second = first.model_copy(
        update={
            "id": "op2",
            "actor_name": "Пётр",
            "changes": [Change(position_id="A", name="Кабель", unit="м", before=15, after=20)],
        }
    )
    other = first.model_copy(update={"id": "other", "spreadsheet_id": "other"})
    result = report_context(second, snap, [first, second, other], "Объект")
    assert "Иван" in result["history"] and "Пётр" in result["history"]
    assert "0 → 15 м" in result["history"] and "15 → 20 м" in result["history"]
    assert result["history"].count("операция op2") == 1
    assert "операция other" not in result["history"]
