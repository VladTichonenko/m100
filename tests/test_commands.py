"""Telegram conversation checks with a fake Bot API, no outgoing messages."""

from datetime import UTC, date, datetime
from html import escape, unescape
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import urlsplit

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.types import CallbackQuery, Chat, Message, Update, User
from aiohttp.test_utils import TestClient, TestServer

from m100_bot.config import Settings
from m100_bot.domain import Line, Position, UserError
from m100_bot.handlers import build_router
from m100_bot.llm import VoiceCommand
from m100_bot.service import Reports
from m100_bot.sheets import Snapshot
from m100_bot.store import Store
from m100_bot.voice import Voice
from m100_bot.web import create_app


def assert_work_links(message, reports, draft=None):
    buttons = [b for row in message.reply_markup.inline_keyboard for b in row]
    labels = [b.text for b in buttons]
    assert labels.count("Открыть отчёт") == labels.count("Открыть таблицу") == 1
    assert next(b for b in buttons if b.text == "Открыть таблицу").url == reports.sheets.url
    opener = next(b for b in buttons if b.text == "Открыть отчёт")
    if draft and reports.settings.web_enabled:
        link = urlsplit(opener.url or opener.web_app.url)
        assert link.path == f"/report/{draft.id}"
        assert reports.store.from_web_token(draft.id, link.fragment.removeprefix("token=")).id == (
            draft.id
        )
    elif not draft:
        assert opener.callback_data == "nav:report"


class TelegramSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.sent = []
        self.documents = []
        self.edited = []

    async def close(self):
        pass

    async def stream_content(self, *args, **kwargs):
        yield b""

    async def make_request(self, bot, method, timeout=None):
        if method.__api_method__ == "editMessageText":
            self.edited.append(method)
            return Message(
                message_id=method.message_id,
                date=datetime.now(UTC),
                chat=Chat(id=7, type="private"),
                text=method.text,
            )
        if method.__api_method__ == "sendDocument":
            self.documents.append(method)
            return Message(
                message_id=1000 + len(self.documents),
                date=datetime.now(UTC),
                chat=Chat(id=7, type="private"),
                caption=method.caption,
            )
        if method.__api_method__ == "sendMessage":
            self.sent.append(method)
            return Message(
                message_id=len(self.sent),
                date=datetime.now(UTC),
                chat=Chat(id=7, type="private"),
                text=method.text,
            ).as_(bot)
        return True


@pytest.fixture
async def conversation(tmp_path, postgres_url):
    settings = Settings(
        _env_file=None, allowed_user_ids="7", web_auto_open=False, database_url=postgres_url
    )
    store = Store(settings.database_dsn)
    p = Position(
        id="cable", row=5, name="Кабель 3х2,5", section="ЭМ", unit="м", planned=100, completed=0
    )
    sheets = SimpleNamespace(
        url="https://docs.google.com/spreadsheets/d/test/edit",
        snapshot=AsyncMock(return_value=Snapshot(0, 80, 702, {p.id: p}, {p.id: 0}, 9, 10, 1)),
        apply=AsyncMock(),
    )
    llm = SimpleNamespace(
        http=None,
        command=AsyncMock(),
        extract=AsyncMock(
            return_value=[
                Line(phrase="кабель 10 метров", position_id=p.id, quantity=10, unit="м"),
            ]
        ),
    )
    reports = Reports(settings, store, sheets, llm, SimpleNamespace(apply=AsyncMock()))
    reports.today = lambda: date(2026, 11, 6)
    session = TelegramSession()
    bot = Bot("123456:TEST", session=session)
    dispatcher = Dispatcher()
    dispatcher.include_router(build_router(reports))
    counter = 0

    async def send(text=None, voice=None):
        nonlocal counter
        counter += 1
        message = Message(
            message_id=counter,
            date=datetime.now(UTC),
            chat=Chat(id=7, type="private"),
            from_user=User(id=7, is_bot=False, first_name="Мастер <Иван>", username="master"),
            text=text,
            voice=voice,
        )
        await dispatcher.feed_update(bot, Update(update_id=counter, message=message))

    async def click(data, text="Список команд"):
        nonlocal counter
        counter += 1
        query = CallbackQuery(
            id=str(counter),
            chat_instance="test",
            from_user=User(id=7, is_bot=False, first_name="Мастер"),
            data=data,
            message=Message(
                message_id=1, date=datetime.now(UTC), chat=Chat(id=7, type="private"), text=text
            ),
        )
        await dispatcher.feed_update(bot, Update(update_id=counter, callback_query=query))

    yield SimpleNamespace(
        reports=reports, llm=llm, send=send, click=click, session=session, bot=bot
    )
    store.close()
    await bot.session.close()


async def test_start_shows_real_telegram_profile_table_and_voice_invitation(conversation):
    c = conversation
    await c.send("/start")
    text = c.session.sent[-1].text
    assert "Ваш профиль" in text and "Мастер &lt;Иван&gt;" in text and "@master" in text
    assert "<code>7</code>" in text and c.reports.sheets.url in text
    assert "голосовую команду" in text
    c.llm.command.assert_not_awaited()


@pytest.mark.parametrize("role", ["admin", "master"])
async def test_only_admin_can_open_user_management(conversation, role):
    c = conversation
    s = c.reports.settings
    s.bitrix_access_enabled = True
    s.bitrix_users_entity_type_id = 1048
    s.bitrix_users_fields = {"telegram_id": "tg", "enabled": "active", "role": "role"}
    s.bitrix_user_roles = {"11": role}
    # Settings assignment is not validated; use a SecretStr exactly as real startup does.
    from pydantic import SecretStr

    s.bitrix_webhook_url = SecretStr("https://portal.example.com/rest/1/secret/")
    c.reports.bitrix.call = AsyncMock(
        return_value={"items": [{"title": "Иван", "tg": "7", "active": "Y", "role": "11"}]}
    )
    await c.send("/users")
    message = c.session.sent[-1]
    if role == "admin":
        assert (
            message.reply_markup.inline_keyboard[0][0].url
            == "https://portal.example.com/crm/type/1048/list/category/0/"
        )
        assert "secret" not in message.text + message.reply_markup.model_dump_json()
    else:
        assert "только администратору" in message.text
        assert_work_links(message, c.reports)
        assert "secret" not in message.reply_markup.model_dump_json()


async def test_voice_edit_shows_transcription_and_remembers_it(conversation, monkeypatch):
    c = conversation
    original = await c.reports.create(7, 7, "кабель 10 метров")
    c.llm.command.return_value = VoiceCommand(
        action="edit",
        edits=[
            {
                "line_number": 1,
                "quantity": 20,
                "position_id": None,
                "remove": False,
            }
        ],
        additions="",
        question="",
    )
    text = "Исправь <первый> пункт на 20 метров"

    async def transcribe(*args):
        assert [method.text for method in c.session.sent] == ["Слушаю"]
        assert c.session.sent[0].reply_markup is None
        assert not c.session.edited
        return text

    async def download(*args, **kwargs):
        assert c.session.sent[0].text == "Слушаю"

    monkeypatch.setattr(Voice, "transcribe", transcribe)
    monkeypatch.setattr(c.bot, "download", download)

    async def plan(*args, **kwargs):
        assert c.session.edited[0].text == (
            f"🎙 Расшифровка:\n\n<blockquote>{escape(text)}</blockquote>"
        )
        return c.llm.command.return_value

    c.llm.command.side_effect = plan
    await c.send(voice={"file_id": "voice", "file_unique_id": "unique", "duration": 3})
    messages = [method.text for method in c.session.sent]
    assert len(messages) == 2 and messages[0] == "Слушаю"
    assert "20 м" in messages[1]
    assert len(c.session.edited) == 1
    assert c.session.edited[0].message_id == 1
    assert c.session.edited[0].parse_mode == "HTML"
    assert c.session.edited[0].reply_markup is None
    assert_work_links(c.session.sent[-1], c.reports, original)
    assert c.reports.store.history(7, 7)[0]["content"] == text
    assert c.session.sent[-1].reply_markup.inline_keyboard[0][0].text == "Открыть отчёт"
    assert c.reports.store.get_draft(original.id, 7).lines[0].quantity == 20
    c.reports.sheets.apply.assert_not_awaited()


async def test_ambiguous_voice_asks_once_without_creating_fake_work(conversation, monkeypatch):
    c = conversation
    c.llm.command.return_value = VoiceCommand(
        action="clarify", edits=[], additions="", question="Какую работу изменить?"
    )
    monkeypatch.setattr(Voice, "transcribe", AsyncMock(return_value="Исправь это"))
    monkeypatch.setattr(c.bot, "download", AsyncMock())
    await c.send(voice={"file_id": "voice", "file_unique_id": "unique", "duration": 3})
    assert len(c.session.sent) == 2
    assert c.session.sent[0].text == "Слушаю"
    assert c.session.edited[0].text == "🎙 Расшифровка:\n\n<blockquote>Исправь это</blockquote>"
    assert c.session.edited[0].reply_markup is None
    assert c.session.sent[1].text == "Какую работу изменить?"
    assert_work_links(c.session.sent[1], c.reports)
    assert c.reports.store.active_draft(7) is None
    assert c.reports.store.history(7, 7)[0]["content"] == "Исправь это"


@pytest.mark.parametrize("spoken", [False, True])
@pytest.mark.parametrize("saved,unresolved", [(False, False), (True, False), (False, True)])
async def test_followup_clarification_uses_current_report_and_keeps_links(
    conversation, monkeypatch, spoken, saved, unresolved
):
    c = conversation
    draft = await c.reports.create(7, 7, "кабель 10 метров")
    if unresolved:
        draft.lines[0].position_id = None
        draft.lines[0].question = "Уточните сечение кабеля."
        c.reports.save_edit(draft)
    if saved:
        await c.reports.confirm(draft)
        draft = c.reports.store.get_draft(draft.id, 7)
    c.llm.command.return_value = VoiceCommand(
        action="clarify", edits=[], additions="", question="Уточните модель наружного блока."
    )
    text = "Так, ещё раз скажи, что надо уточнить?"
    if spoken:
        monkeypatch.setattr(Voice, "transcribe", AsyncMock(return_value=text))
        monkeypatch.setattr(c.bot, "download", AsyncMock())
        await c.send(voice={"file_id": "voice", "file_unique_id": "unique", "duration": 3})
    else:
        await c.send(text)
    assert "Уточните модель" not in c.session.sent[-1].text
    if saved:
        assert "сохранён" in c.session.sent[-1].text
    elif unresolved:
        assert "Уточните сечение кабеля" in c.session.sent[-1].text
    else:
        assert "10 м" in c.session.sent[-1].text
    c.llm.command.assert_not_awaited()
    assert_work_links(c.session.sent[-1], c.reports, draft)
    assert c.reports.store.get_draft(draft.id, 7) == draft


async def test_report_button_without_draft_opens_report_page(conversation):
    c = conversation
    c.llm.command.return_value = VoiceCommand(
        action="clarify", edits=[], additions="", question="Какую работу добавить?"
    )
    await c.send("Добавь это")
    assert_work_links(c.session.sent[-1], c.reports)
    await c.click("nav:report")
    assert "За сегодня пока нет отчёта" in c.session.sent[-1].text
    assert_work_links(c.session.sent[-1], c.reports)
    assert c.reports.store.active_draft(7) is None
    c.reports.sheets.apply.assert_not_awaited()


@pytest.mark.parametrize("action", ["add", "pick", "line", "qty", "position", "candidates"])
async def test_manual_work_prompts_keep_links_and_action_buttons(conversation, action):
    c = conversation
    draft = await c.reports.create(7, 7, "кабель 10 метров")
    if action == "candidates":
        draft.lines[0].candidates = ["cable"]
        c.reports.save_edit(draft)
    await c.click(
        f"d:{draft.id}:{draft.revision}:{'position' if action == 'candidates' else action}:0"
    )
    message = c.session.sent[-1]
    assert_work_links(message, c.reports, draft)
    callbacks = [b.callback_data for row in message.reply_markup.inline_keyboard for b in row]
    if action == "pick":
        assert any(value and ":line:0" in value for value in callbacks)
    elif action == "line":
        assert any(value and ":qty:0" in value for value in callbacks)
    elif action == "candidates":
        assert any(value and ":choose:0:cable" in value for value in callbacks)
    c.reports.sheets.apply.assert_not_awaited()


async def test_invalid_quantity_reply_keeps_report_links(conversation):
    c = conversation
    draft = await c.reports.create(7, 7, "кабель 10 метров")
    draft.action = "qty"
    draft.action_index = 0
    c.reports.save_edit(draft)
    await c.send("-1")
    assert "неотрицательное количество" in c.session.sent[-1].text
    assert_work_links(c.session.sent[-1], c.reports, draft)
    c.reports.sheets.apply.assert_not_awaited()


async def test_save_failure_keeps_retry_and_work_links(conversation):
    c = conversation
    draft = await c.reports.create(7, 7, "кабель 10 метров")
    c.reports.sheets.apply.side_effect = UserError("Временная ошибка Google")
    await c.click(f"d:{draft.id}:{draft.revision}:ok:0")
    message = c.session.sent[-1]
    assert "Временная ошибка Google" in message.text
    assert_work_links(message, c.reports, draft)
    assert message.reply_markup.inline_keyboard[0][0].text == "Повторить сохранение"


async def test_remaining_links_appear_on_every_message(conversation):
    c = conversation
    draft = await c.reports.create(7, 7, "кабель 10 метров")
    positions = c.reports.sheets.snapshot.return_value.positions
    source = positions["cable"]
    for i in range(60):
        position = source.model_copy(
            update={"id": f"work{i}", "name": f"Работа {i}: " + "наружный блок " * 6}
        )
        positions[position.id] = position
    await c.send("/remaining")
    assert len(c.session.sent) > 1
    for message in c.session.sent:
        assert_work_links(message, c.reports, draft)
    c.reports.sheets.apply.assert_not_awaited()


async def test_report_link_without_web_editor_opens_saved_report(conversation):
    c = conversation
    c.reports.settings.web_enabled = False
    draft = await c.reports.create(7, 7, "кабель 10 метров")
    await c.reports.confirm(draft)
    saved = c.reports.store.get_draft(draft.id, 7)
    await c.send("/table")
    buttons = [b for row in c.session.sent[-1].reply_markup.inline_keyboard for b in row]
    opener = next(b for b in buttons if b.text == "Открыть отчёт")
    assert opener.callback_data == f"nav:draft:{draft.id}"
    await c.click(opener.callback_data)
    assert "сохранён" in c.session.sent[-1].text
    assert c.reports.store.get_draft(draft.id, 7) == saved


async def test_no_access_does_not_disclose_work_links(conversation):
    c = conversation
    await c.reports.create(7, 7, "кабель 10 метров")
    c.reports.settings.allowed_user_ids = "99"
    await c.send("/report")
    assert c.session.sent[-1].reply_markup is None
    assert "token=" not in c.session.sent[-1].text


@pytest.mark.parametrize("failure", ["recognition", "download"])
async def test_voice_failure_replaces_listening_status(conversation, monkeypatch, failure):
    c = conversation
    transcribe = AsyncMock(side_effect=UserError("Не удалось разобрать речь."))
    download = AsyncMock(
        side_effect=RuntimeError("private-download-url") if failure == "download" else None
    )
    monkeypatch.setattr(Voice, "transcribe", transcribe)
    monkeypatch.setattr(c.bot, "download", download)
    await c.send(voice={"file_id": "voice", "file_unique_id": "unique", "duration": 3})
    assert [m.text for m in c.session.sent] == ["Слушаю"]
    assert len(c.session.edited) == 1
    error = c.session.edited[0]
    assert error.message_id == 1 and error.parse_mode is None
    assert error.reply_markup is None
    assert "Не удалось" in error.text and "private-download-url" not in error.text
    c.llm.command.assert_not_awaited()
    assert c.reports.store.history(7, 7) == []


async def test_long_voice_transcript_is_shown_in_full(conversation, monkeypatch):
    c = conversation
    text = "Кабель <3×2,5> & 🎙 " * 250
    monkeypatch.setattr(Voice, "transcribe", AsyncMock(return_value=text))
    monkeypatch.setattr(c.bot, "download", AsyncMock())
    c.llm.command.return_value = VoiceCommand(
        action="clarify", edits=[], additions="", question="Какую работу изменить?"
    )
    await c.send(voice={"file_id": "voice", "file_unique_id": "unique", "duration": 180})
    first = c.session.edited[0]
    transcript = [first, *c.session.sent[1:-1]]
    assert first.message_id == 1
    decoded = []
    for m in transcript:
        quoted = m.text.removeprefix("🎙 Расшифровка:\n\n")
        assert quoted.startswith("<blockquote>") and quoted.endswith("</blockquote>")
        decoded.append(unescape(quoted.removeprefix("<blockquote>").removesuffix("</blockquote>")))
        assert m.reply_markup is None
    assert "".join(decoded) == text
    assert all(len(part.encode("utf-16-le")) // 2 + 20 <= 4096 for part in decoded)
    assert all(m.parse_mode == "HTML" for m in transcript)
    assert c.llm.command.call_args.args[0] == text


@pytest.mark.parametrize("extra", [{"duration": 181}, {"file_size": 9 * 1024 * 1024}])
async def test_oversized_voice_is_rejected_without_listening(conversation, monkeypatch, extra):
    c = conversation
    download = AsyncMock()
    monkeypatch.setattr(c.bot, "download", download)
    await c.send(
        voice={"file_id": "voice", "file_unique_id": "unique", "duration": 3, **extra}
    )
    assert len(c.session.sent) == 1 and "Слушаю" not in c.session.sent[0].text
    assert not c.session.edited
    download.assert_not_awaited()
    c.llm.command.assert_not_awaited()


async def test_missing_parameter_is_named_with_voice_and_manual_options(conversation):
    c = conversation
    c.llm.command.return_value = VoiceCommand(
        action="add", edits=[], additions="кабель 15 метров", question=""
    )
    c.llm.extract.return_value = [
        Line(
            phrase="кабель 15 метров",
            quantity=15,
            unit="м",
            question="Не указано сечение кабеля. Какое сечение нужно записать?",
        )
    ]
    await c.send("Запиши кабель 15 метров")
    messages = [method.text for method in c.session.sent]
    assert len(messages) == 1
    clarification = messages[0]
    assert "кабель 15 метров" in clarification
    assert "сечение кабеля" in clarification
    buttons = [button for row in c.session.sent[-1].reply_markup.inline_keyboard for button in row]
    assert [button.text for button in buttons] == ["Открыть отчёт", "Открыть таблицу"]
    c.reports.sheets.apply.assert_not_awaited()


async def test_unclear_edit_preserves_existing_work_and_offers_report(conversation):
    c = conversation
    draft = await c.reports.create(7, 7, "кабель 10 метров")
    c.llm.command.return_value = VoiceCommand(
        action="clarify",
        edits=[],
        additions="",
        question="Какую работу изменить и какой нужен объём?",
    )
    await c.send("Исправь это")
    assert c.reports.store.get_draft(draft.id, 7).lines[0].quantity == 10
    assert any("Какую работу изменить" in method.text for method in c.session.sent)
    assert len(c.session.sent) == 1
    c.reports.sheets.apply.assert_not_awaited()


async def test_voice_parameter_clarification_fixes_same_line_and_keeps_quantity(
    conversation, monkeypatch
):
    c = conversation
    c.llm.extract.return_value = [
        Line(
            phrase="кабель 15 метров",
            quantity=15,
            unit="м",
            question="Не указано сечение кабеля",
            candidates=["cable"],
        )
    ]
    original = await c.reports.create(7, 7, "кабель 15 метров")
    c.llm.command.return_value = VoiceCommand(
        action="edit",
        edits=[
            {
                "line_number": 1,
                "quantity": None,
                "position_id": "cable",
                "remove": False,
            }
        ],
        additions="",
        question="",
    )
    monkeypatch.setattr(
        Voice, "transcribe", AsyncMock(return_value="Уточни кабель: три на два с половиной")
    )
    monkeypatch.setattr(c.bot, "download", AsyncMock())
    await c.send(voice={"file_id": "voice", "file_unique_id": "unique", "duration": 3})
    updated = c.reports.store.get_draft(original.id, 7)
    assert len(updated.lines) == 1
    assert updated.lines[0].position_id == "cable" and updated.lines[0].quantity == 15
    assert not any("Не совсем понял" in method.text for method in c.session.sent)
    c.reports.sheets.apply.assert_not_awaited()


@pytest.mark.parametrize("voice", [False, True])
async def test_save_confirmed_yellow_returns_cells_table_and_remainder(
    conversation, monkeypatch, voice
):
    c = conversation
    draft = await c.reports.create(7, 7, "кабель 10 метров")
    draft.lines.append(Line(phrase="труба", quantity=8, question="Уточните диаметр трубы"))
    c.reports.save_edit(draft)
    c.llm.command.return_value = VoiceCommand(
        action="save_ready",
        edits=[],
        additions="",
        question="",
        highlight="yellow",
    )
    command = (
        "Я сейчас не могу понять, какую таблицу ты вот отредактировал, а те данные, "
        "что уже уточнены, сохрани и пометь их жёлтым. А что надо уточнить, пока не трогай."
    )
    if voice:
        monkeypatch.setattr(Voice, "transcribe", AsyncMock(return_value=command))
        monkeypatch.setattr(c.bot, "download", AsyncMock())
        await c.send(voice={"file_id": "voice", "file_unique_id": "unique", "duration": 3})
    else:
        await c.send(command)
    op = c.reports.sheets.apply.call_args.args[0]
    assert op.highlight == "yellow" and len(op.changes) == 1
    assert c.reports.active_draft(7).lines[0].phrase == "труба"
    assert len(c.session.sent) == 1 + int(voice)
    receipt = c.session.sent[-1]
    assert "жёлтым" in receipt.text
    assert "J5" in receipt.text and "Лист1" in receipt.text
    assert receipt.reply_markup.inline_keyboard[0][0].url.endswith("#gid=0&range=J5")
    assert_work_links(receipt, c.reports, c.reports.active_draft(7))
    assert any("Уточните диаметр трубы" in m.text for m in c.session.sent)
    c.llm.extract.assert_awaited_once()


async def test_table_question_shows_exact_sheet_without_changing_draft(conversation):
    c = conversation
    draft = await c.reports.create(7, 7, "кабель")
    c.llm.command.return_value = VoiceCommand(action="table", edits=[], additions="", question="")
    await c.send("Какую таблицу ты отредактировал?")
    assert c.reports.store.get_draft(draft.id, 7) == draft
    c.reports.sheets.apply.assert_not_awaited()
    assert c.session.sent[-1].reply_markup.inline_keyboard[0][0].url.endswith("#gid=0")


@pytest.mark.parametrize("voice", [True, False])
async def test_format_today_replies_once_without_reopening_unresolved_report(
    conversation, monkeypatch, voice
):
    c = conversation
    first = await c.reports.create(7, 7, "кабель")
    await c.reports.confirm(first)
    c.llm.extract.return_value = [
        Line(phrase="наружные блоки 5 штук", quantity=5, question="Какая модель наружного блока?")
    ]
    pending = await c.reports.create(7, 7, "наружные блоки 5 штук")
    c.llm.command.return_value = VoiceCommand(
        action="format",
        edits=[],
        additions="",
        question="",
        highlight="#0000FF",
        target_scope="today_added",
    )
    text = "Выдели всё, что ты сегодня добавлял, синим"
    if voice:
        monkeypatch.setattr(Voice, "transcribe", AsyncMock(return_value=text))
        monkeypatch.setattr(c.bot, "download", AsyncMock())
        await c.send(voice={"file_id": "voice", "file_unique_id": "unique", "duration": 3})
    else:
        await c.send(text)
    assert len(c.session.sent) == 1 + int(voice)
    assert "синим" in c.session.sent[-1].text
    assert "наружн" not in c.session.sent[-1].text and "черновик" not in c.session.sent[-1].text
    assert c.reports.active_draft(7) == pending
    assert_work_links(c.session.sent[-1], c.reports, pending)
    op = c.reports.sheets.apply.call_args.args[0]
    assert op.kind == "format" and op.cells == ["J5"] and not op.changes


async def test_new_work_with_save_now_is_saved_without_button_or_extra_messages(conversation):
    c = conversation
    c.llm.command.return_value = VoiceCommand(
        action="add",
        edits=[],
        additions="кабель 10 метров",
        question="",
        save_immediately=True,
        highlight="#40E0D0",
    )
    await c.send("Добавь кабель 10 метров и сразу сохрани, отметь бирюзовым")
    assert len(c.session.sent) == 1 and "Сохранил" in c.session.sent[0].text
    c.reports.sheets.apply.assert_awaited_once()
    assert c.reports.sheets.apply.call_args.args[0].highlight == "#40E0D0"
    assert c.reports.active_draft(7) is None


async def test_format_failure_is_one_message_and_does_not_repeat_unrelated_question(conversation):
    from m100_bot.domain import UserError

    c = conversation
    await c.reports.create(7, 7, "кабель")
    c.reports.sheets.apply.side_effect = UserError("Временная ошибка Google")
    c.llm.command.return_value = VoiceCommand(
        action="format",
        edits=[],
        additions="",
        question="",
        highlight="#800080",
        target_scope="cells",
        cells=["B5"],
    )
    await c.send("Выдели B5 фиолетовым")
    assert len(c.session.sent) == 1 and "Временная ошибка Google" in c.session.sent[0].text


async def test_context_passes_previous_command_and_result_to_next_request(conversation):
    c = conversation
    c.llm.command.return_value = VoiceCommand(
        action="format",
        edits=[],
        additions="",
        question="",
        highlight="#008000",
        target_scope="cells",
        cells=["B5:C5"],
    )
    await c.send("Выдели B5:C5 зелёным")
    c.llm.command.return_value = VoiceCommand(
        action="format",
        edits=[],
        additions="",
        question="",
        highlight="#0000FF",
        target_scope="last_selection",
    )
    await c.send("Теперь их синим")
    context = c.llm.command.call_args.kwargs["context"]
    assert context["history"][0]["content"] == "Выдели B5:C5 зелёным"
    assert "зелёным" in context["history"][1]["content"]
    assert context["recent_operations"][0]["cells"] == ["B5", "C5"]
    assert c.reports.sheets.apply.call_args.args[0].cells == ["B5", "C5"]
    assert len(c.session.sent) == 2


@pytest.mark.parametrize(
    "new_request", ["давай запишем новые работы", "давай запищем новые работы"]
)
async def test_browser_clarification_and_save_are_authoritative_for_bot(
    conversation, tmp_path, new_request
):
    c = conversation
    c.llm.extract.return_value = [
        Line(phrase="кабель", quantity=10, candidates=["cable"], question="Какое сечение?")
    ]
    draft = await c.reports.create(7, 7, "кабель десять метров")
    draft.action = "position"
    draft.action_index = 0
    c.reports.save_edit(draft)
    c.reports.store.remember(7, 7, "assistant", "Какое сечение кабеля? Нужно уточнить работу.")
    headers = {"Authorization": "Bearer " + c.reports.store.web_token(draft)}
    url = f"/api/reports/{draft.id}"
    c.llm.command.return_value = VoiceCommand(
        action="clarify", edits=[], additions="", question="Какие новые работы выполнены?"
    )
    async with TestClient(TestServer(create_app(c.reports, tmp_path, c.bot))) as client:
        response = await client.patch(
            url + "/lines/0",
            headers=headers,
            json={
                "revision": draft.revision,
                "line": {"position_id": "cable", "quantity": "12,5", "phrase": "Кабель"},
            },
        )
        assert response.status == 200
        updated = await response.json()
        current = c.reports.store.get_draft(draft.id, 7)
        assert current.action == "" and current.action_index == -1
        await c.reports.plan_command(7, 7, "Проверь текущие работы")
        context = c.llm.command.call_args.kwargs["context"]
        state = context["report_state"]["active_report"]
        assert state["id"] == draft.id and state["revision"] == updated["draft"]["revision"]
        assert state["lines"][0]["quantity"] == "12.5"
        assert state["lines"][0]["question"] == ""
        assert state["lines"][0]["position_id"] == "cable"
        assert "обновлено на сайте" in context["history"][-1]["content"]

        body = {
            "revision": current.revision,
            "lines": [{"position_id": "cable", "quantity": "12.5", "phrase": "Кабель"}],
        }
        for _ in range(2):
            response = await client.post(url + "/confirm", headers=headers, json=body)
            assert response.status == 200
        assert c.reports.active_draft(7) is None
        await c.reports.plan_command(7, 7, "Проверь сохранённый отчёт")
        context = c.llm.command.call_args.kwargs["context"]
        assert context["draft"]["exists"] is False
        assert context["draft"]["status"] == "none"
        assert context["report_state"]["active_report"] is None
        saved = context["report_state"]["latest_saved_report"]
        assert saved["id"] == draft.id and saved["status"] == "done"
        assert saved["lines"][0]["quantity"] == "12.5"
        assert saved["lines"][0]["question"] == ""
        assert '"status": "done"' in context["history"][-1]["content"]
        c.reports.sheets.apply.assert_awaited_once()

    c.llm.command.reset_mock()
    c.llm.extract.reset_mock()
    await c.send(new_request)
    assert "Предыдущий отчёт уже сохранён" in c.session.sent[-1].text
    assert "какие новые работы" in c.session.sent[-1].text
    assert "сечение" not in c.session.sent[-1].text
    c.llm.command.assert_not_awaited()
    c.llm.extract.assert_not_awaited()
    assert c.reports.active_draft(7) is None
    assert_work_links(c.session.sent[-1], c.reports, c.reports.store.get_draft(draft.id, 7))

    await c.send("Так, ещё раз скажи, что надо уточнить?")
    assert "сохранён" in c.session.sent[-1].text
    assert "сечение" not in c.session.sent[-1].text
    c.llm.command.assert_not_awaited()

    c.llm.command.return_value = VoiceCommand(
        action="add", edits=[], additions="кабель 3х2,5 4 метра", question=""
    )
    c.llm.extract.return_value = [
        Line(phrase="кабель 3х2,5 4 метра", position_id="cable", quantity=4, unit="м")
    ]
    await c.send("Добавь кабель 3х2,5 4 метра")
    new = c.reports.active_draft(7)
    assert new.id != draft.id and new.mode == "add"
    assert len(new.lines) == 1 and new.lines[0].quantity == 4
    c.reports.sheets.apply.assert_awaited_once()


async def test_starting_new_work_keeps_a_genuine_pending_report(conversation):
    c = conversation
    draft = await c.reports.create(7, 7, "кабель 10 метров")
    before = c.reports.store.get_draft(draft.id, 7)
    await c.send("давай запишем новые работы")
    assert "текущему несохранённому отчёту" in c.session.sent[-1].text
    assert c.reports.store.get_draft(draft.id, 7) == before
    c.llm.command.assert_not_awaited()
    c.reports.sheets.apply.assert_not_awaited()


async def test_today_command_opens_personal_account_totals_instead_of_shared_sheet(conversation):
    c = conversation
    draft = await c.reports.create(7, 7, "кабель 10 метров")
    await c.reports.confirm(draft)
    c.reports.sheets.snapshot.return_value.day_values["cable"] = 13
    await c.send("/today")
    current = c.reports.active_draft(7)
    assert current.id != draft.id and current.lines[0].quantity == 10
    assert current.before["cable"] == 13 and current.account_before["cable"] == 10
    assert_work_links(c.session.sent[-1], c.reports, current)
    c.llm.command.assert_not_awaited()
    c.reports.sheets.apply.assert_awaited_once()


async def test_format_command_works_while_manual_quantity_prompt_is_open(conversation):
    c = conversation
    draft = await c.reports.create(7, 7, "кабель")
    draft.action = "qty"
    draft.action_index = 0
    c.reports.save_edit(draft)
    c.llm.command.return_value = VoiceCommand(
        action="format",
        edits=[],
        additions="",
        question="",
        highlight="#123456",
        target_scope="cells",
        cells=["B5"],
    )
    await c.send("Выдели B5 цветом #123456")
    assert len(c.session.sent) == 1 and "Выделил" in c.session.sent[0].text
    assert c.reports.active_draft(7) == draft


async def test_bot_save_receipt_marks_web_notification_as_already_delivered(conversation):
    c = conversation
    c.llm.command.return_value = VoiceCommand(
        action="add", edits=[], additions="кабель 10 метров", question="", save_immediately=True
    )
    await c.send("добавь кабель 10 метров и сразу сохрани")
    op = c.reports.sheets.apply.call_args.args[0]
    assert c.reports.store.get_draft(op.draft_id, 7).saved_notified
    assert len(c.session.sent) == 1


@pytest.mark.parametrize("voice", [False, True])
async def test_table_question_answers_once_with_values_and_leaves_draft_untouched(
    conversation, monkeypatch, voice
):
    from m100_bot.queries import TableQuery

    c = conversation
    pending = await c.reports.create(7, 7, "кабель 10 метров")
    c.reports.sheets.snapshot.reset_mock()
    c.llm.extract.reset_mock()
    c.llm.command.return_value = VoiceCommand(
        action="query",
        edits=[],
        additions="",
        question="",
        position_ids=["cable"],
        query=TableQuery(metrics=["planned", "remaining"]),
    )
    text = "Сколько кабеля по смете и сколько осталось?"
    if voice:
        monkeypatch.setattr(Voice, "transcribe", AsyncMock(return_value=text))
        monkeypatch.setattr(c.bot, "download", AsyncMock())
        await c.send(voice={"file_id": "voice", "file_unique_id": "unique", "duration": 3})
    else:
        await c.send(text)
    assert len(c.session.sent) == 1 + int(voice) and not c.session.documents
    assert "100 м" in c.session.sent[-1].text
    assert "Открыть источник" == c.session.sent[-1].reply_markup.inline_keyboard[0][0].text
    assert_work_links(c.session.sent[-1], c.reports, pending)
    assert c.reports.active_draft(7) == pending
    c.reports.sheets.snapshot.assert_awaited_once()
    c.reports.sheets.apply.assert_not_awaited()
    c.llm.extract.assert_not_awaited()


async def test_large_table_query_sends_single_document_instead_of_message_spam(conversation):
    from m100_bot.queries import TableQuery

    c = conversation
    snap = c.reports.sheets.snapshot.return_value
    source = snap.positions["cable"]
    for i in range(20):
        p = source.model_copy(update={"id": f"work{i}", "row": i + 10, "name": f"Работа {i}"})
        snap.positions[p.id] = p
        snap.day_values[p.id] = 0
    c.llm.command.return_value = VoiceCommand(
        action="query", edits=[], additions="", question="", query=TableQuery(metrics=["remaining"])
    )
    await c.send("Покажи остатки по всем работам")
    assert len(c.session.documents) == 1 and len(c.session.sent) == 0
    document = c.session.documents[0]
    assert_work_links(document, c.reports)
    assert document.document.filename.endswith(".csv") and b"work19" in document.document.data
    assert len(document.caption) < 1024
    assert c.reports.active_draft(7) is None
    c.reports.sheets.apply.assert_not_awaited()


async def test_query_does_not_cancel_or_change_previous_days_draft(conversation):
    from datetime import timedelta

    from m100_bot.queries import TableQuery

    c = conversation
    draft = await c.reports.create(7, 7, "кабель 10 метров")
    draft.day -= timedelta(days=1)
    c.reports.store.save_draft(draft)
    c.llm.command.return_value = VoiceCommand(
        action="query", edits=[], additions="", question="", query=TableQuery(metrics=["remaining"])
    )
    await c.send("Сколько осталось кабеля?")
    assert len(c.session.sent) == 1 and "100 м" in c.session.sent[0].text
    assert c.reports.store.get_draft(draft.id, 7) == draft
    c.reports.sheets.apply.assert_not_awaited()


@pytest.mark.parametrize("spoken", [False, True])
@pytest.mark.parametrize(
    ("phrase", "expected"),
    [
        ("Перезапусти бота", "Ваш профиль"),
        ("Открой мой отчёт", "Ваш отчёт"),
        ("Открой таблицу", "Рабочая таблица"),
        ("Покажи список команд", "Выберите раздел"),
        ("Покажи мои черновики", "Мои черновики"),
    ],
)
async def test_main_menu_has_working_text_and_voice_routes(
    conversation, monkeypatch, spoken, phrase, expected
):
    c = conversation
    if spoken:
        monkeypatch.setattr(Voice, "transcribe", AsyncMock(return_value=phrase))
        monkeypatch.setattr(c.bot, "download", AsyncMock())
        await c.send(voice={"file_id": "voice", "file_unique_id": "unique", "duration": 3})
    else:
        await c.send(phrase)
    assert len(c.session.sent) == 1 + int(spoken)
    assert expected in c.session.sent[-1].text
    c.llm.command.assert_not_awaited()
    c.reports.sheets.apply.assert_not_awaited()


async def test_restart_keeps_draft_and_shows_five_buttons(conversation):
    c = conversation
    draft = await c.reports.create(7, 7, "кабель 10 метров")
    draft.action = "position"
    c.reports.store.save_draft(draft)
    await c.send("/restart")
    restored = c.reports.store.get_draft(draft.id, 7)
    assert restored.status == "draft" and restored.lines == draft.lines
    assert restored.action == ""
    buttons = [b.text for row in c.session.sent[-1].reply_markup.keyboard for b in row]
    assert len(buttons) == 5
    assert any("Список команд" in b for b in buttons)
    c.reports.sheets.apply.assert_not_awaited()


@pytest.mark.parametrize("section", ["work", "save", "read", "color", "navigation"])
async def test_help_sections_show_voice_quotes_and_back_button(conversation, section):
    c = conversation
    await c.click(f"help:{section}")
    page = c.session.edited[-1]
    assert "<blockquote>" in page.text and len(page.text) < 4000
    assert page.reply_markup.inline_keyboard[0][0].callback_data == "help:overview"
    assert not c.session.sent
    c.llm.command.assert_not_awaited()


async def test_spoken_help_does_not_save_or_remove_work(conversation):
    c = conversation
    draft = await c.reports.create(7, 7, "кабель 10 метров")
    await c.send("Как удалить работу?")
    assert "<blockquote>Удали второй пункт.</blockquote>" in c.session.sent[-1].text
    assert c.reports.store.get_draft(draft.id, 7) == draft
    c.reports.sheets.apply.assert_not_awaited()


async def test_drafts_list_filters_owner_and_status_and_checks_opening(conversation):
    from uuid import uuid4

    c = conversation
    own = await c.reports.create(7, 7, "кабель 10 метров")
    foreign = own.model_copy(update={"id": uuid4().hex, "user_id": 99})
    saved = own.model_copy(update={"id": uuid4().hex, "status": "done"})
    c.reports.store.save_draft(foreign)
    c.reports.store.save_draft(saved)
    await c.send("/drafts")
    markup = c.session.sent[-1].reply_markup.model_dump_json()
    assert own.id in markup and foreign.id not in markup and saved.id not in markup
    await c.click(f"nav:draft:{foreign.id}")
    assert "Черновик не найден" in c.session.sent[-1].text
    c.reports.sheets.apply.assert_not_awaited()
