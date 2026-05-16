"""
test_pipeline_skeleton.py — фиксирует контракт P0-pipeline'а.

Эти тесты НЕ ходят в LLM и НЕ требуют БД. Они проверяют, что:
  - все агенты собираются и возвращают типы согласно agents/types.py;
  - pipeline.enrich() корректно обрабатывает пустой вход;
  - формат FinalClaim совместим с тем, что save_analysis ждёт от dict'ов;
  - инварианты P0: stance="asserted" для всех, sources с mock=True;
  - debunked_fully-метки фильтруются (моделируем через monkey-patch stance).

Запуск:
    cd backend && python -m pytest tests/ -v
или одиночный файл:
    cd backend && python -m pytest tests/test_pipeline_skeleton.py -v
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

# Тесты живут в backend/tests/, импорты делаются от backend/
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from agents import judge as judge_mod
from agents import query_former as qf_mod
from agents import retriever as ret_mod
from agents import stance as stance_mod
from agents.pipeline import enrich
from agents.types import RawClaim, Snippet, StanceLabel


# --- Фикстуры ----------------------------------------------------------

def _make_snippets() -> list[Snippet]:
    return [
        {"text": "Поговорим о популярных мифах", "start": 0.0, "duration": 3.0},
        {"text": "Прививки якобы вызывают аутизм", "start": 12.3, "duration": 4.0},
        {"text": "Сода лечит рак", "start": 45.1, "duration": 2.0},
    ]


def _make_raw_claims() -> list[RawClaim]:
    return [
        {
            "text": "Прививки вызывают аутизм",
            "start": 12.3,
            "verdict": "false",
            "type": "claim",
            "explanation": "Неверно: разоблачено",
            "confidence": 0.9,
        },
        {
            "text": "Сода лечит рак",
            "start": 45.1,
            "verdict": "false",
            "type": "claim",
            "explanation": "Неверно: научно опровергнуто",
            "confidence": 0.95,
        },
    ]


# --- Тесты -------------------------------------------------------------

def _patch_stance_to_asserted(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Хелпер: подменяет настоящий detect_stance (который ходит в YandexGPT)
    на чистую функцию «всем asserted». Используется в тестах, где stance
    нас не интересует — мы проверяем pipeline, а не stance.
    """

    async def fake_stance(transcript, claims):
        return [
            StanceLabel(claim_index=i, stance="asserted", missing="", confidence=0.0)
            for i in range(len(claims))
        ]

    from agents import pipeline as pipeline_mod
    monkeypatch.setattr(pipeline_mod, "detect_stance", fake_stance)


def test_enrich_empty_input() -> None:
    """Пустой вход не должен падать и не должен звать агентов."""
    result = asyncio.run(enrich([], []))
    assert result["claims"] == []
    assert result["stats"]["claims_in"] == 0
    assert result["stats"]["final_claims"] == 0


def test_enrich_happy_path_p0(monkeypatch: pytest.MonkeyPatch) -> None:
    """P0-stub: все claim'ы доходят, stance=asserted, 1 mock-source."""
    _patch_stance_to_asserted(monkeypatch)
    result = asyncio.run(enrich(_make_snippets(), _make_raw_claims()))

    assert len(result["claims"]) == 2, "оба claim'а должны пройти"

    for c in result["claims"]:
        # Контрактные поля FinalClaim
        for key in (
            "text", "start", "verdict", "type",
            "explanation", "confidence", "sources",
            "stance", "stance_missing", "extractor_verdict",
            "judge_notes", "search_queries",
        ):
            assert key in c, f"missing key in FinalClaim: {key}"

        # P0-инварианты
        assert c["stance"] == "asserted"
        assert c["extractor_verdict"] == c["verdict"], "Judge stub не меняет verdict"
        assert len(c["sources"]) == 1, "Retriever stub возвращает 1 source"
        src = c["sources"][0]
        assert src["mock"] is True
        assert src["tier"] == "pubmed"
        assert src["url"].startswith("https://pubmed.ncbi.nlm.nih.gov/")

    stats = result["stats"]
    assert stats["claims_in"] == 2
    assert stats["stance_asserted"] == 2
    assert stats["debunked_drop_count"] == 0
    assert stats["final_claims"] == 2


def test_debunked_fully_is_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    """Если Stance Detector помечает claim как debunked_fully — pipeline его дропает."""

    async def fake_stance(transcript, claims):
        # Первый claim — автор сам разобрал, второй — нет
        return [
            StanceLabel(claim_index=0, stance="debunked_fully", missing="", confidence=0.9),
            StanceLabel(claim_index=1, stance="asserted", missing="", confidence=0.7),
        ]

    monkeypatch.setattr(stance_mod, "detect_stance", fake_stance)
    # pipeline.py импортирует функцию по имени — патчим там же
    from agents import pipeline as pipeline_mod
    monkeypatch.setattr(pipeline_mod, "detect_stance", fake_stance)

    result = asyncio.run(enrich(_make_snippets(), _make_raw_claims()))

    assert len(result["claims"]) == 1, "debunked_fully должен быть отфильтрован"
    assert result["claims"][0]["text"] == "Сода лечит рак"
    assert result["claims"][0]["stance"] == "asserted"

    stats = result["stats"]
    assert stats["stance_debunked_fully"] == 1
    assert stats["debunked_drop_count"] == 1
    assert stats["claims_after_drop"] == 1
    assert stats["final_claims"] == 1


def test_debunked_partially_keeps_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """debunked_partially проходит дальше, stance_missing сохраняется."""

    async def fake_stance(transcript, claims):
        return [
            StanceLabel(
                claim_index=0, stance="debunked_partially",
                missing="не сказал про реальную профилактику", confidence=0.8,
            ),
            StanceLabel(claim_index=1, stance="asserted", missing="", confidence=0.7),
        ]

    from agents import pipeline as pipeline_mod
    monkeypatch.setattr(pipeline_mod, "detect_stance", fake_stance)

    result = asyncio.run(enrich(_make_snippets(), _make_raw_claims()))

    assert len(result["claims"]) == 2
    partial = [c for c in result["claims"] if c["stance"] == "debunked_partially"]
    assert len(partial) == 1
    assert partial[0]["stance_missing"] == "не сказал про реальную профилактику"


def test_pipeline_stats_keys_stable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Структура stats не должна меняться без явного перехода версии формата.

    Если этот тест упал — вы поменяли набор полей в PipelineStats. Подумайте,
    не сломает ли это main.py / save_analysis / front-end дашборд.
    """
    _patch_stance_to_asserted(monkeypatch)
    result = asyncio.run(enrich(_make_snippets(), _make_raw_claims()))
    expected_keys = {
        "claims_in",
        "stance_asserted",
        "stance_debunked_fully",
        "stance_debunked_partially",
        "stance_quoted_neutral",
        "claims_after_drop",
        "final_claims",
        "debunked_drop_count",
        "unverifiable_count",
        "duration_s",
    }
    assert set(result["stats"].keys()) == expected_keys


def test_versions_present() -> None:
    """Версии всех агентов прописаны (хотя бы как stub-маркеры)."""
    from agents.prompts import (
        JUDGE_VERSION,
        QUERY_FORMER_VERSION,
        CONFLICT_CLASSIFIER_VERSION,
        RETRIEVER_VERSION,
        STANCE_VERSION,
    )
    assert STANCE_VERSION
    assert QUERY_FORMER_VERSION
    assert CONFLICT_CLASSIFIER_VERSION
    assert JUDGE_VERSION
    # Composite check
    assert QUERY_FORMER_VERSION in RETRIEVER_VERSION
    assert CONFLICT_CLASSIFIER_VERSION in RETRIEVER_VERSION
