from datetime import date

import pytest

from m100_bot.domain import Draft, UserError
from m100_bot.store import Store


def test_postgres_survives_reconnect_and_preserves_big_telegram_ids(postgres_url):
    first = Store(postgres_url)
    draft = Draft(user_id=8_000_000_001, chat_id=8_000_000_001, day=date(2026, 11, 8))
    first.save_draft(draft)
    token = first.web_token(draft)
    first.save_profile(draft.user_id, {"name": "Мастер"})
    first.remember(draft.user_id, draft.chat_id, "user", "кабель 15 м")
    first.close()
    second = Store(postgres_url)
    assert second.from_web_token(draft.id, token) == draft
    assert second.profile(draft.user_id)["name"] == "Мастер"
    assert second.history(draft.user_id, draft.chat_id)[0]["content"] == "кабель 15 м"
    second.close()


def test_database_lock_prevents_second_worker_and_releases_on_close(postgres_url):
    first, second = Store(postgres_url), Store(postgres_url)
    try:
        first.acquire_writer("isolated-project")
        with pytest.raises(UserError, match="уже запущен"):
            second.acquire_writer("isolated-project")
        first.close()
        second.acquire_writer("isolated-project")
    finally:
        first.close()
        second.close()


def test_finish_save_rolls_back_the_first_write_when_continuation_fails(postgres_url, monkeypatch):
    store = Store(postgres_url)
    draft = Draft(user_id=7, chat_id=7, day=date(2026, 11, 8))
    store.save_draft(draft)
    done = draft.model_copy(update={"status": "done"})
    continuation = draft.model_copy(update={"id": "next"})
    original = store._save_draft

    def fail_next(item):
        if item.id == "next":
            raise RuntimeError("interrupted")
        original(item)

    monkeypatch.setattr(store, "_save_draft", fail_next)
    with pytest.raises(RuntimeError):
        store.finish_save(done, continuation)
    assert store.get_draft(draft.id, 7).status == "draft"
    store.close()
