from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from m100_bot.access import AccessDenied, BotAccess
from m100_bot.config import Settings
from m100_bot.domain import UserError


def registry_settings():
    return Settings(
        _env_file=None,
        allowed_user_ids="99",
        bitrix_access_enabled=True,
        bitrix_users_entity_type_id=1048,
        bitrix_users_fields={"telegram_id": "tg", "enabled": "active", "role": "role"},
        bitrix_user_roles={"11": "admin", "12": "master"},
    )


@pytest.mark.parametrize("role,expected", [("11", "admin"), ("12", "master")])
async def test_registry_is_authoritative_without_local_allowlist(role, expected):
    api = SimpleNamespace(
        call=AsyncMock(
            return_value={
                "items": [{"id": 1, "title": "Мастер Иван", "tg": "7", "active": "Y", "role": role}]
            }
        )
    )
    access = BotAccess(registry_settings(), api)
    principal = await access.require(7, write=True)
    assert principal.role == expected and principal.name == "Мастер Иван"
    assert api.call.call_args.args[1]["filter"] == {"tg": "7"}


@pytest.mark.parametrize(
    "items",
    [
        [],
        [{"tg": "7", "active": "N", "role": "11"}],
        [{"tg": "7", "active": "Y", "role": "unknown"}],
        [{"tg": "7", "active": "Y", "role": "viewer"}],
        [{"tg": "8", "active": "Y", "role": "11"}],
        [{"tg": "7", "active": "Y", "role": "11"}] * 2,
    ],
)
async def test_missing_disabled_duplicate_or_unknown_role_denied(items):
    access = BotAccess(
        registry_settings(), SimpleNamespace(call=AsyncMock(return_value={"items": items}))
    )
    with pytest.raises(AccessDenied):
        await access.require(7)


async def test_revocation_and_api_failure_take_effect_on_next_request():
    api = SimpleNamespace(
        call=AsyncMock(
            side_effect=[
                {"items": [{"tg": "7", "active": 1, "role": "11"}]},
                {"items": [{"tg": "7", "active": 0, "role": "11"}]},
                UserError("API error"),
            ]
        )
    )
    access = BotAccess(registry_settings(), api)
    assert (await access.require(7)).can_write
    with pytest.raises(AccessDenied):
        await access.require(7)
    with pytest.raises(AccessDenied, match="Не удалось проверить"):
        await access.require(7)


async def test_local_owner_has_no_bypass_after_registry_enabled():
    access = BotAccess(
        registry_settings(), SimpleNamespace(call=AsyncMock(return_value={"items": []}))
    )
    with pytest.raises(AccessDenied):
        await access.require(99)
