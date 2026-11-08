import asyncio
import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from urllib.parse import quote

import google.auth
import httpx
from google.auth.transport.requests import Request
from google.oauth2.service_account import Credentials

from .config import Settings
from .domain import Operation, Position, UserError, number, rgb_color

JOURNAL = "Журнал бота"
SCOPE = "https://www.googleapis.com/auth/spreadsheets"


def column_name(index: int) -> str:
    """Zero-based column index to A1."""
    result = ""
    index += 1
    while index:
        index, rem = divmod(index - 1, 26)
        result = chr(65 + rem) + result
    return result


def expand_cells(ranges: list[str], rows: int, cols: int) -> list[str]:
    result: dict[str, None] = {}

    def coordinate(label: str, row: str) -> tuple[int, int]:
        col = 0
        for char in label:
            col = col * 26 + ord(char) - ord("A") + 1
        if not 1 <= int(row) <= rows or not 1 <= col <= cols:
            raise UserError("Диапазон выходит за границы рабочего листа.")
        return int(row), col

    for address in ranges:
        match = re.fullmatch(
            r"\$?([A-Z]{1,4})\$?([1-9][0-9]*)(?::\$?([A-Z]{1,4})\$?([1-9][0-9]*))?",
            address.strip().upper(),
        )
        if not match:
            raise UserError("Укажите ячейку или диапазон, например J5 или B5:J8.")
        first_col, first_row, last_col, last_row = match.groups()
        r1, c1 = coordinate(first_col, first_row)
        r2, c2 = coordinate(last_col or first_col, last_row or first_row)
        if r2 < r1 or c2 < c1 or (r2 - r1 + 1) * (c2 - c1 + 1) > 5000:
            raise UserError(
                "Укажите диапазон до 5000 ячеек в порядке от верхней левой к нижней правой."
            )
        for row in range(r1, r2 + 1):
            for col in range(c1, c2 + 1):
                result[f"{column_name(col - 1)}{row}"] = None
        if len(result) > 5000:
            raise UserError("За одну команду можно выделить до 5000 ячеек.")
    return list(result)


def background(color: str) -> dict:
    return {} if color == "clear" else {"backgroundColorStyle": {"rgbColor": rgb_color(color)}}


def header_date(value: object) -> date | None:
    if isinstance(value, (int, float)):
        return date(1899, 12, 30) + timedelta(days=int(value))
    if isinstance(value, str):
        for pattern in ("%Y-%m-%d", "%d.%m.%Y", "%d.%m.%y", "%m/%d/%Y"):
            try:
                return datetime.strptime(value, pattern).date()
            except ValueError:
                pass
    return None


@dataclass
class Snapshot:
    sheet_id: int
    row_count: int
    column_count: int
    positions: dict[str, Position]
    day_values: dict[str, Decimal]
    day_column: int | None
    next_column: int
    journal_id: int | None
    date_columns: list[tuple[date, int]] = field(default_factory=list)


class GoogleToken:
    def __init__(self, filename: str, inline_json: str = ""):
        self.filename = filename
        self.inline_json = inline_json
        self.credentials = None

    async def __call__(self) -> str:
        def load() -> str:
            if self.credentials is None:
                self.credentials = (
                    Credentials.from_service_account_info(
                        json.loads(self.inline_json), scopes=[SCOPE]
                    )
                    if self.inline_json
                    else Credentials.from_service_account_file(self.filename, scopes=[SCOPE])
                    if self.filename or self.inline_json
                    else google.auth.default(scopes=[SCOPE])[0]
                )
            if not self.credentials.valid:
                self.credentials.refresh(Request())
            return self.credentials.token

        try:
            return await asyncio.to_thread(load)
        except Exception as exc:
            raise UserError(
                "Не удалось авторизоваться в Google. Проверьте JSON-ключ и доступ."
            ) from exc


class Sheets:
    def __init__(
        self,
        settings: Settings,
        http: httpx.AsyncClient,
        token: Callable[[], Awaitable[str]] | None = None,
    ):
        self.settings = settings
        self.http = http
        self.token = token or GoogleToken(
            settings.google_credentials_file, settings.google_credentials_json.get_secret_value()
        )
        self.base = (
            f"https://sheets.googleapis.com/v4/spreadsheets/{settings.google_spreadsheet_id}"
        )

    @property
    def url(self) -> str:
        return f"https://docs.google.com/spreadsheets/d/{self.settings.google_spreadsheet_id}/edit"

    async def request(self, method: str, suffix: str = "", **kwargs) -> dict:
        # GETs may be retried. Writes are reconciled with the remote operation journal,
        # never blindly repeated after an ambiguous timeout.
        attempts = 3 if method == "GET" else 1
        for attempt in range(attempts):
            try:
                response = await self.http.request(
                    method,
                    self.base + suffix,
                    headers={"Authorization": f"Bearer {await self.token()}"},
                    **kwargs,
                )
                if response.status_code in {429, 500, 502, 503, 504} and attempt + 1 < attempts:
                    await asyncio.sleep(0.5 * 2**attempt)
                    continue
                if response.status_code == 403:
                    raise UserError(
                        "Google запретил доступ. Дайте сервисному аккаунту роль «Редактор»."
                    )
                response.raise_for_status()
                return response.json()
            except httpx.HTTPError as exc:
                if method == "GET" and attempt + 1 < attempts:
                    await asyncio.sleep(0.5 * 2**attempt)
                    continue
                raise UserError("Google Sheets временно недоступен. Попробуйте ещё раз.") from exc
        raise UserError("Google Sheets недоступен.")

    async def metadata(self) -> dict:
        return await self.request("GET", params={"fields": "spreadsheetId,sheets(properties)"})

    async def values(self, tab: str, cells: str) -> list[list]:
        address = f"'{tab.replace(chr(39), chr(39) * 2)}'!{cells}"
        data = await self.request(
            "GET",
            "/values/" + quote(address, safe=""),
            params={"valueRenderOption": "UNFORMATTED_VALUE"},
        )
        return data.get("values", [])

    async def batch_values(self, ranges: list[str], *, formatted: bool = False) -> list[list[list]]:
        tab = self.settings.google_sheet_name.replace("'", "''")
        data = await self.request(
            "GET",
            "/values:batchGet",
            params={
                "ranges": [f"'{tab}'!{address}" for address in ranges],
                "valueRenderOption": "FORMATTED_VALUE" if formatted else "UNFORMATTED_VALUE",
                "majorDimension": "ROWS",
            },
        )
        values = data.get("valueRanges", [])
        if len(values) != len(ranges):
            raise UserError("Google вернул неполные данные. Повторите запрос.")
        return [item.get("values", []) for item in values]

    async def period_values(
        self, start: date, end: date, snap: Snapshot
    ) -> tuple[dict, list[date]]:
        columns = [(day, col) for day, col in snap.date_columns if start <= day <= end]
        dates = [day for day, _ in columns]
        if len(dates) != len(set(dates)):
            raise UserError(
                "За выбранный период есть повторяющиеся даты. Исправьте заголовки таблицы."
            )
        if not columns:
            return {}, []
        ranges = [f"{column_name(col)}1:{column_name(col)}{snap.row_count}" for _, col in columns]
        batches = await self.batch_values(ranges)
        result = dict.fromkeys(snap.positions, Decimal(0))
        for rows in batches:
            for key, position in snap.positions.items():
                if position.row <= len(rows) and rows[position.row - 1]:
                    result[key] += number(rows[position.row - 1][0])
        return result, sorted(dates)

    async def read_cells(self, ranges: list[str], snap: Snapshot) -> list[tuple[str, str]]:
        if not ranges or len(ranges) > 50:
            raise UserError("Укажите от 1 до 50 диапазонов для чтения.")
        expand_cells(ranges, snap.row_count, snap.column_count)
        batches = await self.batch_values(ranges, formatted=True)
        result = {}
        for address, rows in zip(ranges, batches, strict=True):
            cells = expand_cells([address], snap.row_count, snap.column_count)
            first_row = re.search(r"\d+$", cells[0]).group()
            width = sum(re.search(r"\d+$", cell).group() == first_row for cell in cells)
            for index, cell in enumerate(cells):
                row, col = divmod(index, width)
                result[cell] = (
                    str(rows[row][col]) if row < len(rows) and col < len(rows[row]) else ""
                )
        return list(result.items())

    async def snapshot(self, day: date) -> Snapshot:
        meta = await self.metadata()
        props = [sheet["properties"] for sheet in meta["sheets"]]
        target = next((p for p in props if p["title"] == self.settings.google_sheet_name), None)
        if not target:
            raise UserError(f"Не найден лист «{self.settings.google_sheet_name}».")
        grid = target["gridProperties"]
        rows = grid["rowCount"]
        cols = grid["columnCount"]
        if rows > 5000 or cols > 5000:
            raise UserError(
                "Таблица слишком велика для этого прототипа (лимит 5000 строк/столбцов)."
            )
        tab = target["title"]
        raw, header_rows = await asyncio.gather(
            self.values(tab, f"A1:I{rows}"),
            self.values(tab, f"J3:{column_name(cols - 1)}3"),
        )
        headers = header_rows[0] if header_rows else []
        matching = [i + 9 for i, value in enumerate(headers) if header_date(value) == day]
        if len(matching) > 1:
            raise UserError("В таблице два столбца с сегодняшней датой. Устраните дубликат даты.")
        day_col = matching[0] if matching else None
        daily = (
            await self.values(tab, f"{column_name(day_col)}1:{column_name(day_col)}{rows}")
            if day_col is not None
            else []
        )
        positions = {}
        daily_values = {}
        section = ""
        for row_index, raw_row in enumerate(raw):
            row = list(raw_row) + [""] * (9 - len(raw_row))
            if row[1] in {"ОВ 3", "ЭМ3"}:
                section = row[1]
            if row_index < 4 or not row[0] or not row[1] or not row[2]:
                continue
            key = str(row[0]).strip()
            if key in positions:
                raise UserError(f"Повторяется ID позиции {key}. Исправьте столбец A.")
            positions[key] = Position(
                id=key,
                row=row_index + 1,
                name=str(row[1]),
                section=section,
                unit=str(row[2]),
                planned=number(row[3]),
                completed=number(row[8]),
                unit_pay=number(row[4]) if row[4] != "" else None,
                planned_pay=number(row[5]) if row[5] != "" else None,
                completed_pay=number(row[7]) if row[7] != "" else None,
            )
            daily_values[key] = (
                number(daily[row_index][0])
                if row_index < len(daily) and daily[row_index]
                else Decimal(0)
            )
        if not positions:
            raise UserError("Спецификация пуста. Нужны ID, наименование и единица в столбцах A:C.")
        journal = next((p["sheetId"] for p in props if p["title"] == JOURNAL), None)
        next_col = max([i + 9 for i, h in enumerate(headers) if h != ""] + [8]) + 1
        return Snapshot(
            target["sheetId"],
            rows,
            cols,
            positions,
            daily_values,
            day_col,
            next_col,
            journal,
            [(day, i + 9) for i, value in enumerate(headers) if (day := header_date(value))],
        )

    async def has_operation(self, op_id: str, journal_id: int | None) -> bool:
        if journal_id is None:
            return False
        rows = await self.values(JOURNAL, "A1:A50000")
        return any(row and row[0] == op_id for row in rows)

    async def apply(self, op: Operation) -> None:
        snap = await self.snapshot(op.day)
        if await self.has_operation(op.id, snap.journal_id):
            return
        requests = []
        if op.kind == "format":
            if not op.highlight or not op.cells:
                raise UserError("Не указан цвет или ячейки для выделения.")
            cells = expand_cells(op.cells, snap.row_count, snap.column_count)
            for address in cells:
                match = re.fullmatch(r"([A-Z]+)([0-9]+)", address)
                label, row = match.groups()
                col = 0
                for char in label:
                    col = col * 26 + ord(char) - ord("A") + 1
                fields = "userEnteredFormat.backgroundColorStyle"
                if op.highlight == "clear":
                    fields += ",userEnteredFormat.backgroundColor"
                requests.append(
                    {
                        "repeatCell": {
                            "range": {
                                "sheetId": snap.sheet_id,
                                "startRowIndex": int(row) - 1,
                                "endRowIndex": int(row),
                                "startColumnIndex": col - 1,
                                "endColumnIndex": col,
                            },
                            "cell": {"userEnteredFormat": background(op.highlight)},
                            "fields": fields,
                        }
                    }
                )
        else:
            col = snap.day_column if snap.day_column is not None else snap.next_column
            if col >= snap.column_count:
                requests.append(
                    {
                        "appendDimension": {
                            "sheetId": snap.sheet_id,
                            "dimension": "COLUMNS",
                            "length": col - snap.column_count + 1,
                        }
                    }
                )
            for change in op.changes:
                position = snap.positions.get(change.position_id)
                if not position or position.name != change.name or position.unit != change.unit:
                    raise UserError(
                        "Спецификация изменилась. Отмените черновик и создайте отчёт заново."
                    )
                if snap.day_values[change.position_id] != change.before:
                    raise UserError(
                        "Данные за день изменились после проверки. Создайте новый черновик."
                    )
                cell = {"userEnteredValue": {"numberValue": float(change.after)}}
                fields = "userEnteredValue"
                if op.highlight:
                    cell["userEnteredFormat"] = background(op.highlight)
                    fields += ",userEnteredFormat.backgroundColorStyle"
                    if op.highlight == "clear":
                        fields += ",userEnteredFormat.backgroundColor"
                requests.append(
                    {
                        "updateCells": {
                            "start": {
                                "sheetId": snap.sheet_id,
                                "rowIndex": position.row - 1,
                                "columnIndex": col,
                            },
                            "rows": [{"values": [cell]}],
                            "fields": fields,
                        }
                    }
                )
            if snap.day_column is None:
                serial = (op.day - date(1899, 12, 30)).days
                requests.extend(
                    [
                        {
                            "updateCells": {
                                "start": {
                                    "sheetId": snap.sheet_id,
                                    "rowIndex": 2,
                                    "columnIndex": col,
                                },
                                "rows": [
                                    {
                                        "values": [
                                            {
                                                "userEnteredValue": {"numberValue": serial},
                                                "userEnteredFormat": {
                                                    "numberFormat": {
                                                        "type": "DATE",
                                                        "pattern": "dd.mm.yyyy",
                                                    },
                                                    "backgroundColor": {
                                                        "red": 0.90,
                                                        "green": 0.96,
                                                        "blue": 0.92,
                                                    },
                                                    "textFormat": {"bold": True},
                                                },
                                            }
                                        ]
                                    },
                                    {"values": [{"userEnteredValue": {"stringValue": "объём"}}]},
                                ],
                                "fields": "userEnteredValue,userEnteredFormat",
                            }
                        },
                        {
                            "updateDimensionProperties": {
                                "range": {
                                    "sheetId": snap.sheet_id,
                                    "dimension": "COLUMNS",
                                    "startIndex": col,
                                    "endIndex": col + 1,
                                },
                                "properties": {"pixelSize": 105},
                                "fields": "pixelSize",
                            }
                        },
                    ]
                )
            # Extend the total range if a future date goes beyond the original ZZ boundary.
            if col >= 702:
                for position in snap.positions.values():
                    requests.append(
                        {
                            "updateCells": {
                                "start": {
                                    "sheetId": snap.sheet_id,
                                    "rowIndex": position.row - 1,
                                    "columnIndex": 8,
                                },
                                "rows": [
                                    {
                                        "values": [
                                            {
                                                "userEnteredValue": {
                                                    "formulaValue": (
                                                        f"=SUM(J{position.row}:"
                                                        f"{column_name(col)}{position.row})"
                                                    )
                                                }
                                            }
                                        ]
                                    }
                                ],
                                "fields": "userEnteredValue",
                            }
                        }
                    )
        journal_id = snap.journal_id
        if journal_id is None:
            # Positive ID outside typical source IDs; avoid all existing IDs.
            ids = {s["properties"]["sheetId"] for s in (await self.metadata())["sheets"]}
            journal_id = max(ids | {1000}) + 1
            requests.append(
                {
                    "addSheet": {
                        "properties": {
                            "sheetId": journal_id,
                            "title": JOURNAL,
                            "hidden": True,
                            "gridProperties": {"rowCount": 1000, "columnCount": 4},
                        }
                    }
                }
            )
            requests.append(
                {
                    "appendCells": {
                        "sheetId": journal_id,
                        "rows": [
                            {
                                "values": [
                                    {"userEnteredValue": {"stringValue": v}}
                                    for v in [
                                        "ID операции",
                                        "Дата отчёта",
                                        "Изменения",
                                        "Время записи UTC",
                                    ]
                                ]
                            }
                        ],
                        "fields": "userEnteredValue",
                    }
                }
            )
        requests.append(
            {
                "appendCells": {
                    "sheetId": journal_id,
                    "rows": [
                        {
                            "values": [
                                {"userEnteredValue": {"stringValue": v}}
                                for v in [
                                    op.id,
                                    op.day.isoformat(),
                                    op.model_dump_json(),
                                    datetime.now().astimezone().isoformat(),
                                ]
                            ]
                        }
                    ],
                    "fields": "userEnteredValue",
                }
            }
        )
        # All day cells AND operation receipt commit atomically in the same spreadsheet.
        await self.request("POST", ":batchUpdate", json={"requests": requests})
