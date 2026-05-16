"""
agents/conflict_classifier.py — для каждой пары (claim, snippet) определяет
позицию: supports / contradicts / neutral.

Дёшево: один лёгкий вызов YandexGPT-lite на каждую пару. Параллельно
для всех source'ов одного claim'а, с глобальным семафором против
rate-limit'а.

Используется Retriever'ом перед передачей evidence Judge'у — Judge
видит уже сгруппированные supports/contradicts/neutral списки.

См. docs/PROMPTS.md → Conflict Classifier.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Literal

from dotenv import load_dotenv
from openai import AsyncOpenAI, OpenAIError

from .prompts import SYSTEM_PROMPT_CONFLICT_CLASSIFIER
from .types import Source

load_dotenv()
log = logging.getLogger("agents.conflict")

Stance = Literal["supports", "contradicts", "neutral"]

YANDEX_API_KEY = os.getenv("YANDEX_API_KEY", "").strip()
YANDEX_FOLDER_ID = os.getenv("YANDEX_FOLDER_ID", "").strip()
YANDEX_CC_MODEL = os.getenv("YANDEX_CC_MODEL", "yandexgpt-lite/latest").strip()
YANDEX_BASE_URL = "https://ai.api.cloud.yandex.net/v1"

MAX_OUTPUT_TOKENS = 10           # одно слово
TEMPERATURE = 0.0
REQUEST_TIMEOUT_S = 15.0

# Не более N одновременных вызовов на одно /analyze
_SEMAPHORE = asyncio.Semaphore(8)

VALID = {"supports", "contradicts", "neutral"}


_client: AsyncOpenAI | None = None


def _get_client() -> AsyncOpenAI:
    global _client
    if _client is not None:
        return _client
    if not YANDEX_API_KEY or not YANDEX_FOLDER_ID:
        raise RuntimeError("YANDEX_API_KEY / YANDEX_FOLDER_ID не заданы")
    _client = AsyncOpenAI(
        api_key=YANDEX_API_KEY, base_url=YANDEX_BASE_URL,
        project=YANDEX_FOLDER_ID, timeout=REQUEST_TIMEOUT_S,
    )
    return _client


def _model_uri() -> str:
    return f"gpt://{YANDEX_FOLDER_ID}/{YANDEX_CC_MODEL}"


async def _classify_one(claim_text: str, snippet: str) -> Stance:
    """Один LLM-вызов на пару claim ↔ snippet. На любую ошибку — neutral."""
    if not snippet.strip():
        return "neutral"
    user_prompt = (
        f"Утверждение: {claim_text}\n\n"
        f"Snippet: {snippet}\n\n"
        "Метка:"
    )
    try:
        client = _get_client()
    except RuntimeError:
        return "neutral"
    async with _SEMAPHORE:
        try:
            response = await client.responses.create(
                model=_model_uri(),
                instructions=SYSTEM_PROMPT_CONFLICT_CLASSIFIER,
                input=user_prompt,
                temperature=TEMPERATURE,
                max_output_tokens=MAX_OUTPUT_TOKENS,
            )
        except OpenAIError:
            log.debug("conflict: OpenAIError → neutral")
            return "neutral"
        except Exception:  # noqa: BLE001
            log.debug("conflict: unexpected → neutral", exc_info=True)
            return "neutral"

    raw = (getattr(response, "output_text", None) or "").strip().lower().strip(".,;:!?")
    # YandexGPT-lite иногда возвращает ответ из нескольких слов — берём первое валидное
    for token in raw.replace("\n", " ").split():
        cleaned = token.strip(".,;:!?")
        if cleaned in VALID:
            return cleaned  # type: ignore[return-value]
    log.debug("conflict: не распознал ответ %r → neutral", raw[:80])
    return "neutral"


async def classify_sources(
    claim_text: str, sources: list[Source],
) -> list[tuple[Source, Stance]]:
    """
    Параллельно прогнать классификатор по всем sources для одного claim'а.

    Возвращает список (source, stance) в исходном порядке sources.
    На любых ошибках конкретного вызова — neutral. Pipeline не блокируется.
    """
    if not sources:
        return []
    # Берём snippet, если есть; иначе title — он тоже содержит сигнал
    pairs = [
        (s, (s.get("snippet") or s.get("title") or ""))
        for s in sources
    ]
    log.info(
        "conflict: классифицирую %d sources для claim=%r через %s",
        len(pairs), claim_text[:60], _model_uri(),
    )
    stances = await asyncio.gather(
        *(_classify_one(claim_text, snippet) for _, snippet in pairs),
        return_exceptions=False,
    )
    cnt = {"supports": 0, "contradicts": 0, "neutral": 0}
    for st in stances:
        cnt[st] = cnt.get(st, 0) + 1
    log.info(
        "conflict: supports=%d contradicts=%d neutral=%d",
        cnt["supports"], cnt["contradicts"], cnt["neutral"],
    )
    return list(zip([s for s, _ in pairs], stances))
