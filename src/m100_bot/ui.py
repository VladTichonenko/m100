import asyncio
import logging
import webbrowser
from html import escape
from urllib.parse import urlparse

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions, WebAppInfo

from .domain import Draft, Operation, fmt
from .estimate import estimate_warnings, warning_text
from .service import Reports


def button(draft: Draft, label: str, action: str, index: int = -1) -> InlineKeyboardButton:
    return InlineKeyboardButton(
        text=label, callback_data=f"d:{draft.id}:{draft.revision}:{action}:{index}"
    )


def keyboard(rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=rows)


def table_button(reports: Reports) -> InlineKeyboardButton:
    return InlineKeyboardButton(text="Открыть таблицу", url=reports.sheets.url)


def report_button(draft: Draft, reports: Reports) -> InlineKeyboardButton:
    token = reports.store.web_token(draft)
    url = f"{reports.settings.web_base_url.rstrip('/')}/report/{draft.id}#token={token}"
    local = urlparse(url).hostname in {"localhost", "127.0.0.1", "::1"}
    url = url.replace("://localhost", "://127.0.0.1", 1) if local else url
    if not local and urlparse(url).scheme == "https":
        return InlineKeyboardButton(text="Открыть отчёт", web_app=WebAppInfo(url=url))
    return InlineKeyboardButton(text="Открыть отчёт", url=url)


def work_keyboard(
    reports: Reports,
    user_id: int | None,
    *,
    draft: Draft | None = None,
    markup: InlineKeyboardMarkup | None = None,
) -> InlineKeyboardMarkup:
    """Keep report and table access on work replies without losing action buttons."""
    rows = [list(row) for row in markup.inline_keyboard] if markup else []
    labels = {item.text for row in rows for item in row}
    if "Открыть отчёт" not in labels:
        if draft is None and user_id is not None:
            draft = reports.store.active_draft(user_id) or reports.store.latest_saved_report(
                user_id, reports.today()
            )
        if draft and reports.settings.web_enabled:
            opener = report_button(draft, reports)
        elif draft:
            opener = InlineKeyboardButton(
                text="Открыть отчёт", callback_data=f"nav:draft:{draft.id}"
            )
        else:
            opener = InlineKeyboardButton(text="Открыть отчёт", callback_data="nav:report")
        rows.append([opener])
    if "Открыть таблицу" not in labels:
        rows.append([table_button(reports)])
    return keyboard(rows)


def issue_text(draft: Draft, limit: int = 2) -> str:
    issues = [
        line
        for line in draft.lines
        if not line.deleted and (not line.position_id or line.quantity is None or line.question)
    ]
    texts = []
    for line in issues[:limit]:
        question = line.question or (
            "Уточните позицию." if not line.position_id else "Какой выполненный объём?"
        )
        texts.append(f"{escape(line.phrase[:120])}: {escape(question[:450])}")
    if len(issues) > limit:
        texts.append(f"Ещё требуют уточнения: {len(issues) - limit}. Подробности в отчёте.")
    return "\n".join(texts)


def color_text(color: str | None) -> str:
    if not color:
        return ""
    if color == "clear":
        return " Заливка снята."
    names = {
        "yellow": "жёлтым",
        "#FFFF00": "жёлтым",
        "#0000FF": "синим",
        "#008000": "зелёным",
        "#00FF00": "ярко-зелёным",
        "#FF0000": "красным",
        "#FFA500": "оранжевым",
        "#800080": "фиолетовым",
        "#90EE90": "светло-зелёным",
    }
    return f" Выделено {names.get(color.upper(), names.get(color, 'цветом ' + color))}."


async def show_saved(message, op: Operation, reports: Reports) -> None:
    owner = op.user_id if op.user_id is not None else reports.store.draft_owner(op.draft_id)
    draft = (
        reports.store.get_draft(op.draft_id, owner)
        if owner is not None and op.kind == "report"
        else None
    )
    if op.kind == "format":
        text = (
            f"Снял заливку с {len(op.cells)} ячеек."
            if op.highlight == "clear"
            else f"Выделил {len(op.cells)} ячеек.{color_text(op.highlight)}"
        )
        text += f" Лист «{escape(op.sheet_name)}», {op.day:%d.%m.%Y}."
        shown = op.cells[:8]
        text += (
            "\n" + ", ".join(shown) + (f" и ещё {len(op.cells) - 8}." if len(op.cells) > 8 else ".")
        )
    else:
        text = (
            f"Сохранил данные за {op.day:%d.%m.%Y} (позиций: {len(op.changes)})."
            f"{color_text(op.highlight)} Лист «{escape(op.sheet_name)}»."
        )
        for change in op.changes[:3]:
            text += (
                f"\n{escape(change.cell)} · {escape(change.name[:100])}: "
                f"{fmt(change.before)} → {fmt(change.after)} {escape(change.unit)}"
            )
        if len(op.changes) > 3:
            text += f"\nИ ещё {len(op.changes) - 3} позиций — в таблице."
    rows = [
        [
            InlineKeyboardButton(
                text="Открыть изменённые ячейки", url=op.table_url or reports.sheets.url
            )
        ]
    ]
    if op.continuation_id and owner is not None:
        draft = reports.store.get_draft(op.continuation_id, owner)
        text += "\n\nОстальное оставил в черновике.\n" + issue_text(draft)
    await message.answer(
        text,
        reply_markup=work_keyboard(reports, owner, draft=draft, markup=keyboard(rows)),
        link_preview_options=LinkPreviewOptions(is_disabled=True),
    )
    if op.kind == "report":
        owner = reports.store.draft_owner(op.draft_id)
        if owner is not None:
            saved = reports.store.get_draft(op.draft_id, owner)
            saved.saved_notified = True
            reports.store.save_draft(saved)


async def show_draft(message, draft: Draft, reports: Reports, *, notice: str = "") -> None:
    issues = issue_text(draft)
    ready = sum(
        not line.deleted
        and line.position_id is not None
        and line.quantity is not None
        and not line.question
        for line in draft.lines
    )
    if draft.status == "done":
        text = (
            f"✅ <b>Отчёт за {draft.day:%d.%m.%Y} сохранён.</b>\nОткройте карточки для просмотра."
        )
    elif draft.status == "cancelled":
        text = (
            f"🗂 <b>Черновик за {draft.day:%d.%m.%Y} закрыт.</b>\nКарточки доступны для просмотра."
        )
    elif draft.status == "saving":
        text = "Сохранение не завершилось. Скажите «повтори», чтобы продолжить."
    elif notice:
        text = escape(notice)
    elif issues:
        text = f"Готовых позиций: {ready}.\n{issues}" if ready else issues
        if draft.auto_save:
            text += "\nПосле уточнения сохраню сразу."
    else:
        text = f"Черновик за {draft.day:%d.%m.%Y}: {len(draft.lines)} позиций."
        for line in draft.lines[:3]:
            text += (
                f"\n{escape(line.phrase[:120])} — "
                f"{fmt(line.quantity or 0)} {escape(line.unit or '')}"
            )
        if len(draft.lines) > 3:
            text += f"\nЕщё {len(draft.lines) - 3} — в отчёте."
        text += "\nСкажите «сохрани» или проверьте отчёт."
    if draft.status == "draft":
        snapshot = await reports.sheets.snapshot(draft.day)
        warnings = estimate_warnings(draft, snapshot)
        if warnings:
            text += "\n\n" + escape(warning_text(warnings[:3]))
            if len(warnings) > 3:
                text += f"\nЕщё превышений: {len(warnings) - 3}. Все позиции — в отчёте."
            text += "\nПроверьте объём. Если превышение верно, подтвердите сохранение."
    if reports.settings.web_enabled:
        opener = report_button(draft, reports)
        rows = [[opener], [table_button(reports)]]
        if draft.status == "saving":
            rows.insert(0, [button(draft, "Повторить сохранение", "ok")])
    else:
        rows = [
            [button(draft, "Сохранить", "ok")],
            [button(draft, "Изменить / удалить", "pick"), button(draft, "Добавить", "add")],
            [table_button(reports)],
        ]
    await message.answer(
        text,
        reply_markup=work_keyboard(reports, draft.user_id, draft=draft, markup=keyboard(rows)),
        link_preview_options=LinkPreviewOptions(is_disabled=True),
    )
    if reports.settings.web_enabled and reports.settings.web_auto_open:
        try:
            await asyncio.to_thread(webbrowser.open, opener.url or opener.web_app.url)
        except Exception as exc:
            logging.getLogger(__name__).warning("Browser open failed: %s", type(exc).__name__)


HELP = """<b>М100 · Помощник по таблице</b>

Можно писать или говорить обычными фразами:
«Добавь кабель 3х2,5 — 15 метров» — подготовлю черновик.
«Добавь кабель 3х2,5 — 15 метров и сразу сохрани» — сразу запишу готовые данные.
«Исправь на 20 метров и сохрани» — изменю указанную работу.
«Выдели всё, что ты сегодня добавлял, синим» — выделю уже записанные ячейки.
«Теперь их светло-зелёным» — помню предыдущий выбор.
«Выдели B5:D8 цветом #A8DADC» — выделю конкретные поля.
«Сними выделение» — уберу заливку.
«Пока не сохраняй» — оставлю данные в черновике.
«Сколько кабеля выполнено и сколько осталось?» — прочитаю таблицу и отвечу здесь.
«Что записано за вчера?» — покажу дневные объёмы.
«Сколько заработали за неделю?» — посчитаю по данным таблицы.
«А какая по ним ставка?» — использую предыдущий выбор.
«Покажи B5:D8» — пришлю значения ячеек.
Большие списки пришлю одним CSV-файлом, без серии сообщений.

Если данных не хватает, спрошу конкретный параметр. После уточнения продолжу
сохранение, если вы уже его попросили. История и выполненные действия сохраняются.

/draft — текущий черновик
/today — исправить дневной итог
/table — таблица
/remaining — остатки
/cancel — отменить черновик
/help — справка
"""
