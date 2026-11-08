from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import AliasChoices, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", populate_by_name=True
    )

    telegram_bot_token: SecretStr = SecretStr("")
    allowed_user_ids: str = ""
    google_spreadsheet_id: str = "16S1KKEV-8CmRLbasQN6WB1NOfUpJs9oFywIGPZXixZs"
    google_sheet_name: str = "Лист1"
    google_credentials_file: str = ""
    google_credentials_json: SecretStr = SecretStr("")
    llm_base_url: str = "https://api.openai.com/v1"
    llm_api_key: SecretStr = SecretStr("")
    llm_model: str = ""
    llm_response_format: str = "json_schema"
    voice_enabled: bool = True
    voice_model: str = "google/gemini-2.5-flash"
    voice_max_seconds: int = 180
    voice_max_bytes: int = 8 * 1024 * 1024
    timezone: str = "Europe/Minsk"
    database_url: SecretStr = SecretStr("")
    web_enabled: bool = True
    web_host: str = "127.0.0.1"
    web_port: int = Field(default=8765, validation_alias=AliasChoices("PORT", "WEB_PORT"))
    web_base_url: str = "http://localhost:8765"
    web_auto_open: bool = True
    web_dist_path: Path = Path("web/dist")
    bitrix_enabled: bool = False
    bitrix_webhook_url: SecretStr = SecretStr("")
    bitrix_entity_type_id: int = 0
    bitrix_external_key_field: str = ""
    bitrix_report_field: str = "comments"
    bitrix_report_fields: dict[str, str] = Field(default_factory=dict)
    bitrix_report_pending_status: str = ""
    project_name: str = "М100 · Рабочий объект"
    bitrix_access_enabled: bool = False
    bitrix_users_entity_type_id: int = 0
    bitrix_users_fields: dict[str, str] = Field(default_factory=dict)
    bitrix_user_roles: dict[str, str] = Field(default_factory=dict)

    @property
    def database_dsn(self) -> str:
        return self.database_url.get_secret_value()

    @property
    def users(self) -> set[int]:
        return {int(value.strip()) for value in self.allowed_user_ids.split(",") if value.strip()}

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    def problems(self) -> list[str]:
        problems = []
        if not self.database_dsn:
            problems.append("Не задан DATABASE_URL PostgreSQL")
        if not self.telegram_bot_token.get_secret_value():
            problems.append("Не задан TELEGRAM_BOT_TOKEN")
        if not self.users and not self.bitrix_access_enabled:
            problems.append("Не задан ALLOWED_USER_IDS: доступ по умолчанию закрыт")
        if not self.llm_api_key.get_secret_value() or not self.llm_model:
            problems.append("Укажите LLM_API_KEY и LLM_MODEL")
        if (
            self.google_credentials_file
            and not self.google_credentials_json.get_secret_value()
            and not Path(self.google_credentials_file).is_file()
        ):
            problems.append("Не найден GOOGLE_CREDENTIALS_FILE")
        if self.llm_response_format not in {"json_schema", "json_object"}:
            problems.append("LLM_RESPONSE_FORMAT: выберите json_schema или json_object")
        if self.bitrix_enabled and not (
            self.bitrix_webhook_url.get_secret_value()
            and self.bitrix_entity_type_id > 0
            and self.bitrix_external_key_field
        ):
            problems.append("Для Битрикс24 нужны URL вебхука, entityTypeId и поле внешнего ключа")
        if self.bitrix_access_enabled and not (
            self.bitrix_webhook_url.get_secret_value()
            and self.bitrix_users_entity_type_id > 0
            and {"telegram_id", "enabled", "role"} <= self.bitrix_users_fields.keys()
            and self.bitrix_user_roles
        ):
            problems.append("Не настроен реестр пользователей бота в Битрикс24")
        _ = self.tz  # Validate the configured timezone.
        return problems
