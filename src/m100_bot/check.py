import argparse
import asyncio
from datetime import datetime

import httpx

from .access import BotAccess
from .bitrix import Bitrix
from .config import Settings
from .domain import UserError
from .sheets import Sheets
from .store import Store


async def check_remote(settings: Settings) -> None:
    async with httpx.AsyncClient(timeout=30) as http:
        snapshot = await Sheets(settings, http).snapshot(datetime.now(settings.tz).date())
        print(f"Google Sheets: доступ есть, позиций {len(snapshot.positions)}")
        status = "есть" if snapshot.day_column is not None else "будет создан"
        print(f"Столбец сегодняшней даты: {status}")
        if settings.bitrix_enabled:
            result = await Bitrix(settings, http).call(
                "crm.item.fields",
                {
                    "entityTypeId": settings.bitrix_entity_type_id,
                },
            )
            fields = result.get("fields", {})
            if settings.bitrix_external_key_field not in fields:
                raise UserError("Не найдено настроенное поле внешнего ключа Битрикс24.")
            if settings.bitrix_report_field not in fields:
                raise UserError("Не найдено настроенное текстовое поле отчёта Битрикс24.")
            print("Битрикс24: процесс и поле доступны (чтение, без тестовой записи)")
            missing = set(settings.bitrix_report_fields.values()) - fields.keys()
            if missing:
                raise UserError("Не найдены дополнительные поля отчёта Битрикс24.")
        else:
            print("Битрикс24: отключён. Полная сдача задания требует включить интеграцию.")
        if settings.bitrix_access_enabled:
            api = Bitrix(settings, http)
            users = await api.call(
                "crm.item.fields", {"entityTypeId": settings.bitrix_users_entity_type_id}
            )
            if set(settings.bitrix_users_fields.values()) - users["fields"].keys():
                raise UserError("Не найдены поля реестра пользователей Битрикс24.")
            for user_id in settings.users:
                await BotAccess(settings, api).require(user_id)
            print("Реестр пользователей: поля доступны, доступ текущего владельца подтверждён.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Проверка конфигурации без вывода секретов")
    parser.add_argument("--remote", action="store_true", help="Проверить чтение API, без записи")
    args = parser.parse_args()
    settings = Settings()
    try:
        problems = settings.problems()
        if problems:
            print("Не заполнены настройки:\n- " + "\n- ".join(problems))
            raise SystemExit(2)
        print("Локальные настройки заполнены.")
        store = Store(settings.database_dsn)
        store.db.execute("SELECT 1")
        store.close()
        print("PostgreSQL: подключение и схема доступны.")
        if args.remote:
            asyncio.run(check_remote(settings))
    except UserError as exc:
        print(str(exc))
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
