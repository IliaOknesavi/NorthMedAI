"""
test_stance.py — фиксирует контракт Stance Detector'а.

Никаких настоящих вызовов YandexGPT. Подкладываем фейковый клиент через
monkey-patch и проверяем парсинг, выравнивание индексов и fallback'и.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

# tests/ → backend/ для импортов
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from openai import OpenAIError

from agents import stance as stance_mod
from agents.types import RawClaim, Snippet


# --- Фикстуры ----------------------------------------------------------

def _snippets() -> list[Snippet]:
    return [
        {"text": "Поговорим о популярных мифах", "start": 0.0, "duration": 3.0},
        {"text": "Прививки якобы вызывают аутизм", "start": 12.3, "duration": 4.0},
        {"text": "Но крупные исследования это опровергли", "start": 18.4, "duration": 3.0},
        {"text": "Сода от рака — ещё один опасный миф", "start": 45.1, "duration": 3.0},
    ]


def _claims() -> list[RawClaim]:
    return [
        {
            "text": "Прививки вызывают аутизм", "start": 12.3,
            "verdict": "false", "type": "claim",
            "explanation": "", "confidence": 0.9,
        },
        {
            "text": "Сода лечит рак", "start": 45.1,
            "verdict": "false", "type": "claim",
            "explanation": "", "confidence": 0.95,
        },
    ]


def _make_fake_client(output_text: str | None = None, *, raise_exc: Exception | None = None):
    """
    Возвращает объект, у которого `.responses.create(...)` — async-функция,
    отдающая объект с `output_text` или поднимающая `raise_exc`.
    """

    async def fake_create(**kwargs: Any):
        if raise_exc is not None:
            raise raise_exc
        return SimpleNamespace(output_text=output_text or "")

    return SimpleNamespace(responses=SimpleNamespace(create=fake_create))


# --- Тесты -------------------------------------------------------------

def test_happy_path_with_debunked_fully(monkeypatch: pytest.MonkeyPatch) -> None:
    """Реалистичный ответ: первый claim — debunked_fully, второй — asserted."""
    fake = _make_fake_client(output_text="""{
      "stances": [
        {"claim_index": 0, "stance": "debunked_fully", "missing": "", "confidence": 0.95},
        {"claim_index": 1, "stance": "asserted", "missing": "", "confidence": 0.8}
      ]
    }""")
    monkeypatch.setattr(stance_mod, "_get_client", lambda: fake)

    res = asyncio.run(stance_mod.detect_stance(_snippets(), _claims()))
    assert len(res) == 2
    assert res[0]["stance"] == "debunked_fully"
    assert res[0]["claim_index"] == 0
    assert res[1]["stance"] == "asserted"


def test_debunked_partially_keeps_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """`missing` нужно сохранить для debunked_partially."""
    fake = _make_fake_client(output_text="""{
      "stances": [
        {"claim_index": 0, "stance": "debunked_partially",
         "missing": "не упомянул реальную профилактику",
         "confidence": 0.7},
        {"claim_index": 1, "stance": "asserted", "missing": "", "confidence": 0.9}
      ]
    }""")
    monkeypatch.setattr(stance_mod, "_get_client", lambda: fake)

    res = asyncio.run(stance_mod.detect_stance(_snippets(), _claims()))
    assert res[0]["stance"] == "debunked_partially"
    assert res[0]["missing"] == "не упомянул реальную профилактику"


def test_missing_field_ignored_for_non_partial(monkeypatch: pytest.MonkeyPatch) -> None:
    """`missing` должен быть пустой для asserted/debunked_fully/quoted_neutral."""
    fake = _make_fake_client(output_text="""{
      "stances": [
        {"claim_index": 0, "stance": "asserted", "missing": "это не должно остаться", "confidence": 0.9},
        {"claim_index": 1, "stance": "asserted", "missing": "", "confidence": 0.9}
      ]
    }""")
    monkeypatch.setattr(stance_mod, "_get_client", lambda: fake)

    res = asyncio.run(stance_mod.detect_stance(_snippets(), _claims()))
    assert res[0]["missing"] == ""


def test_invalid_stance_falls_back_to_asserted(monkeypatch: pytest.MonkeyPatch) -> None:
    """Неизвестная метка → asserted, не падаем."""
    fake = _make_fake_client(output_text="""{
      "stances": [
        {"claim_index": 0, "stance": "WTF_NEW_LABEL", "missing": "", "confidence": 0.9},
        {"claim_index": 1, "stance": "asserted", "missing": "", "confidence": 0.5}
      ]
    }""")
    monkeypatch.setattr(stance_mod, "_get_client", lambda: fake)

    res = asyncio.run(stance_mod.detect_stance(_snippets(), _claims()))
    assert res[0]["stance"] == "asserted"


def test_missing_claim_indices_filled_with_asserted(monkeypatch: pytest.MonkeyPatch) -> None:
    """LLM вернула меньше элементов — пропуски добиваем asserted."""
    fake = _make_fake_client(output_text="""{
      "stances": [
        {"claim_index": 0, "stance": "debunked_fully", "missing": "", "confidence": 0.9}
      ]
    }""")
    monkeypatch.setattr(stance_mod, "_get_client", lambda: fake)

    res = asyncio.run(stance_mod.detect_stance(_snippets(), _claims()))
    assert len(res) == 2
    assert res[0]["stance"] == "debunked_fully"
    assert res[1]["stance"] == "asserted"
    assert res[1]["confidence"] == 0.0  # маркер дефолта


def test_out_of_range_claim_index_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    """claim_index за пределами — выкидываем, позицию добиваем asserted."""
    fake = _make_fake_client(output_text="""{
      "stances": [
        {"claim_index": 5, "stance": "debunked_fully", "missing": "", "confidence": 0.9},
        {"claim_index": 0, "stance": "quoted_neutral", "missing": "", "confidence": 0.6}
      ]
    }""")
    monkeypatch.setattr(stance_mod, "_get_client", lambda: fake)

    res = asyncio.run(stance_mod.detect_stance(_snippets(), _claims()))
    assert res[0]["stance"] == "quoted_neutral"
    assert res[1]["stance"] == "asserted"
    # ни одного debunked_fully в финале, потому что он шёл с index=5
    assert not any(s["stance"] == "debunked_fully" for s in res)


def test_invalid_json_falls_back_to_asserted(monkeypatch: pytest.MonkeyPatch) -> None:
    """Бессмысленный текст в ответе → fallback."""
    fake = _make_fake_client(output_text="ну вот такой ответ, JSON'а нет")
    monkeypatch.setattr(stance_mod, "_get_client", lambda: fake)

    res = asyncio.run(stance_mod.detect_stance(_snippets(), _claims()))
    assert all(s["stance"] == "asserted" and s["confidence"] == 0.0 for s in res)


def test_openai_error_falls_back_to_asserted(monkeypatch: pytest.MonkeyPatch) -> None:
    """Сетевая ошибка YandexGPT не должна валить pipeline."""
    fake = _make_fake_client(raise_exc=OpenAIError("simulated network failure"))
    monkeypatch.setattr(stance_mod, "_get_client", lambda: fake)

    res = asyncio.run(stance_mod.detect_stance(_snippets(), _claims()))
    assert len(res) == 2
    assert all(s["stance"] == "asserted" for s in res)


def test_missing_credentials_falls_back_to_asserted(monkeypatch: pytest.MonkeyPatch) -> None:
    """Если .env пустой — не падаем, отдаём asserted."""

    def boom():
        raise RuntimeError("YANDEX_API_KEY не задан")

    monkeypatch.setattr(stance_mod, "_get_client", boom)

    res = asyncio.run(stance_mod.detect_stance(_snippets(), _claims()))
    assert len(res) == 2
    assert all(s["stance"] == "asserted" for s in res)


def test_empty_claims_returns_empty() -> None:
    """Пустой вход — без вызова сети."""
    res = asyncio.run(stance_mod.detect_stance(_snippets(), []))
    assert res == []


def test_long_transcript_is_truncated(monkeypatch: pytest.MonkeyPatch) -> None:
    """Транскрипт длиннее лимита обрезается, ошибки не падают."""
    big_snippets: list[Snippet] = [
        {"text": "x" * 1000, "start": float(i), "duration": 1.0}
        for i in range(100)  # 100K симв, выше лимита 30K
    ]
    captured: dict[str, Any] = {}

    async def fake_create(**kwargs: Any):
        captured["input"] = kwargs.get("input", "")
        return SimpleNamespace(output_text='{"stances": []}')

    fake = SimpleNamespace(responses=SimpleNamespace(create=fake_create))
    monkeypatch.setattr(stance_mod, "_get_client", lambda: fake)

    asyncio.run(stance_mod.detect_stance(big_snippets, _claims()))
    # Подаём только usable-часть транскрипта в LLM, иначе бы там было >100K
    assert len(captured["input"]) < 50_000
