"""
agents/sources/ — адаптеры для разных источников доказательной базы.

Каждый адаптер реализует SourceAdapter (см. _base.py) и регистрируется
в реестре ADAPTERS. Retriever выбирает адаптеры по tier'у из ClaimQueries.

P0: реестр пустой. P1 добавит PubMed. P2 — WHO/CDC/Минздрав/news.
"""

from __future__ import annotations

from ..types import SourceTier
from ._base import SourceAdapter
from .cdc_nejm import ADAPTER as _CDC_NEJM
from .minzdrav import ADAPTER as _MINZDRAV
from .news_major import ADAPTER as _NEWS_MAJOR
from .pubmed import ADAPTER as _PUBMED
from .who import ADAPTER as _WHO

# Реестр адаптеров. Ключ — SourceTier (та же категория, что в ClaimQueries),
# значение — экземпляр адаптера. Retriever зовёт по этому реестру.
#
# Адаптеры на whitelist'е Yandex Search (WHO/CDC/Minzdrav/News) НЕ требуют
# отдельной проверки на наличие ключа — общий клиент `_yandex_search.py`
# сам делает graceful fallback на пустой список при отсутствии
# YANDEX_SEARCH_API_KEY в .env.
ADAPTERS: dict[SourceTier, SourceAdapter] = {
    "pubmed": _PUBMED,
    "who": _WHO,
    "cdc_nejm": _CDC_NEJM,
    "minzdrav": _MINZDRAV,
    "news_major": _NEWS_MAJOR,
}

__all__ = ["SourceAdapter", "ADAPTERS"]
