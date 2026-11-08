"""Disposable PostgreSQL schema for live-LLM checks; never touches the working tables."""

import os
from contextlib import contextmanager
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo

from m100_bot.store import Store


@contextmanager
def sandbox_store():
    dsn = os.environ.get("TEST_DATABASE_URL", "postgresql:///m100_test")
    schema = "smoke_" + uuid4().hex
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        store = None
        try:
            store = Store(make_conninfo(dsn, options=f"-csearch_path={schema}"))
            yield store
        finally:
            if store:
                store.close()
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
