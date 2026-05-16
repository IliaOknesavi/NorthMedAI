"""
agents/query_former.py — построение поисковых запросов из claim'ов.

P0 (этот файл): наивная реализация без LLM — текст claim'а становится
запросом для всех источников. Это не даёт хороших результатов поиска,
но удерживает контракт и позволяет Retriever работать.

P1: настоящий вызов YandexGPT-lite, batch по claim'ам.
Промпт и формат — docs/PROMPTS.md → Query Former.
"""

from __future__ import annotations

import logging

from .types import ClaimQueries, QueriesPerSource, RawClaim

log = logging.getLogger("agents.query_former")


async def make_queries(claim: RawClaim) -> ClaimQueries:
    """
    Сформировать ClaimQueries для одного claim'а.

    P0-реализация: запрос = текст claim'а, отправляется только в pubmed
    (остальные адаптеры пока не зарегистрированы — см. agents/sources/).
    """
    text = claim["text"][:200]
    log.debug("query_former stub: claim=%r → 1 запрос по тексту", text[:80])

    queries: QueriesPerSource = {
        "pubmed": [text],
        "who": [],
        "cdc_nejm": [],
        "minzdrav": [],
        "news": [],
    }

    return ClaimQueries(
        claim_text=claim["text"],
        topic=text[:40],
        queries=queries,
        must_not_contain=[],
        use_news=False,
    )
