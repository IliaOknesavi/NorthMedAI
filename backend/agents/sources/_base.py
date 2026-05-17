"""
agents/sources/_base.py — общий интерфейс адаптеров источников.

Каждый адаптер инкапсулирует один tier (PubMed, WHO, CDC, ...).
Поведение наружу одинаковое: получи список запросов, верни список Source.
"""

from __future__ import annotations

from typing import Protocol

from ..types import Source, SourceTier


class SourceAdapter(Protocol):
    """Контракт адаптера. Все адаптеры реализуют этот Protocol."""

    tier: SourceTier
    base_weight: float

    async def search(self, queries: list[str], lang: str = "en") -> list[Source]:
        """
        Выполнить поиск по списку запросов в этом источнике.

        Вернёт до 5 кандидатов, отсортированных по убыванию релевантности.
        НИКОГДА не бросает исключения — на ошибке возвращает [] и логирует.
        """
        ...
