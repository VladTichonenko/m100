import httpx

from .config import Settings
from .domain import Operation, UserError, fmt


class Bitrix:
    """One smart-process item per day; full quantities in the configured text field.

    A configured custom external-key field makes timeout recovery deterministic.
    Only one bot process may write to this project's smart process.
    """

    def __init__(self, settings: Settings, http: httpx.AsyncClient):
        self.settings = settings
        self.http = http

    async def call(self, method: str, payload: dict) -> dict:
        try:
            response = await self.http.post(
                self.settings.bitrix_webhook_url.get_secret_value().rstrip("/")
                + "/"
                + method
                + ".json",
                json=payload,
            )
            response.raise_for_status()
            data = response.json()
            if data.get("error"):
                raise UserError(
                    "Битрикс24 отклонил запись. Проверьте права вебхука и поля процесса."
                )
            return data["result"]
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            raise UserError("Битрикс24 недоступен. Запись можно безопасно повторить.") from exc

    async def apply(
        self, op: Operation, day_lines: list[tuple[str, str, str]], *, context: dict | None = None
    ) -> None:
        if not self.settings.bitrix_enabled:
            return
        external = f"m100:{self.settings.google_spreadsheet_id}:{op.day.isoformat()}"
        common = {"entityTypeId": self.settings.bitrix_entity_type_id}
        result = await self.call(
            "crm.item.list",
            {
                **common,
                "filter": {self.settings.bitrix_external_key_field: external},
                "select": [
                    "id",
                    "title",
                    *(
                        [self.settings.bitrix_report_fields["operation"]]
                        if "operation" in self.settings.bitrix_report_fields
                        else []
                    ),
                ],
            },
        )
        items = result.get("items", [])
        if len(items) > 1:
            raise UserError(
                "В Битрикс24 несколько отчётов с одним внешним ключом. Проверьте портал."
            )
        text = f"Дневной отчёт за {op.day:%d.%m.%Y}\n\n"
        text += "\n".join(f"{name}: {qty} {unit}" for name, qty, unit in day_lines)
        if not day_lines:
            text += "Все дневные объёмы обнулены."
        text += (
            f"\n\nОперация: {op.id}\nТаблица: "
            f"https://docs.google.com/spreadsheets/d/{self.settings.google_spreadsheet_id}/edit"
        )
        fields = {
            "title": f"Дневной отчёт · {op.day:%d.%m.%Y}",
            self.settings.bitrix_report_field: text,
            self.settings.bitrix_external_key_field: external,
        }
        if context:
            mapping = self.settings.bitrix_report_fields
            fields.update(
                {
                    mapping[key]: value
                    for key, value in context.items()
                    if key in mapping and value is not None
                }
            )
            previous = items[0].get(mapping.get("operation", "")) if items else None
            if (
                "status" in mapping
                and self.settings.bitrix_report_pending_status
                and previous != op.id
            ):
                fields[mapping["status"]] = self.settings.bitrix_report_pending_status
        if items:
            await self.call("crm.item.update", {**common, "id": items[0]["id"], "fields": fields})
        else:
            await self.call("crm.item.add", {**common, "fields": fields})


def daily_lines(snapshot) -> list[tuple[str, str, str]]:
    return [
        (p.name, fmt(snapshot.day_values[key]), p.unit)
        for key, p in snapshot.positions.items()
        if snapshot.day_values[key] != 0
    ]
