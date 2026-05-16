"""
agents/pipeline.py — главная точка входа в RAG-pipeline.

Склеивает все шаги:
  1.5  Stance Detector → отметить риторическую роль каждого claim'а
       │ debunked_fully → DROP (не показываем пользователю)
  2.   Query Former    → поисковые запросы по источникам
  3.   Retriever       → live-поиск, ранжирование, evidence
  4.   Judge           → пересчёт verdict с учётом источников и stance

Вход — то, что выдаёт Extractor (detector.py), плюс полный транскрипт
для Stance Detector. Выход — список FinalClaim, готовый к save_analysis.

P0 версия: все агенты — stubs. Pipeline работает end-to-end, формат
выхода совпадает с дизайн-доком, никаких LLM-вызовов.

См. docs/RAG_ARCHITECTURE.md §3 для общей диаграммы.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TypedDict

from .judge import judge
from .query_former import make_queries
from .retriever import retrieve
from .stance import detect_stance
from .types import FinalClaim, RawClaim, Snippet

log = logging.getLogger("agents.pipeline")


class PipelineStats(TypedDict):
    """Метрики прогона — пригодится для логов в main.py и метрик в БД."""

    claims_in: int
    stance_asserted: int
    stance_debunked_fully: int
    stance_debunked_partially: int
    stance_quoted_neutral: int
    claims_after_drop: int
    final_claims: int
    debunked_drop_count: int
    unverifiable_count: int
    duration_s: float


class PipelineResult(TypedDict):
    """Результат: финальные claim'ы + статистика прогона."""

    claims: list[FinalClaim]
    stats: PipelineStats


async def enrich(
    snippets: list[Snippet],
    raw_claims: list[RawClaim],
) -> PipelineResult:
    """
    Прогнать claim'ы через все шаги pipeline'а.

    Безопасно к пустому входу: если raw_claims пуст — возвращаем пустой
    результат без обращений к агентам.
    """
    t0 = time.monotonic()
    n_in = len(raw_claims)
    if n_in == 0:
        log.info("pipeline: пустой вход — пропускаю")
        return PipelineResult(
            claims=[],
            stats=PipelineStats(
                claims_in=0,
                stance_asserted=0,
                stance_debunked_fully=0,
                stance_debunked_partially=0,
                stance_quoted_neutral=0,
                claims_after_drop=0,
                final_claims=0,
                debunked_drop_count=0,
                unverifiable_count=0,
                duration_s=0.0,
            ),
        )

    # --- Шаг 1.5: Stance --------------------------------------------------
    # Один батч-вызов на весь видеоролик: stance видит все claim'ы вместе
    # с полным транскриптом (важно для разоблачающих роликов).
    stances = await detect_stance(snippets, raw_claims)
    # На случай ошибочной длины — добиваем дефолтом «asserted».
    if len(stances) < n_in:
        log.warning(
            "stance вернул %d меток на %d claim'ов — добиваю asserted",
            len(stances), n_in,
        )
        from .types import StanceLabel  # локальный импорт, чтобы не светить наверху
        for i in range(len(stances), n_in):
            stances.append(StanceLabel(
                claim_index=i, stance="asserted", missing="", confidence=0.0,
            ))

    # Группируем для статистики
    cnt = {"asserted": 0, "debunked_fully": 0, "debunked_partially": 0, "quoted_neutral": 0}
    for s in stances:
        cnt[s["stance"]] = cnt.get(s["stance"], 0) + 1

    # --- Drop: claim'ы со stance="debunked_fully" не идут дальше ----------
    survivors: list[tuple[RawClaim, "StanceLabel"]] = []  # type: ignore[name-defined]
    for raw, s in zip(raw_claims, stances):
        if s["stance"] == "debunked_fully":
            log.info(
                "pipeline: drop claim @ %.1fs (автор разобрал полностью): %r",
                raw["start"], raw["text"][:80],
            )
            continue
        survivors.append((raw, s))

    # --- Шаги 2-4: для каждого выжившего параллельно ----------------------
    async def process_one(raw: RawClaim, s: "StanceLabel") -> FinalClaim:  # type: ignore[name-defined]
        cq = await make_queries(raw)
        ev = await retrieve(cq)
        fc = await judge(raw, ev, s)
        # Запишем search_queries в FinalClaim для дебага (на P0 — наивные).
        fc["search_queries"] = cq["queries"]
        return fc

    final_claims_unordered = await asyncio.gather(
        *(process_one(r, s) for r, s in survivors),
        return_exceptions=True,
    )

    final_claims: list[FinalClaim] = []
    failed = 0
    for item in final_claims_unordered:
        if isinstance(item, BaseException):
            failed += 1
            log.warning("pipeline: один claim упал в обработке: %s", item)
            continue
        final_claims.append(item)

    # Сохраняем тот же порядок, что у Extractor'а — по таймкоду.
    final_claims.sort(key=lambda c: c["start"])

    unverifiable_count = sum(1 for c in final_claims if c["verdict"] == "unverifiable")
    duration = time.monotonic() - t0

    stats = PipelineStats(
        claims_in=n_in,
        stance_asserted=cnt["asserted"],
        stance_debunked_fully=cnt["debunked_fully"],
        stance_debunked_partially=cnt["debunked_partially"],
        stance_quoted_neutral=cnt["quoted_neutral"],
        claims_after_drop=len(survivors),
        final_claims=len(final_claims),
        debunked_drop_count=cnt["debunked_fully"],
        unverifiable_count=unverifiable_count,
        duration_s=round(duration, 3),
    )

    log.info(
        "pipeline: in=%d stance(a=%d,df=%d,dp=%d,qn=%d) → after_drop=%d → final=%d "
        "(unverifiable=%d, failed=%d) за %.2fs",
        stats["claims_in"],
        stats["stance_asserted"], stats["stance_debunked_fully"],
        stats["stance_debunked_partially"], stats["stance_quoted_neutral"],
        stats["claims_after_drop"], stats["final_claims"],
        stats["unverifiable_count"], failed, stats["duration_s"],
    )

    return PipelineResult(claims=final_claims, stats=stats)
