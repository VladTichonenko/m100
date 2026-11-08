import logging
import re
from html import escape
from io import BytesIO

from aiogram import BaseMiddleware, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    LinkPreviewOptions,
    Message,
)
from aiogram.utils.chat_action import ChatActionSender
from pydantic import ValidationError

from .bot_pages import BotPages
from .domain import UserError, number, validate_line
from .navigation import NAVIGATION_ACTIONS, help_page, spoken_navigation
from .service import Reports
from .ui import button, keyboard, show_draft, show_saved, table_button, work_keyboard
from .voice import Voice

logger = logging.getLogger(__name__)


class AccessAndErrors(BaseMiddleware):
    def __init__(self, reports: Reports):
        self.reports = reports

    async def __call__(self, handler, event: Message | CallbackQuery, data):
        user = event.from_user
        message = event if isinstance(event, Message) else event.message
        if not user or not isinstance(message, Message):
            return
        if message.chat.type != "private":
            if isinstance(event, CallbackQuery):
                await event.answer("Нет доступа", show_alert=True)
            else:
                await message.answer(
                    f"Доступ закрыт. Ваш Telegram ID: <code>{user.id}</code>. "
                    "Откройте личный чат с ботом."
                )
            return
        authorized = False
        try:
            principal = await self.reports.require_access(user.id)
            authorized = True
            if isinstance(event, CallbackQuery) and (event.data or "").startswith("d:"):
                await self.reports.require_access(user.id, write=True)
            elif (
                message.text
                and message.text.split()
                and message.text.split()[0].split("@")[0] in {"/today", "/cancel"}
            ):
                await self.reports.require_access(user.id, write=True)
            async with self.reports.edit_lock:
                self.reports.store.save_profile(
                    user.id,
                    {
                        "name": user.full_name,
                        "employee_name": principal.name or user.full_name,
                        "username": user.username,
                        "role": principal.role,
                    },
                )
                return await handler(event, data)
        except UserError as exc:
            await message.answer(
                escape(str(exc)) + f"\nВаш Telegram ID: <code>{user.id}</code>.",
                reply_markup=work_keyboard(self.reports, user.id) if authorized else None,
            )
        except Exception as exc:
            # HTTP exception strings can contain a Bitrix webhook or Telegram token.
            logger.error("Handler failed: %s", type(exc).__name__)
            await message.answer(
                "Не удалось завершить действие. Черновик сохранён. "
                "Откройте /draft и повторите действие.",
                reply_markup=work_keyboard(self.reports, user.id) if authorized else None,
            )


def build_router(reports: Reports) -> Router:
    router = Router()
    middleware = AccessAndErrors(reports)
    router.message.outer_middleware(middleware)
    router.callback_query.outer_middleware(middleware)

    pages = BotPages(reports)

    @router.message(CommandStart())
    @router.message(Command("restart"))
    async def start(message: Message):
        await pages.welcome(message, message.from_user.id)

    @router.message(Command("help", "commands"))
    async def help_message(message: Message):
        await pages.commands(message)

    @router.message(Command("report", "draft"))
    async def resume(message: Message):
        await pages.report(message, message.from_user.id)

    @router.message(Command("drafts"))
    async def drafts(message: Message):
        await pages.drafts(message, message.from_user.id)

    @router.message(Command("table", "users", "cancel", "today"))
    async def menu_action(message: Message):
        action = message.text.split()[0].split("@")[0][1:]
        await pages.navigate(message, message.from_user.id, message.chat.id, action)

    @router.message(Command("remaining"))
    async def remaining(message: Message):
        await pages.remaining(
            message, message.from_user.id, (message.text or "").partition(" ")[2]
        )

    @router.callback_query(F.data.startswith("help:"))
    async def help_section(query: CallbackQuery):
        await query.answer()
        text, markup = help_page(query.data.partition(":")[2])
        if query.message.text != text:
            await query.message.edit_text(text, reply_markup=markup)

    @router.callback_query(F.data.startswith("nav:"))
    async def navigation_callback(query: CallbackQuery):
        await query.answer()
        parts = query.data.split(":")
        action = parts[1]
        if action == "draft" and len(parts) == 3:
            draft = reports.store.get_draft(parts[2], query.from_user.id)
            await show_draft(query.message, draft, reports)
        elif action == "drafts":
            offset = int(parts[2]) if len(parts) == 3 and parts[2].isdigit() else 0
            await pages.drafts(query.message, query.from_user.id, offset)
        elif action == "restart":
            await pages.welcome(query.message, query.from_user.id)
        elif action == "report":
            await pages.report(query.message, query.from_user.id)

    async def offer_positions(message: Message, draft, index: int, keys: list[str]):
        snap = await reports.sheets.snapshot(draft.day)
        rows = []
        text = "<b>Выберите позицию</b>\n"
        for rank, key in enumerate(keys[:8], 1):
            p = snap.positions.get(key)
            if not p:
                continue
            text += f"\n{rank}. {escape(p.id)} · {escape(p.name)} ({escape(p.unit)})\n"
            rows.append(
                [
                    InlineKeyboardButton(
                        text=f"{rank}. {p.id}",
                        callback_data=f"d:{draft.id}:{draft.revision}:choose:{index}:{key}",
                    )
                ]
            )
        rows.append([button(draft, "Назад к отчёту", "back")])
        await message.answer(
            text if keys else "Ничего не найдено. Попробуйте другое название.",
            reply_markup=work_keyboard(reports, draft.user_id, draft=draft, markup=keyboard(rows)),
        )

    async def process_command(message: Message, text: str, *, voice: bool = False):
        user_id, chat_id = message.from_user.id, message.chat.id
        reports.store.remember(user_id, chat_id, "user", text)

        class Reply:
            work_links = False

            async def answer(self, content, **kwargs):
                if self.work_links:
                    kwargs["reply_markup"] = work_keyboard(
                        reports, user_id, markup=kwargs.get("reply_markup")
                    )
                result = await message.answer(content, **kwargs)
                reports.store.remember(user_id, chat_id, "assistant", content)
                return result

        reply = Reply()
        target = None
        try:
            navigation = spoken_navigation(text)
            if navigation:
                await pages.navigate(reply, user_id, chat_id, navigation.action, navigation.section)
                return
            reply.work_links = True
            snapshot = await reports.sheets.snapshot(reports.today())
            command, target = await reports.plan_command(user_id, chat_id, text, snapshot=snapshot)
            if command.action in NAVIGATION_ACTIONS:
                reply.work_links = False
                await pages.navigate(reply, user_id, chat_id, command.action, command.help_section)
                return
            if command.action not in {"query", "table"}:
                await reports.require_access(user_id, write=True)
            if command.action == "query":
                answer = await reports.read_query(user_id, chat_id, command, snapshot=snapshot)
                markup = work_keyboard(
                    reports,
                    user_id,
                    markup=keyboard(
                        [[InlineKeyboardButton(text="Открыть источник", url=answer.url)]]
                    ),
                )
                if answer.document:
                    await message.answer_document(
                        BufferedInputFile(answer.document, filename=answer.filename),
                        caption=answer.text,
                        reply_markup=markup,
                    )
                    reports.store.remember(user_id, chat_id, "assistant", answer.text)
                else:
                    await reply.answer(
                        answer.text,
                        reply_markup=markup,
                        link_preview_options=LinkPreviewOptions(is_disabled=True),
                    )
                return
            if command.action == "format":
                op = await reports.format_cells(user_id, chat_id, command)
                await show_saved(reply, op, reports)
                return
            if command.action == "retry":
                await show_saved(reply, await reports.retry(user_id, chat_id), reports)
                return
            if command.action == "table":
                snap = snapshot
                await reply.answer(
                    f"Рабочая таблица, лист «{escape(reports.settings.google_sheet_name)}».",
                    reply_markup=keyboard(
                        [
                            [
                                InlineKeyboardButton(
                                    text="Открыть лист",
                                    url=f"{reports.sheets.url}#gid={snap.sheet_id}",
                                )
                            ]
                        ]
                    ),
                )
                return
            if command.action == "clarify":
                active = reports.store.active_draft(user_id)
                if active and command.save_immediately is False:
                    reports.editable(active)
                    active.auto_save = False
                    reports.save_edit(active)
                await reply.answer(
                    escape(command.question or "Какую работу или ячейки нужно изменить?")
                )
                return
            if command.action == "save_ready" and not reports.active_draft(user_id):
                await reply.answer(
                    "Несохранённых данных нет. "
                    "Могу выделить уже записанное или добавить новые работы.",
                    reply_markup=keyboard([[table_button(reports)]]),
                )
                return
            draft = await reports.execute_command(target, command, text)
            if command.action == "wait":
                await reply.answer("Хорошо, пока не сохраняю.")
            elif draft.status == "done":
                await show_saved(reply, reports.store.operation_for(draft.id), reports)
            else:
                await show_draft(reply, draft, reports)
        except UserError as exc:
            op = reports.store.operation_for(target.id) if target else None
            detail = ""
            rows = [[table_button(reports)]]
            if op:
                if op.sheets_done:
                    detail = (
                        "\nВ таблицу записано. Синхронизация ещё не завершена; скажите «повтори»."
                    )
                else:
                    detail = "\nСкажите «повтори» — проверю запись и продолжу без дублирования."
                current = reports.store.get_draft(op.draft_id, user_id)
                rows.insert(0, [button(current, "Повторить сохранение", "ok")])
            await reply.answer(escape(str(exc)) + detail, reply_markup=keyboard(rows))

    @router.callback_query(F.data.startswith("d:"))
    async def callback(query: CallbackQuery):
        await query.answer()
        parts = query.data.split(":")
        if len(parts) < 5:
            raise UserError("Некорректная кнопка. Откройте /draft.")
        _, key, revision, action, raw_index, *extra = parts
        draft = reports.store.get_draft(key, query.from_user.id)
        if draft.chat_id != query.message.chat.id or str(draft.revision) != revision:
            raise UserError(
                "Эта кнопка устарела. Используйте кнопки последнего черновика или /draft."
            )
        message = query.message

        async def answer(content, **kwargs):
            kwargs["reply_markup"] = work_keyboard(
                reports, draft.user_id, draft=draft, markup=kwargs.get("reply_markup")
            )
            return await message.answer(content, **kwargs)

        if action == "ok":
            try:
                op = await reports.confirm(draft)
            except UserError as exc:
                op = reports.store.operation_for(draft.id)
                detail = ""
                if op:
                    detail = "\nGoogle Sheets: " + (
                        "сохранено" if op.sheets_done else "проверим при повторе"
                    )
                    detail += "\nБитрикс24: " + (
                        "сохранено" if op.bitrix_done else "ожидает сохранения"
                    )
                await answer(
                    escape(str(exc)) + detail,
                    reply_markup=keyboard([[button(draft, "Повторить сохранение", "ok")]]),
                )
                return
            await show_saved(message, op, reports)
            return
        if action == "cancel":
            reports.cancel(draft)
            await answer("Черновик отменён. Данные не записывались.")
            return
        reports.editable(draft)
        if action == "add":
            draft.action = "add"
            reports.save_edit(draft)
            await answer("Напишите, какую работу добавить и сколько выполнено.")
            return
        if action == "pick":
            rows = [
                [button(draft, f"{i + 1}. {line.phrase[:48]}", "line", i)]
                for i, line in enumerate(draft.lines)
            ]
            rows.append([button(draft, "Назад", "back")])
            await answer("Выберите строку для изменения:", reply_markup=keyboard(rows))
            return
        if action == "back":
            draft.action = ""
            reports.save_edit(draft)
            await show_draft(message, draft, reports)
            return
        try:
            index = int(raw_index)
        except ValueError as exc:
            raise UserError("Некорректный номер строки.") from exc
        if index < 0 or index >= len(draft.lines):
            raise UserError("Строка больше не существует. Откройте /draft.")
        line = draft.lines[index]
        if action == "line":
            remove = "Обнулить / удалить" if draft.mode == "replace" else "Удалить"
            await answer(
                f"Строка {index + 1}: {escape(line.phrase)}",
                reply_markup=keyboard(
                    [
                        [
                            button(draft, "Количество", "qty", index),
                            button(draft, "Позиция", "position", index),
                        ],
                        [button(draft, remove, "delete", index), button(draft, "Назад", "back")],
                    ]
                ),
            )
        elif action in {"qty", "position"}:
            draft.action = action
            draft.action_index = index
            reports.save_edit(draft)
            if action == "qty":
                await answer(
                    "Введите количество числом, например 15 или 2,5. "
                    "Для обнуления сегодняшней позиции введите 0."
                )
            else:
                if line.candidates:
                    await offer_positions(message, draft, index, line.candidates)
                else:
                    await answer("Напишите часть названия или ID позиции из таблицы.")
        elif action == "choose":
            snap = await reports.sheets.snapshot(draft.day)
            if not extra or extra[0] not in snap.positions:
                raise UserError("Позиция не найдена в актуальной спецификации.")
            line.position_id = extra[0]
            line.confirmed_position = True
            line.question = ""
            draft.lines[index] = validate_line(line, snap.positions)
            draft.action = ""
            reports.save_edit(draft)
            await show_draft(message, draft, reports)
        elif action == "delete":
            if (
                draft.mode == "replace"
                and line.position_id
                and draft.before.get(line.position_id, 0)
            ):
                line.quantity = number(0)
            else:
                draft.lines.pop(index)
            draft.action = ""
            reports.save_edit(draft)
            await show_draft(message, draft, reports)

    @router.message(F.text)
    async def text_report(message: Message):
        if message.text.startswith("/"):
            raise UserError("Неизвестная команда. Доступные команды: /help.")
        if spoken_navigation(message.text):
            await process_command(message, message.text)
            return
        draft = reports.store.active_draft(message.from_user.id)
        if draft:
            if draft.action in {"qty", "position"}:
                await reports.require_access(message.from_user.id, write=True)
            if draft.action == "qty" and re.fullmatch(
                r"[+-]?\d+(?:[.,]\d+)?", message.text.strip()
            ):
                reports.editable(draft)
                try:
                    quantity = number(message.text)
                    line = draft.lines[draft.action_index].model_copy(update={"quantity": quantity})
                    line.question = ""
                    # model_copy(update=...) does not validate quantities.
                    line = type(line).model_validate(line.model_dump())
                    snap = await reports.sheets.snapshot(draft.day)
                    line = validate_line(line, snap.positions)
                    if line.quantity is None:
                        raise UserError(line.question)
                except ValidationError as exc:
                    raise UserError(
                        "Введите неотрицательное количество, например 15 или 2,5."
                    ) from exc
                draft.lines[draft.action_index] = line
                draft.action = ""
                reports.save_edit(draft)
                await show_draft(message, draft, reports)
            elif draft.action == "position":
                snap = await reports.sheets.snapshot(draft.day)
                query = message.text.lower().strip()
                keys = [
                    p.id
                    for p in snap.positions.values()
                    if query in p.name.lower() or query == p.id.lower()
                ]
                if len(keys) > 8:
                    raise UserError("Найдено больше 8 позиций. Уточните название или укажите ID.")
                if keys:
                    await offer_positions(message, draft, draft.action_index, keys)
                else:
                    async with ChatActionSender.typing(bot=message.bot, chat_id=message.chat.id):
                        await process_command(message, message.text)
            else:
                async with ChatActionSender.typing(bot=message.bot, chat_id=message.chat.id):
                    await process_command(message, message.text)
        else:
            async with ChatActionSender.typing(bot=message.bot, chat_id=message.chat.id):
                await process_command(message, message.text)

    @router.message(F.voice)
    async def voice_report(message: Message):
        voice = message.voice
        if voice.duration > reports.settings.voice_max_seconds:
            raise UserError(
                f"Голосовое длиннее {reports.settings.voice_max_seconds} секунд. "
                "Разделите его на несколько сообщений."
            )
        if voice.file_size and voice.file_size > reports.settings.voice_max_bytes:
            raise UserError("Голосовое слишком большое. Запишите сообщение короче.")
        status = await message.answer("Слушаю")
        async with ChatActionSender.typing(bot=message.bot, chat_id=message.chat.id):
            try:
                buffer = BytesIO()
                await message.bot.download(voice, destination=buffer)
                text = await Voice(reports.settings, reports.llm.http).transcribe(buffer.getvalue())
            except UserError as exc:
                await status.edit_text(
                    str(exc),
                    parse_mode=None,
                )
                return
            except Exception as exc:
                logger.error("Voice transcription failed: %s", type(exc).__name__)
                await status.edit_text(
                    "Не удалось обработать голосовое. Попробуйте ещё раз или отправьте текст.",
                    parse_mode=None,
                )
                return
            # Even emoji occupy at most two UTF-16 units: leave room for the heading
            # within Telegram's 4096-character limit and keep the entire transcript.
            parts = [text[i : i + 2000] for i in range(0, len(text), 2000)]
            await status.edit_text(
                f"🎙 Расшифровка:\n\n<blockquote>{escape(parts[0])}</blockquote>",
                parse_mode="HTML",
            )
            for part in parts[1:]:
                await message.answer(
                    f"<blockquote>{escape(part)}</blockquote>",
                    parse_mode="HTML",
                )
            await process_command(message, text, voice=True)

    @router.message()
    async def unsupported(message: Message):
        await message.answer("Отправьте отчёт текстом или голосовым сообщением Telegram.")

    return router
