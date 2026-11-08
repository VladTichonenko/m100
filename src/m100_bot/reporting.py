"""Human-readable audit and estimate control, without inventing warehouse stock."""

from .domain import Operation, fmt


def report_context(op: Operation, snapshot, operations: list[Operation], project: str) -> dict:
    excess = []
    finishing = []
    for p in snapshot.positions.values():
        if p.remaining < 0:
            excess.append(
                f"{p.id} · {p.name}: выполнено {fmt(p.completed)} {p.unit}, "
                f"смета {fmt(p.planned)} {p.unit}; превышение {fmt(-p.remaining)} {p.unit}."
            )
        elif p.planned > 0 and 0 < p.remaining <= p.planned / 10:
            finishing.append(
                f"{p.id} · {p.name}: осталось работ {fmt(p.remaining)} {p.unit} "
                f"из {fmt(p.planned)} {p.unit}."
            )
    warnings = "ПРЕВЫШЕНИЯ СМЕТЫ\n" + ("\n".join(excess) or "Нет превышений.")
    warnings += "\n\nБЛИЗКО К ЗАВЕРШЕНИЮ\n" + (
        "\n".join(finishing) or "Нет позиций с остатком до 10%."
    )
    warnings += "\n\nЭто контроль объёма работ, а не остатки материалов на складе."
    audit = []
    seen = set()
    for item in [*operations, op]:
        if item.id in seen or item.day != op.day or item.kind != "report" or not item.sheets_done:
            continue
        if item.spreadsheet_id and item.spreadsheet_id != op.spreadsheet_id:
            continue
        seen.add(item.id)
        when = (
            item.created_at.strftime("%d.%m.%Y %H:%M:%S")
            if item.created_at
            else "Время не сохранено"
        )
        author = item.actor_name or (f"Telegram ID {item.user_id}" if item.user_id else "Тест API")
        audit.append(f"{when} · {author} · операция {item.id}")
        audit.extend(
            f"  {c.position_id} · {c.name}: {fmt(c.before)} → {fmt(c.after)} {c.unit}"
            for c in item.changes
        )
    return {
        "day": op.day.isoformat(),
        "project": project,
        "author": op.actor_name or (f"Telegram ID {op.user_id}" if op.user_id else "Тест API"),
        "telegram_id": str(op.user_id or ""),
        "saved_at": op.created_at.isoformat() if op.created_at else None,
        "positions": sum(value != 0 for value in snapshot.day_values.values()),
        "excess_count": len(excess),
        "finishing_count": len(finishing),
        "warnings": warnings,
        "history": "\n".join(audit),
        "sheet_url": op.table_url,
        "operation": op.id,
    }
