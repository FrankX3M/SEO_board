"""Подключение к SQLite для веб-интерфейса и справочника.

Та же БД, что у сборщика (data/techaudit.db). Соединение в режиме autocommit: транзакции
открываются явно через tx(). Секреты в БД не пишутся.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path


DEBUG_SCHEMA = """
CREATE TABLE IF NOT EXISTS run_debug (
  run_id TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL, PRIMARY KEY (run_id, key)
);
"""


def connect(path: Path) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=15, isolation_level=None, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA busy_timeout=15000")
    # SQLite LOWER() не знает кириллицу — поиск в интерфейсе идёт через эту функцию
    db.create_function("pylower", 1, lambda s: s.lower() if isinstance(s, str) else s)
    return db


class Rollback(Exception):
    """Откатить транзакцию без ошибки (пробный прогон импорта)."""


@contextmanager
def tx(db: sqlite3.Connection):
    db.execute("BEGIN IMMEDIATE")
    try:
        yield db
    except BaseException:
        db.execute("ROLLBACK")
        raise
    else:
        db.execute("COMMIT")


def init_all(data_dir: Path) -> Path:
    """Создаёт все таблицы: сборщик, проверки, справочник, задания веб-интерфейса."""
    from . import crawl as crawl_mod
    from . import external as external_mod
    from . import indexed as indexed_mod
    from . import orphans as orphans_mod
    from .ai import SCHEMA as AI_SCHEMA
    from .appsettings import SCHEMA as APP_SCHEMA
    from .checks.ctx import SCHEMA as CHECKS_SCHEMA
    from .checks.ctx import migrate_checks
    from .db_registry import SCHEMA as REGISTRY_SCHEMA
    from .storage import Store
    from .web.jobs import SCHEMA as JOBS_SCHEMA

    Store(Path(data_dir)).close()
    path = Path(data_dir) / "techaudit.db"
    db = connect(path)
    try:
        for schema in (CHECKS_SCHEMA, REGISTRY_SCHEMA, JOBS_SCHEMA, crawl_mod.SCHEMA, indexed_mod.SCHEMA, orphans_mod.SCHEMA, external_mod.SCHEMA, AI_SCHEMA, DEBUG_SCHEMA, APP_SCHEMA):
            db.executescript(schema)
        migrate_checks(db)
        from .db_registry import migrate as migrate_registry
        migrate_registry(db)
    finally:
        db.close()
    return path
