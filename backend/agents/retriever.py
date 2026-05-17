"""
agents/retriever.py — оркестратор source-адаптеров.

P1 (этот файл):
  - Параллельно дёргает все адаптеры, у которых есть запросы в ClaimQueries.
  - Собирает все Source'ы, дедупит по url.
  - Прогоняет Conflict Classifier — для каждого source ставит знак
    supports / contradicts / neutral относительно claim'а.
  - Сортирует по score, режет до MAX_SOURCES_PER_CLAIM.
  - Возвращает Evidence с тремя списками внутри (через грязный канал —
    extras, потому что текущий TypedDict не различает группы; Judge
    группирует сам по полю stance внутри source'а).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from urllib.parse import quote_plus

from .conflict_classifier import classify_sources
from .sources import ADAPTERS, SourceAdapter
from .types import ClaimQueries, Evidence, Source

log = logging.getLogger("agents.retriever")

# Сколько источников максимум доходит до Judge (топ по score после
# conflict classifier'а).
MAX_SOURCES_PER_CLAIM = 5


def _empty_evidence(claim_text: str, errors: list[str] | None = None) -> Evidence:
    return Evidence(
        claim_text=claim_text,
        sources=[],
        errors=errors or [],
        retrieved_at=datetime.now(timezone.utc).isoformat(),
    )


def _fallback_pubmed_search_source(claim_text: str) -> Source:
    """
    Старый mock: ссылка на поиск PubMed по тексту claim'а. Используется,
    когда вообще никакой адаптер не вернул результатов, но мы не хотим
    отдавать пользователю пустые sources (тогда тултип сиротский).

    mock=True маркирует это как «не настоящая статья, а поиск».
    """
    q = quote_plus(claim_text[:120])
    return Source(
        title="PubMed — поиск по теме",
        url=f"https://pubmed.ncbi.nlm.nih.gov/?term={q}",
        tier="pubmed",
        weight=0.5,
        relevance=0.5,
        score=0.25,
        snippet="",
        mock=True,
    )


async def _run_adapter(
    adapter: SourceAdapter, queries: list[str], lang: str,
) -> tuple[list[Source], str | None]:
    """Вызов одного адаптера с graceful catch."""
    try:
        sources = await adapter.search(queries, lang=lang)
        return sources, None
    except Exception as e:  # noqa: BLE001
        log.exception(
            "retriever: адаптер %s упал: %s", getattr(adapter, "tier", "?"), e,
        )
        return [], f"{getattr(adapter, 'tier', 'unknown')}: {e}"


async def retrieve(claim_queries: ClaimQueries) -> Evidence:
    """
    Найти источники для одного claim'а и проставить им stance
    (supports/contradicts/neutral) через Conflict Classifier.

    Stance кладётся в каждый Source как ключ `stance` (через total=False
    у TypedDict это допустимо). Judge получает Source'ы с пометкой и
    группирует сам.
    """
    claim_text = claim_queries["claim_text"]
    queries = claim_queries["queries"]

    # Маппинг ClaimQueries-ключей в SourceTier из ADAPTERS.
    # У нас есть лёгкое расхождение: в QueriesPerSource ключ "news",
    # а тир — "news_major" (видно в дизайн-доке). Преобразуем здесь.
    TIER_FOR_QUERY_KEY = {
        "pubmed": "pubmed",
        "who": "who",
        "cdc_nejm": "cdc_nejm",
        "minzdrav": "minzdrav",
        "news": "news_major",
    }

    # Какие адаптеры зовём — те, у которых в ClaimQueries есть непустые запросы
    tasks: list[tuple[str, asyncio.Task[tuple[list[Source], str | None]]]] = []
    for query_key, q_list in queries.items():
        if not q_list:
            continue
        tier = TIER_FOR_QUERY_KEY.get(query_key)
        if tier is None:
            log.debug("retriever: неизвестный ключ запросов %r — пропускаю", query_key)
            continue
        adapter = ADAPTERS.get(tier)  # type: ignore[arg-type]
        if adapter is None:
            log.debug("retriever: адаптер %s не зарегистрирован, пропускаю", tier)
            continue
        lang = "ru" if tier in {"minzdrav", "news_major"} else "en"
        tasks.append(
            (tier, asyncio.create_task(_run_adapter(adapter, list(q_list), lang)))
        )

    # Локальный RAG-корпус не имеет своего ключа в QueriesPerSource —
    # ищем по нему параллельно теми же словами, что для научных tier'ов.
    # Если адаптер 'corpus' зарегистрирован и в БД что-то загружено —
    # он вернёт что-то полезное, иначе []. Cost: 1-2 Yandex Embeddings
    # query'а на claim.
    corpus_adapter = ADAPTERS.get("corpus")
    if corpus_adapter is not None:
        # Собираем все «научные» запросы — pubmed/who/cdc_nejm/minzdrav.
        # News-запросы не используем — корпус не про новости.
        corpus_queries: list[str] = []
        for key in ("pubmed", "who", "cdc_nejm", "minzdrav"):
            corpus_queries.extend(queries.get(key, []) or [])  # type: ignore[arg-type]
        # Берём максимум 2 уникальных запроса — embeddings стоят денег
        unique_q = list(dict.fromkeys(q for q in corpus_queries if q.strip()))[:2]
        if unique_q:
            tasks.append((
                "corpus",
                asyncio.create_task(_run_adapter(corpus_adapter, unique_q, "ru")),
            ))

    if not tasks:
        log.info("retriever: ни одного зарегистрированного адаптера — fallback")
        return Evidence(
            claim_text=claim_text,
            sources=[_fallback_pubmed_search_source(claim_text)],
            errors=["no_adapters"],
            retrieved_at=datetime.now(timezone.utc).isoformat(),
        )

    log.info("retriever: зову %d адаптеров для claim=%r",
             len(tasks), claim_text[:60])

    errors: list[str] = []
    all_sources: list[Source] = []
    seen_urls: set[str] = set()
    for tier, task in tasks:
        sources, err = await task
        if err:
            errors.append(err)
        for s in sources:
            url = s.get("url")
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            all_sources.append(s)

    if not all_sources:
        log.info("retriever: ни один адаптер не нашёл источников — fallback на поиск")
        return Evidence(
            claim_text=claim_text,
            sources=[_fallback_pubmed_search_source(claim_text)],
            errors=errors or ["no_results"],
            retrieved_at=datetime.now(timezone.utc).isoformat(),
        )

    # Conflict Classifier для каждого source'а
    classified = await classify_sources(claim_text, all_sources)

    # Кладём stance внутрь Source'а (TypedDict total=False — допустимо)
    annotated: list[Source] = []
    for src, stance in classified:
        src_with_stance = dict(src)
        src_with_stance["stance"] = stance  # type: ignore[typeddict-unknown-key]
        annotated.append(src_with_stance)  # type: ignore[arg-type]

    # Корректируем итоговый score: contradicts/supports получают boost, neutral — пенальти
    def adjusted_score(s: Source) -> float:
        base = float(s.get("score", 0.0))
        st = s.get("stance", "neutral")  # type: ignore[typeddict-item]
        if st in ("supports", "contradicts"):
            return base + 0.1
        return base

    annotated.sort(key=adjusted_score, reverse=True)
    top = annotated[:MAX_SOURCES_PER_CLAIM]

    log.info(
        "retriever: claim=%r → %d sources (%d total, %d errors)",
        claim_text[:60], len(top), len(annotated), len(errors),
    )

    return Evidence(
        claim_text=claim_text,
        sources=top,
        errors=errors,
        retrieved_at=datetime.now(timezone.utc).isoformat(),
    )
