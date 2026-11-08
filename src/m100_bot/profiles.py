"""Telegram profile data and private avatar delivery for an authorized report owner."""

import logging
import time

from aiogram import Bot

from .store import Store

logger = logging.getLogger(__name__)


async def telegram_profile(store: Store, user_id: int, bot: Bot | None) -> dict:
    profile = store.profile(user_id) or {}
    if bot and hasattr(bot, "get_chat") and time.time() - profile.get("checked_at", 0) > 300:
        try:
            chat = await bot.get_chat(user_id)
            profile = store.save_profile(
                user_id,
                {
                    "name": " ".join(filter(None, [chat.first_name, chat.last_name])),
                    "username": chat.username,
                    "photo_file_id": chat.photo.small_file_id if chat.photo else None,
                    "checked_at": time.time(),
                },
            )
        except Exception as exc:
            logger.warning("Telegram profile unavailable: %s", type(exc).__name__)
            profile = store.save_profile(user_id, {"checked_at": time.time()})
    return profile


def public_profile(profile: dict) -> dict | None:
    if not profile.get("name"):
        return None
    username = profile.get("username")
    return {
        "name": profile["name"],
        "username": username,
        "url": f"https://t.me/{username}" if username else None,
        "has_photo": bool(profile.get("photo_file_id")),
    }
