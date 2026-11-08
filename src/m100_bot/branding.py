"""Idempotent Telegram identity and the five primary menu commands."""

import asyncio
from pathlib import Path

from aiogram import Bot
from aiogram.types import FSInputFile, InputProfilePhotoStatic, MenuButtonCommands

from .navigation import bot_commands

DESCRIPTION = (
    "М100 · Голосовой помощник мастера\n\n"
    "🎙 Расскажите, что сделали — получите понятный отчёт.\n"
    "📋 Проверьте работы, исправьте объёмы и сохраните результат.\n"
    "📊 Узнавайте остатки, ставки и оплату из рабочей таблицы.\n\n"
    "Начните с /start или откройте «Список команд» — там примеры голосовых фраз."
)
SHORT_DESCRIPTION = (
    "М100 · Работы голосом. Отчёты, объёмы, остатки и оплата — в "
    "Telegram, Google Sheets и Битрикс24."
)


async def configure_menu(bot: Bot):
    for language in ("", "ru"):
        await bot.set_my_commands(bot_commands(), language_code=language)
    await bot.set_chat_menu_button(menu_button=MenuButtonCommands())


async def configure_brand(bot: Bot, avatar: Path | None = None):
    for language in ("", "ru"):
        await bot.set_my_name(name="М100", language_code=language)
        await bot.set_my_description(description=DESCRIPTION, language_code=language)
        await bot.set_my_short_description(
            short_description=SHORT_DESCRIPTION, language_code=language
        )
    await configure_menu(bot)
    if avatar:
        await bot.set_my_profile_photo(photo=InputProfilePhotoStatic(photo=FSInputFile(avatar)))


async def main():
    from .config import Settings

    settings = Settings()
    avatar = Path("web/public/assets/m100-telegram-avatar.png")
    async with Bot(settings.telegram_bot_token.get_secret_value()) as bot:
        await configure_brand(bot, avatar if avatar.is_file() else None)
        me = await bot.get_me()
        print(f"Оформление настроено: {me.full_name} · @{me.username}")


if __name__ == "__main__":
    asyncio.run(main())
