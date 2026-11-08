"""One-time, atomic SQLite -> PostgreSQL import. The source is never modified."""

import argparse
import hashlib
import json
import sqlite3
from datetime import datetime
from pathlib import Path

from psycopg import sql
from psycopg.types.json import Jsonb

from .config import Settings
from .domain import Draft, Operation, UserError
from .store import Store

TABLES = {
    "drafts": ("id", "user_id", "payload"),
    "operations": ("id", "draft_id", "payload"),
    "web_links": ("token_hash", "draft_id", "user_id", "expires_at"),
    "user_profiles": ("user_id", "payload"),
    "conversation_turns": ("id", "user_id", "chat_id", "role", "content", "created_at"),
    "table_selections": ("user_id", "chat_id", "spreadsheet_id", "sheet_name", "payload"),
}


def migrate(source: Path, target: Store, backup: Path) -> dict:
    if not source.is_file() or backup.exists() or source.resolve() == backup.resolve():
        raise UserError("Нужна существующая SQLite-база и новый путь резервной копии.")
    backup.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True) as original:
        with sqlite3.connect(backup) as saved:
            original.backup(saved)
    backup.chmod(0o600)
    data = {}
    with sqlite3.connect(backup) as legacy:
        existing = {
            r[0] for r in legacy.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        for table, columns in TABLES.items():
            data[table] = (
                list(legacy.execute(f"SELECT {','.join(columns)} FROM {table} ORDER BY rowid"))
                if table in existing
                else []
            )
    fingerprint = hashlib.sha256(json.dumps(data, ensure_ascii=False).encode()).hexdigest()
    counts = {table: len(rows) for table, rows in data.items()}
    with target.db.transaction():
        target.db.execute("SELECT pg_advisory_xact_lock(100, 2)")
        target.db.execute(
            "CREATE TABLE IF NOT EXISTS legacy_imports "
            "(source_hash TEXT PRIMARY KEY, counts JSONB NOT NULL)"
        )
        if target.db.execute(
            "SELECT 1 FROM legacy_imports WHERE source_hash=%s", (fingerprint,)
        ).fetchone():
            return {"already_imported": True, "counts": counts}
        for table in TABLES:
            if target.db.execute(
                sql.SQL("SELECT 1 FROM {} LIMIT 1").format(sql.Identifier(table))
            ).fetchone():
                raise UserError("PostgreSQL уже содержит данные. Импорт разрешён в пустую базу.")
        for table, columns in TABLES.items():
            for raw in data[table]:
                values = list(raw)
                if "payload" in columns:
                    index = columns.index("payload")
                    decoded = json.loads(values[index])
                    if table == "drafts":
                        Draft.model_validate(decoded)
                    if table == "operations":
                        Operation.model_validate(decoded)
                    values[index] = Jsonb(decoded)
                target.db.execute(
                    sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
                        sql.Identifier(table),
                        sql.SQL(",").join(map(sql.Identifier, columns)),
                        sql.SQL(",").join(sql.Placeholder() for _ in columns),
                    ),
                    values,
                )
            actual = target.db.execute(
                sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(table))
            ).fetchone()[0]
            if actual != counts[table]:
                raise UserError("Число строк после импорта не совпало; транзакция отменена.")
        target.db.execute(
            "SELECT setval(pg_get_serial_sequence('conversation_turns','id'), "
            "GREATEST(COALESCE((SELECT max(id) FROM conversation_turns),0),1), "
            "EXISTS(SELECT 1 FROM conversation_turns))"
        )
        target.db.execute("INSERT INTO legacy_imports VALUES (%s,%s)", (fingerprint, Jsonb(counts)))
    return {"already_imported": False, "counts": counts}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--backup", type=Path)
    args = parser.parse_args()
    backup = args.backup or args.source.with_name(
        args.source.stem + ".pre-postgres-" + datetime.now().strftime("%Y%m%d-%H%M%S") + ".sqlite3"
    )
    store = Store(Settings().database_dsn)
    try:
        print(json.dumps(migrate(args.source, store, backup), ensure_ascii=False))
        print("Резервная копия:", backup)
    finally:
        store.close()


if __name__ == "__main__":
    main()
