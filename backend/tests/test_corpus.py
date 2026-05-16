"""
test_corpus.py — corpus adapter и helper'ы без сети/БД.

Полноценный e2e тестировать с pgvector не будем — слишком зависит от
инфраструктуры. Проверяем форматирование вектора и graceful fallback'и.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from agents.sources import _embeddings as emb_mod
from agents.sources import corpus as corpus_mod


def test_pgvector_literal_format() -> None:
    """Векторный литерал должен быть в формате '[a,b,c,...]'."""
    s = corpus_mod._format_pgvector_param([0.1, 0.2, -0.3])
    assert s.startswith("[") and s.endswith("]")
    # 6 знаков после запятой
    parts = s[1:-1].split(",")
    assert len(parts) == 3
    assert all("." in p for p in parts)


def test_corpus_returns_empty_without_embeddings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Если embeddings не настроены — corpus сразу пусто."""
    monkeypatch.setattr(emb_mod, "YANDEX_API_KEY", "")
    monkeypatch.setattr(corpus_mod, "embeddings_configured", lambda: False)

    res = asyncio.run(corpus_mod.CorpusAdapter().search(["test"]))
    assert res == []


def test_corpus_empty_queries() -> None:
    """Пустой список запросов → []."""
    res = asyncio.run(corpus_mod.CorpusAdapter().search([]))
    assert res == []


def test_corpus_embed_failure_yields_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """Если embed вернул None для всех query — пусто."""

    async def fake_embed(text, *, kind="doc"):
        return None

    monkeypatch.setattr(corpus_mod, "embeddings_configured", lambda: True)
    monkeypatch.setattr(corpus_mod, "embed", fake_embed)

    # SessionLocal будет недоступен в тесте — но мы не дойдём, потому что
    # embed возвращает None и весь цикл пропускается.
    res = asyncio.run(corpus_mod.CorpusAdapter().search(["query"]))
    assert res == []


def test_chunker_creates_overlap() -> None:
    """Проверяем sliding-window чанкер из corpus_ingest."""
    from corpus_ingest import _chunk

    text = "a" * 2500
    chunks = _chunk(text, size=1000, overlap=100)
    # 2500 / (1000-100) = ~3 chunks
    assert len(chunks) == 3
    # последний обрезается тем чем осталось
    assert all(len(c) <= 1000 for c in chunks)


def test_chunker_short_text() -> None:
    """Текст короче чанка — один кусок."""
    from corpus_ingest import _chunk
    chunks = _chunk("short text", size=1000, overlap=100)
    assert chunks == ["short text"]


def test_chunker_empty() -> None:
    from corpus_ingest import _chunk
    assert _chunk("") == []
    assert _chunk("   \n\n  ") == []
