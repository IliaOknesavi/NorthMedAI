"""
agents/sources/who.py — World Health Organization через Yandex Search.

API нет. Делаем whitelist-поиск по domain who.int и его субдоменам.
PDF guidelines парсим pdfplumber'ом — берём первые 2 страницы как
snippet (там обычно summary).
"""

from __future__ import annotations

from ..types import SourceTier
from ._whitelist import WhitelistAdapter


class WHOAdapter(WhitelistAdapter):
    tier: SourceTier = "who"
    base_weight: float = 0.85
    WHITELIST_DOMAINS = [
        "who.int",
        "iris.who.int",       # репозиторий WHO документов
        "apps.who.int",
        "extranet.who.int",
    ]
    DEFAULT_LANG = "en"
    FETCH_PDF = True
    MAX_PER_ADAPTER = 3


ADAPTER = WHOAdapter()
