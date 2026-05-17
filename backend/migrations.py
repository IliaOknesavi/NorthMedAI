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
# Каждая миграция: (имя, SQL, optional).
# optional=True — провал лога но не блокирует startup приложения. Это
# нужно для RAG-миграций, которым требуется pgvector в Postgres-инстансе.
MIGRATIONS: list[tuple[str, str, bool]] = [
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
        False,
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
        False,
    ),
    (
        "M0002_analyses_qa_version",
        """
        ALTER TABLE analyses
          ADD COLUMN IF NOT EXISTS qa_version VARCHAR(32) DEFAULT '' NOT NULL;
        """,
        False,
    ),
    # M0003 — локальный RAG. Опциональны: если инстанс БЕЗ pgvector
    # (старый postgres:16-alpine), они упадут с WARNING, но pipeline
    # запустится. corpus.py будет возвращать [] до тех пор, пока
    # docker-compose не пересоберётся на pgvector/pgvector:pg16.
    (
        "M0003_pgvector_extension",
        "CREATE EXTENSION IF NOT EXISTS vector;",
        True,
    ),
    (
        "M0003_corpus_chunks_vector_column",
        # 1024 = размерность text-search-doc от Yandex Embeddings.
        """
        ALTER TABLE corpus_chunks
          ADD COLUMN IF NOT EXISTS embedding vector(1024);
        """,
        True,
    ),
    (
        "M0003_corpus_chunks_ivfflat_index",
        # IVFFlat — самый практичный ANN-индекс в pgvector для нашего
        # объёма (тысячи-десятки тысяч chunks). 100 списков — старт.
        """
        CREATE INDEX IF NOT EXISTS ix_corpus_chunks_embedding
        ON corpus_chunks
        USING ivfflat (embedding vector_cosine_ops)
        WITH (lists = 100);
        """,
        True,
    ),
    # M0004 — пересоздаём embedding column под реальную размерность
    # Yandex Foundation Models (256). Раньше было vector(1024).
    # Все старые chunks (если были) теряют embedding (NULL) и должны
    # быть переингесчены через python -m corpus_ingest.
    (
        "M0004_drop_old_ivfflat_index",
        "DROP INDEX IF EXISTS ix_corpus_chunks_embedding;",
        True,
    ),
    (
        "M0004_corpus_chunks_embedding_256",
        """
        ALTER TABLE corpus_chunks
          DROP COLUMN IF EXISTS embedding;
        ALTER TABLE corpus_chunks
          ADD COLUMN embedding vector(256);
        """,
        True,
    ),
    (
        "M0004_recreate_ivfflat_index_256",
        """
        CREATE INDEX IF NOT EXISTS ix_corpus_chunks_embedding
        ON corpus_chunks
        USING ivfflat (embedding vector_cosine_ops)
        WITH (lists = 100);
        """,
        True,
    ),
    # M0005 — pipeline_stats JSONB. В нём лежат метрики прогона
    # (qa_kept/dropped/repaired/dedup, stance_*, duration_s).
    # Используется UI-попапом для «Pipeline» секции (воронка + время).
    (
        "M0005_analyses_pipeline_stats",
        """
        ALTER TABLE analyses
          ADD COLUMN IF NOT EXISTS pipeline_stats JSONB DEFAULT NULL;
        """,
        False,
    ),
]


async def apply_inplace_migrations(conn: AsyncConnection) -> None:
    """Прогоняет все миграции по порядку. Безопасно повторно вызывать.

    `optional=True` миграции при сбое не блокируют startup — пишут WARNING
    и переходят к следующей. Используется для RAG-расширения, которое
    зависит от pgvector в Postgres-инстансе.
    """
    for name, sql, optional in MIGRATIONS:
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
            if optional:
                log.warning(
                    "migration %s (optional) не применилась — продолжаю. "
                    "Часто причина: расширение pgvector не установлено в инстансе. "
                    "RAG-функции работать не будут до тех пор, пока контейнер не "
                    "будет пересобран на pgvector/pgvector:pg16.",
                    name,
                )
                continue
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
