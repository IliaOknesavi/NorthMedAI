"""
agents/sources/corpus.py — локальный RAG: поиск по corpus_chunks через
pgvector cosine similarity.

Чанки загружаются заранее CLI-командой `python -m corpus_ingest <path>`
(см. backend/corpus_ingest.py). Здесь — только query-time часть: для
текущего search-query берём эмбеддинг (модель text-search-query) и
делаем `ORDER BY embedding <=> :query_vec` через сырой SQL.

Graceful fallback:
  - pgvector расширения нет → таблица embedding-колонка пуста → []
  - YANDEX_API_KEY нет → embed() вернёт None → []
  - таблица corpus_chunks вообще нет → catch и []
"""

from __future__ import annotations

import logging
from typing import ClassVar

from sqlalchemy import text as sql_text

from ..types import Source, SourceTier
from ._embeddings import embed, is_configured as embeddings_configured

log = logging.getLogger("agents.sources.corpus")


# Сколько ближайших chunks возвращать на ОДИН запрос (после ранжирования)
MAX_RESULTS_PER_QUERY = 5
# Порог cosine distance: если расстояние > порога — chunk слишком далёкий.
# В pgvector `vector_cosine_ops` distance = 1 - cosine_similarity, диапазон [0..2].
# 0 = идеальное совпадение; 1.0 = ортогонально. Берём всё что ≤ 0.6.
MAX_COSINE_DISTANCE = 0.6


def _format_pgvector_param(vec: list[float]) -> str:
    """pgvector принимает строку формата '[0.1,0.2,...]' как литерал."""
    return "[" + ",".join(f"{x:.6f}" for x in vec) + "]"


class CorpusAdapter:
    """См. agents/sources/_base.py → SourceAdapter."""

    tier: ClassVar[SourceTier] = "corpus"
    base_weight: ClassVar[float] = 0.85    # сравним с WHO, потому что в корпусе обычно WHO/Минздрав

    async def search(self, queries: list[str], lang: str = "en") -> list[Source]:
        if not queries:
            return []
        if not embeddings_configured():
            log.debug("corpus: embeddings не настроены — пусто")
            return []

        # Ленивый импорт SessionLocal — это связано с тем, что текущий
        # SessionLocal живёт в db.py, который зависит от моделей; импорт
        # здесь, а не наверху, чтобы тесты agents/* не тянули asyncpg
        # без явной нужды.
        try:
            from db import SessionLocal  # type: ignore[import-not-found]
        except ImportError:
            log.warning("corpus: db.SessionLocal недоступен — пусто")
            return []

        # Объединяем все запросы в один большой query-вектор? Нет —
        # делаем поиск по каждому запросу, объединяем результаты.
        # На практике у нас 1-2 запроса в этой категории.
        all_hits: dict[int, dict] = {}  # chunk_id → row
        for q in queries:
            vec = await embed(q, kind="query")
            if vec is None:
                continue

            vec_literal = _format_pgvector_param(vec)
            sql = sql_text("""
                SELECT
                  id,
                  doc_url,
                  doc_title,
                  doc_tier,
                  chunk_idx,
                  text,
                  chunk_metadata,
                  embedding <=> CAST(:qvec AS vector) AS distance
                FROM corpus_chunks
                WHERE embedding IS NOT NULL
                ORDER BY distance ASC
                LIMIT :lim
            """)

            try:
                async with SessionLocal() as session:
                    result = await session.execute(
                        sql, {"qvec": vec_literal, "lim": MAX_RESULTS_PER_QUERY},
                    )
                    rows = result.mappings().all()
            except Exception:  # noqa: BLE001
                log.warning("corpus: SQL упал — возможно нет pgvector/таблицы")
                continue

            for row in rows:
                dist = float(row["distance"])
                if dist > MAX_COSINE_DISTANCE:
                    continue
                chunk_id = int(row["id"])
                if chunk_id in all_hits:
                    # Берём наименьшую дистанцию между разными query
                    if dist >= all_hits[chunk_id]["distance"]:
                        continue
                all_hits[chunk_id] = {**dict(row), "distance": dist}

        if not all_hits:
            log.info("corpus: ни одного chunk'а не нашлось (могут быть пустая БД/нет vec/далеко)")
            return []

        # Сортируем по distance возрастанию
        sorted_rows = sorted(all_hits.values(), key=lambda r: r["distance"])

        # Дедуплицируем по doc_url — нам нужен один Source на документ
        seen_urls: set[str] = set()
        sources: list[Source] = []
        for row in sorted_rows:
            url = row["doc_url"]
            if url in seen_urls:
                continue
            seen_urls.add(url)
            distance = row["distance"]
            # relevance = 1 - distance (cosine similarity), нормализован
            relevance = max(0.0, 1.0 - distance)
            tier_field = row.get("doc_tier") or self.tier
            # Вес: используем base_weight адаптера, или поднимаем если doc_tier
            # указывает на сильный источник (who, cdc_nejm).
            tier_weight_lookup = {
                "who": 0.85, "cdc_nejm": 0.80, "minzdrav": 0.70,
                "pubmed": 0.90, "news_major": 0.60, "corpus": 0.85,
            }
            weight = tier_weight_lookup.get(tier_field, self.base_weight)
            score = round(weight * relevance, 3)
            sources.append(Source(
                title=str(row.get("doc_title") or "Корпус: документ")[:200],
                url=url,
                tier=self.tier,
                weight=weight,
                relevance=round(relevance, 3),
                score=score,
                snippet=str(row.get("text") or "")[:600],
                published_at="",
                language="ru" if tier_field in ("minzdrav",) else "en",
                mock=False,
            ))
            if len(sources) >= MAX_RESULTS_PER_QUERY:
                break

        log.info(
            "corpus: вернул %d Source'ов (%d hits, лучшее distance=%.3f)",
            len(sources), len(all_hits),
            sorted_rows[0]["distance"] if sorted_rows else 0.0,
        )
        return sources


ADAPTER = CorpusAdapter()
