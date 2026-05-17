"""
agents/sources/news_major.py — крупные новостные агентства.

Используется только для claim'ов с use_news=true в ClaimQueries (вспышка
инфекции, отзыв препарата, новые рекомендации). Query Former сам решает.

Веса низкие (0.6) — новость не равна научной публикации, но иногда
быстрее реагирует на свежие события.
"""

from __future__ import annotations

from ..types import SourceTier
from ._whitelist import WhitelistAdapter


class NewsMajorAdapter(WhitelistAdapter):
    tier: SourceTier = "news_major"
    base_weight: float = 0.60
    WHITELIST_DOMAINS = [
        "reuters.com",
        "apnews.com",
        "bbc.com",
        "bbc.co.uk",
        "tass.ru",
        "ria.ru",
        "kommersant.ru",
        "vedomosti.ru",
    ]
    DEFAULT_LANG = "ru"     # запросы у Query Former обычно русские
    FETCH_PDF = False
    MAX_PER_ADAPTER = 2


ADAPTER = NewsMajorAdapter()
