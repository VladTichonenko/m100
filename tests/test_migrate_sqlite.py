import sqlite3
from datetime import date

import pytest

from m100_bot.domain import Draft, UserError
from m100_bot.migrate_sqlite import migrate
from m100_bot.store import Store


def test_import_preserves_tokens_history_and_is_idempotent(tmp_path, postgres_url):
    import hashlib

    source = tmp_path / "old.db"
    draft = Draft(user_id=7, chat_id=7, day=date(2026, 11, 8))
    with sqlite3.connect(source) as db:
        db.executescript("""
            CREATE TABLE drafts(id TEXT,user_id INTEGER,payload TEXT);
            CREATE TABLE web_links(
                token_hash TEXT,draft_id TEXT,user_id INTEGER,expires_at REAL);
            CREATE TABLE conversation_turns(
                id INTEGER PRIMARY KEY,user_id INTEGER,chat_id INTEGER,
                role TEXT,content TEXT,created_at REAL);
        """)
        db.execute("INSERT INTO drafts VALUES (?,?,?)", (draft.id, 7, draft.model_dump_json()))
        db.execute(
            "INSERT INTO web_links VALUES (?,?,?,?)",
            (hashlib.sha256(b"old-token").hexdigest(), draft.id, 7, 9_999_999_999),
        )
        db.execute("INSERT INTO conversation_turns VALUES (9,7,7,'user','кабель',123)")
    store = Store(postgres_url)
    result = migrate(source, store, tmp_path / "backup.db")
    assert result["counts"]["drafts"] == 1
    assert store.from_web_token(draft.id, "old-token") == draft
    store.remember(7, 7, "assistant", "ответ")
    assert [turn["content"] for turn in store.history(7, 7)] == ["кабель", "ответ"]
    assert migrate(source, store, tmp_path / "backup2.db")["already_imported"]
    assert source.exists()
    store.close()


def test_import_refuses_nonempty_destination(tmp_path, postgres_url):
    source = tmp_path / "old.db"
    with sqlite3.connect(source) as db:
        db.execute("CREATE TABLE drafts(id TEXT,user_id INTEGER,payload TEXT)")
    store = Store(postgres_url)
    draft = Draft(user_id=7, chat_id=7, day=date(2026, 11, 8))
    store.save_draft(draft)
    with pytest.raises(UserError, match="уже содержит"):
        migrate(source, store, tmp_path / "backup.db")
    assert store.get_draft(draft.id, 7) == draft
    store.close()
