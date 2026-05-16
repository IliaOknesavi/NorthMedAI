"""
NorthMedAI Backend — FastAPI entry point.

Запуск: uvicorn main:app --reload
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from log_setup import setup_logging
setup_logging()

from db import (
    SessionLocal,
    analysis_to_dict,
    get_latest_analysis,
    init_db,
    list_analyses,
    save_analysis,
    upsert_video_transcript,
)
from agents.prompts import (
    JUDGE_VERSION,
    QA_VERSION,
    RETRIEVER_VERSION,
    STANCE_VERSION,
)
from detector import YANDEX_GPT_MODEL, analyze_claims
from models import Video
from transcript import extract_video_id, get_transcript
from youtube_transcript_api import NoTranscriptFound, TranscriptsDisabled

log = logging.getLogger("backend.main")

DETECTOR_VERSION = "v3-no-self-warnings"

# Блокируем параллельные /analyze для одного и того же video_id.
# Если расширение успело отправить второй запрос, пока первый ещё считает
# (модель идёт ~10 секунд), второй встанет здесь и дождётся, а потом
# просто отдаст уже сохранённый кэш.
_analyze_locks: dict[str, asyncio.Lock] = {}


def _get_video_lock(video_id: str) -> asyncio.Lock:
    lock = _analyze_locks.get(video_id)
    if lock is None:
        lock = asyncio.Lock()
        _analyze_locks[video_id] = lock
    return lock


# --- Lifecycle -----------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("startup: инициализирую БД")
    try:
        await init_db()
    except Exception:
        log.exception("startup: init_db упал — БД скорее всего не запущена")
    yield


app = FastAPI(title="NorthMedAI API", version="0.2.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# --- DI ------------------------------------------------------------------

async def get_session() -> AsyncSession:
    async with SessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


# --- Models --------------------------------------------------------------

class AnalyzeRequest(BaseModel):
    video_id: str  # YouTube video ID или полный URL


class AnalyzeResponse(BaseModel):
    video_id: str
    cached: bool                # True если отдали ранее сохранённый анализ
    analysis_id: int | None
    created_at: str | None
    model: str | None
    transcript_snippets: int
    transcript_preview: list[Any]
    claims: list[Any]


# --- Helpers -------------------------------------------------------------

async def _fetch_transcript_or_4xx(video_id: str) -> list[dict]:
    """Тонкая обёртка вокруг get_transcript с маппингом в HTTPException."""
    try:
        return get_transcript(video_id)
    except TranscriptsDisabled:
        raise HTTPException(
            status_code=422,
            detail="У этого видео субтитры отключены автором.",
        )
    except NoTranscriptFound:
        raise HTTPException(
            status_code=422,
            detail="Для этого видео не найдено ни одного трека субтитров.",
        )
    except RuntimeError as e:
        log.error("transcript: сетевой сбой: %s", e)
        raise HTTPException(
            status_code=502,
            detail=(
                "YouTube временно не отдаёт субтитры (Connection reset). "
                "Подождите 30–60 секунд и нажмите «Проверить» ещё раз. "
                "Если повторяется — отключите VPN/прокси."
            ),
        )
    except Exception as e:
        log.exception("transcript: неожиданная ошибка")
        raise HTTPException(status_code=500, detail=f"Субтитры недоступны: {e}")


def _filter_for_overlay(claims: list[dict]) -> list[dict]:
    """
    Что доходит до content_script.js:
    - убираем verdict='unverifiable' — судья не уверен, пользователю не показываем
      (но claim сохранён в БД и виден в /video/{id}/history для дебага).
    См. docs/RAG_ARCHITECTURE.md §4.5.
    """
    return [c for c in claims if c.get("verdict") != "unverifiable"]


def _payload_from_analysis(
    *, video_id: str, analysis_dict: dict, transcript_preview: list, transcript_total: int, cached: bool
) -> dict:
    return {
        "video_id": video_id,
        "cached": cached,
        "analysis_id": analysis_dict["id"],
        "created_at": analysis_dict["created_at"],
        "model": analysis_dict["model"],
        "transcript_snippets": transcript_total,
        "transcript_preview": transcript_preview,
        "claims": _filter_for_overlay(analysis_dict["claims"]),
    }


# --- Routes --------------------------------------------------------------

@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/video/{video_id}")
async def get_video_state(
    video_id: str,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """
    Возвращает последний сохранённый анализ для video_id.
    404 если ничего ещё не считалось — фронт это поймёт как «надо запустить /analyze».
    """
    try:
        video_id = extract_video_id(video_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    analysis = await get_latest_analysis(session, video_id)
    if analysis is None:
        raise HTTPException(status_code=404, detail="Видео ещё не проверялось.")

    video = await session.get(Video, video_id)
    transcript = (video.transcript if video else None) or []

    return _payload_from_analysis(
        video_id=video_id,
        analysis_dict=analysis_to_dict(analysis),
        transcript_preview=transcript[:10],
        transcript_total=len(transcript),
        cached=True,
    )


@app.get("/video/{video_id}/history")
async def get_video_history(
    video_id: str,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Список всех версий анализа этого видео (без самих claims)."""
    try:
        video_id = extract_video_id(video_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    analyses = await list_analyses(session, video_id)
    return {
        "video_id": video_id,
        "versions": [
            {
                "id": a.id,
                "created_at": a.created_at.isoformat(),
                "model": a.model,
                "detector_version": a.detector_version,
                "is_latest": a.is_latest,
                "claims_count": a.claims_count,
                "false_count": a.false_count,
                "misleading_count": a.misleading_count,
                "conflicting_count": a.conflicting_count,
                "sophism_count": a.sophism_count,
            }
            for a in analyses
        ],
    }


@app.post("/analyze", response_model=AnalyzeResponse)
async def analyze(
    req: AnalyzeRequest,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """
    Основной эндпоинт. Если для видео уже есть свежий анализ — отдаём его.
    Иначе тянем субтитры, прогоняем YandexGPT и сохраняем как новую версию.
    Параллельные вызовы для одного video_id сериализуются через lock —
    второй вызов дождётся первого и отдаст уже сохранённый результат.
    """
    try:
        video_id = extract_video_id(req.video_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # Быстрый путь без локов — если уже есть кэш, нечего ждать
    cached = await get_latest_analysis(session, video_id)
    if cached is not None:
        video = await session.get(Video, video_id)
        transcript = (video.transcript if video else None) or []
        log.info("analyze: cache hit для %s (analysis_id=%d)", video_id, cached.id)
        return _payload_from_analysis(
            video_id=video_id,
            analysis_dict=analysis_to_dict(cached),
            transcript_preview=transcript[:10],
            transcript_total=len(transcript),
            cached=True,
        )

    # Кэша нет — встаём в очередь по video_id
    lock = _get_video_lock(video_id)
    if lock.locked():
        log.info("analyze: ждём другой /analyze для %s", video_id)

    async with lock:
        # Под локом ещё раз проверяем кэш — возможно, пока мы ждали,
        # другой запрос уже всё посчитал и сохранил
        cached = await get_latest_analysis(session, video_id)
        if cached is not None:
            video = await session.get(Video, video_id)
            transcript = (video.transcript if video else None) or []
            log.info("analyze: дождались чужого анализа для %s (id=%d)",
                     video_id, cached.id)
            return _payload_from_analysis(
                video_id=video_id,
                analysis_dict=analysis_to_dict(cached),
                transcript_preview=transcript[:10],
                transcript_total=len(transcript),
                cached=True,
            )

        return await _run_fresh_analysis(session, video_id)


@app.post("/reanalyze", response_model=AnalyzeResponse)
async def reanalyze(
    req: AnalyzeRequest,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Принудительная перепроверка. Создаёт НОВУЮ версию, старые сохраняются."""
    try:
        video_id = extract_video_id(req.video_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    log.info("reanalyze: принудительный пересчёт %s", video_id)
    async with _get_video_lock(video_id):
        return await _run_fresh_analysis(session, video_id)


async def _run_fresh_analysis(session: AsyncSession, video_id: str) -> dict:
    snippets = await _fetch_transcript_or_4xx(video_id)
    if not snippets:
        raise HTTPException(status_code=422, detail="Субтитры пустые")

    # Сохраняем транскрипт сразу — даже если детектор упадёт, в БД будет видео
    await upsert_video_transcript(session, video_id=video_id, transcript=snippets)

    try:
        analysis_result = await analyze_claims(snippets)
    except RuntimeError as e:
        log.error("detector конфиг: %s", e)
        analysis_result = {"claims": [], "stats": None}
    except Exception:
        log.exception("detector упал, отдаю пустые claims")
        analysis_result = {"claims": [], "stats": None}

    claims = analysis_result["claims"]
    stats = analysis_result["stats"]

    analysis = await save_analysis(
        session,
        video_id=video_id,
        claims=claims,
        model=YANDEX_GPT_MODEL,
        detector_version=DETECTOR_VERSION,
        stance_version=STANCE_VERSION,
        retriever_version=RETRIEVER_VERSION,
        judge_version=JUDGE_VERSION,
        qa_version=QA_VERSION,
        debunked_drop_count=(stats["debunked_drop_count"] if stats else 0),
    )

    return _payload_from_analysis(
        video_id=video_id,
        analysis_dict=analysis_to_dict(analysis),
        transcript_preview=snippets[:10],
        transcript_total=len(snippets),
        cached=False,
    )
