"""
agents/stance.py — определение риторической роли claim'а в видео.

Решает кейс «автор сам разоблачает миф»: если в ролике врач-блогер
называет миф «прививки вызывают аутизм», а через минуту показывает что
это неправда — мы НЕ должны ставить красную метку поверх такого ролика.

См. docs/RAG_ARCHITECTURE.md §4.1.5 и docs/PROMPTS.md → Stance Detector.

Реализация: ОДИН batch-вызов YandexGPT-lite на видео. На вход — весь
транскрипт + список claim'ов с таймкодами, на выход — массив stance'ов.

Failure mode: при любой ошибке (нет ключей, таймаут, невалидный JSON)
все claim'ы получают stance="asserted" с confidence=0.0. Pipeline
продолжает работать, в логи WARNING.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

from dotenv import load_dotenv
from openai import AsyncOpenAI, OpenAIError

from .prompts import SYSTEM_PROMPT_STANCE
from .types import RawClaim, Snippet, Stance, StanceLabel

load_dotenv()
log = logging.getLogger("agents.stance")


# --- Конфигурация --------------------------------------------------------

# Те же креды, что и у detector.py — Yandex AI Studio один на проект.
YANDEX_API_KEY = os.getenv("YANDEX_API_KEY", "").strip()
YANDEX_FOLDER_ID = os.getenv("YANDEX_FOLDER_ID", "").strip()
# Stance смотрит на риторическую структуру всего видео — это не такая
# простая задача, как казалось изначально. Lite-модель на 30K-символьном
# транскрипте систематически промахивалась (ставила asserted разоблачённым
# мифам). Полная yandexgpt медленнее на 5-10 секунд, зато читает контекст
# по-настоящему. Можно переключить через .env (YANDEX_STANCE_MODEL).
YANDEX_STANCE_MODEL = os.getenv("YANDEX_STANCE_MODEL", "yandexgpt/latest").strip()
YANDEX_BASE_URL = "https://ai.api.cloud.yandex.net/v1"

MAX_OUTPUT_TOKENS = 2000      # достаточно на 50 stance'ов с описанием
TEMPERATURE = 0.0             # детерминированно для кэшируемости
REQUEST_TIMEOUT_S = 45.0

# Сколько символов транскрипта максимум кладём в один вызов.
# Эмпирическая оценка: 30K символов ≈ 10K токенов, lite-модель держит 32K.
# На очень длинных видео (>1 ч) будем чанковать — пока обрезаем по верху.
MAX_TRANSCRIPT_CHARS = 30_000

VALID_STANCES: set[str] = {
    "asserted", "debunked_fully", "debunked_partially", "quoted_neutral",
}


# --- Клиент --------------------------------------------------------------

_client: AsyncOpenAI | None = None


def _get_client() -> AsyncOpenAI:
    """Ленивая инициализация — позволяет импортировать модуль без .env."""
    global _client
    if _client is not None:
        return _client
    if not YANDEX_API_KEY or not YANDEX_FOLDER_ID:
        raise RuntimeError(
            "YANDEX_API_KEY / YANDEX_FOLDER_ID не заданы. "
            "Stance Detector не может работать без креденшелов Yandex AI Studio."
        )
    _client = AsyncOpenAI(
        api_key=YANDEX_API_KEY,
        base_url=YANDEX_BASE_URL,
        project=YANDEX_FOLDER_ID,
        timeout=REQUEST_TIMEOUT_S,
    )
    return _client


def _model_uri() -> str:
    return f"gpt://{YANDEX_FOLDER_ID}/{YANDEX_STANCE_MODEL}"


# --- Форматирование входа ------------------------------------------------

def _format_transcript(snippets: list[Snippet]) -> str:
    """Транскрипт в виде `[12.34] текст\\n[15.10] следующий…`."""
    lines: list[str] = []
    total = 0
    for s in snippets:
        text = (s.get("text") or "").replace("\n", " ").strip()
        if not text:
            continue
        line = f"[{float(s.get('start', 0.0)):.2f}] {text}"
        if total + len(line) > MAX_TRANSCRIPT_CHARS:
            log.warning(
                "transcript обрезан на лимит %d симв. (видео слишком длинное)",
                MAX_TRANSCRIPT_CHARS,
            )
            break
        lines.append(line)
        total += len(line) + 1
    return "\n".join(lines)


def _format_claims(claims: list[RawClaim]) -> str:
    """`[0] (t=12.30s) текст` — индекс совпадает с claim_index в ответе."""
    return "\n".join(
        f"[{i}] (t={float(c['start']):.2f}s) {c['text']}"
        for i, c in enumerate(claims)
    )


# --- Парсинг ответа ------------------------------------------------------

_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)
_JSON_OBJ_RE = re.compile(r"\{[\s\S]*\}")


def _extract_json(raw: str) -> dict[str, Any]:
    """Достать JSON из ответа модели (та же логика что в detector.py)."""
    raw = (raw or "").strip()
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    m = _JSON_FENCE_RE.search(raw)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    m = _JSON_OBJ_RE.search(raw)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass
    log.warning("stance: не удалось распарсить JSON; raw=%r", raw[:500])
    return {}


def _normalize_label(item: Any, default_index: int) -> StanceLabel:
    """Один элемент массива stances из ответа → StanceLabel с дефолтами."""
    if not isinstance(item, dict):
        return StanceLabel(
            claim_index=default_index, stance="asserted",
            missing="", confidence=0.0,
        )
    try:
        idx = int(item.get("claim_index", default_index))
    except (TypeError, ValueError):
        idx = default_index

    raw_stance = str(item.get("stance", "asserted")).strip().lower()
    stance: Stance = raw_stance if raw_stance in VALID_STANCES else "asserted"  # type: ignore[assignment]

    missing = str(item.get("missing") or "").strip()[:300]
    if stance != "debunked_partially":
        missing = ""  # игнорируем missing для всех кроме partially

    try:
        confidence = float(item.get("confidence", 0.5))
    except (TypeError, ValueError):
        confidence = 0.5
    confidence = max(0.0, min(1.0, confidence))

    return StanceLabel(
        claim_index=idx, stance=stance, missing=missing, confidence=confidence,
    )


def _all_asserted(n: int) -> list[StanceLabel]:
    """Fallback: всем asserted с confidence=0. Pipeline пойдёт по дефолту."""
    return [
        StanceLabel(claim_index=i, stance="asserted", missing="", confidence=0.0)
        for i in range(n)
    ]


def _align_to_claims(parsed: list[StanceLabel], n_claims: int) -> list[StanceLabel]:
    """
    Привести массив stance'ов к длине claim'ов:
    - выкидываем элементы с claim_index вне диапазона;
    - дубликаты по claim_index — оставляем первый;
    - пропуски — добиваем asserted.
    """
    by_index: dict[int, StanceLabel] = {}
    for s in parsed:
        idx = s["claim_index"]
        if 0 <= idx < n_claims and idx not in by_index:
            by_index[idx] = s

    aligned: list[StanceLabel] = []
    for i in range(n_claims):
        s = by_index.get(i)
        if s is None:
            aligned.append(StanceLabel(
                claim_index=i, stance="asserted", missing="", confidence=0.0,
            ))
        else:
            # claim_index фиксируем на правильную позицию, на случай если
            # LLM перепутала индексацию.
            aligned.append(StanceLabel(
                claim_index=i,
                stance=s["stance"],
                missing=s["missing"],
                confidence=s["confidence"],
            ))
    return aligned


# --- Основная функция ----------------------------------------------------

async def detect_stance(
    transcript: list[Snippet],
    claims: list[RawClaim],
) -> list[StanceLabel]:
    """
    Определить stance для каждого claim'а одним batch-вызовом YandexGPT-lite.

    Безопасно к ошибкам: при любой проблеме возвращает все `asserted` —
    pipeline продолжит работать, просто не отфильтрует разоблачения.
    """
    n = len(claims)
    if n == 0:
        return []

    transcript_block = _format_transcript(transcript)
    claims_block = _format_claims(claims)

    user_prompt = (
        f"Транскрипт:\n{transcript_block}\n\n"
        f"Утверждения для классификации:\n{claims_block}\n\n"
        "Верни строго JSON по описанному формату."
    )

    log.info(
        "stance: вызываю %s, transcript=%d симв., claims=%d",
        _model_uri(), len(transcript_block), n,
    )

    try:
        client = _get_client()
    except RuntimeError as e:
        log.warning("stance: %s — fallback на asserted для всех", e)
        return _all_asserted(n)

    try:
        response = await client.responses.create(
            model=_model_uri(),
            instructions=SYSTEM_PROMPT_STANCE,
            input=user_prompt,
            temperature=TEMPERATURE,
            max_output_tokens=MAX_OUTPUT_TOKENS,
        )
    except OpenAIError:
        log.exception("stance: YandexGPT упал — fallback на asserted")
        return _all_asserted(n)
    except Exception:  # noqa: BLE001 — сеть/таймауты бывают разные
        log.exception("stance: непредвиденная ошибка — fallback на asserted")
        return _all_asserted(n)

    raw = getattr(response, "output_text", None) or ""
    # На INFO кладём первые 800 симв, чтобы видеть качество ответов в
    # обычных логах без поднятия уровня. Полный raw идёт в DEBUG.
    log.info("stance raw (preview %d/%d симв.): %s",
             min(800, len(raw)), len(raw), raw[:800].replace("\n", " ⏎ "))
    log.debug("stance full raw response: %s", raw)

    data = _extract_json(raw)
    raw_stances = data.get("stances") if isinstance(data, dict) else None
    if not isinstance(raw_stances, list):
        log.warning("stance: ответ без поля 'stances' — fallback на asserted")
        return _all_asserted(n)

    parsed = [_normalize_label(item, default_index=i) for i, item in enumerate(raw_stances)]
    aligned = _align_to_claims(parsed, n)

    # Логи для дебага и UX-телеметрии
    cnt = {"asserted": 0, "debunked_fully": 0, "debunked_partially": 0, "quoted_neutral": 0}
    for s in aligned:
        cnt[s["stance"]] = cnt.get(s["stance"], 0) + 1
    log.info(
        "stance: итого — asserted=%d, debunked_fully=%d, "
        "debunked_partially=%d, quoted_neutral=%d",
        cnt["asserted"], cnt["debunked_fully"],
        cnt["debunked_partially"], cnt["quoted_neutral"],
    )
    for i, (raw_c, s) in enumerate(zip(claims, aligned)):
        if s["stance"] != "asserted":
            log.info(
                "stance: [%d] @%.1fs %s (conf=%.2f) %s%s",
                i, raw_c["start"], s["stance"], s["confidence"],
                raw_c["text"][:80],
                f" — missing: {s['missing']}" if s["missing"] else "",
            )

    return aligned
