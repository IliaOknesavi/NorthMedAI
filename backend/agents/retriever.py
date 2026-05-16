"""
agents/retriever.py — оркестратор source-адаптеров.

P0 (этот файл): возвращает один dummy Source с mock=True для каждого
claim'а. Это заменяет старый `_placeholder_sources` из detector.py, но
живёт уже в правильном слое.

P1+: дёргает зарегистрированные SourceAdapter'ы из agents/sources/ADAPTERS,
собирает кандидатов, прогоняет conflict_classifier, ранжирует, возвращает.

NB: подпись `retrieve(claim_queries)` совпадает с дизайн-доком, но
параллельный fan-out по адаптерам приедет в P1. Сейчас линейно.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from urllib.parse import quote_plus

from .types import ClaimQueries, Evidence, Source

log = logging.getLogger("agents.retriever")


def _dummy_pubmed_source(claim_text: str) -> Source:
    """
    Placeholder в стиле старого _placeholder_sources из detector.py:
    ссылка ведёт на страницу поиска PubMed по тексту claim'а. Это
    НЕ выдуманный URL — клик ведёт на реальную страницу с результатами.

    Помечен mock=True, чтобы фронт и аналитика могли отличить от настоящих.
    """
    q = quote_plus(claim_text[:120])
    return Source(
        title="PubMed (поиск)",
        url=f"https://pubmed.ncbi.nlm.nih.gov/?term={q}",
        tier="pubmed",
        weight=0.9,
        mock=True,
    )


async def retrieve(claim_queries: ClaimQueries) -> Evidence:
    """
    Найти источники для одного claim'а.

    P0: возвращает один mock-Source — поиск PubMed по тексту claim'а.
    """
    claim_text = claim_queries["claim_text"]
    log.debug("retriever stub: claim=%r → 1 mock PubMed source", claim_text[:80])

    return Evidence(
        claim_text=claim_text,
        sources=[_dummy_pubmed_source(claim_text)],
        errors=[],
        retrieved_at=datetime.now(timezone.utc).isoformat(),
    )
