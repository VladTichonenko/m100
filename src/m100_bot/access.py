"""Bitrix-managed bot access. A personal report token never overrides revocation."""

from dataclasses import dataclass

from .bitrix import Bitrix
from .config import Settings
from .domain import UserError

ROLE_NAMES = {"admin": "Администратор", "master": "Мастер"}


class AccessDenied(UserError):
    pass


@dataclass(frozen=True)
class Principal:
    user_id: int
    name: str
    role: str

    @property
    def can_write(self) -> bool:
        return self.role in {"admin", "master"}


class BotAccess:
    def __init__(self, settings: Settings, bitrix: Bitrix):
        self.settings = settings
        self.bitrix = bitrix

    async def require(self, user_id: int, *, write: bool = False) -> Principal:
        if not self.settings.bitrix_access_enabled:
            if user_id not in self.settings.users:
                raise AccessDenied("Доступ к боту закрыт.")
            return Principal(user_id, "", "admin")
        fields = self.settings.bitrix_users_fields
        try:
            result = await self.bitrix.call(
                "crm.item.list",
                {
                    "entityTypeId": self.settings.bitrix_users_entity_type_id,
                    "filter": {fields["telegram_id"]: str(user_id)},
                    "select": [
                        "id",
                        "title",
                        fields["telegram_id"],
                        fields["enabled"],
                        fields["role"],
                    ],
                },
            )
        except UserError:
            raise AccessDenied(
                "Не удалось проверить доступ в Битрикс24. Попробуйте позже; данные не изменены."
            ) from None
        items = result.get("items", [])
        # A duplicate/partial/malformed profile must never accidentally grant access.
        if len(items) != 1:
            raise AccessDenied(
                "Доступ закрыт. Администратор должен проверить ваш профиль в Битрикс24."
            )
        item = items[0]
        enabled = item.get(fields["enabled"])
        role = self.settings.bitrix_user_roles.get(str(item.get(fields["role"], "")), "")
        if (
            str(item.get(fields["telegram_id"], "")) != str(user_id)
            or enabled not in (True, 1, "1", "Y")
            or role not in ROLE_NAMES
        ):
            raise AccessDenied("Доступ закрыт. Проверьте разрешение и роль профиля в Битрикс24.")
        principal = Principal(user_id, item.get("title", ""), role)
        if write and not principal.can_write:
            raise AccessDenied("Назначенная роль не разрешает изменение данных.")
        return principal
