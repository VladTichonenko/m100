"""Local Bitrix24 setup and a real API round trip, without writing Google Sheets.

Run from the project root: python -m m100_bot.bitrix_setup --help.
The webhook is entered with getpass, never as a command-line argument.
"""

import argparse
import asyncio
import getpass
import logging
import re
import sys
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from .bitrix import Bitrix
from .config import Settings
from .domain import Operation, UserError

TEST_DAY = date(2099, 12, 31)
TEST_MARKER = "ТЕСТ ПОДКЛЮЧЕНИЯ М100 — не выполненные работы"


def save_env(values: dict[str, str], path: Path = Path(".env")) -> None:
    """Replace only specified BITRIX lines; preserve all other text and credentials."""
    if any(not key.startswith("BITRIX_") for key in values):
        raise UserError("Эта команда изменяет только настройки BITRIX_*.")
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    for key, value in values.items():
        if "\n" in value or "\r" in value or "'" in value:
            raise UserError("Некорректное значение настройки.")
        pattern = rf"(?m)^(?:export\s+)?{re.escape(key)}\s*=.*$"
        line = f"{key}='{value}'"
        if re.search(pattern, text):
            text = re.sub(pattern, lambda _, replacement=line: replacement, text)
        else:
            text = text.rstrip("\n") + "\n" + line + "\n"
    # r+ preserves permissions/inode of an existing .env. No backup containing secrets.
    mode = "r+" if path.exists() else "w"
    with path.open(mode, encoding="utf-8") as stream:
        stream.write(text)
        stream.truncate()
    path.chmod(0o600)


def validate_webhook(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or not re.fullmatch(r"/rest/[0-9]+/[A-Za-z0-9_-]+/?", parsed.path)
    ):
        raise UserError("Нужен HTTPS-адрес входящего вебхука /rest/<user_id>/<ключ>/.")
    return value.rstrip("/") + "/"


async def fields(api: Bitrix, entity_id: int) -> dict:
    result = await api.call("crm.item.fields", {"entityTypeId": entity_id})
    return result["fields"]


def check_field(schema: dict, name: str, report_field: str = "comments") -> None:
    field = schema.get(name)
    if not field or field.get("type") != "string":
        raise UserError("Внешний ключ должен быть существующим строковым полем.")
    if field.get("isMultiple") or field.get("isReadOnly"):
        raise UserError("Внешний ключ должен быть одиночным и доступным для записи.")
    missing = {"title", report_field} - schema.keys()
    if missing:
        raise UserError("В процессе недоступны название или настроенное текстовое поле отчёта.")
    body = schema[report_field]
    if body.get("isMultiple") or body.get("isReadOnly"):
        raise UserError("Текст отчёта должен быть одиночным и доступным для записи.")


async def inspect(api: Bitrix) -> list[dict]:
    types = []
    start = 0
    while True:
        # Bitrix.call returns result only, so page by ordered numeric type id instead
        # of relying on the top-level 'next' property.
        result = await api.call("crm.type.list", {"filter": {">id": start}, "order": {"id": "ASC"}})
        page = result.get("types", [])
        if not page:
            break
        types.extend(page)
        last = int(page[-1]["id"])
        if last <= start:
            raise UserError("Портал вернул некорректную страницу процессов.")
        start = last
    return types


async def provision(api: Bitrix, settings: Settings) -> None:
    types = await inspect(api)
    if settings.bitrix_entity_type_id:
        matches = [t for t in types if int(t["entityTypeId"]) == settings.bitrix_entity_type_id]
    else:
        matches = [t for t in types if t["title"] == "Дневные отчёты"]
    if len(matches) > 1:
        raise UserError("Найдено несколько процессов. Укажите BITRIX_ENTITY_TYPE_ID вручную.")
    if matches:
        process = matches[0]
    elif settings.bitrix_entity_type_id:
        raise UserError("Настроенный процесс не найден. Проверьте ID и права.")
    else:
        result = await api.call(
            "crm.type.add",
            {
                "fields": {
                    "title": "Дневные отчёты",
                    "isStagesEnabled": "N",
                    "isCategoriesEnabled": "N",
                    "isAutomationEnabled": "N",
                    "isBizProcEnabled": "N",
                    "isClientEnabled": "N",
                    "isLinkWithProductsEnabled": "N",
                }
            },
        )
        process = result["type"]
    entity_id = int(process["entityTypeId"])
    save_env({"BITRIX_ENTITY_TYPE_ID": str(entity_id)})
    schema = await fields(api, entity_id)
    name = settings.bitrix_external_key_field
    if not name:
        original = f"UF_CRM_{process['id']}_M100_KEY"
        names = [k for k, v in schema.items() if v.get("upperName") == original]
        if not names:
            await api.call(
                "userfieldconfig.add",
                {
                    "moduleId": "crm",
                    "field": {
                        "entityId": f"CRM_{process['id']}",
                        "fieldName": original,
                        "userTypeId": "string",
                        "multiple": "N",
                        "mandatory": "N",
                        "showFilter": "Y",
                        "editFormLabel": {"ru": "Внешний ключ"},
                    },
                },
            )
            schema = await fields(api, entity_id)
            names = [k for k, v in schema.items() if v.get("upperName") == original]
        if len(names) != 1:
            raise UserError("Не удалось определить REST-имя внешнего ключа через crm.item.fields.")
        name = names[0]
    report_field = settings.bitrix_report_field
    if report_field not in schema and report_field == "comments":
        original = f"UF_CRM_{process['id']}_M100_WORKS"
        names = [k for k, v in schema.items() if v.get("upperName") == original]
        if not names:
            await api.call(
                "userfieldconfig.add",
                {
                    "moduleId": "crm",
                    "field": {
                        "entityId": f"CRM_{process['id']}",
                        "fieldName": original,
                        "userTypeId": "string",
                        "multiple": "N",
                        "mandatory": "N",
                        "editFormLabel": {"ru": "Работы за день"},
                        "settings": {"ROWS": 12},
                    },
                },
            )
            schema = await fields(api, entity_id)
            names = [k for k, v in schema.items() if v.get("upperName") == original]
        if len(names) != 1:
            raise UserError("Не удалось получить REST-имя поля «Работы за день».")
        report_field = names[0]
    check_field(schema, name, report_field)
    save_env({"BITRIX_EXTERNAL_KEY_FIELD": name, "BITRIX_REPORT_FIELD": report_field})
    print(f"Процесс готов: entityTypeId={entity_id}, ключ={name}, текст={report_field}.")
    print("Интеграция ещё не включена. Далее выполните verify, затем enable.")


async def verify(api: Bitrix, settings: Settings) -> None:
    """Exercise the SAME adapter as the bot; keep one clearly marked test item."""
    if not settings.bitrix_entity_type_id or not settings.bitrix_external_key_field:
        raise UserError("Сначала выполните provision или заполните ID и поле вручную.")
    check_field(
        await fields(api, settings.bitrix_entity_type_id),
        settings.bitrix_external_key_field,
        settings.bitrix_report_field,
    )
    external = f"m100:{settings.google_spreadsheet_id}:{TEST_DAY.isoformat()}"
    common = {"entityTypeId": settings.bitrix_entity_type_id}

    async def items() -> list[dict]:
        result = await api.call(
            "crm.item.list",
            {
                **common,
                "filter": {settings.bitrix_external_key_field: external},
                "select": [
                    "id",
                    "title",
                    settings.bitrix_report_field,
                    settings.bitrix_external_key_field,
                ],
            },
        )
        return result.get("items", [])

    existing = await items()
    if len(existing) > 1 or (
        existing and TEST_MARKER not in existing[0].get(settings.bitrix_report_field, "")
    ):
        raise UserError("Дата 31.12.2099 занята другой записью. Проверка остановлена.")
    operation = Operation(
        id="m100-bitrix-connection-test",
        draft_id="m100-bitrix-connection-test",
        day=TEST_DAY,
        changes=[],
    )
    expected_id = str(existing[0]["id"]) if existing else None
    for step, quantity in (("Добавление/повтор", "15"), ("Изменение", "20"), ("Повтор", "20")):
        await api.apply(operation, [(TEST_MARKER, quantity, "м")])
        saved = await items()
        if len(saved) != 1:
            raise UserError("После записи найдено не ровно одно тестовое значение.")
        record = saved[0]
        if expected_id and str(record["id"]) != expected_id:
            raise UserError("Изменился ID тестовой карточки: проверка не пройдена.")
        expected_id = str(record["id"])
        if f"{TEST_MARKER}: {quantity} м" not in record.get(settings.bitrix_report_field, ""):
            raise UserError("API не вернул ожидаемый объём в текстовом поле отчёта.")
        print(f"{step}: OK, ID={expected_id}, {quantity} м, одна карточка.")
    print("Проверка REST пройдена. Google Sheets и SQLite этой командой не изменялись.")
    print("Карточка 31.12.2099 оставлена в портале как свидетельство проверки.")


async def run(command: str, settings: Settings) -> None:
    validate_webhook(settings.bitrix_webhook_url.get_secret_value())
    # The bot may be disabled during setup; only this adapter instance is enabled.
    settings = settings.model_copy(update={"bitrix_enabled": True})
    async with httpx.AsyncClient(timeout=30, follow_redirects=False) as http:
        api = Bitrix(settings, http)
        if command == "inspect":
            types = await inspect(api)
            print(f"REST CRM доступен. Видимых смарт-процессов: {len(types)}.")
            for process in types:
                print(f"entityTypeId={process['entityTypeId']}; {process['title']}")
        elif command == "provision":
            await provision(api, settings)
        elif command == "verify":
            await verify(api, settings)
        elif command == "enable":
            await verify(api, settings)
            save_env({"BITRIX_ENABLED": "true"})
            print("BITRIX_ENABLED=true. Перезапустите процесс бота для применения .env.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Настройка тестового Битрикс24 без вывода секретов"
    )
    parser.add_argument("command", choices=["webhook", "inspect", "provision", "verify", "enable"])
    args = parser.parse_args()
    # HTTP debug logs may include the webhook URL. Never enable them in this utility.
    logging.disable(logging.CRITICAL)
    try:
        if args.command == "webhook":
            if not sys.stdin.isatty():
                raise UserError("Откройте команду в обычном терминале для скрытого ввода вебхука.")
            value = validate_webhook(getpass.getpass("Входящий вебхук (ввод скрыт): ").strip())
            save_env({"BITRIX_WEBHOOK_URL": value})
            print("Вебхук сохранён в локальном .env (права 600); остальные настройки сохранены.")
        else:
            asyncio.run(run(args.command, Settings()))
    except (UserError, OSError, ValueError, KeyError, TypeError) as exc:
        # Never print low-level exceptions: they can contain request URLs or env values.
        print(
            str(exc)
            if isinstance(exc, UserError)
            else "Не удалось выполнить настройку. Проверьте .env и доступ к порталу."
        )
        raise SystemExit(1) from None
    except (KeyboardInterrupt, EOFError):
        print("Настройка отменена.")
        raise SystemExit(130) from None


if __name__ == "__main__":
    main()
