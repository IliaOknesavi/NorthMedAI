"""
test_query_former.py — юнит-тесты Query Former без сети.
Мочим OpenAI-совместимый клиент, проверяем парсинг и fallback'и.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from openai import OpenAIError

from agents import query_former as qf_mod
from agents.types import RawClaim


def _raw(text: str, verdict: str = "false") -> RawClaim:
    return {
        "text": text, "start": 1.0, "verdict": verdict, "type": "claim",
        "explanation": "", "confidence": 0.8,
    }


def _make_fake(out: str | None = None, *, raise_exc: Exception | None = None):
    async def fake_create(**kwargs: Any):
        if raise_exc is not None:
            raise raise_exc
        return SimpleNamespace(output_text=out or "")
    return SimpleNamespace(responses=SimpleNamespace(create=fake_create))


def test_happy_path_parses_queries(monkeypatch: pytest.MonkeyPatch) -> None:
    out = """{
      "queries": [
        {
          "claim_index": 0,
          "topic": "vaccine autism",
          "queries": {
            "pubmed": ["vaccine autism epidemiology", "MMR autism"],
            "who": ["vaccine safety"],
            "cdc_nejm": [],
            "minzdrav": [],
            "news": []
          },
          "must_not_contain": ["antivax"],
          "use_news": false
        }
      ]
    }"""
    monkeypatch.setattr(qf_mod, "_get_client", lambda: _make_fake(out))
    res = asyncio.run(qf_mod.make_queries_batch([_raw("Прививки вызывают аутизм")]))
    assert len(res) == 1
    cq = res[0]
    assert "vaccine autism epidemiology" in cq["queries"]["pubmed"]
    assert cq["queries"]["who"] == ["vaccine safety"]
    assert cq["must_not_contain"] == ["antivax"]
    assert cq["use_news"] is False


def test_empty_input_short_circuits() -> None:
    res = asyncio.run(qf_mod.make_queries_batch([]))
    assert res == []


def test_openai_error_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(qf_mod, "_get_client", lambda: _make_fake(raise_exc=OpenAIError("network")))
    claims = [_raw("X"), _raw("Y")]
    res = asyncio.run(qf_mod.make_queries_batch(claims))
    # Fallback на наивные запросы — каждый текст идёт в pubmed
    assert len(res) == 2
    for cq, c in zip(res, claims):
        assert cq["queries"]["pubmed"] == [c["text"]]


def test_invalid_json_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(qf_mod, "_get_client", lambda: _make_fake("без JSON"))
    res = asyncio.run(qf_mod.make_queries_batch([_raw("Z")]))
    assert res[0]["queries"]["pubmed"] == ["Z"]


def test_missing_claim_indices_filled(monkeypatch: pytest.MonkeyPatch) -> None:
    """LLM вернула меньше — пропуски добиваются fallback'ом."""
    out = """{
      "queries": [
        {"claim_index": 0, "topic": "t", "queries": {"pubmed": ["a"]}, "must_not_contain": [], "use_news": false}
      ]
    }"""
    monkeypatch.setattr(qf_mod, "_get_client", lambda: _make_fake(out))
    res = asyncio.run(qf_mod.make_queries_batch([_raw("A"), _raw("B")]))
    assert res[0]["queries"]["pubmed"] == ["a"]
    assert res[1]["queries"]["pubmed"] == ["B"]


def test_out_of_range_claim_index_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    out = """{
      "queries": [
        {"claim_index": 99, "topic": "x", "queries": {"pubmed": ["wrong"]}, "must_not_contain": [], "use_news": false},
        {"claim_index": 0, "topic": "y", "queries": {"pubmed": ["right"]}, "must_not_contain": [], "use_news": false}
      ]
    }"""
    monkeypatch.setattr(qf_mod, "_get_client", lambda: _make_fake(out))
    res = asyncio.run(qf_mod.make_queries_batch([_raw("A")]))
    assert res[0]["queries"]["pubmed"] == ["right"]


def test_missing_credentials_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom():
        raise RuntimeError("no creds")
    monkeypatch.setattr(qf_mod, "_get_client", boom)
    res = asyncio.run(qf_mod.make_queries_batch([_raw("test")]))
    assert res[0]["queries"]["pubmed"] == ["test"]
