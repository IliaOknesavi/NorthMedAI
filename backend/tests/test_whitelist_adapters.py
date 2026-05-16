"""
test_whitelist_adapters.py — WHO/CDC/Минздрав/News адаптеры без сети.

Все 4 используют общий WhitelistAdapter поверх _yandex_search.search.
Мочим только этот клиент, проверяем что адаптер корректно строит Source'ы.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from agents.sources import _whitelist, _yandex_search
from agents.sources.cdc_nejm import ADAPTER as CDC_NEJM_ADAPTER
from agents.sources.minzdrav import ADAPTER as MINZDRAV_ADAPTER
from agents.sources.news_major import ADAPTER as NEWS_ADAPTER
from agents.sources.who import ADAPTER as WHO_ADAPTER


def _fake_search(items: list[dict[str, Any]]):
    """Возвращает async-функцию, которая возвращает items как результат поиска."""
    async def fake(queries, *, domains=None, lang="en", per_query=5):
        return items
    return fake


def test_who_adapter_builds_sources(monkeypatch: pytest.MonkeyPatch) -> None:
    items = [
        {
            "url": "https://who.int/guideline1.pdf",
            "title": "WHO Guideline 1",
            "snippet": "snippet from yandex search",
            "modtime": "20230515T100000",
        },
    ]
    monkeypatch.setattr(_yandex_search, "search", _fake_search(items))
    # Принудительно отключим PDF-fetch чтобы не лезть в сеть
    monkeypatch.setattr(WHO_ADAPTER, "FETCH_PDF", False)

    sources = asyncio.run(WHO_ADAPTER.search(["vaccine safety"]))
    assert len(sources) == 1
    s = sources[0]
    assert s["tier"] == "who"
    assert s["url"] == "https://who.int/guideline1.pdf"
    assert s["weight"] > 0.85   # base + freshness modifier
    assert s["mock"] is False


def test_cdc_nejm_adapter_does_not_fetch_pdf(monkeypatch: pytest.MonkeyPatch) -> None:
    """CDC/NEJM не качает PDF — берёт snippet от Yandex Search."""
    items = [{
        "url": "https://cdc.gov/article.html",
        "title": "CDC article",
        "snippet": "yandex snippet text",
        "modtime": "",
    }]
    monkeypatch.setattr(_yandex_search, "search", _fake_search(items))

    sources = asyncio.run(CDC_NEJM_ADAPTER.search(["topic"]))
    assert sources[0]["snippet"] == "yandex snippet text"
    assert sources[0]["tier"] == "cdc_nejm"
    assert CDC_NEJM_ADAPTER.FETCH_PDF is False


def test_minzdrav_adapter_lang_ru(monkeypatch: pytest.MonkeyPatch) -> None:
    """Минздрав по умолчанию ru-язык."""
    captured: dict[str, Any] = {}

    async def fake_search(queries, *, domains=None, lang="en", per_query=5):
        captured["lang"] = lang
        captured["domains"] = domains
        return []

    monkeypatch.setattr(_yandex_search, "search", fake_search)
    asyncio.run(MINZDRAV_ADAPTER.search(["клинические рекомендации"]))
    assert captured["lang"] == "ru"
    assert "minzdrav.gov.ru" in captured["domains"]


def test_news_adapter_short_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    """News возвращает максимум MAX_PER_ADAPTER=2 источников."""
    items = [
        {"url": f"https://reuters.com/a{i}", "title": f"Article {i}",
         "snippet": "", "modtime": ""}
        for i in range(10)
    ]
    monkeypatch.setattr(_yandex_search, "search", _fake_search(items))
    sources = asyncio.run(NEWS_ADAPTER.search(["вспышка кори"]))
    assert len(sources) <= NEWS_ADAPTER.MAX_PER_ADAPTER == 2


def test_empty_search_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """Если Yandex Search ничего не вернул — адаптер возвращает []."""
    monkeypatch.setattr(_yandex_search, "search", _fake_search([]))
    res = asyncio.run(WHO_ADAPTER.search(["whatever"]))
    assert res == []


def test_pdf_fetch_used_when_url_is_pdf(monkeypatch: pytest.MonkeyPatch) -> None:
    """Адаптер с FETCH_PDF=True должен пытаться скачать PDF."""
    items = [{
        "url": "https://who.int/file.pdf", "title": "WHO PDF",
        "snippet": "search snippet", "modtime": "",
    }]
    monkeypatch.setattr(_yandex_search, "search", _fake_search(items))

    pdf_calls: list[str] = []

    async def fake_pdf(url):
        pdf_calls.append(url)
        return "parsed pdf content"

    monkeypatch.setattr(_whitelist, "fetch_pdf_snippet", fake_pdf)
    monkeypatch.setattr(WHO_ADAPTER, "FETCH_PDF", True)

    sources = asyncio.run(WHO_ADAPTER.search(["x"]))
    assert pdf_calls == ["https://who.int/file.pdf"]
    assert "parsed pdf content" in sources[0]["snippet"]
