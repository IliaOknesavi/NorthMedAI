"""
migrations.py — лёгкие in-place миграции для NorthMedAI.

Зачем не Alembic: на хакатоне база перезаливается раз в неделю, миграции
нужны простые — добавить колонку, добавить индекс. Alembic избыточен.
А пересоздавать таблицы через `Base.metadata.create_all` нельзя — мы
потеряем накопленные анализы.

Подход: декларативный список миграций ниже. Каждая — кортеж
(version, sql). `apply_inplace_migrations(conn)` гоняет их по порядку,
все SQL обёрнуты в `IF NOT EXISTS` и поэтому идемпотентны.

История миграций (что-зачем-когда):
  M0001 — поля для агентов pipeline'а P0 (stance/retriever/judge versions,
          unverifiable/debunked counters, аудит на Claim).
"""

from __future__ import annotations

import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

log = logging.getLogger("db.migrations")


# ВАЖНО: каждый запрос идемпотентен.
# Если добавляешь новую миграцию — клади её В КОНЕЦ списка, не меняй
# существующие. Имя версии — только для логов и не пишется в БД (если
# понадобится отслеживание applied-миграций, заведём табличку schema_migrations
# и перепишем под неё).
MIGRATIONS: list[tuple[str, str]] = [
    (
        "M0001_analyses_agent_versions",
        """
        ALTER TABLE analyses
          ADD COLUMN IF NOT EXISTS stance_version    VARCHAR(32)  DEFAULT '' NOT NULL,
          ADD COLUMN IF NOT EXISTS retriever_version VARCHAR(64)  DEFAULT '' NOT NULL,
          ADD COLUMN IF NOT EXISTS judge_version     VARCHAR(32)  DEFAULT '' NOT NULL,
          ADD COLUMN IF NOT EXISTS debunked_drop_count INTEGER    DEFAULT 0  NOT NULL,
          ADD COLUMN IF NOT EXISTS unverifiable_count  INTEGER    DEFAULT 0  NOT NULL;
        """,
    ),
    (
        "M0001_claims_audit_fields",
        """
        ALTER TABLE claims
          ADD COLUMN IF NOT EXISTS extractor_verdict VARCHAR(32) DEFAULT '' NOT NULL,
          ADD COLUMN IF NOT EXISTS stance            VARCHAR(32) DEFAULT 'asserted' NOT NULL,
          ADD COLUMN IF NOT EXISTS stance_missing    TEXT        DEFAULT '' NOT NULL,
          ADD COLUMN IF NOT EXISTS judge_notes       TEXT        DEFAULT '' NOT NULL,
          ADD COLUMN IF NOT EXISTS search_queries    JSONB       DEFAULT NULL;
        """,
    ),
]


async def apply_inplace_migrations(conn: AsyncConnection) -> None:
    """Прогоняет все миграции по порядку. Безопасно повторно вызывать."""
    for name, sql in MIGRATIONS:
        # Если БД на SQLite (например, в юнит-тестах) — `ALTER TABLE ... ADD
        # COLUMN IF NOT EXISTS` не поддерживается. Пропускаем тихо: в тестах
        # на чистой SQLite-базе таблицы создаются `create_all` уже с нужными
        # колонками из models.py, миграции там не нужны.
        dialect = conn.dialect.name
        if dialect != "postgresql":
            log.debug("migrations: пропускаю %s — диалект %s", name, dialect)
            continue
        try:
            await conn.execute(text(sql))
            log.info("migration applied: %s", name)
        except Exception:  # noqa: BLE001
            log.exception("migration failed: %s", name)
            raise


# CLI: `python -m migrations` — для применения вне старта приложения
if __name__ == "__main__":
    import asyncio

    from db import engine  # type: ignore[import-not-found]

    async def _main() -> None:
        logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
        async with engine.begin() as conn:
            await apply_inplace_migrations(conn)
        await engine.dispose()

    asyncio.run(_main())
