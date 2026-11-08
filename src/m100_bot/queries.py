"""Read-only table queries. The model chooses a plan; Python reads and computes answers."""

import csv
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from html import escape
from io import StringIO
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .domain import Operation, Position, UserError, fmt
from .sheets import Sheets, Snapshot, column_name

Metric = Literal[
    "id",
    "name",
    "unit",
    "section",
    "daily",
    "completed",
    "remaining",
    "planned",
    "unit_pay",
    "planned_pay",
    "completed_pay",
    "day_pay",
    "progress",
]


class TableQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    metrics: list[Metric] = Field(default_factory=lambda: ["completed", "remaining"])
    position_ids: list[str] = Field(
        default_factory=list, description="Точные ID всех запрошенных работ из catalog"
    )
    cells: list[str] = Field(default_factory=list)
    include_zero: bool = False
    scope: Literal["all", "positions", "last_selection", "today_added", "last_changed", "cells"] = (
        "all"
    )
    aggregate: Literal["list", "sum", "count"] = "list"
    status: Literal["all", "recorded", "remaining", "completed", "not_started", "over_budget"] = (
        "all"
    )
    section: str | None = None
    date_from: str | None = None
    date_to: str | None = None
    sort_by: Metric | None = None
    descending: bool = True
    limit: int | None = Field(default=None, ge=1, le=5000)


@dataclass
class QueryAnswer:
    text: str
    url: str
    selection: dict
    document: bytes | None = None
    filename: str = "table-query.csv"


LABELS = {
    "id": "ID",
    "name": "Название",
    "unit": "Единица",
    "section": "Раздел",
    "daily": "За период",
    "completed": "Выполнено всего",
    "remaining": "Осталось",
    "planned": "По смете",
    "unit_pay": "Ставка",
    "planned_pay": "Оплата по смете",
    "completed_pay": "Оплата выполненного всего",
    "day_pay": "Оплата за период",
    "progress": "Готовность",
}
COLUMNS = {
    "id": "A",
    "name": "B",
    "unit": "C",
    "section": "B",
    "planned": "D",
    "completed": "I",
    "remaining": "G",
    "unit_pay": "E",
    "planned_pay": "F",
    "completed_pay": "H",
    "progress": "I",
}
MONEY = {"planned_pay", "completed_pay", "day_pay"}


def metric_value(position: Position, metric: str, daily: dict) -> Decimal | str | None:
    if metric == "daily":
        return daily.get(position.id, Decimal(0))
    if metric == "day_pay":
        return (
            daily.get(position.id, Decimal(0)) * position.unit_pay
            if position.unit_pay is not None
            else None
        )
    if metric == "progress":
        return position.completed / position.planned * 100 if position.planned else None
    return getattr(position, metric)


def display(value: Decimal | str | None, metric: str, unit: str) -> str:
    if value is None:
        return "нет данных"
    if isinstance(value, str):
        return escape(value)
    if metric == "progress":
        return f"{fmt(value.quantize(Decimal('0.1')))} %"
    suffix = "ден. ед." if metric in MONEY else f"ден. ед./{unit}" if metric == "unit_pay" else unit
    return f"{fmt(value)} {escape(suffix)}"


def csv_bytes(rows: list[list]) -> bytes:
    stream = StringIO()
    writer = csv.writer(stream, delimiter=";")
    for row in rows:
        safe = []
        for value in row:
            text = str(value)
            if text.startswith(("=", "+", "-", "@", "\t", "\r")) and not re.fullmatch(
                r"-?\d+(?:[.,]\d+)?", text
            ):
                text = "'" + text
            safe.append(text)
        writer.writerow(safe)
    return stream.getvalue().encode("utf-8-sig")


def selected_positions(
    spec: TableQuery,
    ids: list[str],
    snap: Snapshot,
    selection: dict | None,
    operations: list[Operation],
    start: date,
    end: date,
) -> list[Position]:
    keys = list(ids)
    if spec.scope == "last_selection":
        if selection:
            keys = selection.get("position_ids", [])
            if not keys and selection.get("cells"):
                rows = {int(re.search(r"\d+$", cell).group()) for cell in selection["cells"]}
                keys = [p.id for p in snap.positions.values() if p.row in rows]
        else:
            previous = next((op for op in operations if op.sheets_done), None)
            keys = [c.position_id for c in previous.changes] if previous else []
        if not keys:
            raise UserError("Какие работы вы имеете в виду?")
    elif spec.scope in {"today_added", "last_changed"}:
        sources = [
            op
            for op in operations
            if op.sheets_done and op.kind == "report" and start <= op.day <= end
        ]
        if spec.scope == "last_changed":
            sources = sources[:1]
        keys = list(
            dict.fromkeys(
                c.position_id for op in sources for c in op.changes if c.before != c.after
            )
        )
        if not keys:
            return []
    elif spec.scope == "positions" and not keys:
        raise UserError("По какой работе показать данные?")
    if keys and not set(keys) <= set(snap.positions):
        raise UserError("Некоторые работы больше не найдены в таблице. Уточните название или ID.")
    positions = (
        [snap.positions[key] for key in dict.fromkeys(keys)]
        if keys
        else list(snap.positions.values())
    )
    if spec.section:

        def normalize(text):
            return re.sub(r"\s+", "", text).casefold().replace("ё", "е")

        positions = [p for p in positions if normalize(p.section) == normalize(spec.section)]
    return positions


async def answer_query(
    sheets: Sheets,
    snap: Snapshot,
    spec: TableQuery,
    ids: list[str],
    cells: list[str],
    today: date,
    selection: dict | None,
    operations: list[Operation],
    sheet_name: str,
) -> QueryAnswer:
    metrics = list(dict.fromkeys(spec.metrics))
    if not metrics and spec.aggregate != "count":
        raise UserError("Какие данные показать: выполнено, остаток, смету или оплату?")
    try:
        start = date.fromisoformat(spec.date_from) if spec.date_from else today
        end = date.fromisoformat(spec.date_to) if spec.date_to else start
    except ValueError as exc:
        raise UserError("За какую дату или период показать данные?") from exc
    if end < start or (end - start).days > 366:
        raise UserError("Укажите период по порядку, продолжительностью до года.")
    if spec.scope == "last_selection" and not (spec.date_from or spec.date_to) and selection:
        start = date.fromisoformat(
            selection.get("date_from", selection.get("day", today.isoformat()))
        )
        end = date.fromisoformat(selection.get("date_to", start.isoformat()))
    if spec.scope == "last_changed" and not (spec.date_from or spec.date_to):
        previous = next((op for op in operations if op.sheets_done and op.kind == "report"), None)
        if previous:
            start = end = previous.day
    prefix = f"Лист «{escape(sheet_name)}»."
    url = f"{sheets.url}#gid={snap.sheet_id}"
    if spec.scope == "cells":
        values = await sheets.read_cells(cells, snap)
        lines = [
            f"<b>{address}</b>: {escape(value[:160]) if value else 'пусто'}"
            for address, value in values
        ]
        text = prefix + "\n" + "\n".join(lines[:12])
        document = (
            csv_bytes([["Ячейка", "Значение"], *values])
            if len(values) > 12 or any(len(v) > 160 for _, v in values)
            else None
        )
        if document:
            text = prefix + f" Ячеек: {len(values)}. Все значения — в файле."
        cell_rows = {int(re.search(r"\d+$", cell).group()) for cell, _ in values}
        selected = {
            "kind": "query",
            "cells": [a for a, _ in values],
            "position_ids": [p.id for p in snap.positions.values() if p.row in cell_rows],
            "day": start.isoformat(),
            "date_from": start.isoformat(),
            "date_to": end.isoformat(),
        }
        return QueryAnswer(text, url, selected, document)
    positions = selected_positions(spec, ids, snap, selection, operations, start, end)
    needs_daily = (
        any(m in {"daily", "day_pay"} for m in metrics)
        or spec.status == "recorded"
        or spec.sort_by in {"daily", "day_pay"}
    )
    daily = {}
    dates = []
    if needs_daily:
        if start == end == today:
            daily = snap.day_values
            dates = [today] if snap.day_column is not None else []
        else:
            daily, dates = await sheets.period_values(start, end, snap)
        period = f"{start:%d.%m.%Y}" if start == end else f"{start:%d.%m.%Y}–{end:%d.%m.%Y}"
        prefix += f" Период: {period}."
        if not dates:
            return QueryAnswer(
                prefix + " В таблице нет столбцов за этот период.", url, selection or {}
            )
        if start != end and len(dates) < (end - start).days + 1:
            prefix += f" Данные есть за {len(dates)} дней периода."
    predicates = {
        "all": lambda p: True,
        "recorded": lambda p: daily.get(p.id, 0) != 0,
        "remaining": lambda p: p.remaining > 0,
        "completed": lambda p: p.completed >= p.planned and p.planned > 0,
        "not_started": lambda p: p.completed == 0,
        "over_budget": lambda p: p.remaining < 0,
    }
    status = spec.status
    if (
        status == "all"
        and needs_daily
        and spec.scope == "all"
        and not ids
        and not spec.include_zero
    ):
        status = "recorded"
    positions = [p for p in positions if predicates[status](p)]
    if spec.sort_by:
        known = [p for p in positions if metric_value(p, spec.sort_by, daily) is not None]
        unknown = [p for p in positions if metric_value(p, spec.sort_by, daily) is None]
        positions = (
            sorted(
                known, key=lambda p: metric_value(p, spec.sort_by, daily), reverse=spec.descending
            )
            + unknown
        )
    if spec.limit:
        positions = positions[: spec.limit]
    selected_cells = []
    daily_columns = [col for day, col in snap.date_columns if start <= day <= end]
    if start == end == today and snap.day_column is not None:
        daily_columns = [snap.day_column]
    for p in positions:
        for metric in metrics:
            columns = (
                [column_name(col) for col in daily_columns]
                if metric in {"daily", "day_pay"}
                else [COLUMNS[metric]]
            )
            selected_cells.extend(f"{col}{p.row}" for col in columns)
    selected = {
        "kind": "query",
        "position_ids": [p.id for p in positions],
        "cells": list(dict.fromkeys(selected_cells)),
        "day": start.isoformat(),
        "date_from": start.isoformat(),
        "date_to": end.isoformat(),
        "metrics": metrics,
    }
    if not positions:
        return QueryAnswer(prefix + " Подходящих записей нет.", url, selected)
    if spec.aggregate == "count":
        return QueryAnswer(prefix + f" Подходящих позиций: {len(positions)}.", url, selected)
    if spec.aggregate == "sum":
        if any(m in {"id", "name", "unit", "section", "unit_pay", "progress"} for m in metrics):
            raise UserError("Эти поля покажу по отдельным работам, без суммирования.")
        lines = []
        for metric in metrics:
            groups: dict[str, list] = {}
            for p in positions:
                unit = "ден. ед." if metric in MONEY else p.unit
                groups.setdefault(unit, []).append(metric_value(p, metric, daily))
            for unit, values in groups.items():
                known = [v for v in values if v is not None]
                missing = len(values) - len(known)
                value = fmt(sum(known, Decimal(0))) + " " + escape(unit) if known else "нет данных"
                if missing and known:
                    value += f" (неполная сумма: нет данных по {missing} позициям)"
                lines.append(f"{LABELS[metric]}: <b>{value}</b>")
        return QueryAnswer(
            prefix + f" Позиций: {len(positions)}.\n" + "\n".join(lines), url, selected
        )
    rows = [["ID", "Работа", "Ед.", *[LABELS[m] for m in metrics]]]
    lines = []
    for p in positions:
        values = [metric_value(p, metric, daily) for metric in metrics]
        rows.append(
            [
                p.id,
                p.name,
                p.unit,
                *[
                    (fmt(v).replace(".", ",") if isinstance(v, Decimal) else v)
                    if v is not None
                    else "нет данных"
                    for v in values
                ],
            ]
        )
        detail = "; ".join(
            f"{LABELS[m]}: {display(v, m, p.unit)}" for m, v in zip(metrics, values, strict=True)
        )
        lines.append(f"<b>{escape(p.id)} · {escape(p.name[:140])}</b>\n{detail}")
    text = prefix + "\n\n" + "\n\n".join(lines)
    if len(text) > 3400 or len(positions) > 12:
        caption = prefix + f" Найдено позиций: {len(positions)}. Все значения — в файле."
        return QueryAnswer(caption, url, selected, csv_bytes(rows))
    if selected_cells:
        url += "&range=" + selected_cells[0]
    return QueryAnswer(text, url, selected)
