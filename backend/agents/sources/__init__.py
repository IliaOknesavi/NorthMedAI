"""
agents/sources/ — адаптеры для разных источников доказательной базы.

Каждый адаптер реализует SourceAdapter (см. _base.py) и регистрируется
в реестре ADAPTERS. Retriever выбирает адаптеры по tier'у из ClaimQueries.

P0: реестр пустой. P1 добавит PubMed. P2 — WHO/CDC/Минздрав/news.
"""

from __future__ import annotations

from ..types import SourceTier
from ._base import SourceAdapter
from .pubmed import ADAPTER as _PUBMED

# Реестр адаптеров. Ключ — SourceTier (та же категория, что в ClaimQueries),
# значение — экземпляр адаптера. Retriever зовёт по этому реестру.
ADAPTERS: dict[SourceTier, SourceAdapter] = {
    "pubmed": _PUBMED,
}

__all__ = ["SourceAdapter", "ADAPTERS"]
