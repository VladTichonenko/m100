"""Readable Telegram screens, also reachable through spoken navigation."""

from html import escape
from urllib.parse import urlsplit

from aiogram.types import InlineKeyboardButton, LinkPreviewOptions

from .access import ROLE_NAMES
from .domain import UserError, fmt
from .navigation import help_page, main_keyboard
from .service import Reports
from .ui import keyboard, report_button, show_draft, table_button, work_keyboard


class BotPages:
    def __init__(self, reports: Reports):
        self.reports = reports

    async def welcome(self, message, user_id: int):
        reports = self.reports
        profile = reports.store.profile(user_id) or {}
        username = f" · @{escape(profile['username'])}" if profile.get("username") else ""
        draft = reports.store.active_draft(user_id)
        if draft and draft.status == "draft" and draft.action:
            draft.action = ""
            reports.save_edit(draft)
        await message.answer(
            "👋 <b>М100 · Голосовой помощник</b>\n"
            "<i>Расскажите о работе — я подготовлю отчёт.</i>\n\n"
            "🎙 <b>Отправьте голосовую команду</b>\n"
            "<blockquote>Добавь кабель три на два с половиной — 15 метров.</blockquote>\n"
            "Проверьте карточки и скажите <b>«Сохрани»</b>. "
            "Или добавьте <b>«и сразу сохрани»</b> в конце команды.\n\n"
            "🧭 <b>Всё под рукой</b>\n"
            "Отчёт · таблица · черновики · список голосовых команд. "
            "Нажмите кнопку внизу или просто назовите действие.\n\n"
            f"<b>Ваш профиль</b>\n{escape(profile.get('name', 'Мастер'))}{username}\n"
            f"{ROLE_NAMES.get(profile.get('role', ''), 'Мастер')} · ID <code>{user_id}</code>\n"
            f'<a href="{escape(reports.sheets.url, quote=True)}">Рабочая таблица ↗</a>'
            + ("\n\n🗂 Ваш незавершённый черновик сохранён." if draft else ""),
            reply_markup=main_keyboard(),
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )

    async def commands(self, message, section: str = "overview"):
        text, markup = help_page(section)
        await message.answer(text, reply_markup=markup)

    async def drafts(self, message, user_id: int, offset: int = 0):
        drafts = self.reports.store.pending_drafts(user_id, offset=offset)
        rows, descriptions = [], []
        for rank, draft in enumerate(drafts[:5], offset + 1):
            count = sum(not line.deleted for line in draft.lines)
            state = "нужно завершить сохранение" if draft.status == "saving" else "не сохранён"
            descriptions.append(
                f"<b>{rank}. {draft.day:%d.%m.%Y}</b> · работ: {count}\n<i>{state}</i>"
            )
            rows.append(
                [
                    InlineKeyboardButton(
                        text=f"📋 {draft.day:%d.%m.%Y} · {count} работ",
                        callback_data=f"nav:draft:{draft.id}",
                    )
                ]
            )
        if drafts:
            text = "\n\n".join(descriptions) + "\n\nНажмите на дату, чтобы открыть отчёт."
        else:
            text = (
                "Пока нет незавершённых отчётов.\n\n"
                "<blockquote>Добавь кабель три на два с половиной — 15 метров.</blockquote>\n"
                "Отправьте эту фразу голосом — появится первый черновик."
            )
        pager = []
        if offset:
            pager.append(
                InlineKeyboardButton(
                    text="‹ Назад", callback_data=f"nav:drafts:{max(0, offset - 5)}"
                )
            )
        if len(drafts) > 5:
            pager.append(
                InlineKeyboardButton(text="Дальше ›", callback_data=f"nav:drafts:{offset + 5}")
            )
        if pager:
            rows.append(pager)
        rows.append([InlineKeyboardButton(text="⌂ Главное меню", callback_data="nav:restart")])
        await message.answer(
            "🗂 <b>Мои черновики</b>\n\n" + text,
            reply_markup=work_keyboard(self.reports, user_id, markup=keyboard(rows)),
        )

    async def report(self, message, user_id: int):
        reports = self.reports
        draft = reports.active_draft(user_id) or reports.store.latest_saved_report(
            user_id, reports.today()
        )
        if not draft:
            await message.answer(
                "📋 <b>Ваш отчёт</b>\n\nЗа сегодня пока нет отчёта. Расскажите, что сделали:\n"
                "<blockquote>Проложили кабель три на два с половиной — 15 метров.</blockquote>",
                reply_markup=work_keyboard(
                    reports,
                    user_id,
                    markup=keyboard(
                        [[InlineKeyboardButton(text="🎙 Примеры команд", callback_data="help:work")]]
                    ),
                ),
            )
        elif draft.status == "done":
            await message.answer(
                f"✅ <b>Отчёт за {draft.day:%d.%m.%Y} сохранён</b>\n\n"
                "Откройте карточки, чтобы посмотреть результат. Для исправления скажите:\n"
                "<blockquote>Открой отчёт за сегодня.</blockquote>",
                reply_markup=keyboard([[report_button(draft, reports)], [table_button(reports)]]),
            )
        else:
            await show_draft(message, draft, reports)

    async def registry(self, message, user_id: int):
        reports = self.reports
        principal = await reports.require_access(user_id)
        if principal.role != "admin":
            raise UserError("Управление пользователями доступно только администратору.")
        if not reports.settings.bitrix_access_enabled:
            raise UserError("Реестр пользователей Битрикс24 ещё не подключён.")
        host = urlsplit(reports.settings.bitrix_webhook_url.get_secret_value()).hostname
        url = f"https://{host}/crm/type/{reports.settings.bitrix_users_entity_type_id}/list/category/0/"
        await message.answer(
            "Профили и доступ к боту настраиваются в Битрикс24. "
            "Для открытия нужны права на реестр в портале.",
            reply_markup=keyboard([[InlineKeyboardButton(text="Пользователи бота", url=url)]]),
        )

    async def remaining(self, message, user_id: int, query: str = ""):
        reports = self.reports
        snap = await reports.sheets.snapshot(reports.today())
        query = query.lower().strip()
        positions = [
            p
            for p in snap.positions.values()
            if not query or query in p.name.lower() or query == p.id.lower()
        ]
        if not positions:
            raise UserError("Позиции не найдены. Попробуйте /remaining кабель или ID из таблицы.")
        text = "📐 <b>Остатки по спецификации</b>\n"
        for p in positions:
            line = (
                f"\n{escape(p.id)} · {escape(p.name)}\n<b>{fmt(p.remaining)} {escape(p.unit)}</b>\n"
            )
            if len(text) + len(line) > 3500:
                await message.answer(text, reply_markup=work_keyboard(reports, user_id))
                text = ""
            text += line
        await message.answer(text, reply_markup=work_keyboard(reports, user_id))

    async def navigate(self, message, user_id: int, chat_id: int, action: str, section="overview"):
        reports = self.reports
        if action == "restart":
            await self.welcome(message, user_id)
        elif action == "open_report":
            await self.report(message, user_id)
        elif action == "new_report":
            await reports.require_access(user_id, write=True)
            draft = reports.active_draft(user_id)
            if draft and draft.status == "saving":
                await show_draft(message, draft, reports)
                return
            saved = reports.store.latest_saved_report(user_id, reports.today())
            state = (
                "Новые работы добавлю к текущему несохранённому отчёту.\n\n"
                if draft
                else "Предыдущий отчёт уже сохранён.\n\n"
                if saved
                else ""
            )
            await message.answer(
                state + "🎙 Расскажите голосом или текстом, какие новые работы выполнены "
                "и в каком объёме.\nНапример: «Проложили кабель 3х2,5 — 15 метров».",
                reply_markup=work_keyboard(reports, user_id),
            )
        elif action == "drafts":
            await self.drafts(message, user_id)
        elif action == "help":
            await self.commands(message, section)
        elif action == "table":
            await message.answer(
                "📊 <b>Рабочая таблица М100</b>\nСпецификация, объёмы и результат вашей работы.",
                reply_markup=work_keyboard(reports, user_id),
            )
        elif action == "today":
            await reports.require_access(user_id, write=True)
            draft = reports.active_draft(user_id) or await reports.create(user_id, chat_id)
            await show_draft(message, draft, reports)
        elif action == "cancel":
            await reports.require_access(user_id, write=True)
            draft = reports.active_draft(user_id)
            if not draft:
                await message.answer(
                    "🗂 Незавершённого черновика нет.",
                    reply_markup=work_keyboard(reports, user_id),
                )
            else:
                reports.cancel(draft)
                await message.answer(
                    "Черновик отменён. Данные не записывались.",
                    reply_markup=work_keyboard(reports, user_id, draft=draft),
                )
        elif action == "remaining":
            await self.remaining(message, user_id)
        elif action == "users":
            await self.registry(message, user_id)
