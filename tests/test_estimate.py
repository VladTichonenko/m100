from datetime import date
from decimal import Decimal
from types import SimpleNamespace

from m100_bot.domain import Draft, Line, Position
from m100_bot.estimate import estimate_warnings


def test_preview_groups_duplicate_positions_and_uses_cumulative_volume():
    p = Position(
        id="cable", row=5, name="Кабель", section="ЭМ", unit="м", planned=100, completed=90
    )
    draft = Draft(
        user_id=7,
        chat_id=7,
        day=date(2026, 11, 8),
        lines=[
            Line(phrase="кабель", position_id=p.id, unit="м", quantity=8),
            Line(phrase="ещё кабель", position_id=p.id, unit="м", quantity=7),
        ],
    )
    warning = estimate_warnings(draft, SimpleNamespace(positions={p.id: p}))
    assert len(warning) == 1
    assert warning[0]["excess"] == "5" and warning[0]["remaining"] == "10"
    draft.mode = "replace"
    draft.before = {p.id: Decimal(10)}
    assert estimate_warnings(draft, SimpleNamespace(positions={p.id: p})) == []
    draft.lines[1].deleted = True
    draft.mode = "add"
    assert estimate_warnings(draft, SimpleNamespace(positions={p.id: p})) == []
