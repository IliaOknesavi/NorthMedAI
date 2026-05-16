"""
agents/sources/ — адаптеры для разных источников доказательной базы.

Каждый адаптер реализует SourceAdapter (см. _base.py) и регистрируется
в реестре ADAPTERS. Retriever выбирает адаптеры по tier'у из ClaimQueries.

P0: реестр пустой. P1 добавит PubMed. P2 — WHO/CDC/Минздрав/news.
"""

from __future__ import annotations

from ..types import SourceTier
from ._base import SourceAdapter

# Реестр адаптеров. Ключ — SourceTier, значение — экземпляр адаптера.
# P0 пустой; в P1 появится `ADAPTERS["pubmed"] = PubMedAdapter()` и т.д.
ADAPTERS: dict[SourceTier, SourceAdapter] = {}

__all__ = ["SourceAdapter", "ADAPTERS"]
