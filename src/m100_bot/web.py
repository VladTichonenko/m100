"""Local React editor and capability-scoped report API, running with the bot."""

import logging
from decimal import Decimal
from io import BytesIO
from pathlib import Path
from urllib.parse import urlsplit

from aiogram import Bot
from aiogram.types import ForceReply
from aiohttp import web
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .access import ROLE_NAMES, AccessDenied
from .domain import CardDetails, Draft, Line, UserError, number, validate_line
from .estimate import estimate_warnings
from .profiles import public_profile, telegram_profile
from .service import Reports
from .ui import keyboard, table_button

logger = logging.getLogger(__name__)


class EditLine(BaseModel):
    model_config = ConfigDict(extra="forbid")
    position_id: str | None = None
    quantity: str | None = None
    phrase: str = Field(default="", max_length=600)
    details: CardDetails = Field(default_factory=CardDetails)
    deleted: bool = False


class Edit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int = Field(ge=0)
    lines: list[EditLine] = Field(max_length=40)


class CardEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int = Field(ge=0)
    line: EditLine | None = None


class Revision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int = Field(ge=0)


def create_app(
    reports: Reports, dist: Path | None = None, bot: Bot | None = None
) -> web.Application:
    dist = (dist or reports.settings.web_dist_path).resolve()

    def recipient(draft: Draft) -> int:
        if reports.settings.bitrix_access_enabled:
            return draft.chat_id
        users = reports.settings.users
        return next(iter(users)) if len(users) == 1 else draft.chat_id

    @web.middleware
    async def errors(request, handler):
        try:
            response = await handler(request)
        except UserError as exc:
            response = web.json_response({"error": str(exc)}, status=409)
        except (ValidationError, ValueError, TypeError):
            response = web.json_response(
                {"error": "Проверьте поля: количество должно быть неотрицательным числом."},
                status=400,
            )
        except web.HTTPException as exc:
            response = web.json_response({"error": exc.reason}, status=exc.status)
        except Exception as exc:
            logger.error("Web request failed: %s", type(exc).__name__)
            response = web.json_response(
                {"error": "Не удалось завершить запрос. Проверьте соединение и повторите."},
                status=500,
            )
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        return response

    async def authorized(request, *, write: bool = False) -> Draft:
        if write and not reports.writer_ready:
            raise web.HTTPServiceUnavailable(
                reason="Обновление сервиса. Повторите через несколько секунд."
            )
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer ") or len(auth) > 256:
            raise web.HTTPUnauthorized(reason="Откройте персональную ссылку из бота.")
        try:
            draft = reports.store.from_web_token(request.match_info["id"], auth[7:])
        except UserError as exc:
            raise web.HTTPUnauthorized(reason=str(exc)) from None
        try:
            await reports.require_access(draft.user_id, write=write)
        except AccessDenied as exc:
            raise web.HTTPForbidden(reason=str(exc)) from None
        return draft

    async def payload(draft):
        principal = await reports.require_access(draft.user_id)
        snap = await reports.sheets.snapshot(draft.day)
        op = reports.store.operation_for(draft.id)
        today = reports.today()
        daily = snap if draft.day == today else await reports.sheets.snapshot(today)
        today_lines = []
        total_pay = Decimal(0)
        pay_complete = True
        for key, position in daily.positions.items():
            quantity = daily.day_values.get(key, Decimal(0))
            if not quantity:
                continue
            pay = quantity * position.unit_pay if position.unit_pay is not None else None
            if pay is None:
                pay_complete = False
            else:
                total_pay += pay
            today_lines.append(
                {
                    "position_id": key,
                    "name": position.name,
                    "unit": position.unit,
                    "quantity": str(quantity),
                    "pay": str(pay) if pay is not None else None,
                }
            )
        # Only the public portal origin is exposed; a REST webhook contains credentials.
        portal = urlsplit(reports.settings.bitrix_webhook_url.get_secret_value())
        bitrix_url = (
            f"https://{portal.hostname}/crm/type/"
            f"{reports.settings.bitrix_entity_type_id}/list/category/0/"
            if portal.hostname and reports.settings.bitrix_entity_type_id > 0
            else ""
        )
        return {
            "estimate_warnings": estimate_warnings(draft, snap)
            if not (op and op.sheets_done)
            else [],
            "sync": {
                "google": "saved" if op and op.sheets_done else "pending" if op else "not_started",
                "bitrix": "disabled"
                if not reports.settings.bitrix_enabled
                else "saved"
                if op and op.bitrix_done and not op.bitrix_skipped
                else "pending"
                if op
                else "not_started",
            },
            "access": {
                "role": principal.role,
                "label": ROLE_NAMES[principal.role],
                "can_write": principal.can_write,
            },
            "profile": public_profile(await telegram_profile(reports.store, draft.user_id, bot)),
            "draft": draft.model_dump(mode="json", exclude={"user_id", "chat_id", "action"}),
            "catalog": [
                p.model_dump(mode="json", exclude={"row"}) for p in snap.positions.values()
            ],
            "table_url": op.table_url if op and op.table_url else reports.sheets.url,
            "bitrix_enabled": reports.settings.bitrix_enabled,
            "operation": op.model_dump(mode="json") if op else None,
            "rewrite_requested": draft.action == "voice",
            "bot_url": reports.bot_url,
            "notification_pending": draft.status == "done" and not draft.saved_notified,
            "timezone": reports.settings.timezone,
            "bitrix_url": bitrix_url,
            "today": {
                "day": today.isoformat(),
                "lines": today_lines,
                "total_pay": str(total_pay) if pay_complete else None,
            },
        }

    async def avatar(request):
        draft = await authorized(request)
        profile = await telegram_profile(reports.store, draft.user_id, bot)
        if bot is None or not profile.get("photo_file_id"):
            raise web.HTTPNotFound(reason="У аккаунта нет доступного фото.")
        buffer = BytesIO()
        await bot.download(profile["photo_file_id"], destination=buffer)
        return web.Response(body=buffer.getvalue(), content_type="image/jpeg")

    async def rewrite_report(request):
        async with reports.edit_lock:
            draft = await authorized(request, write=True)
            reports.editable(draft)
            edit = Revision.model_validate(await request.json())
            # Retrying after a lost response must not send a second invitation.
            if draft.action == "voice" and edit.revision in {draft.revision, draft.revision - 1}:
                return web.json_response(await payload(draft))
            if edit.revision != draft.revision:
                raise UserError("Отчёт изменён в другом окне или боте. Обновите страницу.")
            if bot is None:
                raise UserError(
                    "Связь с ботом недоступна. Откройте чат и отправьте голосовую команду."
                )
            await bot.send_message(
                recipient(draft),
                "🎙 <b>Давайте перезапишем.</b>\n\n"
                "Отправьте голосовое с новым отчётом: «Перезапиши весь отчёт…». "
                "Или расскажите, что отредактировать: «Исправь кабель на 20 метров».\n\n"
                "Текущий черновик сохранён до получения новой команды.",
                parse_mode="HTML",
                reply_markup=ForceReply(
                    selective=True,
                    input_field_placeholder="Запишите голосовое или опишите правки",
                ),
            )
            draft.action = "voice"
            reports.save_edit(draft)
            return web.json_response(await payload(draft))

    async def edit_today(request):
        """Open a replacement draft from current saved day totals; never write externals."""
        async with reports.edit_lock:
            source = await authorized(request, write=True)
            edit = Revision.model_validate(await request.json())
            if source.status != "done" or edit.revision != source.revision:
                raise UserError("Откройте актуальный сохранённый отчёт перед исправлением.")
            draft = reports.active_draft(source.user_id)
            if draft is not None:
                if draft.mode != "replace":
                    raise UserError(
                        "У вас уже есть незавершённый отчёт. "
                        "Завершите его или откройте /draft в боте."
                    )
            else:
                draft = await reports.create(source.user_id, source.chat_id)
            token = reports.store.web_token(draft)
            return web.json_response(
                {
                    **await payload(draft),
                    "edit_url": f"/report/{draft.id}#token={token}",
                }
            )

    async def apply_edit(request, draft):
        reports.editable(draft)
        edit = Edit.model_validate(await request.json())
        if draft.revision != edit.revision:
            raise UserError("Отчёт изменён в другом окне или боте. Обновите страницу.")
        snap = await reports.sheets.snapshot(draft.day)
        lines = []
        for item in edit.lines:
            position = snap.positions.get(item.position_id or "")
            if item.position_id and not position:
                raise UserError("Позиция больше не найдена в спецификации. Выберите другую.")
            quantity = item.quantity.replace(",", ".").strip() if item.quantity else None
            line = Line(
                phrase=item.phrase or (position.name if position else "Новая работа"),
                position_id=item.position_id,
                quantity=quantity or None,
                unit=position.unit if position else None,
                confirmed_position=True,
                details=CardDetails(),
                deleted=item.deleted,
            )
            lines.append(validate_line(line, snap.positions))
        # Removing a previously recorded daily total must explicitly write zero.
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
                        )
                    )
        draft.lines = lines
        draft.action = ""
        reports.save_edit(draft)

    async def get_report(request):
        async with reports.edit_lock:
            return web.json_response(await payload(await authorized(request)))

    async def edit_report(request):
        async with reports.edit_lock:
            draft = await authorized(request, write=True)
            await apply_edit(request, draft)
            return web.json_response(await payload(draft))

    async def confirm_report(request):
        async with reports.edit_lock:
            draft = await authorized(request, write=True)
            # A retry after a lost response resumes the existing operation.
            # It never applies edits or adds the quantities a second time.
            if not reports.store.operation_for(draft.id):
                await apply_edit(request, draft)
            try:
                await reports.confirm(draft)
            except UserError as exc:
                current = reports.store.get_draft(draft.id, draft.user_id)
                return web.json_response(
                    {"error": str(exc), **await payload(current)},
                    status=409,
                )
            current = reports.store.get_draft(draft.id, draft.user_id)
            if bot and not current.saved_notified:
                try:
                    await bot.send_message(
                        recipient(current),
                        f"✅ <b>Отчёт за {current.day:%d.%m.%Y} сохранён.</b>\n\n"
                        "Работы и объёмы записаны в исходную таблицу.\n"
                        "Если нужно исправить результат, отправьте голосовую команду.",
                        parse_mode="HTML",
                        reply_markup=keyboard([[table_button(reports)]]),
                    )
                except Exception as exc:
                    # A notification failure must not turn a completed write into a failed save.
                    logger.warning("Save notification failed: %s", type(exc).__name__)
                else:
                    current.saved_notified = True
                    reports.store.save_draft(current)
            return web.json_response(await payload(current))

    async def update_card(request):
        async with reports.edit_lock:
            draft = await authorized(request, write=True)
            reports.editable(draft)
            edit = CardEdit.model_validate(await request.json())
            if edit.revision != draft.revision:
                raise UserError("Отчёт изменён в другом окне. Обновите страницу.")
            index = int(request.match_info["index"])
            if index < 0 or index >= len(draft.lines):
                raise UserError("Карточка больше не существует. Обновите страницу.")
            if request.method == "DELETE":
                line = draft.lines[index]
                if draft.mode == "replace" and draft.before.get(line.position_id, 0):
                    line.quantity = number(0)
                    line.deleted = True
                else:
                    draft.lines.pop(index)
            else:
                if edit.line is None:
                    raise UserError("Не переданы данные выбранной карточки.")
                snap = await reports.sheets.snapshot(draft.day)
                item = edit.line
                position = snap.positions.get(item.position_id or "")
                if not position:
                    raise UserError("Выберите позицию из спецификации.")
                value = item.quantity.replace(",", ".").strip() if item.quantity else None
                line = validate_line(
                    Line(
                        phrase=item.phrase or position.name,
                        position_id=position.id,
                        quantity=value or None,
                        unit=position.unit,
                        confirmed_position=True,
                        details=CardDetails(),
                    ),
                    snap.positions,
                )
                if line.quantity is None:
                    raise UserError(line.question or "Укажите выполненный объём.")
                draft.lines[index] = line
            reports.save_edit(draft)
            return web.json_response(await payload(draft))

    async def health(request):
        try:
            reports.store.db.execute("SELECT 1")
            return web.json_response(
                {"status": "ok" if reports.writer_ready else "starting", "database": "postgresql"}
            )
        except Exception:
            return web.json_response({"status": "unavailable"}, status=503)

    async def index(request):
        path = dist / "index.html"
        if not path.is_file():
            return web.Response(
                text="Соберите интерфейс: cd web && npm install && npm run build",
                status=503,
            )
        return web.FileResponse(path)

    app = web.Application(middlewares=[errors], client_max_size=128 * 1024)
    if reports.settings.telegram_mode == "webhook":
        from .telegram_webhook import register_webhook

        register_webhook(app, reports)
    app.router.add_get("/healthz", health)
    app.router.add_get("/api/reports/{id}", get_report)
    app.router.add_get("/api/reports/{id}/avatar", avatar)
    app.router.add_put("/api/reports/{id}", edit_report)
    app.router.add_post("/api/reports/{id}/confirm", confirm_report)
    app.router.add_post("/api/reports/{id}/rewrite", rewrite_report)
    app.router.add_post("/api/reports/{id}/edit-today", edit_today)
    app.router.add_patch("/api/reports/{id}/lines/{index:\\d+}", update_card)
    app.router.add_delete("/api/reports/{id}/lines/{index:\\d+}", update_card)
    if (dist / "assets").is_dir():
        app.router.add_static("/assets/", dist / "assets")
    app.router.add_get("/", index)
    app.router.add_get("/report/{id}", index)
    return app


async def start_web(reports: Reports, bot: Bot | None = None) -> web.AppRunner:
    runner = web.AppRunner(create_app(reports, bot=bot), access_log=None)
    await runner.setup()
    try:
        await web.TCPSite(
            runner,
            reports.settings.web_host,
            reports.settings.web_port,
        ).start()
    except Exception:
        await runner.cleanup()
        raise
    logger.info("Report editor: %s", reports.settings.web_base_url)
    return runner
