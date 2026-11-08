from types import SimpleNamespace
from unittest.mock import AsyncMock

from m100_bot.bitrix_workspace import configure_card


async def test_card_configuration_uses_actual_category_and_original_field_names(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "artifacts").mkdir()

    async def call(method, payload):
        if method == "crm.item.fields":
            return {
                "fields": {
                    "title": {"upperName": "TITLE"},
                    "ufCrm_10_ROLE": {"upperName": "UF_CRM_10_ROLE"},
                }
            }
        if method == "crm.category.list":
            return {"categories": [{"id": 16, "isDefault": "Y"}]}
        assert payload["extras"] == {"categoryId": 16}
        if method.endswith(".get"):
            return []
        assert payload["data"][0]["elements"] == [
            {"name": "TITLE", "optionFlags": 1},
            {"name": "UF_CRM_10_ROLE", "optionFlags": 1},
        ]
        return True

    api = SimpleNamespace(call=AsyncMock(side_effect=call))
    await configure_card(api, 1048, [("profile", "Профиль", ["title", "ufCrm_10_ROLE"])])
    writes = [c.args[1] for c in api.call.call_args_list if c.args[0].endswith(".set")]
    assert {w["scope"] for w in writes} == {"C", "P"}
