import json
from datetime import date
from urllib.parse import unquote

import httpx
import pytest

from m100_bot.bitrix import Bitrix
from m100_bot.config import Settings
from m100_bot.domain import Change, Operation, Position, UserError
from m100_bot.llm import LLM
from m100_bot.sheets import JOURNAL, Sheets
from m100_bot.voice import Voice


class GoogleServer:
    """In-memory Google API simulator for atomic batch and receipt reconciliation."""

    def __init__(self):
        self.column_count = 10
        self.day_value = 0
        self.date_headers = [(date(2026, 11, 1) - date(1899, 12, 30)).days]
        self.journal = []
        self.journal_id = None
        self.posts = []
        self.timeout_after_commit = False

    def __call__(self, request):
        if request.method == "POST":
            requests = json.loads(request.content)["requests"]
            self.posts.append(requests)
            for req in requests:
                if append := req.get("appendDimension"):
                    self.column_count += append["length"]
                if add := req.get("addSheet"):
                    self.journal_id = add["properties"]["sheetId"]
                if update := req.get("updateCells"):
                    start = update["start"]
                    if start["rowIndex"] == 4:
                        self.day_value = update["rows"][0]["values"][0]["userEnteredValue"][
                            "numberValue"
                        ]
                    elif start["rowIndex"] == 2:
                        self.date_headers.append(
                            update["rows"][0]["values"][0]["userEnteredValue"]["numberValue"]
                        )
                if append := req.get("appendCells"):
                    for row in append["rows"]:
                        self.journal.append(
                            [v["userEnteredValue"]["stringValue"] for v in row["values"]]
                        )
            if self.timeout_after_commit:
                self.timeout_after_commit = False
                raise httpx.ReadTimeout("lost response", request=request)
            return httpx.Response(200, json={"replies": [{} for _ in requests]})
        if "/values/" not in request.url.path:
            sheets = [
                {
                    "properties": {
                        "sheetId": 0,
                        "title": "Лист1",
                        "gridProperties": {
                            "rowCount": 80,
                            "columnCount": self.column_count,
                        },
                    }
                }
            ]
            if self.journal_id:
                sheets.append({"properties": {"sheetId": self.journal_id, "title": JOURNAL}})
            return httpx.Response(200, json={"sheets": sheets})
        address = unquote(request.url.path.split("/values/")[1])
        if JOURNAL in address:
            return httpx.Response(200, json={"values": [[r[0]] for r in self.journal]})
        if "!A1:I" in address:
            values = [
                ["ID", "Наименование", "Ед."],
                [],
                [],
                ["", "ЭМ3"],
                [
                    "EM3-001",
                    "Кабель 3х2,5",
                    "м",
                    100,
                    2,
                    200,
                    80 - self.day_value,
                    40 + self.day_value * 2,
                    20 + self.day_value,
                ],
            ]
        elif "!J3:" in address:
            values = [self.date_headers]
        else:
            values = [[], [], [], [], [self.day_value]]
        return httpx.Response(200, json={"values": values})


def operation():
    return Operation(
        id="op-test",
        draft_id="op-test",
        day=date(2026, 11, 2),
        changes=[
            Change(position_id="EM3-001", name="Кабель 3х2,5", unit="м", before=0, after=15),
        ],
    )


async def token():
    return "test-token"


async def test_new_date_and_day_write_commit_with_journal_in_one_batch():
    server = GoogleServer()
    async with httpx.AsyncClient(transport=httpx.MockTransport(server)) as http:
        sheets = Sheets(Settings(_env_file=None), http, token)
        await sheets.apply(operation())
        await sheets.apply(operation())
    assert server.day_value == 15
    assert server.column_count == 11
    assert len(server.posts) == 1
    assert server.journal[-1][0] == "op-test"
    assert any("appendCells" in r for r in server.posts[0])
    assert any("updateCells" in r for r in server.posts[0])


async def test_catalog_includes_wage_and_money_columns_from_the_specification():
    server = GoogleServer()
    async with httpx.AsyncClient(transport=httpx.MockTransport(server)) as http:
        snapshot = await Sheets(Settings(_env_file=None), http, token).snapshot(date(2026, 11, 2))
    item = snapshot.positions["EM3-001"]
    assert item.unit_pay == 2
    assert item.planned_pay == 200
    assert item.completed_pay == 40
    assert item.remaining == 80
    assert not server.posts


async def test_remote_receipt_prevents_duplicate_after_timeout():
    server = GoogleServer()
    server.timeout_after_commit = True
    async with httpx.AsyncClient(transport=httpx.MockTransport(server)) as http:
        sheets = Sheets(Settings(_env_file=None), http, token)
        with pytest.raises(UserError):
            await sheets.apply(operation())
        await sheets.apply(operation())
    assert server.day_value == 15
    assert len(server.posts) == 1


async def test_duplicate_today_columns_rejected_before_write():
    server = GoogleServer()
    serial = (date(2026, 11, 2) - date(1899, 12, 30)).days
    server.date_headers = [serial, serial]
    async with httpx.AsyncClient(transport=httpx.MockTransport(server)) as http:
        with pytest.raises(UserError, match="два столбца"):
            await Sheets(Settings(_env_file=None), http, token).snapshot(date(2026, 11, 2))
    assert not server.posts


async def test_llm_result_has_to_refer_to_current_catalog():
    def api(request):
        payload = json.loads(request.content)
        assert payload["response_format"]["type"] == "json_schema"
        assert (
            "maxItems"
            not in payload["response_format"]["json_schema"]["schema"]["properties"]["lines"]
        )
        content = {
            "lines": [
                {
                    "phrase": "кабель",
                    "position_id": "invented",
                    "quantity": 5,
                    "unit": "м",
                    "candidates": ["invented"],
                    "question": "",
                }
            ]
        }
        return httpx.Response(
            200, json={"choices": [{"message": {"content": json.dumps(content)}}]}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(api)) as http:
        result = await LLM(Settings(_env_file=None), http).extract("кабель 5 м", {})
    assert result[0].position_id is None
    assert result[0].candidates == []


async def test_bitrix_upserts_existing_daily_report():
    methods = []

    def api(request):
        payload = json.loads(request.content)
        methods.append(request.url.path)
        if "crm.item.list" in request.url.path:
            assert payload["filter"]["ufCrmExternal"]
            return httpx.Response(200, json={"result": {"items": [{"id": 42}]}})
        assert payload["id"] == 42
        assert "15" in payload["fields"]["comments"]
        return httpx.Response(200, json={"result": {"item": {"id": 42}}})

    settings = Settings(
        _env_file=None,
        bitrix_enabled=True,
        bitrix_entity_type_id=100,
        bitrix_external_key_field="ufCrmExternal",
        bitrix_webhook_url="https://example.test/rest/1/test/",
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(api)) as http:
        await Bitrix(settings, http).apply(operation(), [("Кабель", "15", "м")])
    assert len(methods) == 2
    assert "crm.item.update" in methods[-1]


async def test_telegram_ogg_is_sent_as_audio_and_returns_transcript():
    def api(request):
        payload = json.loads(request.content)
        audio = payload["messages"][1]["content"][1]["input_audio"]
        assert audio["format"] == "ogg"
        assert audio["data"] == "T2dnUw=="
        return httpx.Response(200, json={"choices": [{"message": {"content": "Кабель 15 метров"}}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(api)) as http:
        assert await Voice(Settings(_env_file=None), http).transcribe(b"OggS") == "Кабель 15 метров"


async def test_empty_or_oversized_audio_rejected_without_network():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: pytest.fail("network"))
    ) as http:
        voice = Voice(Settings(_env_file=None, voice_max_bytes=4), http)
        for audio in [b"", b"12345"]:
            with pytest.raises(UserError):
                await voice.transcribe(audio)


async def test_legacy_candidate_names_resolve_to_ids_and_do_not_overflow_buttons():
    catalog = {
        f"id{i}": Position(
            id=f"id{i}",
            row=i + 5,
            name=f"Кабель {i}",
            section="ЭМ3",
            unit="м",
            planned=100,
            completed=0,
        )
        for i in range(10)
    }
    content = {
        "lines": [
            {
                "phrase": "кабель",
                "position_id": None,
                "quantity": 5,
                "unit": "м",
                "candidates": [p.name for p in catalog.values()],
                "question": "Какой кабель?",
            }
        ]
    }

    def api(request):
        return httpx.Response(
            200, json={"choices": [{"message": {"content": json.dumps(content)}}]}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(api)) as http:
        result = await LLM(Settings(_env_file=None), http).extract("кабель 5 м", catalog)
    assert len(result[0].candidates) == 8
    assert all(key in catalog for key in result[0].candidates)


async def test_yellow_fill_is_atomic_with_value_and_does_not_reset_other_formatting():
    server = GoogleServer()
    op = operation()
    op.highlight = "yellow"
    async with httpx.AsyncClient(transport=httpx.MockTransport(server)) as http:
        sheets = Sheets(Settings(_env_file=None), http, token)
        await sheets.apply(op)
        await sheets.apply(op)
    updates = [
        r["updateCells"]
        for r in server.posts[0]
        if "updateCells" in r and r["updateCells"]["start"]["rowIndex"] == 4
    ]
    assert len(updates) == 1 and len(server.posts) == 1
    update = updates[0]
    assert update["start"] == {"sheetId": 0, "rowIndex": 4, "columnIndex": 10}
    assert update["fields"] == "userEnteredValue,userEnteredFormat.backgroundColorStyle"
    assert update["rows"][0]["values"][0] == {
        "userEnteredValue": {"numberValue": 15},
        "userEnteredFormat": {
            "backgroundColorStyle": {"rgbColor": {"red": 1, "green": 1, "blue": 0}}
        },
    }


async def test_regular_save_does_not_change_cell_color():
    server = GoogleServer()
    async with httpx.AsyncClient(transport=httpx.MockTransport(server)) as http:
        await Sheets(Settings(_env_file=None), http, token).apply(operation())
    cell = next(
        r["updateCells"]
        for r in server.posts[0]
        if "updateCells" in r and r["updateCells"]["start"]["rowIndex"] == 4
    )
    assert cell["fields"] == "userEnteredValue"
    assert "userEnteredFormat" not in cell["rows"][0]["values"][0]


@pytest.mark.parametrize("color", ["#A8DADC", "#0044FF", "#F15ABC", "clear"])
async def test_format_only_preserves_values_dates_and_all_other_formatting(color):
    server = GoogleServer()
    op = Operation(
        id="format-test",
        draft_id="format-test",
        day=date(2026, 11, 2),
        kind="format",
        changes=[],
        highlight=color,
        cells=["B5", "D5", "J5"],
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(server)) as http:
        sheets = Sheets(Settings(_env_file=None), http, token)
        await sheets.apply(op)
        await sheets.apply(op)
    assert len(server.posts) == 1 and server.day_value == 0
    assert len(server.date_headers) == 1 and server.column_count == 10
    assert not any("updateCells" in r or "appendDimension" in r for r in server.posts[0])
    requests = [r["repeatCell"] for r in server.posts[0] if "repeatCell" in r]
    assert len(requests) == 3
    assert [r["range"]["startColumnIndex"] for r in requests] == [1, 3, 9]
    for request in requests:
        assert "userEnteredValue" not in request["fields"]
        assert "numberFormat" not in request["fields"]
        if color == "clear":
            assert request["cell"]["userEnteredFormat"] == {}
            assert "backgroundColor" in request["fields"]
        else:
            from m100_bot.domain import rgb_color

            assert request["cell"]["userEnteredFormat"] == {
                "backgroundColorStyle": {"rgbColor": rgb_color(color)}
            }


async def test_format_color_and_receipt_survive_timeout_without_second_batch():
    server = GoogleServer()
    server.timeout_after_commit = True
    op = Operation(
        id="format-retry",
        draft_id="format-retry",
        day=date(2026, 11, 2),
        kind="format",
        changes=[],
        highlight="#A8DADC",
        cells=["B5"],
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(server)) as http:
        sheets = Sheets(Settings(_env_file=None), http, token)
        with pytest.raises(UserError):
            await sheets.apply(op)
        await sheets.apply(op)
    assert len(server.posts) == 1 and server.day_value == 0


async def test_period_query_uses_one_read_only_batch_and_includes_only_selected_dates():
    from decimal import Decimal

    from m100_bot.sheets import Snapshot

    seen = []

    def api(request):
        seen.append(request)
        assert request.method == "GET" and request.url.path.endswith("/values:batchGet")
        assert request.url.params.get_list("ranges") == ["'Лист1'!J1:J80", "'Лист1'!K1:K80"]
        assert request.url.params["valueRenderOption"] == "UNFORMATTED_VALUE"
        return httpx.Response(
            200,
            json={
                "valueRanges": [
                    {"values": [[], [], [], [], ["1.5"]]},
                    {"values": [[], [], [], [], [2]]},
                ]
            },
        )

    p = Position(id="a", row=5, name="Кабель", section="ЭМ3", unit="м", planned=100, completed=20)
    snap = Snapshot(
        0,
        80,
        702,
        {"a": p},
        {"a": Decimal(0)},
        None,
        11,
        1,
        [(date(2026, 11, 5), 9), (date(2026, 11, 6), 10), (date(2026, 11, 8), 11)],
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(api)) as http:
        result, dates = await Sheets(Settings(_env_file=None), http, token).period_values(
            date(2026, 11, 5), date(2026, 11, 6), snap
        )
    assert result == {"a": Decimal("3.5")} and len(dates) == 2 and len(seen) == 1


async def test_raw_range_query_uses_formatted_values_and_preserves_missing_cells():
    from m100_bot.sheets import Snapshot

    seen = []

    def api(request):
        seen.append(request)
        assert request.method == "GET"
        assert request.url.params.get_list("ranges") == ["'Лист1'!B5:D6"]
        assert request.url.params["valueRenderOption"] == "FORMATTED_VALUE"
        return httpx.Response(
            200, json={"valueRanges": [{"values": [["Кабель", "м", "0"], ["Блок"]]}]}
        )

    snap = Snapshot(0, 80, 702, {}, {}, None, 10, 1)
    async with httpx.AsyncClient(transport=httpx.MockTransport(api)) as http:
        values = await Sheets(Settings(_env_file=None), http, token).read_cells(["B5:D6"], snap)
    assert values == [
        ("B5", "Кабель"),
        ("C5", "м"),
        ("D5", "0"),
        ("B6", "Блок"),
        ("C6", ""),
        ("D6", ""),
    ]
    assert len(seen) == 1


async def test_period_query_rejects_duplicate_dates_without_network():
    from m100_bot.sheets import Snapshot

    calls = []
    snap = Snapshot(
        0, 80, 702, {}, {}, None, 10, 1, [(date(2026, 11, 6), 9), (date(2026, 11, 6), 10)]
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: calls.append(request))
    ) as http:
        with pytest.raises(UserError, match="повторяющиеся даты"):
            await Sheets(Settings(_env_file=None), http, token).period_values(
                date(2026, 11, 6), date(2026, 11, 6), snap
            )
    assert not calls


async def test_json_provider_unused_null_text_does_not_break_a_read_query():
    from m100_bot.llm import VoiceCommand

    payload = {
        "action": "query",
        "edits": [],
        "additions": None,
        "question": None,
        "query": {"scope": "positions", "position_ids": ["a"], "metrics": ["remaining"]},
    }
    command = VoiceCommand.model_validate(payload)
    assert (
        command.question == "" and command.additions == "" and command.query.position_ids == ["a"]
    )
