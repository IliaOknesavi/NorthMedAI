"""
agents/judge.py — финальное судейство claim'а с учётом источников и stance.

P1: реальный вызов YandexGPT с промптом из docs/PROMPTS.md → Judge.
Может менять verdict (например, на unverifiable если источники не нашлись
или extractor ошибся), переписывать explanation с упоминанием источников.

Failure mode: при любой ошибке возвращает пассивный FinalClaim (verdict
от Extractor, explanation от Extractor) — pipeline всё равно завершится.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

from dotenv import load_dotenv
from openai import AsyncOpenAI, OpenAIError

from .prompts import SYSTEM_PROMPT_JUDGE
from .types import (
    Evidence,
    FinalClaim,
    FinalVerdict,
    RawClaim,
    Source,
    StanceLabel,
)

load_dotenv()
log = logging.getLogger("agents.judge")

YANDEX_API_KEY = os.getenv("YANDEX_API_KEY", "").strip()
YANDEX_FOLDER_ID = os.getenv("YANDEX_FOLDER_ID", "").strip()
# Judge — главная задача, полная модель.
YANDEX_JUDGE_MODEL = os.getenv("YANDEX_JUDGE_MODEL", "yandexgpt-5.1/latest").strip()
YANDEX_BASE_URL = "https://ai.api.cloud.yandex.net/v1"

MAX_OUTPUT_TOKENS = 1500
TEMPERATURE = 0.1
REQUEST_TIMEOUT_S = 45.0

VALID_VERDICTS = {"false", "misleading", "conflicting", "unverifiable"}


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
    return f"gpt://{YANDEX_FOLDER_ID}/{YANDEX_JUDGE_MODEL}"


# --- Парсинг -------------------------------------------------------------

_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)
_JSON_OBJ_RE = re.compile(r"\{[\s\S]*\}")


def _extract_json(raw: str) -> dict[str, Any]:
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
    log.warning("judge: не удалось распарсить JSON; raw=%r", raw[:500])
    return {}


# --- Форматирование входа -----------------------------------------------

def _group_sources_by_stance(
    sources: list[Source],
) -> tuple[list[tuple[int, Source]], list[tuple[int, Source]], list[tuple[int, Source]]]:
    """Разделить sources на (supports, contradicts, neutral) с сохранением глобальных индексов."""
    supports: list[tuple[int, Source]] = []
    contradicts: list[tuple[int, Source]] = []
    neutral: list[tuple[int, Source]] = []
    for idx, s in enumerate(sources):
        st = s.get("stance", "neutral")  # type: ignore[typeddict-item]
        if st == "supports":
            supports.append((idx, s))
        elif st == "contradicts":
            contradicts.append((idx, s))
        else:
            neutral.append((idx, s))
    return supports, contradicts, neutral


def _format_source_line(idx: int, s: Source) -> str:
    title = s.get("title") or "(no title)"
    tier = s.get("tier") or "unknown"
    weight = s.get("weight") or 0.0
    published = s.get("published_at") or ""
    url = s.get("url") or ""
    snippet = (s.get("snippet") or "").strip()
    return (
        f"[{idx}] {title} ({tier}, weight={weight:.2f}, {published})\n"
        f"  {url}\n"
        + (f"  «{snippet[:300]}»" if snippet else "  (snippet не получен)")
    )


def _format_block(items: list[tuple[int, Source]]) -> str:
    if not items:
        return "—"
    return "\n".join(_format_source_line(i, s) for i, s in items)


def _build_user_prompt(
    raw: RawClaim, evidence: Evidence, stance: StanceLabel,
) -> str:
    supports, contradicts, neutral = _group_sources_by_stance(evidence["sources"])
    stance_missing_block = ""
    if stance["stance"] == "debunked_partially" and stance.get("missing"):
        stance_missing_block = f"Автор упустил: {stance['missing']}\n"

    return (
        f"Утверждение:\n{raw['text']}\n\n"
        f"Тип: {raw['type']}    Вердикт экстрактора: {raw['verdict']}\n"
        f"Объяснение экстрактора: {raw.get('explanation', '')}\n\n"
        f"Stance (как к claim'у относится сам автор видео): {stance['stance']}\n"
        f"{stance_missing_block}\n"
        f"Источники, поддерживающие утверждение:\n{_format_block(supports)}\n\n"
        f"Источники, опровергающие утверждение:\n{_format_block(contradicts)}\n\n"
        f"Нейтральные источники:\n{_format_block(neutral)}\n\n"
        "(Если какой-то раздел пуст — там написано «—».)\n\n"
        "Верни строго JSON по описанному формату."
    )


def _passthrough(
    raw: RawClaim, evidence: Evidence, stance: StanceLabel, reason: str,
) -> FinalClaim:
    """Fallback: verdict и explanation от Extractor'а, sources от Retriever'а.

    Если у нас НЕТ настоящих источников (только mock'и или вообще пусто) —
    мы не можем подтвердить верность Extractor'а, поэтому verdict
    становится `unverifiable` и confidence ≤ 0.4. Это удерживает claim
    в БД для дебага, но он отфильтруется из overlay (см. main.py).
    """
    final_stance = stance["stance"]
    if final_stance == "debunked_fully":
        final_stance = "asserted"
    log.info("judge: passthrough (%s) для claim=%r", reason, raw["text"][:80])
    real_sources = [s for s in evidence["sources"] if not s.get("mock")]
    has_real = bool(real_sources)
    verdict: FinalVerdict = raw["verdict"] if has_real else "unverifiable"  # type: ignore[assignment]
    return FinalClaim(
        text=raw["text"],
        start=raw["start"],
        verdict=verdict,
        type=raw["type"],
        explanation=raw.get("explanation", ""),
        confidence=raw.get("confidence", 0.5) if has_real else 0.4,
        sources=list(evidence["sources"]),
        stance=final_stance,  # type: ignore[typeddict-item]
        stance_missing=stance.get("missing", ""),
        extractor_verdict=raw["verdict"],
        judge_notes=f"passthrough: {reason}",
        search_queries=None,
    )


# --- Основная функция ---------------------------------------------------

async def judge(
    raw: RawClaim, evidence: Evidence, stance: StanceLabel,
) -> FinalClaim:
    """
    Свести RawClaim + Evidence + stance в FinalClaim через YandexGPT.

    Безопасно к ошибкам: на любой сбой возвращает «passthrough» FinalClaim.
    """
    # Если sources вообще нет (или только mock) — Judge всё равно нужен,
    # но в большинстве случаев результат будет unverifiable. Сэкономим
    # LLM-вызов и сразу passthrough.
    real_sources = [s for s in evidence["sources"] if not s.get("mock")]
    if not real_sources:
        return _passthrough(raw, evidence, stance, reason="no_real_sources")

    try:
        client = _get_client()
    except RuntimeError as e:
        return _passthrough(raw, evidence, stance, reason=f"no_creds: {e}")

    user_prompt = _build_user_prompt(raw, evidence, stance)
    log.info(
        "judge: вызываю %s, claim=%r, sources=%d",
        _model_uri(), raw["text"][:60], len(evidence["sources"]),
    )

    try:
        response = await client.responses.create(
            model=_model_uri(),
            instructions=SYSTEM_PROMPT_JUDGE,
            input=user_prompt,
            temperature=TEMPERATURE,
            max_output_tokens=MAX_OUTPUT_TOKENS,
        )
    except OpenAIError as e:
        return _passthrough(raw, evidence, stance, reason=f"openai_error: {e}")
    except Exception as e:  # noqa: BLE001
        log.exception("judge: непредвиденная ошибка")
        return _passthrough(raw, evidence, stance, reason=f"unexpected: {e}")

    raw_text = getattr(response, "output_text", None) or ""
    log.debug("judge raw response: %s", raw_text[:1500])

    data = _extract_json(raw_text)
    if not isinstance(data, dict) or not data:
        return _passthrough(raw, evidence, stance, reason="bad_json")

    # verdict
    verdict_raw = str(data.get("verdict", "")).strip().lower()
    if verdict_raw not in VALID_VERDICTS:
        log.warning("judge: невалидный verdict=%r → passthrough", verdict_raw)
        return _passthrough(raw, evidence, stance, reason=f"bad_verdict={verdict_raw}")

    verdict: FinalVerdict = verdict_raw  # type: ignore[assignment]
    explanation = str(data.get("explanation") or raw.get("explanation", ""))[:2000]
    judge_notes = str(data.get("judge_notes") or "")[:1000]
    try:
        confidence = float(data.get("confidence", raw.get("confidence", 0.5)))
    except (TypeError, ValueError):
        confidence = raw.get("confidence", 0.5)
    confidence = max(0.0, min(1.0, confidence))
    # Sanity: unverifiable должен быть ≤ 0.4
    if verdict == "unverifiable":
        confidence = min(confidence, 0.4)

    # selected_sources — индексы в исходный sources[]
    selected: list[int] = []
    raw_sel = data.get("selected_sources")
    if isinstance(raw_sel, list):
        for v in raw_sel:
            try:
                idx = int(v)
            except (TypeError, ValueError):
                continue
            if 0 <= idx < len(evidence["sources"]) and idx not in selected:
                selected.append(idx)
    if not selected:
        selected = list(range(min(3, len(evidence["sources"]))))
    final_sources = [evidence["sources"][i] for i in selected[:3]]

    final_stance = stance["stance"]
    if final_stance == "debunked_fully":
        final_stance = "asserted"

    log.info(
        "judge: verdict=%s (extractor=%s), conf=%.2f, sources=%d/%d",
        verdict, raw["verdict"], confidence, len(final_sources), len(evidence["sources"]),
    )

    return FinalClaim(
        text=raw["text"],
        start=raw["start"],
        verdict=verdict,
        type=raw["type"],
        explanation=explanation,
        confidence=round(confidence, 2),
        sources=final_sources,
        stance=final_stance,  # type: ignore[typeddict-item]
        stance_missing=stance.get("missing", ""),
        extractor_verdict=raw["verdict"],
        judge_notes=judge_notes,
        search_queries=None,
    )
