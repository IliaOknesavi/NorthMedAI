"""
test_yandex_search.py — общий клиент Yandex Search XML API без сети.
Мочим httpx, проверяем парсинг XML и graceful fallback'и.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx
import pytest

from agents.sources import _yandex_search as ys


VALID_XML = """<?xml version="1.0" encoding="utf-8"?>
<yandexsearch version="1.0">
  <response>
    <results>
      <grouping>
        <group>
          <doc>
            <url>https://who.int/guideline-1</url>
            <title>WHO guideline on <hlword>vaccines</hlword></title>
            <passages>
              <passage>Vaccines are safe and effective when administered by qualified providers.</passage>
            </passages>
            <modtime>20230515T100000</modtime>
          </doc>
        </group>
        <group>
          <doc>
            <url>https://who.int/another.pdf</url>
            <title>Another guideline</title>
            <passages>
              <passage>Short snippet.</passage>
            </passages>
            <modtime>20100315T100000</modtime>
          </doc>
        </group>
      </grouping>
    </results>
  </response>
</yandexsearch>"""

ERROR_XML = """<?xml version="1.0" encoding="utf-8"?>
<yandexsearch>
  <response>
    <error code="42">Invalid API key</error>
  </response>
</yandexsearch>"""


def test_parse_year_from_modtime() -> None:
    assert ys.parse_modtime_year("20230515T100000") == 2023
    assert ys.parse_modtime_year("") is None
    assert ys.parse_modtime_year("garbage") is None


def test_parse_xml_extracts_two_docs() -> None:
    items = ys._parse_yandex_search_xml(VALID_XML)
    assert len(items) == 2
    assert items[0]["url"] == "https://who.int/guideline-1"
    # hlword внутри title должен быть схлопнут в plain text
    assert "<hlword>" not in items[0]["title"]
    assert "vaccines" in items[0]["title"].lower()
    assert "safe and effective" in items[0]["snippet"]


def test_parse_xml_returns_empty_on_error() -> None:
    items = ys._parse_yandex_search_xml(ERROR_XML)
    assert items == []


def test_parse_xml_returns_empty_on_garbage() -> None:
    items = ys._parse_yandex_search_xml("not xml at all <<<")
    assert items == []


def test_join_sites_or_filter() -> None:
    f = ys._join_sites(["a.com", "b.com"])
    assert "site:a.com" in f
    assert "site:b.com" in f
    assert "|" in f


def test_search_without_credentials_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """Без ключа клиент возвращает [] и пишет WARNING — не падает."""
    monkeypatch.setattr(ys, "YANDEX_SEARCH_API_KEY", "")
    res = asyncio.run(ys.search(["test"]))
    assert res == []


def test_search_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """Мокаем httpx, проверяем что вернутся 2 нормальных результата."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=VALID_XML)

    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient
    monkeypatch.setattr(ys, "YANDEX_SEARCH_API_KEY", "fake_key")
    monkeypatch.setattr(ys, "YANDEX_FOLDER_ID", "fake_folder")
    monkeypatch.setattr(
        "agents.sources._yandex_search.httpx.AsyncClient",
        lambda *a, **kw: real_client(*a, **{**kw, "transport": transport}),
    )

    items = asyncio.run(ys.search(["vaccine safety"], domains=["who.int"]))
    assert len(items) == 2
    assert all("who.int" in it["url"] for it in items)


def test_search_http_error_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """HTTP 500 → []. Pipeline не падает."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="oops")

    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient
    monkeypatch.setattr(ys, "YANDEX_SEARCH_API_KEY", "fake")
    monkeypatch.setattr(ys, "YANDEX_FOLDER_ID", "folder")
    monkeypatch.setattr(
        "agents.sources._yandex_search.httpx.AsyncClient",
        lambda *a, **kw: real_client(*a, **{**kw, "transport": transport}),
    )

    res = asyncio.run(ys.search(["x"], domains=["who.int"]))
    assert res == []


def test_search_dedups_by_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """Одинаковые url в разных query'ях не дублируются."""
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        return httpx.Response(200, text=VALID_XML)

    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient
    monkeypatch.setattr(ys, "YANDEX_SEARCH_API_KEY", "fake")
    monkeypatch.setattr(ys, "YANDEX_FOLDER_ID", "folder")
    monkeypatch.setattr(
        "agents.sources._yandex_search.httpx.AsyncClient",
        lambda *a, **kw: real_client(*a, **{**kw, "transport": transport}),
    )

    res = asyncio.run(ys.search(["a", "b"]))
    # 2 query => 2 запроса в API
    assert call_count["n"] == 2
    # Но URL'ы в результате дедуплицированы (VALID_XML возвращается дважды)
    urls = [r["url"] for r in res]
    assert len(urls) == len(set(urls))
