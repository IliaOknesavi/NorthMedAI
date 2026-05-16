"""
agents/judge.py — финальное судейство claim'а с учётом источников и stance.

P0 (этот файл): pass-through — берёт verdict экстрактора как есть,
explanation тоже не трогает, sources перекладывает из Evidence.

P1: настоящий вызов YandexGPT с промптом из docs/PROMPTS.md → Judge.
Может менять verdict (например, на unverifiable если источников нет
или extractor ошибся).
"""

from __future__ import annotations

import logging

from .types import Evidence, FinalClaim, RawClaim, StanceLabel

log = logging.getLogger("agents.judge")


async def judge(
    raw: RawClaim,
    evidence: Evidence,
    stance: StanceLabel,
) -> FinalClaim:
    """
    Свести RawClaim + Evidence + stance в FinalClaim.

    P0-реализация:
    - verdict, type, confidence, text, start, explanation → как у Extractor'а
    - sources → как у Retriever'а (на P0 это один mock-Source)
    - stance → как у Stance Detector'а
    - judge_notes — пустая строка (Judge ещё не "думает")
    - extractor_verdict — оригинальный verdict для будущего сравнения
    """
    # На P0 stance всегда "asserted" (см. stance.py stub).
    # debunked_fully не доходит — он отфильтрован в pipeline.py.
    final_stance = stance["stance"]
    if final_stance == "debunked_fully":
        # Не должно случиться — заглушка для type checker и страховка.
        log.error("judge: получил stance=debunked_fully, чего быть не должно")
        final_stance = "asserted"

    log.debug(
        "judge stub: claim=%r verdict=%s stance=%s",
        raw["text"][:80], raw["verdict"], final_stance,
    )

    return FinalClaim(
        text=raw["text"],
        start=raw["start"],
        verdict=raw["verdict"],            # P0: pass-through
        type=raw["type"],
        explanation=raw["explanation"],
        confidence=raw["confidence"],
        sources=list(evidence["sources"]),
        stance=final_stance,               # type: ignore[typeddict-item]
        stance_missing=stance["missing"],
        extractor_verdict=raw["verdict"],
        judge_notes="",
        search_queries=None,
    )
