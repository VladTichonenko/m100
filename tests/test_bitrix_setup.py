import json

import httpx
import pytest

from m100_bot.bitrix import Bitrix
from m100_bot.bitrix_setup import TEST_MARKER, provision, save_env, validate_webhook, verify
from m100_bot.config import Settings
from m100_bot.domain import UserError


def settings(**values):
    return Settings(
        _env_file=None,
        bitrix_enabled=True,
        bitrix_webhook_url="https://example.test/rest/1/secret/",
        bitrix_entity_type_id=100,
        bitrix_external_key_field="ufCrm7M100Key",
        **values,
    )


def schema(report_field="comments"):
    return {
        "title": {},
        report_field: {"type": "string"},
        "ufCrm7M100Key": {
            "type": "string",
            "upperName": "UF_CRM_7_M100_KEY",
            "isMultiple": False,
            "isReadOnly": False,
        },
    }


def test_env_update_preserves_unrelated_secrets_and_comments(tmp_path):
    path = tmp_path / ".env"
    original = (
        "# comment\nTELEGRAM_BOT_TOKEN=keep-me\nBITRIX_ENABLED=false\nLLM_API_KEY=also-keep\n"
    )
    path.write_text(original)
    save_env({"BITRIX_ENABLED": "true", "BITRIX_EXTERNAL_KEY_FIELD": "ufCrm7M100Key"}, path)
    updated = path.read_text()
    assert "TELEGRAM_BOT_TOKEN=keep-me\n" in updated
    assert "LLM_API_KEY=also-keep\n" in updated
    assert updated.startswith("# comment\n")
    assert updated.count("BITRIX_ENABLED=") == 1
    assert "BITRIX_ENABLED='true'" in updated
    assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize(
    "url",
    [
        "http://example.test/rest/1/secret/",
        "https://example.test/rest/1/secret/crm.item.add",
        "https://example.test/rest/1/secret/?query=secret",
        "https://a:b@example.test/rest/1/key/",
    ],
)
def test_invalid_webhook_never_echoes_secret(url):
    with pytest.raises(UserError) as exc:
        validate_webhook(url)
    assert "secret" not in str(exc.value)


@pytest.mark.parametrize("report_field", ["comments", "ufCrm_8_M100_WORKS"])
async def test_round_trip_uses_real_adapter_and_reuses_one_card(report_field):
    saved = []
    writes = []

    def api(request):
        method = request.url.path.rsplit("/", 1)[-1].removesuffix(".json")
        payload = json.loads(request.content)
        if method == "crm.item.fields":
            result = {"fields": schema(report_field)}
        elif method == "crm.item.list":
            result = {"items": saved}
        elif method == "crm.item.add":
            saved.append({"id": 42, **payload["fields"]})
            writes.append(method)
            result = {"item": saved[0]}
        elif method == "crm.item.update":
            assert payload["id"] == 42
            saved[0].update(payload["fields"])
            writes.append(method)
            result = {"item": saved[0]}
        else:
            pytest.fail(method)
        return httpx.Response(200, json={"result": result})

    configured = settings(bitrix_report_field=report_field)
    async with httpx.AsyncClient(transport=httpx.MockTransport(api)) as http:
        await verify(Bitrix(configured, http), configured)
        await verify(Bitrix(configured, http), configured)
    assert writes.count("crm.item.add") == 1
    assert writes.count("crm.item.update") == 5
    assert len(saved) == 1
    assert f"{TEST_MARKER}: 20 м" in saved[0][report_field]


async def test_round_trip_refuses_to_overwrite_non_test_report():
    def api(request):
        if "crm.item.fields" in request.url.path:
            return httpx.Response(200, json={"result": {"fields": schema()}})
        assert "crm.item.list" in request.url.path
        return httpx.Response(200, json={"result": {"items": [{"id": 42, "comments": "real"}]}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(api)) as http:
        with pytest.raises(UserError, match="занята"):
            await verify(Bitrix(settings(), http), settings())


async def test_provision_reuses_existing_type_and_exact_rest_field(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    def api(request):
        payload = json.loads(request.content)
        if "crm.type.list" in request.url.path:
            page = (
                []
                if payload["filter"][">id"]
                else [{"id": 7, "entityTypeId": 100, "title": "Дневные отчёты"}]
            )
            result = {"types": page}
        else:
            assert "crm.item.fields" in request.url.path  # No create calls on repeated setup.
            result = {"fields": schema()}
        return httpx.Response(200, json={"result": result})

    configured = settings().model_copy(update={"bitrix_external_key_field": ""})
    async with httpx.AsyncClient(transport=httpx.MockTransport(api)) as http:
        await provision(Bitrix(configured, http), configured)
    content = (tmp_path / ".env").read_text()
    assert "BITRIX_ENTITY_TYPE_ID='100'" in content
    assert "BITRIX_EXTERNAL_KEY_FIELD='ufCrm7M100Key'" in content
    assert "BITRIX_ENABLED" not in content
