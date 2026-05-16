"""
agents/sources/_whitelist.py — шаблон адаптера на базе Yandex Search.

Конкретные адаптеры (who.py, cdc_nejm.py, minzdrav.py, news_major.py)
наследуют от WhitelistAdapter и задают:
  - tier
  - base_weight
  - WHITELIST_DOMAINS
  - предпочитаемый язык
  - стоит ли качать PDF (для who/minzdrav — да, для news — нет)

Это убирает копипасту между 4-мя адаптерами.
"""

from __future__ import annotations

import asyncio
import logging
from typing import ClassVar

from ..types import Source, SourceTier
from . import _yandex_search
from ._pdf import fetch_pdf_snippet, is_pdf_url

log = logging.getLogger("agents.sources.whitelist")


# Сколько URL'ов реально скачиваем под PDF — отсекаем по топу выдачи,
# чтобы не качать 50 PDF за один claim.
MAX_PDFS_TO_FETCH = 2


class WhitelistAdapter:
    """Базовый адаптер, делегирующий поиск Yandex Search'у с whitelist'ом."""

    tier: ClassVar[SourceTier]
    base_weight: ClassVar[float]
    WHITELIST_DOMAINS: ClassVar[list[str]] = []
    DEFAULT_LANG: ClassVar[str] = "en"
    FETCH_PDF: ClassVar[bool] = False     # тащить ли PDF и парсить
    MAX_PER_ADAPTER: ClassVar[int] = 5    # сколько Source'ов возвращаем максимум

    async def search(self, queries: list[str], lang: str = "") -> list[Source]:
        if not queries:
            return []
        lang_to_use = lang or self.DEFAULT_LANG
        items = await _yandex_search.search(
            queries,
            domains=self.WHITELIST_DOMAINS,
            lang=lang_to_use,
        )
        if not items:
            return []

        # Скачиваем PDF в ограниченном количестве — берём top-N URL'ов.
        pdf_indices: list[int] = []
        if self.FETCH_PDF:
            for i, it in enumerate(items):
                if is_pdf_url(it["url"]) and len(pdf_indices) < MAX_PDFS_TO_FETCH:
                    pdf_indices.append(i)

        pdf_snippets: dict[int, str] = {}
        if pdf_indices:
            log.info("%s adapter: качаю %d PDF для парсинга", self.tier, len(pdf_indices))
            snippets = await asyncio.gather(
                *(fetch_pdf_snippet(items[i]["url"]) for i in pdf_indices),
                return_exceptions=False,
            )
            for i, snippet in zip(pdf_indices, snippets):
                if snippet:
                    pdf_snippets[i] = snippet

        # Готовим Source'ы
        out: list[Source] = []
        total = len(items)
        for rank, it in enumerate(items[: self.MAX_PER_ADAPTER * 2]):
            # Используем PDF-snippet если есть, иначе Yandex Search snippet
            snippet = pdf_snippets.get(rank) or it.get("snippet", "")

            year = _yandex_search.parse_modtime_year(it.get("modtime", ""))
            fresh = _yandex_search.freshness_modifier(year)
            weight = max(0.0, min(1.0, self.base_weight + fresh))

            # Простое ранжирование: позиция в выдаче
            position_score = 1.0 - (rank / max(total, 1)) * 0.5  # 1.0..0.5
            score = round(weight * position_score, 3)

            src: Source = {
                "title": it["title"],
                "url": it["url"],
                "tier": self.tier,
                "weight": weight,
                "relevance": round(position_score, 3),
                "score": score,
                "snippet": snippet,
                "published_at": it.get("modtime", ""),
                "language": lang_to_use,
                "mock": False,
            }
            out.append(src)

        out.sort(key=lambda s: s.get("score", 0.0), reverse=True)
        log.info(
            "%s adapter: вернул %d Source'ов (whitelist=%d домен(а), %d PDF)",
            self.tier, len(out), len(self.WHITELIST_DOMAINS), len(pdf_snippets),
        )
        return out[: self.MAX_PER_ADAPTER]
