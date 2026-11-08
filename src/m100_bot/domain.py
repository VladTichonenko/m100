from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Annotated, Literal
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator

Color = Annotated[str, Field(pattern=r"^(#[0-9a-fA-F]{6}|yellow|clear)$")]


def rgb_color(color: str) -> dict[str, float]:
    color = "#FFFF00" if color == "yellow" else color
    if len(color) != 7 or color[0] != "#":
        raise UserError("Укажите цвет названием или в формате #RRGGBB.")
    try:
        return {
            key: int(color[offset : offset + 2], 16) / 255
            for key, offset in (("red", 1), ("green", 3), ("blue", 5))
        }
    except ValueError as exc:
        raise UserError("Не удалось определить цвет. Укажите название или #RRGGBB.") from exc


class UserError(Exception):
    """An actionable, secret-free message safe to show to the master."""


def number(value: object) -> Decimal:
    if value is None or value == "":
        return Decimal(0)
    try:
        result = Decimal(str(value).replace(",", ".").replace(" ", ""))
    except InvalidOperation as exc:
        raise UserError("В таблице найдено некорректное число. Проверьте объёмы.") from exc
    if not result.is_finite():
        raise UserError("Объём должен быть конечным числом.")
    return result


def unit(value: str) -> str:
    value = value.lower().strip().rstrip(".")
    return {
        "метр": "м",
        "метров": "м",
        "метра": "м",
        "m": "м",
        "штук": "шт",
        "штуки": "шт",
        "штука": "шт",
        "комплект": "компл",
        "система": "сист",
        "килограмм": "кг",
    }.get(value, value)


def fmt(value: Decimal) -> str:
    return format(value, "f").rstrip("0").rstrip(".") if "." in format(value, "f") else str(value)


class Position(BaseModel):
    id: str
    row: int
    name: str
    section: str
    unit: str
    planned: Decimal
    completed: Decimal
    unit_pay: Decimal | None = None
    planned_pay: Decimal | None = None
    completed_pay: Decimal | None = None

    @property
    def remaining(self) -> Decimal:
        return self.planned - self.completed


class CardDetails(BaseModel):
    """Edits to this report card; the shared source specification stays intact."""

    name: str | None = Field(default=None, min_length=1, max_length=600)
    unit: str | None = Field(default=None, min_length=1, max_length=20)
    planned: Decimal | None = Field(default=None, ge=0)
    unit_pay: Decimal | None = Field(default=None, ge=0)
    planned_pay: Decimal | None = Field(default=None, ge=0)
    remaining: Decimal | None = None
    completed_pay: Decimal | None = Field(default=None, ge=0)


class Line(BaseModel):
    phrase: str = Field(max_length=600)
    position_id: str | None = None
    quantity: Decimal | None = None
    unit: str | None = None
    candidates: list[str] = Field(default_factory=list, max_length=8)
    question: str = ""
    confirmed_position: bool = False
    details: CardDetails = Field(default_factory=CardDetails)
    deleted: bool = False

    @field_validator("quantity")
    @classmethod
    def valid_quantity(cls, value: Decimal | None) -> Decimal | None:
        if value is not None and (not value.is_finite() or value < 0):
            raise ValueError("Количество должно быть конечным и неотрицательным")
        return value


class Draft(BaseModel):
    id: str = Field(default_factory=lambda: uuid4().hex[:12])
    user_id: int
    chat_id: int
    day: date
    mode: Literal["add", "replace"] = "add"
    lines: list[Line] = Field(default_factory=list)
    transcript: str = ""
    # Snapshot shown to the user; confirmation must not silently rebase on newer values.
    before: dict[str, Decimal] = Field(default_factory=dict)
    # This account's contribution before editing; before still holds shared sheet totals.
    account_before: dict[str, Decimal] | None = None
    # Already saved by an earlier partial operation; absence is not a deletion.
    preserved_position_ids: list[str] = Field(default_factory=list)
    action: str = ""
    action_index: int = -1
    revision: int = 0
    status: Literal["draft", "saving", "done", "cancelled"] = "draft"
    saved_notified: bool = False
    auto_save: bool = False
    save_color: Color | None = None
    edit_scope: list[str] = Field(default_factory=list)


class Change(BaseModel):
    position_id: str
    name: str
    unit: str
    before: Decimal
    after: Decimal
    cell: str = ""


class Operation(BaseModel):
    id: str
    draft_id: str
    day: date
    changes: list[Change]
    highlight: Color | None = None
    kind: Literal["report", "format"] = "report"
    user_id: int | None = None
    chat_id: int | None = None
    spreadsheet_id: str = ""
    cells: list[str] = Field(default_factory=list)
    line_indexes: list[int] | None = None
    continuation_id: str | None = None
    table_url: str = ""
    sheet_name: str = ""
    sheets_done: bool = False
    bitrix_done: bool = False
    bitrix_skipped: bool = False
    created_at: datetime | None = None
    actor_name: str = ""
    actor_username: str = ""


def validate_line(line: Line, catalog: dict[str, Position]) -> Line:
    line = line.model_copy(deep=True)
    line.candidates = [key for key in dict.fromkeys(line.candidates) if key in catalog]
    if line.position_id not in catalog:
        line.position_id = None
        line.question = line.question or "Выберите позицию из спецификации."
    if line.position_id:
        position = catalog[line.position_id]
        duplicates = [
            p.id
            for p in catalog.values()
            if p.name.strip() == position.name.strip()
            and p.unit == position.unit
            and p.section == position.section
        ]
        if len(duplicates) > 1 and not line.confirmed_position:
            line.position_id = None
            line.candidates = duplicates[:8]
            line.question = "В спецификации несколько одинаковых названий. Выберите нужный ID."
            return line
        if line.unit and unit(line.unit) != unit(position.unit):
            line.question = f"Единица в спецификации — {position.unit}. Уточните количество."
            line.quantity = None
        if unit(position.unit) in {"шт", "компл", "сист"} and (
            line.quantity is not None and line.quantity % 1
        ):
            line.question = "Для этой позиции нужно целое количество."
            line.quantity = None
        line.unit = position.unit
    return line
