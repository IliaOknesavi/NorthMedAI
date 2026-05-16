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

from .final_qa import qa_pass
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
    # Сколько финальных claim'ов было ДО QA-прохода (для сравнения)
    claims_before_qa: int
    final_claims: int
    debunked_drop_count: int
    # Метрики Final QA
    qa_kept: int
    qa_dropped: int
    qa_repaired: int
    qa_dedup_merges: int
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
                claims_before_qa=0,
                final_claims=0,
                debunked_drop_count=0,
                qa_kept=0,
                qa_dropped=0,
                qa_repaired=0,
                qa_dedup_merges=0,
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

    # --- Drop: claim'ы с stance, которые не должны идти в overlay --------
    # Дропаем И debunked_fully (автор сам разоблачил), И quoted_neutral
    # (автор цитирует чужую позицию — пользователю не нужно «исправлять»
    # автора, который и так нейтрально передал чужой тезис).
    # См. docs/RAG_ARCHITECTURE.md §4.1.5.
    DROP_STANCES = {"debunked_fully", "quoted_neutral"}
    survivors: list[tuple[RawClaim, "StanceLabel"]] = []  # type: ignore[name-defined]
    for raw, s in zip(raw_claims, stances):
        if s["stance"] in DROP_STANCES:
            log.info(
                "pipeline: drop claim @ %.1fs (stance=%s): %r",
                raw["start"], s["stance"], raw["text"][:80],
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

    before_qa: list[FinalClaim] = []
    failed = 0
    for item in final_claims_unordered:
        if isinstance(item, BaseException):
            failed += 1
            log.warning("pipeline: один claim упал в обработке: %s", item)
            continue
        before_qa.append(item)

    # Сохраняем тот же порядок, что у Extractor'а — по таймкоду.
    before_qa.sort(key=lambda c: c["start"])

    # --- Шаг 5: Final QA --------------------------------------------------
    # Видит всё видео + все final claims сразу. Может дропать, репэйрить,
    # дедупать. Подробности — docs/RAG_ARCHITECTURE.md §4.6.
    qa = await qa_pass(snippets, before_qa)
    final_claims = qa["claims"]

    # Подсчёт действий QA для метрик
    qa_dropped = sum(
        len(a["claim_indices"]) for a in qa["actions"] if a["action"] == "drop"
    )
    qa_repaired = sum(
        len(a["claim_indices"]) for a in qa["actions"] if a["action"] == "repair"
    )
    qa_dedup_merges = sum(
        # каждая dedup-группа схлопывает (N-1) дубликатов в один
        max(0, len(a["claim_indices"]) - 1)
        for a in qa["actions"]
        if a["action"] == "dedup_into"
    )

    unverifiable_count = sum(1 for c in final_claims if c["verdict"] == "unverifiable")
    duration = time.monotonic() - t0

    # debunked_drop_count теперь сумма всех stance, которые мы дропаем
    # (debunked_fully + quoted_neutral). Раздельные счётчики живут в
    # stance_* полях для дебага и /history.
    stats = PipelineStats(
        claims_in=n_in,
        stance_asserted=cnt["asserted"],
        stance_debunked_fully=cnt["debunked_fully"],
        stance_debunked_partially=cnt["debunked_partially"],
        stance_quoted_neutral=cnt["quoted_neutral"],
        claims_after_drop=len(survivors),
        claims_before_qa=len(before_qa),
        final_claims=len(final_claims),
        debunked_drop_count=cnt["debunked_fully"] + cnt["quoted_neutral"],
        qa_kept=len(final_claims),
        qa_dropped=qa_dropped,
        qa_repaired=qa_repaired,
        qa_dedup_merges=qa_dedup_merges,
        unverifiable_count=unverifiable_count,
        duration_s=round(duration, 3),
    )

    log.info(
        "pipeline: in=%d stance(a=%d,df=%d,dp=%d,qn=%d) → after_drop=%d → "
        "judge=%d → QA(kept=%d,drop=%d,repair=%d,dedup=%d) → final=%d "
        "(unverifiable=%d, failed=%d) за %.2fs",
        stats["claims_in"],
        stats["stance_asserted"], stats["stance_debunked_fully"],
        stats["stance_debunked_partially"], stats["stance_quoted_neutral"],
        stats["claims_after_drop"], stats["claims_before_qa"],
        stats["qa_kept"], stats["qa_dropped"], stats["qa_repaired"],
        stats["qa_dedup_merges"],
        stats["final_claims"], stats["unverifiable_count"],
        failed, stats["duration_s"],
    )

    return PipelineResult(claims=final_claims, stats=stats)
