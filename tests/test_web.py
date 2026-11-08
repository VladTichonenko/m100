from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp.test_utils import TestClient, TestServer

from m100_bot.config import Settings
from m100_bot.domain import Line, Position, UserError
from m100_bot.service import Reports
from m100_bot.sheets import Snapshot
from m100_bot.store import Store
from m100_bot.ui import show_draft
from m100_bot.web import create_app


@pytest.fixture
async def editor(tmp_path, postgres_url):
    settings = Settings(
        _env_file=None,
        allowed_user_ids="7",
        database_url=postgres_url,
        bitrix_enabled=True,
        web_auto_open=False,
    )
    store = Store(settings.database_dsn)
    quantities = {"cable": Decimal(5), "clamp": Decimal(3)}
    positions = {
        "cable": Position(
            id="cable",
            row=5,
            name="Кабель 3х2,5",
            section="ЭМ3",
            unit="м",
            planned=100,
            completed=20,
        ),
        "clamp": Position(
            id="clamp",
            row=6,
            name="Хомут",
            section="ОВ3",
            unit="шт",
            planned=100,
            completed=10,
        ),
    }

    async def snapshot(day):
        return Snapshot(0, 80, 702, positions, quantities.copy(), 9, 10, 1)

    async def apply(op):
        for change in op.changes:
            quantities[change.position_id] = change.after

    sheets = SimpleNamespace(
        url="https://docs.google.com/spreadsheets/d/test/edit",
        snapshot=snapshot,
        apply=AsyncMock(side_effect=apply),
    )
    llm = SimpleNamespace(
        extract=AsyncMock(
            return_value=[
                Line(phrase="кабель десять метров", position_id="cable", quantity=10, unit="м"),
            ]
        )
    )
    reports = Reports(settings, store, sheets, llm, SimpleNamespace(apply=AsyncMock()))
    reports.today = lambda: date(2026, 11, 5)
    draft = await reports.create(7, 7, "кабель десять метров")
    token = store.web_token(draft)
    bot = SimpleNamespace(send_message=AsyncMock())
    client = TestClient(TestServer(create_app(reports, tmp_path / "dist", bot=bot)))
    await client.start_server()
    yield SimpleNamespace(
        reports=reports,
        client=client,
        draft=draft,
        quantities=quantities,
        bot=bot,
        url=f"/api/reports/{draft.id}",
        headers={"Authorization": f"Bearer {token}"},
        edit={
            "revision": 0,
            "lines": [
                {"position_id": "cable", "quantity": "2,5", "phrase": "кабель"},
            ],
        },
    )
    await client.close()
    store.close()


async def test_personal_link_is_required_and_scoped_to_one_report(editor):
    e = editor
    for headers in ({}, {"Authorization": "Bearer invented"}):
        assert (await e.client.get(e.url, headers=headers)).status == 401
    assert (await e.client.get("/api/reports/another", headers=e.headers)).status == 401
    response = await e.client.get(e.url, headers=e.headers)
    data = await response.json()
    assert response.status == 200
    assert data["draft"]["transcript"] == "кабель десять метров"
    assert "user_id" not in data["draft"]
    assert response.headers["Cache-Control"] == "no-store"
    e.reports.settings.allowed_user_ids = "8"
    assert (await e.client.get(e.url, headers=e.headers)).status == 403


async def test_edits_save_draft_only_and_stale_revision_is_rejected(editor):
    e = editor
    response = await e.client.put(e.url, headers=e.headers, json=e.edit)
    assert response.status == 200
    assert (await response.json())["draft"]["lines"][0]["quantity"] == "2.5"
    e.reports.sheets.apply.assert_not_awaited()
    response = await e.client.put(e.url, headers=e.headers, json=e.edit)
    assert response.status == 409
    assert "другом окне" in (await response.json())["error"]


async def test_confirmation_and_retry_do_not_double_add(editor):
    e = editor
    for _ in range(2):
        response = await e.client.post(e.url + "/confirm", headers=e.headers, json=e.edit)
        assert response.status == 200
        assert (await response.json())["draft"]["status"] == "done"
    assert e.quantities["cable"] == Decimal("7.5")
    e.reports.sheets.apply.assert_awaited_once()
    e.reports.bitrix.apply.assert_awaited_once()


async def test_deleting_all_daily_rows_writes_zero_in_replace_mode(editor):
    e = editor
    e.reports.cancel(e.draft)
    draft = await e.reports.create(7, 7)
    headers = {"Authorization": "Bearer " + e.reports.store.web_token(draft)}
    response = await e.client.post(
        f"/api/reports/{draft.id}/confirm",
        headers=headers,
        json={"revision": 0, "lines": []},
    )
    assert response.status == 200
    assert e.quantities == {"cable": 0, "clamp": 0}


async def test_partial_failure_returns_persistent_state_and_can_resume(editor):
    e = editor
    e.reports.bitrix.apply.side_effect = [UserError("Битрикс недоступен"), None]
    response = await e.client.post(e.url + "/confirm", headers=e.headers, json=e.edit)
    assert response.status == 409
    data = await response.json()
    assert data["operation"]["sheets_done"]
    assert data["draft"]["status"] == "saving"
    assert (await e.client.put(e.url, headers=e.headers, json=e.edit)).status == 409
    response = await e.client.post(e.url + "/confirm", headers=e.headers, json=e.edit)
    assert response.status == 200
    assert e.quantities["cable"] == Decimal("7.5")
    e.reports.sheets.apply.assert_awaited_once()


@pytest.mark.parametrize("quantity", ["NaN", "Infinity", "-1", "abc"])
async def test_bad_quantities_cannot_be_written(editor, quantity):
    e = editor
    e.edit["lines"][0]["quantity"] = quantity
    assert (await e.client.post(e.url + "/confirm", headers=e.headers, json=e.edit)).status == 400
    e.reports.sheets.apply.assert_not_awaited()


async def test_ambiguous_and_fractional_piece_quantities_block_confirmation(editor):
    e = editor
    e.edit["lines"] = [{"position_id": "clamp", "quantity": "2.5", "phrase": "хомут"}]
    response = await e.client.post(e.url + "/confirm", headers=e.headers, json=e.edit)
    assert response.status == 409
    data = await response.json()
    assert data["draft"]["lines"][0]["quantity"] is None
    assert data["draft"]["revision"] == 1
    e.reports.sheets.apply.assert_not_awaited()


async def test_unknown_position_and_empty_report_are_not_written(editor):
    e = editor
    e.edit["lines"][0]["position_id"] = "invented"
    assert (await e.client.post(e.url + "/confirm", headers=e.headers, json=e.edit)).status == 409
    e.edit["lines"] = []
    assert (await e.client.post(e.url + "/confirm", headers=e.headers, json=e.edit)).status == 409
    e.reports.sheets.apply.assert_not_awaited()


async def test_expired_links_are_rejected(editor):
    e = editor
    e.reports.store.db.execute("UPDATE web_links SET expires_at=0")
    assert (await e.client.get(e.url, headers=e.headers)).status == 401


async def test_static_editor_has_spa_fallback_without_exposing_local_files(editor, tmp_path):
    e = editor
    # No build in the test directory: give an actionable response, no directory listing.
    assert (await e.client.get("/report/example")).status == 503
    assert (await e.client.get("/.env")).status == 404


async def test_bot_reply_contains_clickable_personal_report_link(editor):
    e = editor
    message = SimpleNamespace(answer=AsyncMock())
    await show_draft(message, e.draft, e.reports)
    args, kwargs = message.answer.call_args
    message.answer.assert_awaited_once()
    assert "Черновик" in args[0]
    button = kwargs["reply_markup"].inline_keyboard[0][0]
    assert button.text == "Открыть отчёт"
    assert button.url.startswith("http://127.0.0.1:8765/report/")
    assert "#token=" in button.url
    assert [b.text for row in kwargs["reply_markup"].inline_keyboard for b in row] == [
        "Открыть отчёт",
        "Открыть таблицу",
    ]


async def test_card_edits_cannot_override_reference_fields_and_preserve_other_cards(editor):
    e = editor
    other = Line(phrase="хомуты", position_id="clamp", quantity=3, unit="шт")
    e.draft.lines.append(other)
    e.reports.store.save_draft(e.draft)
    response = await e.client.patch(
        e.url + "/lines/0",
        headers=e.headers,
        json={
            "revision": 0,
            "line": {
                "position_id": "cable",
                "quantity": "12,5",
                "phrase": "кабель",
                "details": {
                    "name": "Новое название только в карточке",
                    "unit": "кг",
                    "planned": "120",
                    "unit_pay": "3",
                    "planned_pay": "360",
                    "remaining": "87.5",
                    "completed_pay": "97.5",
                },
            },
        },
    )
    assert response.status == 200
    data = await response.json()
    edited, untouched = data["draft"]["lines"]
    assert edited["quantity"] == "12.5"
    assert edited["details"]["unit_pay"] is None
    assert edited["details"]["name"] is None
    assert untouched == other.model_dump(mode="json")
    assert next(p for p in data["catalog"] if p["id"] == "cable")["name"] == "Кабель 3х2,5"
    e.reports.sheets.apply.assert_not_awaited()
    stale = await e.client.patch(e.url + "/lines/0", headers=e.headers, json={"revision": 0})
    assert stale.status == 409


async def test_card_deletion_leaves_other_cards_untouched(editor):
    e = editor
    e.draft.lines.append(Line(phrase="хомуты", position_id="clamp", quantity=3, unit="шт"))
    e.reports.store.save_draft(e.draft)
    response = await e.client.delete(e.url + "/lines/0", headers=e.headers, json={"revision": 0})
    assert response.status == 200
    lines = (await response.json())["draft"]["lines"]
    assert len(lines) == 1 and lines[0]["position_id"] == "clamp"
    e.reports.sheets.apply.assert_not_awaited()


async def test_deleted_daily_card_is_hidden_but_zeroed_on_confirmation(editor):
    e = editor
    e.reports.cancel(e.draft)
    draft = await e.reports.create(7, 7)
    headers = {"Authorization": "Bearer " + e.reports.store.web_token(draft)}
    url = f"/api/reports/{draft.id}"
    response = await e.client.delete(url + "/lines/0", headers=headers, json={"revision": 0})
    assert response.status == 200
    updated = (await response.json())["draft"]
    assert updated["lines"][0]["deleted"]
    response = await e.client.post(
        url + "/confirm",
        headers=headers,
        json={
            "revision": updated["revision"],
            "lines": [
                {k: line[k] for k in ("position_id", "quantity", "phrase", "details", "deleted")}
                for line in updated["lines"]
            ],
        },
    )
    assert response.status == 200
    assert e.quantities == {"cable": 0, "clamp": 3}


async def test_rewrite_invites_voice_once_without_writing_or_deleting_draft(editor):
    e = editor
    for _ in range(2):
        response = await e.client.post(e.url + "/rewrite", headers=e.headers, json={"revision": 0})
        assert response.status == 200
        data = await response.json()
        assert data["rewrite_requested"]
        assert data["draft"]["lines"] == [line.model_dump(mode="json") for line in e.draft.lines]
    e.bot.send_message.assert_awaited_once()
    assert "Давайте перезапишем" in e.bot.send_message.call_args.args[1]
    e.reports.sheets.apply.assert_not_awaited()
    assert e.reports.store.active_draft(7).action == "voice"


async def test_rewrite_does_not_close_on_delivery_failure_and_can_retry(editor):
    e = editor
    e.bot.send_message.side_effect = [RuntimeError("offline"), None]
    response = await e.client.post(e.url + "/rewrite", headers=e.headers, json={"revision": 0})
    assert response.status == 500
    assert e.reports.store.active_draft(7).revision == 0
    response = await e.client.post(e.url + "/rewrite", headers=e.headers, json={"revision": 0})
    assert response.status == 200


async def test_rewrite_requires_auth_current_revision_and_editable_report(editor):
    e = editor
    assert (await e.client.post(e.url + "/rewrite", json={"revision": 0})).status == 401
    assert (
        await e.client.post(e.url + "/rewrite", headers=e.headers, json={"revision": 2})
    ).status == 409
    await e.reports.confirm(e.draft)
    assert (
        await e.client.post(e.url + "/rewrite", headers=e.headers, json={"revision": 0})
    ).status == 409
    e.bot.send_message.assert_not_awaited()


async def test_web_save_notifies_single_owner_once_even_after_retry(editor):
    e = editor
    e.draft.chat_id = 999  # Browser visitor/chat metadata does not select the recipient.
    e.reports.store.save_draft(e.draft)
    for _ in range(2):
        response = await e.client.post(e.url + "/confirm", headers=e.headers, json=e.edit)
        assert response.status == 200
        assert not (await response.json())["notification_pending"]
    e.bot.send_message.assert_awaited_once()
    assert e.bot.send_message.call_args.args[0] == 7
    assert "сохранён" in e.bot.send_message.call_args.args[1]
    e.reports.sheets.apply.assert_awaited_once()


async def test_notification_failure_does_not_undo_saved_report_or_duplicate_write(editor):
    e = editor
    e.bot.send_message.side_effect = [RuntimeError("offline"), None]
    response = await e.client.post(e.url + "/confirm", headers=e.headers, json=e.edit)
    data = await response.json()
    assert response.status == 200 and data["draft"]["status"] == "done"
    assert data["notification_pending"]
    response = await e.client.post(e.url + "/confirm", headers=e.headers, json=e.edit)
    assert not (await response.json())["notification_pending"]
    e.reports.sheets.apply.assert_awaited_once()


async def test_https_result_opens_as_telegram_mini_app(editor):
    e = editor
    e.reports.settings.web_base_url = "https://reports.example.com"
    message = SimpleNamespace(answer=AsyncMock())
    await show_draft(message, e.draft, e.reports)
    button = message.answer.call_args.kwargs["reply_markup"].inline_keyboard[0][0]
    assert button.web_app.url.startswith("https://reports.example.com/report/")
    assert button.url is None
    assert [
        b.text
        for row in message.answer.call_args.kwargs["reply_markup"].inline_keyboard
        for b in row
    ] == [
        "Открыть отчёт",
        "Открыть таблицу",
    ]


async def test_web_editing_partial_remainder_preserves_saved_cells(editor):
    e = editor
    e.reports.cancel(e.draft)
    draft = await e.reports.create(7, 7)
    draft.lines[0].quantity = Decimal(2)
    draft.lines[1].quantity = None
    draft.lines[1].question = "Уточните количество хомутов"
    e.reports.save_edit(draft)
    op = await e.reports.confirm(draft, ready_only=True, highlight="yellow")
    remaining = e.reports.active_draft(7)
    headers = {"Authorization": "Bearer " + e.reports.store.web_token(remaining)}
    response = await e.client.post(
        f"/api/reports/{remaining.id}/confirm",
        headers=headers,
        json={
            "revision": 0,
            "lines": [{"position_id": "clamp", "quantity": "4", "phrase": "Хомут"}],
        },
    )
    assert response.status == 200
    assert e.quantities == {"cable": 2, "clamp": 4}
    old_headers = {"Authorization": "Bearer " + e.reports.store.web_token(draft)}
    result = await (await e.client.get(f"/api/reports/{draft.id}", headers=old_headers)).json()
    assert result["table_url"] == op.table_url
    assert len(result["draft"]["lines"]) == 1


async def test_daily_dashboard_uses_saved_totals_and_safe_bitrix_link(editor):
    from pydantic import SecretStr

    e = editor
    e.reports.settings.bitrix_webhook_url = SecretStr(
        "https://portal.example.com/rest/private-user/private-key/"
    )
    e.reports.settings.bitrix_entity_type_id = 123
    data = await (await e.client.get(e.url, headers=e.headers)).json()
    assert data["today"]["day"] == "2026-11-05"
    assert [
        (line["position_id"], line["quantity"], line["unit"]) for line in data["today"]["lines"]
    ] == [
        ("cable", "5", "м"),
        ("clamp", "3", "шт"),
    ]
    assert data["today"]["total_pay"] is None
    assert data["bitrix_url"] == "https://portal.example.com/crm/type/123/list/category/0/"
    assert "private-key" not in str(data)
    assert data["timezone"] == "Europe/Minsk"
    e.reports.sheets.apply.assert_not_awaited()


async def test_daily_dashboard_refreshes_after_save_without_adding_draft_twice(editor):
    e = editor
    response = await e.client.post(e.url + "/confirm", headers=e.headers, json=e.edit)
    data = await response.json()
    assert response.status == 200
    assert (
        next(line for line in data["today"]["lines"] if line["position_id"] == "cable")["quantity"]
        == "7.5"
    )


async def test_telegram_profile_and_avatar_are_scoped_to_authorized_report(editor):
    e = editor
    e.bot.get_chat = AsyncMock(
        return_value=SimpleNamespace(
            first_name="Иван",
            last_name="Мастер",
            username="ivan_master",
            photo=SimpleNamespace(small_file_id="private-telegram-file-id"),
        )
    )

    async def download(file_id, destination):
        assert file_id == "private-telegram-file-id"
        destination.write(b"jpeg-content")

    e.bot.download = AsyncMock(side_effect=download)
    assert (await e.client.get(e.url + "/avatar")).status == 401
    data = await (await e.client.get(e.url, headers=e.headers)).json()
    assert data["profile"] == {
        "name": "Иван Мастер",
        "username": "ivan_master",
        "url": "https://t.me/ivan_master",
        "has_photo": True,
    }
    assert "private-telegram-file-id" not in str(data)
    avatar = await e.client.get(e.url + "/avatar", headers=e.headers)
    assert avatar.status == 200 and await avatar.read() == b"jpeg-content"
    assert avatar.content_type == "image/jpeg"
    e.bot.get_chat.assert_awaited_once_with(7)


async def test_missing_profile_photo_does_not_block_report(editor):
    e = editor
    e.reports.store.save_profile(7, {"name": "Мастер", "username": None})
    data = await (await e.client.get(e.url, headers=e.headers)).json()
    assert data["profile"]["name"] == "Мастер"
    assert not data["profile"]["has_photo"]
    assert data["profile"]["url"] is None
    assert (await e.client.get(e.url + "/avatar", headers=e.headers)).status == 404


async def test_bitrix_revocation_blocks_existing_report_token_and_all_mutations(editor):
    e = editor
    s = e.reports.settings
    s.bitrix_access_enabled = True
    s.bitrix_users_entity_type_id = 1048
    s.bitrix_users_fields = {"telegram_id": "tg", "enabled": "active", "role": "role"}
    s.bitrix_user_roles = {"12": "master"}
    e.reports.bitrix.call = AsyncMock(
        return_value={"items": [{"title": "Иван", "tg": "7", "active": "Y", "role": "12"}]}
    )
    assert (await e.client.get(e.url, headers=e.headers)).status == 200
    e.reports.bitrix.call.return_value["items"][0]["active"] = "N"
    for method, suffix in [
        ("get", ""),
        ("get", "/avatar"),
        ("put", ""),
        ("post", "/confirm"),
        ("post", "/rewrite"),
        ("post", "/edit-today"),
        ("delete", "/lines/0"),
    ]:
        response = await getattr(e.client, method)(e.url + suffix, headers=e.headers, json=e.edit)
        assert response.status == 403, (method, suffix, await response.text())
    e.reports.sheets.apply.assert_not_awaited()
    e.bot.send_message.assert_not_awaited()


async def test_service_confirm_checks_revoked_access_before_google(editor):
    e = editor
    e.reports.settings.bitrix_access_enabled = True
    e.reports.settings.bitrix_users_entity_type_id = 1048
    e.reports.settings.bitrix_users_fields = {
        "telegram_id": "tg",
        "enabled": "active",
        "role": "role",
    }
    e.reports.bitrix.call = AsyncMock(return_value={"items": []})
    with pytest.raises(UserError, match="Доступ закрыт"):
        await e.reports.confirm(e.draft)
    e.reports.sheets.apply.assert_not_awaited()


async def test_bitrix_member_notification_goes_to_own_chat_not_local_owner(editor):
    e = editor
    s = e.reports.settings
    s.allowed_user_ids = "99"
    s.bitrix_access_enabled = True
    s.bitrix_users_entity_type_id = 1048
    s.bitrix_users_fields = {"telegram_id": "tg", "enabled": "active", "role": "role"}
    s.bitrix_user_roles = {"12": "master"}
    e.reports.bitrix.call = AsyncMock(
        return_value={"items": [{"title": "Иван", "tg": "7", "active": "Y", "role": "12"}]}
    )
    draft = e.reports.store.get_draft(e.draft.id, 7)
    draft.chat_id = 123
    e.reports.store.save_draft(draft)
    response = await e.client.post(e.url + "/confirm", headers=e.headers, json=e.edit)
    assert response.status == 200
    assert e.bot.send_message.call_args.args[0] == 123


async def test_preview_exposes_overrun_before_any_write(editor):
    e = editor
    draft = e.reports.store.get_draft(e.draft.id, 7)
    draft.lines[0].quantity = Decimal(90)
    e.reports.store.save_draft(draft)
    data = await (await e.client.get(e.url, headers=e.headers)).json()
    assert data["estimate_warnings"][0]["excess"] == "10"
    assert data["sync"] == {"google": "not_started", "bitrix": "not_started"}
    e.reports.sheets.apply.assert_not_awaited()


async def test_health_checks_actual_postgres_without_exposing_secrets(editor):
    response = await editor.client.get("/healthz")
    assert response.status == 200
    assert await response.json() == {"status": "ok", "database": "postgresql"}


async def test_standby_deployment_is_healthy_but_cannot_mutate(editor):
    e = editor
    e.reports.writer_ready = False
    health = await e.client.get("/healthz")
    assert health.status == 200
    assert (await health.json())["status"] == "starting"
    response = await e.client.put(e.url, headers=e.headers, json=e.edit)
    assert response.status == 503
    assert e.reports.store.get_draft(e.draft.id, 7).revision == 0
    e.reports.sheets.apply.assert_not_awaited()


async def test_edit_today_reuses_replacement_and_preserves_all_daily_works(editor):
    e = editor
    # Save the original report, leaving another existing daily work untouched.
    saved_response = await e.client.post(e.url + "/confirm", headers=e.headers, json=e.edit)
    assert saved_response.status == 200
    saved = await saved_response.json()
    writes_before_edit = e.reports.sheets.apply.await_count
    body = {"revision": saved["draft"]["revision"]}
    response = await e.client.post(e.url + "/edit-today", headers=e.headers, json=body)
    assert response.status == 200
    data = await response.json()
    new_id = data["draft"]["id"]
    assert new_id != e.draft.id and data["draft"]["mode"] == "replace"
    assert {line["position_id"]: line["quantity"] for line in data["draft"]["lines"]} == {
        "cable": "7.5",
        "clamp": "3",
    }
    assert e.reports.sheets.apply.await_count == writes_before_edit
    assert data["edit_url"].startswith(f"/report/{new_id}#token=")
    again = await (await e.client.post(e.url + "/edit-today", headers=e.headers, json=body)).json()
    assert again["draft"]["id"] == new_id
    token = data["edit_url"].split("#token=", 1)[1]
    headers = {"Authorization": f"Bearer {token}"}
    new_url = f"/api/reports/{new_id}"
    changed = await e.client.patch(
        new_url + "/lines/0",
        headers=headers,
        json={
            "revision": data["draft"]["revision"],
            "line": {"position_id": "cable", "quantity": "2", "phrase": "Кабель"},
        },
    )
    assert changed.status == 200
    patched = await changed.json()
    assert e.quantities == {"cable": Decimal("7.5"), "clamp": Decimal(3)}
    final = await e.client.post(
        new_url + "/confirm",
        headers=headers,
        json={
            "revision": patched["draft"]["revision"],
            "lines": [
                {
                    "position_id": line["position_id"],
                    "quantity": line["quantity"],
                    "phrase": line["phrase"],
                }
                for line in patched["draft"]["lines"]
            ],
        },
    )
    assert final.status == 200
    assert e.quantities == {"cable": Decimal(2), "clamp": Decimal(3)}


async def test_edit_today_requires_saved_report_valid_revision_and_personal_access(editor):
    e = editor
    assert (await e.client.post(e.url + "/edit-today", json={"revision": 0})).status == 401
    assert (
        await e.client.post(e.url + "/edit-today", headers=e.headers, json={"revision": 0})
    ).status == 409
    saved = await (await e.client.post(e.url + "/confirm", headers=e.headers, json=e.edit)).json()
    assert (
        await e.client.post(
            e.url + "/edit-today",
            headers=e.headers,
            json={"revision": saved["draft"]["revision"] - 1},
        )
    ).status == 409


async def test_edit_today_does_not_overwrite_an_unrelated_active_draft(editor):
    e = editor
    saved = await (await e.client.post(e.url + "/confirm", headers=e.headers, json=e.edit)).json()
    pending = await e.reports.create(7, 7, "кабель 10 метров")
    before = pending.model_dump()
    response = await e.client.post(
        e.url + "/edit-today", headers=e.headers, json={"revision": saved["draft"]["revision"]}
    )
    assert response.status == 409
    assert e.reports.store.get_draft(pending.id, 7).model_dump() == before


async def test_edit_today_uses_current_day_even_when_source_report_is_old(editor):
    from datetime import timedelta

    e = editor
    saved = await (await e.client.post(e.url + "/confirm", headers=e.headers, json=e.edit)).json()
    old_day = e.reports.today()
    e.reports.today = lambda: old_day + timedelta(days=1)
    response = await e.client.post(
        e.url + "/edit-today", headers=e.headers, json={"revision": saved["draft"]["revision"]}
    )
    assert response.status == 200
    assert (await response.json())["draft"]["day"] == e.reports.today().isoformat()


async def test_edit_today_without_recorded_work_does_not_create_empty_draft(editor):
    e = editor
    saved = await (await e.client.post(e.url + "/confirm", headers=e.headers, json=e.edit)).json()
    e.quantities.update({"cable": Decimal(0), "clamp": Decimal(0)})
    response = await e.client.post(
        e.url + "/edit-today", headers=e.headers, json={"revision": saved["draft"]["revision"]}
    )
    assert response.status == 409
    assert e.reports.store.active_draft(7) is None
    assert "За сегодня ещё нет записей" in (await response.json())["error"]
