"""
agents/sources/minzdrav.py — Минздрав РФ и связанные ведомства.

Whitelist охватывает:
  - minzdrav.gov.ru — головной сайт министерства
  - cr.minzdrav.gov.ru — клинические рекомендации (большинство в PDF)
  - roszdravnadzor.gov.ru — Росздравнадзор

Веса ниже чем у WHO/CDC, потому что российские документы иногда отстают
от международных guideline-обновлений (см. docs/RAG_ARCHITECTURE.md §2).
"""

from __future__ import annotations

from ..types import SourceTier
from ._whitelist import WhitelistAdapter


class MinzdravAdapter(WhitelistAdapter):
    tier: SourceTier = "minzdrav"
    base_weight: float = 0.70
    WHITELIST_DOMAINS = [
        "minzdrav.gov.ru",
        "cr.minzdrav.gov.ru",
        "roszdravnadzor.gov.ru",
    ]
    DEFAULT_LANG = "ru"
    FETCH_PDF = True
    MAX_PER_ADAPTER = 3


ADAPTER = MinzdravAdapter()
