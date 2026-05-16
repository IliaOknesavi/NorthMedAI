"""
test_pubmed.py — юнит-тесты PubMed adapter без сети.

Сетевые вызовы (esearch / esummary) мочим через httpx mock transport.
Проверяем парсинг JSON-ответов NCBI, ранжирование, freshness, дедуп.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx
import pytest

from agents.sources.pubmed import (
    PubMedAdapter,
    _freshness_modifier,
    _parse_pubdate_year,
)


# --- Unit-функции -------------------------------------------------------

def test_parse_year_basic() -> None:
    assert _parse_pubdate_year("2020 Mar 15") == 2020
    assert _parse_pubdate_year("1999") == 1999
    assert _parse_pubdate_year("") is None
    assert _parse_pubdate_year("garbage") is None
    # > 2100 не принимаем
    assert _parse_pubdate_year("3025 Jan 1") is None


def test_freshness_modifier_boundaries() -> None:
    current = datetime.utcnow().year
    assert _freshness_modifier(current) == 0.05
    assert _freshness_modifier(current - 5) == 0.05
    assert _freshness_modifier(current - 10) == 0.0  # между 5 и 15 — нейтрально
    assert _freshness_modifier(current - 15) == -0.10
    assert _freshness_modifier(current - 30) == -0.10
    assert _freshness_modifier(None) == 0.0


# --- HTTP fixtures -----------------------------------------------------

def _esearch_response(pmids: list[str]) -> httpx.Response:
    body = {"esearchresult": {"idlist": pmids, "count": str(len(pmids))}}
    return httpx.Response(200, json=body)


def _esummary_response(items: dict[str, dict]) -> httpx.Response:
    body = {"result": {"uids": list(items.keys()), **items}}
    return httpx.Response(200, json=body)


def _build_mock_transport(handler) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


# --- Adapter -----------------------------------------------------------

def test_adapter_search_returns_sources_with_pmid_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """Happy path: esearch вернул PMID'ы, esummary — мета, получаем Source'ы."""

    def handler(request: httpx.Request) -> httpx.Response:
        if "esearch" in request.url.path:
            return _esearch_response(["111", "222"])
        if "esummary" in request.url.path:
            return _esummary_response({
                "111": {"title": "Article A", "pubdate": "2022 Jan", "source": "JAMA"},
                "222": {"title": "Article B", "pubdate": "2010 Mar", "source": "BMJ"},
            })
        return httpx.Response(404)

    transport = _build_mock_transport(handler)

    # Monkey-patch AsyncClient чтобы он использовал mock transport
    real_client = httpx.AsyncClient

    def patched_client(*args, **kwargs):
        kwargs.setdefault("transport", transport)
        return real_client(*args, **kwargs)

    monkeypatch.setattr("agents.sources.pubmed.httpx.AsyncClient", patched_client)

    adapter = PubMedAdapter()
    sources = asyncio.run(adapter.search(["vaccine autism"]))

    assert len(sources) == 2
    urls = [s["url"] for s in sources]
    assert all(u.startswith("https://pubmed.ncbi.nlm.nih.gov/") for u in urls)
    titles = [s["title"] for s in sources]
    assert "Article A" in titles
    assert "Article B" in titles

    for s in sources:
        assert s["tier"] == "pubmed"
        assert s["mock"] is False
        assert 0.0 <= s["weight"] <= 1.0


def test_adapter_dedups_queries(monkeypatch: pytest.MonkeyPatch) -> None:
    """Дубли в queries → только один поход в esearch."""

    esearch_calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if "esearch" in request.url.path:
            esearch_calls.append(str(request.url.params.get("term", "")))
            return _esearch_response(["1"])
        if "esummary" in request.url.path:
            return _esummary_response({"1": {"title": "X", "pubdate": "2024"}})
        return httpx.Response(404)

    transport = _build_mock_transport(handler)
    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        "agents.sources.pubmed.httpx.AsyncClient",
        lambda *a, **kw: real_client(*a, **{**kw, "transport": transport}),
    )

    adapter = PubMedAdapter()
    asyncio.run(adapter.search(["abc", "abc", "abc"]))
    assert len(esearch_calls) == 1, f"expected 1 esearch call, got {len(esearch_calls)}: {esearch_calls}"


def test_adapter_empty_queries_returns_empty() -> None:
    adapter = PubMedAdapter()
    res = asyncio.run(adapter.search([]))
    assert res == []


def test_adapter_esearch_failure_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """Если esearch упал — возвращаем []. Pipeline не падает."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    transport = _build_mock_transport(handler)
    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        "agents.sources.pubmed.httpx.AsyncClient",
        lambda *a, **kw: real_client(*a, **{**kw, "transport": transport}),
    )

    adapter = PubMedAdapter()
    res = asyncio.run(adapter.search(["query"]))
    assert res == []


def test_adapter_freshness_modifies_weight(monkeypatch: pytest.MonkeyPatch) -> None:
    """Свежая статья получает +0.05 к base_weight, старая — -0.10."""
    current = datetime.utcnow().year

    def handler(request: httpx.Request) -> httpx.Response:
        if "esearch" in request.url.path:
            return _esearch_response(["fresh", "old"])
        if "esummary" in request.url.path:
            return _esummary_response({
                "fresh": {"title": "Fresh", "pubdate": f"{current - 1} Jan"},
                "old": {"title": "Old", "pubdate": f"{current - 20} Jan"},
            })
        return httpx.Response(404)

    transport = _build_mock_transport(handler)
    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        "agents.sources.pubmed.httpx.AsyncClient",
        lambda *a, **kw: real_client(*a, **{**kw, "transport": transport}),
    )

    sources = asyncio.run(PubMedAdapter().search(["topic"]))
    by_title = {s["title"]: s for s in sources}
    assert by_title["Fresh"]["weight"] > by_title["Old"]["weight"]
