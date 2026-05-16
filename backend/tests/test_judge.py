"""
test_judge.py — Judge без сети. Мочим клиент, проверяем парсинг и
fallback'и (passthrough при ошибках).
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from openai import OpenAIError

from agents import judge as judge_mod
from agents.types import Evidence, RawClaim, Source, StanceLabel


def _raw(text: str = "Прививки вызывают аутизм") -> RawClaim:
    return {
        "text": text, "start": 12.0, "verdict": "false", "type": "claim",
        "explanation": "Неверно: связи нет.", "confidence": 0.9,
    }


def _source(title: str, *, stance: str = "contradicts", url: str = "https://pubmed/abc") -> Source:
    return {  # type: ignore[typeddict-item]
        "title": title, "url": url, "tier": "pubmed", "weight": 0.9,
        "snippet": "blah", "mock": False, "stance": stance,
    }


def _evidence(sources: list[Source]) -> Evidence:
    return Evidence(
        claim_text="Прививки вызывают аутизм",
        sources=sources, errors=[],
        retrieved_at=datetime.now(timezone.utc).isoformat(),
    )


def _stance(s: str = "asserted", missing: str = "") -> StanceLabel:
    return {"claim_index": 0, "stance": s, "missing": missing, "confidence": 0.9}  # type: ignore[typeddict-item]


def _fake_client(out: str | None = None, *, raise_exc: Exception | None = None):
    async def fake_create(**kwargs: Any):
        if raise_exc is not None:
            raise raise_exc
        return SimpleNamespace(output_text=out or "")
    return SimpleNamespace(responses=SimpleNamespace(create=fake_create))


def test_happy_path_judge(monkeypatch: pytest.MonkeyPatch) -> None:
    out = """{
      "verdict": "false",
      "explanation": "Неверно: связи нет (PubMed, 2019).",
      "confidence": 0.95,
      "selected_sources": [0],
      "judge_notes": ""
    }"""
    monkeypatch.setattr(judge_mod, "_get_client", lambda: _fake_client(out))
    ev = _evidence([_source("Article A")])
    fc = asyncio.run(judge_mod.judge(_raw(), ev, _stance()))
    assert fc["verdict"] == "false"
    assert fc["confidence"] == 0.95
    assert "(PubMed, 2019)" in fc["explanation"]


def test_unverifiable_caps_confidence(monkeypatch: pytest.MonkeyPatch) -> None:
    """unverifiable → confidence ≤ 0.4 (sanity-check в коде)."""
    out = """{
      "verdict": "unverifiable",
      "explanation": "Не удалось проверить: ...",
      "confidence": 0.95,
      "selected_sources": []
    }"""
    monkeypatch.setattr(judge_mod, "_get_client", lambda: _fake_client(out))
    fc = asyncio.run(judge_mod.judge(_raw(), _evidence([_source("X")]), _stance()))
    assert fc["verdict"] == "unverifiable"
    assert fc["confidence"] <= 0.4


def test_no_real_sources_passthrough() -> None:
    """Если все sources mock — Judge вообще не зовётся, passthrough."""
    mock_src: Source = {  # type: ignore[typeddict-item]
        "title": "PubMed search", "url": "https://pubmed/?term=x",
        "tier": "pubmed", "weight": 0.5, "mock": True,
    }
    fc = asyncio.run(judge_mod.judge(_raw(), _evidence([mock_src]), _stance()))
    # passthrough при отсутствии реальных источников → unverifiable
    assert fc["verdict"] == "unverifiable"
    assert "passthrough" in fc["judge_notes"]


def test_openai_error_passthrough(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        judge_mod, "_get_client",
        lambda: _fake_client(raise_exc=OpenAIError("net")),
    )
    fc = asyncio.run(judge_mod.judge(_raw(), _evidence([_source("X")]), _stance()))
    # passthrough: verdict как у Extractor
    assert fc["verdict"] == "false"
    assert "passthrough" in fc["judge_notes"]


def test_invalid_verdict_passthrough(monkeypatch: pytest.MonkeyPatch) -> None:
    out = """{"verdict": "totally_wrong", "explanation": "x", "confidence": 0.9}"""
    monkeypatch.setattr(judge_mod, "_get_client", lambda: _fake_client(out))
    fc = asyncio.run(judge_mod.judge(_raw(), _evidence([_source("X")]), _stance()))
    assert fc["verdict"] == "false"  # passthrough
    assert "bad_verdict" in fc["judge_notes"]


def test_missing_credentials_passthrough(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom():
        raise RuntimeError("no creds")
    monkeypatch.setattr(judge_mod, "_get_client", boom)
    fc = asyncio.run(judge_mod.judge(_raw(), _evidence([_source("X")]), _stance()))
    assert fc["verdict"] == "false"
    assert "no_creds" in fc["judge_notes"]


def test_stance_passed_through(monkeypatch: pytest.MonkeyPatch) -> None:
    """debunked_partially остаётся в FinalClaim, missing передан."""
    out = """{
      "verdict": "misleading",
      "explanation": "Автор разобрал частично: ...",
      "confidence": 0.6,
      "selected_sources": [0]
    }"""
    monkeypatch.setattr(judge_mod, "_get_client", lambda: _fake_client(out))
    fc = asyncio.run(judge_mod.judge(
        _raw(), _evidence([_source("X")]),
        _stance("debunked_partially", "не упомянул дозы"),
    ))
    assert fc["stance"] == "debunked_partially"
    assert fc["stance_missing"] == "не упомянул дозы"


def test_selected_sources_indices(monkeypatch: pytest.MonkeyPatch) -> None:
    """selected_sources индексы → правильные Source'ы попадают в FinalClaim."""
    out = """{
      "verdict": "false",
      "explanation": "x",
      "confidence": 0.9,
      "selected_sources": [1, 2]
    }"""
    monkeypatch.setattr(judge_mod, "_get_client", lambda: _fake_client(out))
    srcs = [_source("A", url="https://x/1"), _source("B", url="https://x/2"), _source("C", url="https://x/3")]
    fc = asyncio.run(judge_mod.judge(_raw(), _evidence(srcs), _stance()))
    urls = [s["url"] for s in fc["sources"]]
    assert urls == ["https://x/2", "https://x/3"]
