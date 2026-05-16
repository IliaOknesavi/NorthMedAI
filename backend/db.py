"""
db.py — асинхронный слой доступа к Postgres для NorthMedAI.

Главные операции:
  init_db()                  — создать таблицы если их нет (вызывается на старте)
  get_video(video_id)        — есть ли уже такое видео
  get_latest_analysis(vid)   — последняя версия анализа (или None)
  save_analysis(vid, ...)    — добавить НОВУЮ версию, старые остаются;
                               снимает is_latest со старых, ставит на новой
  list_analyses(video_id)    — история всех версий (для UI «прошлые проверки»)
"""

from __future__ import annotations

import logging
import os
from typing import Any

from dotenv import load_dotenv
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import selectinload

from models import Analysis, Base, Claim, Video

load_dotenv()
log = logging.getLogger("db")

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+asyncpg://nmai:nmai@localhost:5432/nmai",
).strip()

# echo=False — мы и так логируем явные операции; pool_pre_ping чтобы пересоздавать
# мёртвые подключения после долгих простоев.
_engine_kwargs: dict[str, Any] = {"echo": False, "pool_pre_ping": True}
if not DATABASE_URL.startswith("sqlite"):
    # SQLite не поддерживает классический pool — параметры применяем только к Postgres.
    _engine_kwargs.update(pool_size=5, max_overflow=10)
engine = create_async_engine(DATABASE_URL, **_engine_kwargs)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def init_db() -> None:
    """Создаёт таблицы при первом запуске. Идемпотентно."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    log.info("init_db: схема готова (%s)", _safe_url())


def _safe_url() -> str:
    """Урезанный DSN без пароля — для логов."""
    if "@" not in DATABASE_URL:
        return DATABASE_URL
    head, tail = DATABASE_URL.split("@", 1)
    if "//" in head and ":" in head.split("//", 1)[1]:
        scheme, rest = head.split("//", 1)
        user = rest.split(":", 1)[0]
        return f"{scheme}//{user}:***@{tail}"
    return f"***@{tail}"


# --- Видео ---------------------------------------------------------------

async def get_video(session: AsyncSession, video_id: str) -> Video | None:
    return await session.get(Video, video_id)


async def upsert_video_transcript(
    session: AsyncSession,
    video_id: str,
    transcript: list[dict],
    lang: str | None = None,
) -> Video:
    """Создаёт видео если его нет; обновляет снимок транскрипта."""
    video = await session.get(Video, video_id)
    if video is None:
        video = Video(video_id=video_id)
        session.add(video)
    video.transcript = transcript
    video.transcript_snippets = len(transcript)
    video.transcript_lang = lang
    await session.flush()
    return video


# --- Анализ --------------------------------------------------------------

def _aggregate(claims: list[dict]) -> dict[str, int]:
    return {
        "claims_count": len(claims),
        "false_count": sum(1 for c in claims if c.get("verdict") == "false"),
        "misleading_count": sum(1 for c in claims if c.get("verdict") == "misleading"),
        "conflicting_count": sum(1 for c in claims if c.get("verdict") == "conflicting"),
        "sophism_count": sum(1 for c in claims if c.get("type") == "sophism"),
    }


async def get_latest_analysis(
    session: AsyncSession, video_id: str
) -> Analysis | None:
    """Возвращает последний анализ для видео вместе с claims (eager-load)."""
    stmt = (
        select(Analysis)
        .where(Analysis.video_id == video_id, Analysis.is_latest.is_(True))
        .options(selectinload(Analysis.claims))
        .order_by(Analysis.created_at.desc())
        .limit(1)
    )
    res = await session.execute(stmt)
    return res.scalar_one_or_none()


async def list_analyses(
    session: AsyncSession, video_id: str
) -> list[Analysis]:
    """Все версии анализа для видео (без claims — для индекса истории)."""
    stmt = (
        select(Analysis)
        .where(Analysis.video_id == video_id)
        .order_by(Analysis.created_at.desc())
    )
    res = await session.execute(stmt)
    return list(res.scalars().all())


async def save_analysis(
    session: AsyncSession,
    *,
    video_id: str,
    claims: list[dict],
    model: str = "",
    detector_version: str = "",
) -> Analysis:
    """
    Добавляет новую версию анализа. Снимает is_latest со старых версий и
    проставляет True для новой.
    """
    # Со старых снимаем флаг — но не удаляем
    await session.execute(
        update(Analysis)
        .where(Analysis.video_id == video_id, Analysis.is_latest.is_(True))
        .values(is_latest=False)
    )

    agg = _aggregate(claims)
    analysis = Analysis(
        video_id=video_id,
        model=model[:128],
        detector_version=detector_version[:32],
        is_latest=True,
        **agg,
    )
    session.add(analysis)
    await session.flush()  # чтобы появился analysis.id

    for c in claims:
        session.add(
            Claim(
                analysis_id=analysis.id,
                text=str(c.get("text", ""))[:4000],
                start=float(c.get("start", 0.0)),
                verdict=str(c.get("verdict", "misleading"))[:32],
                type=str(c.get("type", "claim"))[:32],
                explanation=str(c.get("explanation", ""))[:4000],
                confidence=float(c.get("confidence", 0.0)),
                sources=c.get("sources") or None,
            )
        )
    await session.flush()
    # Сразу подгружаем коллекцию claims в объект analysis — иначе при
    # последующем analysis_to_dict() SQLAlchemy попытается её лениво
    # дотянуть и упадёт с MissingGreenlet (мы в async-сессии).
    await session.refresh(analysis, attribute_names=["claims"])
    log.info(
        "save_analysis: video=%s analysis_id=%d claims=%d (false=%d, mis=%d, conf=%d, soph=%d)",
        video_id, analysis.id,
        agg["claims_count"], agg["false_count"], agg["misleading_count"],
        agg["conflicting_count"], agg["sophism_count"],
    )
    return analysis


# --- Сериализация для API ------------------------------------------------

def analysis_to_dict(analysis: Analysis) -> dict[str, Any]:
    """Готовим объект для JSON-ответа FastAPI."""
    return {
        "id": analysis.id,
        "video_id": analysis.video_id,
        "model": analysis.model,
        "detector_version": analysis.detector_version,
        "is_latest": analysis.is_latest,
        "created_at": analysis.created_at.isoformat(),
        "claims_count": analysis.claims_count,
        "false_count": analysis.false_count,
        "misleading_count": analysis.misleading_count,
        "conflicting_count": analysis.conflicting_count,
        "sophism_count": analysis.sophism_count,
        "claims": [claim_to_dict(c) for c in analysis.claims],
    }


def claim_to_dict(c: Claim) -> dict[str, Any]:
    return {
        "text": c.text,
        "start": c.start,
        "verdict": c.verdict,
        "type": c.type,
        "explanation": c.explanation,
        "confidence": c.confidence,
        "sources": c.sources or [],
    }
