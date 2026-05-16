"""
test_final_qa.py — фиксирует контракт Final QA-агента.

Без сети — клиент монки-патчится. Проверяет:
  - empty input;
  - keep на всё → claims идут как есть;
  - drop с reason;
  - repair меняет verdict и/или explanation;
  - dedup_into схлопывает дубликаты и объединяет sources;
  - merge_into не в claim_indices → dedup игнорируется;
  - claim_index вне диапазона → action игнорируется, остальные применяются;
  - невалидный JSON → fallback на input без изменений;
  - OpenAIError → fallback на input;
  - нет .env креденшелов → fallback на input.
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

from agents import final_qa as qa_mod
from agents.types import FinalClaim, Snippet


# --- Фикстуры ----------------------------------------------------------

def _snippets() -> list[Snippet]:
    return [
        {"text": "О мифах", "start": 0.0, "duration": 2.0},
        {"text": "Прививки якобы вызывают аутизм", "start": 12.3, "duration": 3.0},
        {"text": "Но это опровергнуто", "start": 18.0, "duration": 2.0},
        {"text": "А витамин D полезен", "start": 100.0, "duration": 2.0},
        {"text": "Дозы обсуждаются", "start": 110.0, "duration": 2.0},
    ]


def _claims() -> list[FinalClaim]:
    """Три claim'а: 0 — нормальный, 1 — кандидат на drop (правда), 2 — для repair."""
    return [
        {
            "text": "Прививки вызывают аутизм", "start": 12.3,
            "verdict": "false", "type": "claim",
            "explanation": "Неверно: связи нет (PubMed).",
            "confidence": 0.9,
            "sources": [{"title": "PubMed", "url": "https://pubmed/abc",
                          "tier": "pubmed", "weight": 0.9, "mock": True}],
            "stance": "asserted", "stance_missing": "",
            "extractor_verdict": "false", "judge_notes": "",
            "search_queries": None,
        },
        {
            "text": "Свежие овощи быстро теряют полезность", "start": 25.0,
            "verdict": "misleading", "type": "claim",
            "explanation": "Спорно",
            "confidence": 0.6,
            "sources": [{"title": "PubMed", "url": "https://pubmed/xyz",
                          "tier": "pubmed", "weight": 0.9, "mock": True}],
            "stance": "asserted", "stance_missing": "",
            "extractor_verdict": "misleading", "judge_notes": "",
            "search_queries": None,
        },
        {
            "text": "Витамин D полезен", "start": 100.0,
            "verdict": "false", "type": "claim",
            "explanation": "Спорно",
            "confidence": 0.5,
            "sources": [{"title": "PubMed", "url": "https://pubmed/vitd1",
                          "tier": "pubmed", "weight": 0.9, "mock": True}],
            "stance": "asserted", "stance_missing": "",
            "extractor_verdict": "false", "judge_notes": "",
            "search_queries": None,
        },
    ]


def _make_fake(output: str | None = None, *, raise_exc: Exception | None = None):
    async def fake_create(**kwargs: Any):
        if raise_exc is not None:
            raise raise_exc
        return SimpleNamespace(output_text=output or "")
    return SimpleNamespace(responses=SimpleNamespace(create=fake_create))


# --- Тесты -------------------------------------------------------------

def test_empty_claims_returns_empty() -> None:
    res = asyncio.run(qa_mod.qa_pass(_snippets(), []))
    assert res["claims"] == []
    assert res["actions"] == []


def test_keep_all(monkeypatch: pytest.MonkeyPatch) -> None:
    """Все keep — claims идут как есть."""
    output = """{
      "actions": [
        {"claim_indices":[0],"action":"keep","reason":"","merge_into":null,"patch_verdict":null,"patch_explanation":null},
        {"claim_indices":[1],"action":"keep","reason":"","merge_into":null,"patch_verdict":null,"patch_explanation":null},
        {"claim_indices":[2],"action":"keep","reason":"","merge_into":null,"patch_verdict":null,"patch_explanation":null}
      ]
    }"""
    monkeypatch.setattr(qa_mod, "_get_client", lambda: _make_fake(output))

    claims = _claims()
    res = asyncio.run(qa_mod.qa_pass(_snippets(), claims))
    assert len(res["claims"]) == 3
    assert res["claims"][0]["text"] == "Прививки вызывают аутизм"


def test_drop_removes_claim(monkeypatch: pytest.MonkeyPatch) -> None:
    """drop убирает claim, остальные остаются."""
    output = """{
      "actions": [
        {"claim_indices":[1],"action":"drop","reason":"Это верный факт автора, Extractor зря выписал.","merge_into":null,"patch_verdict":null,"patch_explanation":null}
      ]
    }"""
    monkeypatch.setattr(qa_mod, "_get_client", lambda: _make_fake(output))

    res = asyncio.run(qa_mod.qa_pass(_snippets(), _claims()))
    assert len(res["claims"]) == 2
    texts = [c["text"] for c in res["claims"]]
    assert "Свежие овощи быстро теряют полезность" not in texts


def test_repair_changes_verdict_and_explanation(monkeypatch: pytest.MonkeyPatch) -> None:
    """repair применяет patch_verdict и patch_explanation."""
    output = """{
      "actions": [
        {"claim_indices":[2],"action":"repair","reason":"Verdict не согласован.",
         "merge_into":null,
         "patch_verdict":"conflicting",
         "patch_explanation":"Неоднозначно: общая полезность не оспаривается."}
      ]
    }"""
    monkeypatch.setattr(qa_mod, "_get_client", lambda: _make_fake(output))

    res = asyncio.run(qa_mod.qa_pass(_snippets(), _claims()))
    repaired = next(c for c in res["claims"] if c["text"] == "Витамин D полезен")
    assert repaired["verdict"] == "conflicting"
    assert "Неоднозначно" in repaired["explanation"]
    # extractor_verdict не должен меняться — это аудит-поле истории
    assert repaired["extractor_verdict"] == "false"


def test_dedup_merges_sources(monkeypatch: pytest.MonkeyPatch) -> None:
    """dedup_into схлопывает дубликаты, sources объединяются (по url)."""
    claims = _claims()
    # Делаем claim'ы [0] и [1] дубликатами с разными источниками
    claims[1]["text"] = "Прививки и аутизм связаны"  # дубликат [0]
    claims[1]["sources"] = [{
        "title": "WHO", "url": "https://who/abc",
        "tier": "who", "weight": 0.85, "mock": True,
    }]

    output = """{
      "actions": [
        {"claim_indices":[0,1],"action":"dedup_into","reason":"Один миф разными словами.",
         "merge_into":0,"patch_verdict":null,"patch_explanation":null}
      ]
    }"""
    monkeypatch.setattr(qa_mod, "_get_client", lambda: _make_fake(output))

    res = asyncio.run(qa_mod.qa_pass(_snippets(), claims))
    # 3 - 1 (dedup) = 2 claim'а
    assert len(res["claims"]) == 2
    main = next(c for c in res["claims"] if c["text"] == "Прививки вызывают аутизм")
    urls = {s["url"] for s in main["sources"]}
    assert "https://pubmed/abc" in urls
    assert "https://who/abc" in urls  # из дубликата подтянут


def test_dedup_invalid_merge_into_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    """Если merge_into не в claim_indices — dedup игнорируется."""
    output = """{
      "actions": [
        {"claim_indices":[0,1],"action":"dedup_into","reason":"x",
         "merge_into":5,"patch_verdict":null,"patch_explanation":null}
      ]
    }"""
    monkeypatch.setattr(qa_mod, "_get_client", lambda: _make_fake(output))

    res = asyncio.run(qa_mod.qa_pass(_snippets(), _claims()))
    # action игнорирован → все 3 на месте
    assert len(res["claims"]) == 3


def test_out_of_range_index_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    """claim_index вне диапазона → этот action игнорируется."""
    output = """{
      "actions": [
        {"claim_indices":[10],"action":"drop","reason":"x","merge_into":null,"patch_verdict":null,"patch_explanation":null},
        {"claim_indices":[1],"action":"drop","reason":"valid","merge_into":null,"patch_verdict":null,"patch_explanation":null}
      ]
    }"""
    monkeypatch.setattr(qa_mod, "_get_client", lambda: _make_fake(output))

    res = asyncio.run(qa_mod.qa_pass(_snippets(), _claims()))
    # Первый action игнорируется, второй применяется → 2 claim'а
    assert len(res["claims"]) == 2


def test_invalid_json_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    """Невалидный JSON → claims без изменений."""
    monkeypatch.setattr(qa_mod, "_get_client", lambda: _make_fake("какая-то ерунда"))
    claims = _claims()
    res = asyncio.run(qa_mod.qa_pass(_snippets(), claims))
    assert len(res["claims"]) == 3
    assert res["errors"]


def test_openai_error_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    """OpenAIError → claims без изменений."""
    monkeypatch.setattr(
        qa_mod, "_get_client",
        lambda: _make_fake(raise_exc=OpenAIError("network down")),
    )
    res = asyncio.run(qa_mod.qa_pass(_snippets(), _claims()))
    assert len(res["claims"]) == 3
    assert res["errors"]


def test_missing_credentials_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    """Без креденшелов QA не падает, отдаёт claims как есть."""
    def boom():
        raise RuntimeError("creds missing")
    monkeypatch.setattr(qa_mod, "_get_client", boom)
    res = asyncio.run(qa_mod.qa_pass(_snippets(), _claims()))
    assert len(res["claims"]) == 3
    assert res["errors"]


def test_combined_repair_then_drop(monkeypatch: pytest.MonkeyPatch) -> None:
    """Один claim может пройти через repair (поправили), другой — drop."""
    output = """{
      "actions": [
        {"claim_indices":[2],"action":"repair","reason":"x",
         "merge_into":null,"patch_verdict":"misleading","patch_explanation":null},
        {"claim_indices":[1],"action":"drop","reason":"y",
         "merge_into":null,"patch_verdict":null,"patch_explanation":null}
      ]
    }"""
    monkeypatch.setattr(qa_mod, "_get_client", lambda: _make_fake(output))
    res = asyncio.run(qa_mod.qa_pass(_snippets(), _claims()))
    assert len(res["claims"]) == 2  # 1 дропнут, остался 0 и поправленный 2
    repaired = next(c for c in res["claims"] if c["text"] == "Витамин D полезен")
    assert repaired["verdict"] == "misleading"
