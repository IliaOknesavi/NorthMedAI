"""
agents/final_qa.py — финальный whole-video QA после Judge.

Решает задачу «несколько false positives, которые ни Extractor, ни
Stance, ни Judge поодиночке не отловили» — см. RAG_ARCHITECTURE.md §4.6.

Один batch-вызов YandexGPT на видео. Видит весь транскрипт + все
final_claims со всеми метаданными. Может:
  - drop claim (Extractor зря выписал, претензий нет);
  - dedup_into (схлопнуть дубликаты в один claim);
  - repair (поправить verdict / explanation у Judge);
  - keep (оставить как есть).

Failure mode: при любой ошибке возвращает claims БЕЗ изменений.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

from dotenv import load_dotenv
from openai import AsyncOpenAI, OpenAIError

from .prompts import SYSTEM_PROMPT_QA
from .types import (
    FinalClaim,
    FinalVerdict,
    QAAction,
    QAActionType,
    QAResult,
    Snippet,
)

load_dotenv()
log = logging.getLogger("agents.final_qa")


# --- Конфигурация --------------------------------------------------------

YANDEX_API_KEY = os.getenv("YANDEX_API_KEY", "").strip()
YANDEX_FOLDER_ID = os.getenv("YANDEX_FOLDER_ID", "").strip()
# QA — последний голос, нужна полная модель.
YANDEX_QA_MODEL = os.getenv("YANDEX_QA_MODEL", "yandexgpt/latest").strip()
YANDEX_BASE_URL = "https://ai.api.cloud.yandex.net/v1"

MAX_OUTPUT_TOKENS = 3000          # actions могут быть длинными с reason'ами
TEMPERATURE = 0.0
REQUEST_TIMEOUT_S = 45.0
MAX_TRANSCRIPT_CHARS = 30_000

VALID_ACTIONS: set[str] = {"keep", "drop", "repair", "dedup_into"}
VALID_VERDICTS: set[str] = {"false", "misleading", "conflicting", "unverifiable"}


# --- Клиент --------------------------------------------------------------

_client: AsyncOpenAI | None = None


def _get_client() -> AsyncOpenAI:
    global _client
    if _client is not None:
        return _client
    if not YANDEX_API_KEY or not YANDEX_FOLDER_ID:
        raise RuntimeError(
            "YANDEX_API_KEY / YANDEX_FOLDER_ID не заданы. "
            "Final QA не может работать без креденшелов Yandex AI Studio."
        )
    _client = AsyncOpenAI(
        api_key=YANDEX_API_KEY,
        base_url=YANDEX_BASE_URL,
        project=YANDEX_FOLDER_ID,
        timeout=REQUEST_TIMEOUT_S,
    )
    return _client


def _model_uri() -> str:
    return f"gpt://{YANDEX_FOLDER_ID}/{YANDEX_QA_MODEL}"


# --- Форматирование входа ------------------------------------------------

def _format_transcript(snippets: list[Snippet]) -> str:
    lines: list[str] = []
    total = 0
    for s in snippets:
        text = (s.get("text") or "").replace("\n", " ").strip()
        if not text:
            continue
        line = f"[{float(s.get('start', 0.0)):.2f}] {text}"
        if total + len(line) > MAX_TRANSCRIPT_CHARS:
            log.warning("QA: транскрипт обрезан на лимит %d симв.", MAX_TRANSCRIPT_CHARS)
            break
        lines.append(line)
        total += len(line) + 1
    return "\n".join(lines)


def _format_claims(claims: list[FinalClaim]) -> str:
    out: list[str] = []
    for i, c in enumerate(claims):
        out.append(
            f"[{i}] ({float(c['start']):.2f}s) {c['text']}\n"
            f"  verdict={c['verdict']}, type={c['type']}, "
            f"stance={c['stance']}, sources={len(c.get('sources', []))}\n"
            f"  explanation: {c.get('explanation', '')}"
        )
    return "\n".join(out)


# --- Парсинг ответа ------------------------------------------------------

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
    log.warning("QA: не удалось распарсить JSON; raw=%r", raw[:500])
    return {}


def _normalize_action(item: Any, n_claims: int) -> QAAction | None:
    """Один элемент actions → QAAction. None если совсем сломано."""
    if not isinstance(item, dict):
        return None

    action_raw = str(item.get("action", "")).strip().lower()
    if action_raw not in VALID_ACTIONS:
        log.warning("QA: неизвестное действие %r — пропускаю", action_raw)
        return None
    action: QAActionType = action_raw  # type: ignore[assignment]

    raw_indices = item.get("claim_indices")
    if not isinstance(raw_indices, list):
        log.warning("QA: action без claim_indices: %r", item)
        return None
    indices: list[int] = []
    for v in raw_indices:
        try:
            idx = int(v)
        except (TypeError, ValueError):
            continue
        if 0 <= idx < n_claims:
            indices.append(idx)
    if not indices:
        log.warning("QA: все claim_indices вне диапазона: %r", raw_indices)
        return None

    reason = str(item.get("reason") or "")[:1000]

    patch_verdict_raw = item.get("patch_verdict")
    patch_verdict: FinalVerdict | None = None
    if isinstance(patch_verdict_raw, str) and patch_verdict_raw in VALID_VERDICTS:
        patch_verdict = patch_verdict_raw  # type: ignore[assignment]

    patch_explanation: str | None = None
    if isinstance(item.get("patch_explanation"), str):
        patch_explanation = item["patch_explanation"][:4000]

    merge_into: int | None = None
    if action == "dedup_into":
        mi_raw = item.get("merge_into")
        try:
            mi = int(mi_raw)
        except (TypeError, ValueError):
            log.warning("QA: dedup_into без merge_into: %r — игнорирую", item)
            return None
        if mi not in indices:
            log.warning(
                "QA: merge_into=%d не в claim_indices=%r — игнорирую dedup",
                mi, indices,
            )
            return None
        merge_into = mi

    return QAAction(
        claim_indices=indices,
        action=action,
        reason=reason,
        patch_verdict=patch_verdict,
        patch_explanation=patch_explanation,
        merge_into=merge_into,
    )


# --- Применение действий ------------------------------------------------

def _apply_actions(
    claims: list[FinalClaim], actions: list[QAAction],
) -> list[FinalClaim]:
    """
    Применяет actions в правильном порядке: dedup → repair → drop.
    Возвращает новый список (не мутирует входной).
    """
    # Работаем на копии — не правим исходный список
    work: list[FinalClaim | None] = list(claims)  # type: ignore[arg-type]

    # 1) dedup_into: сливаем sources и убираем не-merge_into из группы
    for a in actions:
        if a["action"] != "dedup_into":
            continue
        target = a["merge_into"]
        if target is None or work[target] is None:
            continue
        # Объединяем sources с дубликатами в target
        for idx in a["claim_indices"]:
            if idx == target or work[idx] is None:
                continue
            # Сольём sources, dedupping по url
            existing_urls = {s.get("url") for s in work[target]["sources"]}  # type: ignore[index]
            for s in work[idx]["sources"]:  # type: ignore[index]
                if s.get("url") not in existing_urls:
                    work[target]["sources"].append(s)  # type: ignore[index]
                    existing_urls.add(s.get("url"))
            log.info(
                "QA: dedup [%d] → [%d] (sources merged); reason: %s",
                idx, target, a["reason"][:120],
            )
            work[idx] = None  # пометили на удаление

    # 2) repair: применяем patch_verdict / patch_explanation
    for a in actions:
        if a["action"] != "repair":
            continue
        for idx in a["claim_indices"]:
            if work[idx] is None:
                continue
            if a.get("patch_verdict") is not None:
                old = work[idx]["verdict"]  # type: ignore[index]
                work[idx]["verdict"] = a["patch_verdict"]  # type: ignore[index]
                log.info(
                    "QA: repair [%d] verdict %s → %s; reason: %s",
                    idx, old, a["patch_verdict"], a["reason"][:120],
                )
            if a.get("patch_explanation") is not None:
                work[idx]["explanation"] = a["patch_explanation"]  # type: ignore[index]
                log.info(
                    "QA: repair [%d] explanation переписан; reason: %s",
                    idx, a["reason"][:120],
                )

    # 3) drop: убираем
    for a in actions:
        if a["action"] != "drop":
            continue
        for idx in a["claim_indices"]:
            if work[idx] is None:
                continue
            log.info(
                "QA: drop [%d] '%s'; reason: %s",
                idx, work[idx]["text"][:80], a["reason"][:160],  # type: ignore[index]
            )
            work[idx] = None

    return [c for c in work if c is not None]


# --- Основная функция ---------------------------------------------------

def _passthrough(claims: list[FinalClaim], reason: str) -> QAResult:
    log.info("QA: fallback — claims без изменений (%s)", reason)
    return QAResult(claims=claims, actions=[], errors=[reason])


async def qa_pass(
    transcript: list[Snippet],
    final_claims: list[FinalClaim],
) -> QAResult:
    """
    Финальный QA-проход. См. docs/RAG_ARCHITECTURE.md §4.6.

    На любой сбой возвращает claims без изменений — QA должен только
    улучшать, никогда не ухудшать.
    """
    n = len(final_claims)
    if n == 0:
        return QAResult(claims=[], actions=[], errors=[])

    transcript_block = _format_transcript(transcript)
    claims_block = _format_claims(final_claims)

    user_prompt = (
        f"Транскрипт:\n{transcript_block}\n\n"
        f"Claim'ы для проверки:\n{claims_block}\n\n"
        "Верни строго JSON по описанному формату."
    )

    log.info(
        "QA: вызываю %s, transcript=%d симв., claims=%d",
        _model_uri(), len(transcript_block), n,
    )

    try:
        client = _get_client()
    except RuntimeError as e:
        return _passthrough(final_claims, f"no_creds: {e}")

    try:
        response = await client.responses.create(
            model=_model_uri(),
            instructions=SYSTEM_PROMPT_QA,
            input=user_prompt,
            temperature=TEMPERATURE,
            max_output_tokens=MAX_OUTPUT_TOKENS,
        )
    except OpenAIError as e:
        log.exception("QA: YandexGPT упал — fallback")
        return _passthrough(final_claims, f"openai_error: {e}")
    except Exception as e:  # noqa: BLE001
        log.exception("QA: непредвиденная ошибка — fallback")
        return _passthrough(final_claims, f"unexpected: {e}")

    raw = getattr(response, "output_text", None) or ""
    log.info("QA raw (preview %d/%d симв.): %s",
             min(800, len(raw)), len(raw), raw[:800].replace("\n", " ⏎ "))
    log.debug("QA full raw response: %s", raw)

    data = _extract_json(raw)
    raw_actions = data.get("actions") if isinstance(data, dict) else None
    if not isinstance(raw_actions, list):
        return _passthrough(final_claims, "no_actions_field")

    actions: list[QAAction] = []
    for item in raw_actions:
        a = _normalize_action(item, n_claims=n)
        if a is not None:
            actions.append(a)

    log.info(
        "QA: распарсено actions: keep=%d, drop=%d, repair=%d, dedup_into=%d (всего %d)",
        sum(1 for a in actions if a["action"] == "keep"),
        sum(1 for a in actions if a["action"] == "drop"),
        sum(1 for a in actions if a["action"] == "repair"),
        sum(1 for a in actions if a["action"] == "dedup_into"),
        len(actions),
    )

    cleaned = _apply_actions(final_claims, actions)

    log.info(
        "QA: итог — было %d → стало %d (убрано %d)",
        n, len(cleaned), n - len(cleaned),
    )

    return QAResult(claims=cleaned, actions=actions, errors=[])
