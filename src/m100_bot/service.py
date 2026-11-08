import asyncio
import re
from datetime import date, datetime
from decimal import Decimal

from .access import BotAccess
from .bitrix import Bitrix, daily_lines
from .config import Settings
from .domain import Change, Draft, Line, Operation, UserError, number, validate_line
from .estimate import estimate_warnings
from .llm import LLM, VoiceCommand
from .queries import QueryAnswer, TableQuery, answer_query
from .reporting import report_context
from .sheets import Sheets, Snapshot, column_name, expand_cells
from .store import Store


class Reports:
    def __init__(self, settings: Settings, store: Store, sheets: Sheets, llm: LLM, bitrix: Bitrix):
        self.settings = settings
        self.store = store
        self.sheets = sheets
        self.llm = llm
        self.bitrix = bitrix
        self.access = BotAccess(settings, bitrix)
        self.bot_url = ""
        self.writer_ready = True
        self.lock = asyncio.Lock()
        # Share this lock between Telegram handlers and browser mutations.
        self.edit_lock = asyncio.Lock()

    async def require_access(self, user_id: int, *, write: bool = False):
        return await self.access.require(user_id, write=write)

    async def require_write(self, user_id: int) -> None:
        if self.settings.bitrix_access_enabled:
            principal = await self.require_access(user_id, write=True)
            self.store.save_profile(
                user_id, {"employee_name": principal.name, "role": principal.role}
            )

    def audit_identity(self, user_id: int) -> dict:
        profile = self.store.profile(user_id) or {}
        return {
            "created_at": datetime.now(self.settings.tz),
            "actor_name": profile.get("employee_name") or profile.get("name", ""),
            "actor_username": profile.get("username") or "",
        }

    def today(self):
        return datetime.now(self.settings.tz).date()

    def active_draft(self, user_id: int) -> Draft | None:
        draft = self.store.active_draft(user_id)
        # An old, unsubmitted draft cannot be confirmed after midnight. Keep its
        # contents in storage, but let the master start today's report immediately.
        if (
            draft
            and draft.day != self.today()
            and draft.status == "draft"
            and not self.store.operation_for(draft.id)
        ):
            draft.status = "cancelled"
            self.save_edit(draft)
            return None
        return draft

    async def create(self, user_id: int, chat_id: int, text: str | None = None) -> Draft:
        await self.require_write(user_id)
        old = self.active_draft(user_id)
        if old:
            raise UserError("У вас есть незавершённый отчёт. Откройте /draft или отмените /cancel.")
        day = self.today()
        snap = await self.sheets.snapshot(day)
        if text is None:
            lines = [
                Line(
                    phrase=p.name,
                    position_id=p.id,
                    quantity=snap.day_values[p.id],
                    unit=p.unit,
                    confirmed_position=True,
                )
                for p in snap.positions.values()
                if snap.day_values[p.id] != 0
            ]
            if not lines:
                raise UserError("За сегодня ещё нет записей. Отправьте текст нового отчёта.")
        else:
            lines = await self.llm.extract(text, snap.positions)
        draft = Draft(
            user_id=user_id,
            chat_id=chat_id,
            day=day,
            mode="replace" if text is None else "add",
            lines=lines,
            transcript=text or "",
            before=snap.day_values,
        )
        self.store.save_draft(draft)
        return draft

    def editable(self, draft: Draft) -> None:
        if draft.status != "draft" or self.store.operation_for(draft.id):
            raise UserError("Этот отчёт уже сохраняется или сохранён. Откройте /draft или /today.")
        if draft.day != self.today():
            raise UserError("Наступил новый день. Отмените старый черновик и создайте новый отчёт.")

    def save_edit(self, draft: Draft) -> None:
        draft.revision += 1
        self.store.save_draft(draft)

    def cancel(self, draft: Draft) -> None:
        if draft.status != "draft" or self.store.operation_for(draft.id):
            raise UserError("Сохранение уже началось. Завершите его через /draft.")
        draft.status = "cancelled"
        self.save_edit(draft)

    async def append(self, draft: Draft, text: str) -> None:
        await self.require_write(draft.user_id)
        self.editable(draft)
        snap = await self.sheets.snapshot(draft.day)
        lines = await self.llm.extract(text, snap.positions)
        if len(draft.lines) + len(lines) > 40:
            raise UserError("В одном черновике допускается до 40 строк.")
        draft.lines.extend(lines)
        draft.transcript = "\n".join(filter(None, [draft.transcript, text]))
        draft.action = ""
        self.save_edit(draft)

    async def plan_command(
        self, user_id: int, chat_id: int, text: str, *, snapshot: Snapshot | None = None
    ):
        # Planning a read must not expire or otherwise mutate an older draft.
        draft = self.store.active_draft(user_id)
        snap = snapshot or await self.sheets.snapshot(self.today())
        saved_lines = [
            Line(
                phrase=p.name,
                position_id=p.id,
                quantity=snap.day_values[p.id],
                unit=p.unit,
                confirmed_position=True,
            )
            for p in snap.positions.values()
            if snap.day_values.get(p.id, 0)
        ]
        if not draft:
            # A spoken edit after saving addresses today's recorded totals.
            # Do not persist anything until the entire command has been validated.
            draft = Draft(
                user_id=user_id,
                chat_id=chat_id,
                day=self.today(),
                mode="replace",
                before=snap.day_values,
                lines=[
                    Line(
                        phrase=p.name,
                        position_id=p.id,
                        quantity=snap.day_values[p.id],
                        unit=p.unit,
                        confirmed_position=True,
                    )
                    for p in snap.positions.values()
                    if snap.day_values.get(p.id, 0)
                ],
            )
        operations = self.store.user_operations(
            user_id, chat_id, self.settings.google_spreadsheet_id
        )
        recorded = [op for op in operations if op.sheets_done]
        context = {
            "today": self.today().isoformat(),
            "history": self.store.history(user_id, chat_id),
            "last_selection": self.store.selection(
                user_id,
                chat_id,
                self.settings.google_spreadsheet_id,
                self.settings.google_sheet_name,
            ),
            "draft": {
                "exists": self.store.active_draft(user_id) is not None,
                "day": draft.day.isoformat(),
                "auto_save": draft.auto_save,
                "status": draft.status,
                "save_color": draft.save_color,
            },
            "saved_lines": [
                {"line_number": i + 1, **line.model_dump(mode="json")}
                for i, line in enumerate(saved_lines)
            ],
            "recent_operations": [
                {
                    "id": op.id,
                    "day": op.day.isoformat(),
                    "kind": op.kind,
                    "color": op.highlight,
                    "cells": (op.cells or [c.cell for c in op.changes])[:40],
                    "cell_count": len(op.cells or op.changes),
                    "positions": [c.position_id for c in op.changes],
                    "changes": [
                        {
                            "id": c.position_id,
                            "name": c.name,
                            "before": str(c.before),
                            "after": str(c.after),
                        }
                        for c in op.changes
                        if c.before != c.after
                    ],
                }
                for op in recorded[:20]
            ],
            "pending_operations": [
                {
                    "id": op.id,
                    "kind": op.kind,
                    "day": op.day.isoformat(),
                    "sheets_done": op.sheets_done,
                    "bitrix_done": op.bitrix_done,
                    "color": op.highlight,
                    "cells": op.cells,
                }
                for op in operations
                if not (op.sheets_done and op.bitrix_done)
            ],
        }
        selected = context["last_selection"]
        if selected:
            context["last_selection"] = {
                **selected,
                "cells": selected.get("cells", [])[:40],
                "cell_count": len(selected.get("cells", [])),
            }
        command = await self.llm.command(text, snap.positions, draft.lines, context=context)
        if (
            command.action == "query"
            and command.query
            and command.query.scope == "today_added"
            and not re.search(
                r"\b(ты|тобой|тобою|бот(?:ом|а)?|добавлял|записывал|вносил|сохранял)\b",
                text.casefold(),
            )
        ):
            # A question about table entries includes manual writes unless authorship is requested.
            command.query.scope = "all"

        if command.action == "edit" and command.target_scope == "positions":
            if not command.position_ids or not set(command.position_ids) <= set(snap.positions):
                raise UserError("Какую записанную работу нужно изменить?")
            if not set(command.position_ids) <= {line.position_id for line in saved_lines}:
                if self.active_draft(user_id):
                    return command, draft
                raise UserError("Эта работа ещё не записана. Какой объём добавить?")
            draft = Draft(
                user_id=user_id,
                chat_id=chat_id,
                day=self.today(),
                mode="replace",
                before=snap.day_values,
                lines=saved_lines,
                edit_scope=command.position_ids,
                preserved_position_ids=[
                    key for key in snap.positions if key not in command.position_ids
                ],
            )
        return command, draft

    async def read_query(
        self, user_id: int, chat_id: int, command: VoiceCommand, *, snapshot: Snapshot | None = None
    ) -> QueryAnswer:
        snap = snapshot or await self.sheets.snapshot(self.today())
        selection = self.store.selection(
            user_id, chat_id, self.settings.google_spreadsheet_id, self.settings.google_sheet_name
        )
        answer = await answer_query(
            self.sheets,
            snap,
            command.query or TableQuery(),
            (
                command.query.position_ids
                if command.query and command.query.position_ids
                else command.position_ids
            ),
            (command.query.cells if command.query and command.query.cells else command.cells),
            self.today(),
            selection,
            self.store.user_operations(user_id, chat_id, self.settings.google_spreadsheet_id),
            self.settings.google_sheet_name,
        )
        if answer.selection:
            self.store.save_selection(
                user_id,
                chat_id,
                self.settings.google_spreadsheet_id,
                self.settings.google_sheet_name,
                answer.selection,
            )
        return answer

    def remember_operation_selection(self, op: Operation) -> None:
        if op.user_id is not None and op.chat_id is not None:
            self.store.save_selection(
                op.user_id,
                op.chat_id,
                self.settings.google_spreadsheet_id,
                self.settings.google_sheet_name,
                {
                    "kind": op.kind,
                    "position_ids": [c.position_id for c in op.changes],
                    "cells": op.cells or [c.cell for c in op.changes if c.cell],
                    "day": op.day.isoformat(),
                },
            )

    async def execute_command(self, draft: Draft, command: VoiceCommand, text: str) -> Draft:
        await self.require_write(draft.user_id)
        if command.action == "clarify":
            raise UserError(command.question or "Уточните работу и выполненный объём.")
        if command.action == "save_ready":
            if command.edits or command.additions.strip():
                raise UserError(
                    "Сначала уточните правки, затем попросите сохранить готовые данные."
                )
            if not self.active_draft(draft.user_id):
                raise UserError(
                    "Нет черновика для сохранения. Откройте /today или отправьте работы."
                )
            if draft.status == "draft":
                draft.auto_save = True
                if command.highlight is not None:
                    draft.save_color = command.highlight
                self.save_edit(draft)
            await self.confirm(
                draft,
                ready_only=True,
                line_numbers=command.line_numbers,
                highlight=command.highlight or draft.save_color,
            )
            return self.store.get_draft(draft.id, draft.user_id)
        if command.action == "table":
            return draft
        if command.action == "wait":
            active = self.active_draft(draft.user_id)
            if active:
                self.editable(active)
                active.auto_save = False
                self.save_edit(active)
                return active
            return draft
        active = self.active_draft(draft.user_id)
        if command.action == "add" and not active:
            draft = await self.create(draft.user_id, draft.chat_id, command.additions or text)
            return await self.maybe_save(draft, command)
        self.editable(draft)
        snap = await self.sheets.snapshot(draft.day)
        lines = [line.model_copy(deep=True) for line in draft.lines]
        if command.action == "rewrite":
            if not command.additions.strip():
                raise UserError("Расскажите, какие работы и объёмы записать заново.")
            lines = await self.llm.extract(command.additions, snap.positions)
            if not active:
                # Re-recording without a current draft starts a new report.
                draft.mode = "add"
        elif command.action == "add":
            lines.extend(await self.llm.extract(command.additions or text, snap.positions))
        else:
            if not command.edits and not command.additions.strip():
                raise UserError("Уточните, какую строку и как изменить.")
            seen = set()
            removed = set()
            for edit in command.edits:
                index = edit.line_number - 1
                if index < 0 or index >= len(lines) or index in seen:
                    raise UserError(
                        "Не удалось однозначно выбрать строку. Назовите работу и новый объём."
                    )
                seen.add(index)
                line = lines[index]
                if draft.edit_scope and line.position_id not in draft.edit_scope:
                    raise UserError("Правка выбрала другую работу. Уточните её название.")
                if edit.remove:
                    removed.add(index)
                    continue
                if edit.position_id is not None:
                    if edit.position_id not in snap.positions:
                        raise UserError("Позиция не найдена в спецификации. Уточните её название.")
                    line.position_id = edit.position_id
                    line.unit = snap.positions[edit.position_id].unit
                    line.phrase = snap.positions[edit.position_id].name
                    line.confirmed_position = False
                    # Overrides belonging to the previous position do not carry over.
                    line.details = type(line.details)()
                if edit.quantity is not None:
                    line.quantity = number(edit.quantity)
                line.question = ""
                line.deleted = False
                line = Line.model_validate(line.model_dump())
                lines[index] = validate_line(line, snap.positions)
            lines = [line for i, line in enumerate(lines) if i not in removed]
            if command.additions.strip():
                lines.extend(await self.llm.extract(command.additions, snap.positions))
        if draft.mode == "replace":
            kept = {line.position_id for line in lines}
            for key, qty in draft.before.items():
                if qty and key not in kept and key not in draft.preserved_position_ids:
                    p = snap.positions.get(key)
                    if not p:
                        raise UserError("Спецификация изменилась. Создайте отчёт заново.")
                    lines.append(
                        Line(
                            phrase=p.name,
                            position_id=key,
                            quantity=0,
                            unit=p.unit,
                            confirmed_position=True,
                            deleted=True,
                        )
                    )
        affected = set(draft.edit_scope) | {
            edit.position_id for edit in command.edits if edit.position_id is not None
        }
        draft.lines = (
            [line for line in lines if line.position_id in affected] if draft.edit_scope else lines
        )
        if len(draft.lines) > 40:
            raise UserError("В одном отчёте допускается до 40 строк.")
        draft.transcript = "\n".join(filter(None, [draft.transcript, text]))
        draft.action = ""
        self.save_edit(draft)
        return await self.maybe_save(draft, command)

    async def maybe_save(self, draft: Draft, command: VoiceCommand) -> Draft:
        if command.save_immediately is not None:
            draft.auto_save = command.save_immediately
        if command.highlight is not None:
            draft.save_color = command.highlight
        self.save_edit(draft)
        if draft.auto_save:
            snap = await self.sheets.snapshot(draft.day)
            try:
                self.ready_indexes(draft, snap, [])
            except UserError:
                return draft
            if estimate_warnings(draft, snap):
                # A new excess requires the master to see it before confirming.
                draft.auto_save = False
                self.save_edit(draft)
                return draft
            await self.confirm(draft, ready_only=True, highlight=draft.save_color)
            return self.store.get_draft(draft.id, draft.user_id)
        return draft

    async def format_cells(self, user_id: int, chat_id: int, command: VoiceCommand) -> Operation:
        await self.require_write(user_id)
        if not command.highlight:
            raise UserError("Каким цветом выделить ячейки?")
        try:
            day = date.fromisoformat(command.target_day) if command.target_day else self.today()
        except ValueError as exc:
            raise UserError("Укажите дату выделяемых данных в формате ДД.ММ.ГГГГ.") from exc
        async with self.lock:
            operations = self.store.user_operations(
                user_id, chat_id, self.settings.google_spreadsheet_id
            )
            selection = (
                self.store.selection(
                    user_id,
                    chat_id,
                    self.settings.google_spreadsheet_id,
                    self.settings.google_sheet_name,
                )
                if command.target_scope == "last_selection"
                else None
            )
            if selection and not command.target_day:
                day = date.fromisoformat(selection["day"])
            if not command.target_day and command.target_scope in {
                "last_selection",
                "last_changed",
            }:
                candidates = [
                    op
                    for op in operations
                    if op.sheets_done
                    and (command.target_scope == "last_selection" or op.kind == "report")
                ]
                if candidates and not selection:
                    day = candidates[0].day
            snap = await self.sheets.snapshot(day)
            completed = [op for op in operations if op.sheets_done and op.day == day]
            keys = command.position_ids
            addresses = command.cells
            if selection:
                keys = selection.get("position_ids", [])
                addresses = selection.get("cells", [])
            if not selection and command.target_scope in {
                "today_added",
                "last_changed",
                "last_selection",
            }:
                if command.target_scope == "last_selection":
                    sources = completed[:1]
                else:
                    reports = [op for op in completed if op.kind == "report"]
                    sources = reports if command.target_scope == "today_added" else reports[:1]
                if not sources:
                    raise UserError(
                        "За эту дату у меня нет сохранённых действий. Какие ячейки выделить?"
                    )
                keys = list(
                    dict.fromkeys(
                        c.position_id
                        for op in sources
                        for c in op.changes
                        if c.before != c.after or command.target_scope != "today_added"
                    )
                )
                addresses = [cell for op in sources for cell in op.cells]
                if command.fields and not keys and addresses:
                    selected_rows = {int(re.search(r"\d+$", cell).group()) for cell in addresses}
                    keys = [p.id for p in snap.positions.values() if p.row in selected_rows]
                if command.target_scope == "today_added" and not keys:
                    raise UserError("За эту дату я ещё не менял объёмы. Какие ячейки выделить?")
                if not addresses or command.fields:
                    addresses = []
                    if not keys:
                        addresses = [c.cell for op in sources for c in op.changes if c.cell]
            if command.target_scope == "cells" and not addresses:
                raise UserError("Какие ячейки выделить? Например J5:J8.")
            if command.fields:
                if not keys and addresses:
                    rows = {int(re.search(r"\d+$", cell).group()) for cell in addresses}
                    keys = [p.id for p in snap.positions.values() if p.row in rows]
                addresses = []
            if not addresses:
                if not keys or not set(keys) <= set(snap.positions):
                    raise UserError("Какую работу или диапазон выделить?")
                columns = {
                    "name": 1,
                    "unit": 2,
                    "planned": 3,
                    "unit_pay": 4,
                    "planned_pay": 5,
                    "remaining": 6,
                    "completed_pay": 7,
                    "total": 8,
                    "volume": snap.day_column,
                }
                fields = command.fields or ["volume"]
                if any(columns[field] is None for field in fields):
                    raise UserError(
                        "За эту дату ещё нет столбца объёмов. Какие существующие поля выделить?"
                    )
                addresses = [
                    f"{column_name(columns[field])}{snap.positions[key].row}"
                    for key in keys
                    for field in fields
                ]
            cells = expand_cells(addresses, snap.row_count, snap.column_count)
            pending = self.store.pending_operation()
            if pending:
                if (
                    pending.kind == "format"
                    and pending.user_id == user_id
                    and pending.chat_id == chat_id
                    and pending.cells == cells
                    and pending.highlight == command.highlight
                ):
                    op = pending
                else:
                    raise UserError("Предыдущее действие ещё не завершено. Скажите «повтори».")
            else:
                key = Draft(user_id=user_id, chat_id=chat_id, day=day).id
                op = Operation(
                    id=key,
                    draft_id=key,
                    day=day,
                    kind="format",
                    changes=[],
                    highlight=command.highlight,
                    cells=cells,
                    user_id=user_id,
                    chat_id=chat_id,
                    spreadsheet_id=self.settings.google_spreadsheet_id,
                    table_url=f"{self.sheets.url}#gid={snap.sheet_id}&range={cells[0]}",
                    sheet_name=self.settings.google_sheet_name,
                    bitrix_done=True,
                    bitrix_skipped=True,
                    **self.audit_identity(user_id),
                )
                self.store.save_operation(op)
            await self.sheets.apply(op)
            op.sheets_done = True
            self.store.save_operation(op)
            self.remember_operation_selection(op)
            return op

    async def retry(self, user_id: int, chat_id: int) -> Operation:
        await self.require_write(user_id)
        pending = self.store.pending_operation()
        if not pending:
            raise UserError("Незавершённых действий нет.")
        owned = self.store.user_operations(user_id, chat_id, self.settings.google_spreadsheet_id)
        if pending.id not in {op.id for op in owned}:
            raise UserError("Нет вашего незавершённого действия.")
        if pending.kind == "report":
            return await self.confirm(self.store.get_draft(pending.draft_id, user_id))
        async with self.lock:
            await self.sheets.apply(pending)
            pending.sheets_done = True
            self.store.save_operation(pending)
            self.remember_operation_selection(pending)
            return pending

    def changes(self, draft: Draft, snap: Snapshot) -> list[Change]:
        if not draft.lines:
            raise UserError("Черновик пуст. Добавьте хотя бы одну позицию.")
        quantities: dict[str, Decimal] = {}
        for raw_line in draft.lines:
            line = validate_line(raw_line, snap.positions)
            if not line.position_id or line.quantity is None or line.question:
                raise UserError("Сначала уточните все позиции и количества в черновике.")
            key = line.position_id
            quantities[key] = quantities.get(key, Decimal(0)) + line.quantity
        changes = []
        for key, qty in quantities.items():
            position = snap.positions[key]
            before = draft.before.get(key, Decimal(0))
            after = before + qty if draft.mode == "add" else qty
            changes.append(
                Change(
                    position_id=key,
                    name=position.name,
                    unit=position.unit,
                    before=before,
                    after=after,
                )
            )
        return changes

    def ready_indexes(self, draft: Draft, snap: Snapshot, line_numbers: list[int]) -> list[int]:
        if any(n < 1 or n > len(draft.lines) for n in line_numbers):
            raise UserError("Строка не найдена. Назовите номер из текущего отчёта.")
        lines = [validate_line(line, snap.positions) for line in draft.lines]
        # An unresolved quantity for a known position blocks that day's cell entirely.
        blocked = {
            line.position_id
            for line in lines
            if not line.position_id or line.quantity is None or line.question
        }
        selected = set(line_numbers) if line_numbers else set(range(1, len(lines) + 1))
        indexes = [
            i
            for i, line in enumerate(lines)
            if i + 1 in selected and line.position_id not in blocked
        ]
        # Several report lines can contribute to one cell. Leave the entire cell alone
        # if a requested subset does not include all of its contributions.
        omitted = {line.position_id for i, line in enumerate(lines) if i not in indexes}
        indexes = [i for i in indexes if lines[i].position_id not in omitted]
        if not indexes:
            raise UserError(
                "Нет готовых данных для сохранения. Требующие уточнения поля не изменены."
            )
        return indexes

    async def confirm(
        self,
        draft: Draft,
        *,
        ready_only: bool = False,
        line_numbers: list[int] | None = None,
        highlight: str | None = None,
    ) -> Operation:
        await self.require_write(draft.user_id)
        async with self.lock:
            # Reload, because another callback can have changed the draft while waiting.
            draft = self.store.get_draft(draft.id, draft.user_id)
            if draft.status == "cancelled":
                raise UserError("Отчёт отменён.")
            op = self.store.operation_for(draft.id)
            if op and draft.status == "done":
                return op
            if op is None:
                self.editable(draft)
                pending = self.store.pending_operation()
                if pending:
                    raise UserError(
                        "Сначала завершите предыдущую запись через /draft. "
                        "Одна из систем ещё не подтвердила сохранение."
                    )
                snap = await self.sheets.snapshot(draft.day)
                indexes = (
                    self.ready_indexes(draft, snap, line_numbers or [])
                    if ready_only
                    else list(range(len(draft.lines)))
                )
                selected = draft.model_copy(update={"lines": [draft.lines[i] for i in indexes]})
                changes = self.changes(selected, snap)
                for change in changes:
                    if snap.day_values[change.position_id] != change.before:
                        raise UserError(
                            "Объёмы изменились после создания черновика. "
                            "Отмените его и отправьте отчёт заново."
                        )
                col = snap.day_column if snap.day_column is not None else snap.next_column
                for change in changes:
                    change.cell = f"{column_name(col)}{snap.positions[change.position_id].row}"
                op = Operation(
                    id=draft.id,
                    draft_id=draft.id,
                    day=draft.day,
                    changes=changes,
                    highlight=highlight,
                    user_id=draft.user_id,
                    chat_id=draft.chat_id,
                    spreadsheet_id=self.settings.google_spreadsheet_id,
                    line_indexes=indexes,
                    continuation_id=Draft(
                        user_id=draft.user_id, chat_id=draft.chat_id, day=draft.day
                    ).id
                    if len(indexes) < len(draft.lines)
                    else None,
                    table_url=f"{self.sheets.url}#gid={snap.sheet_id}&range={changes[0].cell}",
                    sheet_name=self.settings.google_sheet_name,
                    **self.audit_identity(draft.user_id),
                )
                self.store.save_operation(op)
            draft.status = "saving"
            draft.action = ""
            self.store.save_draft(draft)
            if not op.sheets_done:
                await self.sheets.apply(op)
                op.sheets_done = True
                self.store.save_operation(op)
            if not op.bitrix_done:
                if self.settings.bitrix_enabled:
                    snapshot = await self.sheets.snapshot(op.day)
                    context = report_context(
                        op,
                        snapshot,
                        self.store.daily_operations(op.day, self.settings.google_spreadsheet_id),
                        self.settings.project_name,
                    )
                    await self.bitrix.apply(op, daily_lines(snapshot), context=context)
                else:
                    op.bitrix_skipped = True
                op.bitrix_done = True
                self.store.save_operation(op)
            if draft.status != "done":
                continuation = None
                if op.continuation_id:
                    indexes = set(op.line_indexes or [])
                    before = dict(draft.before)
                    before.update({change.position_id: change.after for change in op.changes})
                    continuation = Draft(
                        id=op.continuation_id,
                        user_id=draft.user_id,
                        chat_id=draft.chat_id,
                        day=draft.day,
                        mode=draft.mode,
                        before=before,
                        transcript=draft.transcript,
                        auto_save=draft.auto_save,
                        save_color=draft.save_color,
                        preserved_position_ids=list(
                            dict.fromkeys(
                                [
                                    *draft.preserved_position_ids,
                                    *(change.position_id for change in op.changes),
                                ]
                            )
                        ),
                        lines=[line for i, line in enumerate(draft.lines) if i not in indexes],
                    )
                    draft.lines = [line for i, line in enumerate(draft.lines) if i in indexes]
                draft.status = "done"
                self.store.finish_save(draft, continuation)
                self.remember_operation_selection(op)
            return op
