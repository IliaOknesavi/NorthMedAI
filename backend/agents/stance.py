"""
agents/stance.py — определение риторической роли claim'а в видео.

P0 (этот файл): stub — возвращает stance="asserted" для всех claim'ов.
Pipeline ниже по конвейеру работает корректно (debunked_fully не
встретится → ничего не фильтруется), формат соблюдён.

P1: настоящий вызов YandexGPT-lite, batch по всему видео.
Промпт и формат — docs/PROMPTS.md → Stance Detector.
"""

from __future__ import annotations

import logging

from .types import RawClaim, Snippet, StanceLabel

log = logging.getLogger("agents.stance")


async def detect_stance(
    transcript: list[Snippet],
    claims: list[RawClaim],
) -> list[StanceLabel]:
    """
    Определить stance для каждого claim'а.

    P0-реализация: для всех ставит "asserted" с confidence=0.0,
    чтобы pipeline шёл по дефолтному пути (claim в работу).
    """
    log.info(
        "stance stub: claims=%d, transcript_len=%d snippets — ставлю всем asserted",
        len(claims), len(transcript),
    )
    return [
        StanceLabel(
            claim_index=i,
            stance="asserted",
            missing="",
            confidence=0.0,
        )
        for i in range(len(claims))
    ]
