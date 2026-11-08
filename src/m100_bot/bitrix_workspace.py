"""Provision native Bitrix forms for reports and bot accounts. Never grants new users access."""

import asyncio
import json
import logging
from pathlib import Path

import httpx

from .bitrix import Bitrix
from .bitrix_setup import inspect, save_env
from .config import Settings
from .domain import UserError
from .store import Store

REPORT_FIELDS = {
    "day": ("DATE", "Дата отчёта", "date"),
    "project": ("PROJECT", "Объект", "string"),
    "author": ("AUTHOR", "Последний автор · Telegram", "string"),
    "telegram_id": ("AUTHOR_ID", "Telegram ID автора", "string"),
    "saved_at": ("SAVED_AT", "Время записи · Минск", "datetime"),
    "positions": ("POSITIONS", "Позиций за день", "integer"),
    "excess_count": ("EXCESS", "Превышений сметы", "integer"),
    "finishing_count": ("FINISHING", "Близко к завершению", "integer"),
    "warnings": ("WARNINGS", "Контроль сметы", "string"),
    "history": ("HISTORY", "История изменений", "string"),
    "sheet_url": ("SHEET_URL", "Google-таблица", "url"),
    "operation": ("OPERATION", "Последняя операция", "string"),
    "status": ("REVIEW", "Проверка отчёта", "enumeration"),
    "reviewer_note": ("REVIEW_NOTE", "Замечания проверяющего", "string"),
}
USER_FIELDS = {
    "telegram_id": ("TG_ID", "Telegram ID", "string"),
    "username": ("TG_USERNAME", "Telegram username", "string"),
    "enabled": ("ENABLED", "Доступ к боту разрешён", "boolean"),
    "role": ("ROLE", "Роль в боте", "enumeration"),
    "position": ("POSITION", "Должность / бригада", "string"),
    "project": ("PROJECT", "Объект", "string"),
    "note": ("NOTE", "Комментарий администратора", "string"),
}
ROLES = [("admin", "Администратор"), ("master", "Мастер")]
REVIEW = [("pending", "На проверке"), ("accepted", "Принят"), ("clarify", "Нужно уточнение")]


async def ensure_fields(api: Bitrix, process: dict, definitions: dict) -> tuple[dict, dict]:
    schema = (await api.call("crm.item.fields", {"entityTypeId": process["entityTypeId"]}))[
        "fields"
    ]
    config = {}
    enums = {}
    for logical, (suffix, label, kind) in definitions.items():
        original = f"UF_CRM_{process['id']}_M100_{suffix}"
        names = [name for name, data in schema.items() if data.get("upperName") == original]
        if not names:
            field = {
                "entityId": f"CRM_{process['id']}",
                "fieldName": original,
                "userTypeId": kind,
                "multiple": "N",
                "mandatory": "N",
                "showFilter": "Y",
                "editFormLabel": {"ru": label},
                "sort": 100,
            }
            if kind == "enumeration":
                values = ROLES if logical == "role" else REVIEW
                field["enum"] = [
                    {"xmlId": key, "value": title, "sort": (i + 1) * 100}
                    for i, (key, title) in enumerate(values)
                ]
            if kind == "string":
                field["settings"] = {
                    "ROWS": 12
                    if logical in {"warnings", "history"}
                    else 3
                    if logical in {"note", "reviewer_note"}
                    else 1,
                    "SIZE": 80,
                }
            if kind == "boolean":
                field["settings"] = {"DEFAULT_VALUE": 0, "DISPLAY": "CHECKBOX"}
            await api.call("userfieldconfig.add", {"moduleId": "crm", "field": field})
            schema = (await api.call("crm.item.fields", {"entityTypeId": process["entityTypeId"]}))[
                "fields"
            ]
            names = [name for name, data in schema.items() if data.get("upperName") == original]
        if len(names) != 1:
            raise UserError(f"Не удалось определить поле: {label}.")
        config[logical] = names[0]
        if kind == "enumeration":
            # Fetch settings to preserve stable xmlId mapping rather than trust display labels.
            result = await api.call(
                "userfieldconfig.list",
                {
                    "moduleId": "crm",
                    "filter": {"entityId": f"CRM_{process['id']}", "fieldName": original},
                },
            )
            matches = [f for f in result.get("fields", []) if f.get("fieldName") == original]
            if len(matches) != 1:
                raise UserError(f"Не найдены варианты списка: {label}.")
            detail = await api.call(
                "userfieldconfig.get", {"moduleId": "crm", "id": matches[0]["id"]}
            )
            values = detail["field"].get("enum", [])
            enums[logical] = {str(item["id"]): item["xmlId"] for item in values}
    return config, enums


async def configure_card(api: Bitrix, entity_id: int, sections: list[tuple]) -> None:
    schema = (await api.call("crm.item.fields", {"entityTypeId": entity_id}))["fields"]
    categories = (await api.call("crm.category.list", {"entityTypeId": entity_id}))["categories"]
    category = next(item["id"] for item in categories if item.get("isDefault") in ("Y", True, 1))
    extras = {"categoryId": category}
    data = [
        {
            "name": name,
            "title": title,
            "type": "section",
            "elements": [
                {"name": schema[field].get("upperName", field), "optionFlags": 1}
                for field in fields
            ],
        }
        for name, title, fields in sections
    ]
    previous = await api.call(
        "crm.item.details.configuration.get",
        {
            "entityTypeId": entity_id,
            "scope": "C",
            "extras": extras,
        },
    )
    target = Path(f"artifacts/bitrix-card-{entity_id}-category-{category}-before.json")
    if not target.exists():
        target.write_text(json.dumps(previous, ensure_ascii=False, indent=2) + "\n")
    await api.call(
        "crm.item.details.configuration.set",
        {
            "entityTypeId": entity_id,
            "scope": "C",
            "data": data,
            "extras": extras,
        },
    )
    await api.call(
        "crm.item.details.configuration.set",
        {
            "entityTypeId": entity_id,
            "scope": "P",
            "data": data,
            "extras": extras,
        },
    )


async def provision_workspace(settings: Settings, api: Bitrix) -> dict:
    types = await inspect(api)
    report = next(t for t in types if int(t["entityTypeId"]) == settings.bitrix_entity_type_id)
    matches = [
        t for t in types if t.get("code") == "m100_bot_users" or t["title"] == "Пользователи бота"
    ]
    if len(matches) > 1:
        raise UserError("Несколько реестров пользователей. Нужен ручной выбор процесса.")
    if matches:
        users = matches[0]
    else:
        users = (
            await api.call(
                "crm.type.add",
                {
                    "fields": {
                        "title": "Пользователи бота",
                        "code": "m100_bot_users",
                        "isCategoriesEnabled": "N",
                        "isStagesEnabled": "N",
                        "isAutomationEnabled": "N",
                        "isBizProcEnabled": "N",
                        "isClientEnabled": "N",
                        "isLinkWithProductsEnabled": "N",
                        "isSetOpenPermissions": "N",
                    }
                },
            )
        )["type"]
    report_fields, report_enums = await ensure_fields(api, report, REPORT_FIELDS)
    users_fields, user_enums = await ensure_fields(api, users, USER_FIELDS)
    values = {
        "BITRIX_REPORT_FIELDS": json.dumps(report_fields, ensure_ascii=False),
        "BITRIX_REPORT_PENDING_STATUS": next(
            k for k, v in report_enums["status"].items() if v == "pending"
        ),
        "BITRIX_USERS_ENTITY_TYPE_ID": str(users["entityTypeId"]),
        "BITRIX_USERS_FIELDS": json.dumps(users_fields, ensure_ascii=False),
        "BITRIX_USER_ROLES": json.dumps(user_enums["role"], ensure_ascii=False),
    }
    # Bootstrap only existing local owners, and never overwrite an existing CRM decision.
    connection = Store(settings.database_dsn)
    try:
        admin_role = next(k for k, v in user_enums["role"].items() if v == "admin")
        for user_id in settings.users:
            existing = (
                await api.call(
                    "crm.item.list",
                    {
                        "entityTypeId": users["entityTypeId"],
                        "filter": {users_fields["telegram_id"]: str(user_id)},
                        "select": ["id"],
                    },
                )
            )["items"]
            if existing:
                continue
            profile = connection.profile(user_id) or {}
            await api.call(
                "crm.item.add",
                {
                    "entityTypeId": users["entityTypeId"],
                    "fields": {
                        "title": profile.get("name") or f"Администратор {user_id}",
                        users_fields["telegram_id"]: str(user_id),
                        users_fields["username"]: profile.get("username") or "",
                        users_fields["enabled"]: "Y",
                        users_fields["role"]: admin_role,
                        users_fields["project"]: settings.project_name,
                        users_fields[
                            "note"
                        ]: "Существующий владелец бота. Профиль создан при подключении реестра.",
                    },
                },
            )
    finally:
        connection.close()
    save_env(values)
    await configure_card(
        api,
        int(report["entityTypeId"]),
        [
            (
                "summary",
                "Отчёт об объекте",
                ["title", report_fields["day"], report_fields["project"], report_fields["status"]],
            ),
            ("people", "Кто и когда", [report_fields[k] for k in ["author", "saved_at"]]),
            (
                "works",
                "Выполнено за день",
                [
                    report_fields["positions"],
                    settings.bitrix_report_field,
                    report_fields["sheet_url"],
                ],
            ),
            (
                "control",
                "Контроль сметы",
                [
                    report_fields[k]
                    for k in ["excess_count", "finishing_count", "warnings", "reviewer_note"]
                ],
            ),
            ("audit", "История изменений", [report_fields["history"]]),
            (
                "technical",
                "Служебные данные",
                [
                    settings.bitrix_external_key_field,
                    report_fields["telegram_id"],
                    report_fields["operation"],
                ],
            ),
        ],
    )
    await configure_card(
        api,
        int(users["entityTypeId"]),
        [
            (
                "profile",
                "Профиль сотрудника",
                [
                    "title",
                    users_fields["telegram_id"],
                    users_fields["username"],
                    users_fields["position"],
                    users_fields["project"],
                ],
            ),
            ("access", "Доступ к Telegram-боту", [users_fields["enabled"], users_fields["role"]]),
            ("notes", "Служебная информация", [users_fields["note"], "createdTime", "updatedTime"]),
        ],
    )
    return {
        "reports": report["entityTypeId"],
        "users": users["entityTypeId"],
        "report_fields": report_fields,
        "user_fields": users_fields,
    }


def main():
    logging.disable(logging.CRITICAL)

    async def run():
        settings = Settings()
        async with httpx.AsyncClient(timeout=30) as http:
            result = await provision_workspace(settings, Bitrix(settings, http))
            print(json.dumps(result, ensure_ascii=False, indent=2))
            print("Поля и карточки настроены. Реестр доступа ещё не включён.")

    try:
        asyncio.run(run())
    except (UserError, OSError, KeyError, ValueError, StopIteration):
        print("Настройка остановлена. Проверьте права полей и структуру CRM. Секреты не выводятся.")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
