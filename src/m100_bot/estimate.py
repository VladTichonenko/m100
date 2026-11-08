"""Read-only estimate preview shared by Telegram and the report API."""

from decimal import Decimal

from .domain import Draft, fmt, validate_line


def estimate_warnings(draft: Draft, snapshot) -> list[dict]:
    if draft.status in {"done", "cancelled"}:
        return []
    totals = {}
    for raw in draft.lines:
        line = validate_line(raw, snapshot.positions)
        if line.deleted or not line.position_id or line.quantity is None or line.question:
            continue
        totals[line.position_id] = totals.get(line.position_id, Decimal(0)) + line.quantity
    warnings = []
    for key, qty in totals.items():
        position = snapshot.positions[key]
        delta = qty - (draft.before.get(key, Decimal(0)) if draft.mode == "replace" else 0)
        projected = position.completed + delta
        # Correcting down an already over-budget total doesn't introduce a new excess.
        if delta <= 0 or projected <= position.planned:
            continue
        warnings.append(
            {
                "position_id": key,
                "name": position.name,
                "unit": position.unit,
                "remaining": str(max(position.remaining, Decimal(0))),
                "quantity": str(qty),
                "excess": str(projected - position.planned),
            }
        )
    return warnings


def warning_text(warnings: list[dict]) -> str:
    return "\n".join(
        f"⚠ {item['name']}: осталось по смете {fmt(Decimal(item['remaining']))} {item['unit']}; "
        f"превышение после записи — {fmt(Decimal(item['excess']))} {item['unit']}."
        for item in warnings
    )
