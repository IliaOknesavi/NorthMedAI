"""
models.py — SQLAlchemy ORM-модели для NorthMedAI.

Структура (история версий):
  videos      : 1 запись на YouTube video_id (метаинформация + кэш транскрипта)
  analyses    : N записей на одно видео — каждая проверка добавляет новую,
                старые сохраняются. is_latest=True у самой свежей.
  claims      : N записей на один analysis — собственно метки на таймлайне.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Video(Base):
    __tablename__ = "videos"

    # YouTube video_id — 11 символов, естественный первичный ключ
    video_id: Mapped[str] = mapped_column(String(16), primary_key=True)

    # Снимок транскрипта (на каком языке пришёл, сколько сниппетов и сами сниппеты)
    transcript_lang: Mapped[str | None] = mapped_column(String(16), nullable=True)
    transcript_snippets: Mapped[int] = mapped_column(Integer, default=0)
    transcript: Mapped[list | None] = mapped_column(JSONB, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    analyses: Mapped[list["Analysis"]] = relationship(
        back_populates="video",
        cascade="all, delete-orphan",
        order_by="Analysis.created_at.desc()",
    )


class Analysis(Base):
    __tablename__ = "analyses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    video_id: Mapped[str] = mapped_column(
        ForeignKey("videos.video_id", ondelete="CASCADE"), index=True
    )

    # Какой моделью получено + версия промпта/детектора — пригодится при отладке
    model: Mapped[str] = mapped_column(String(128), default="")
    detector_version: Mapped[str] = mapped_column(String(32), default="")
    # Версии остальных агентов (P0+): пустые строки пока агенты — stub'ы.
    # См. docs/RAG_ARCHITECTURE.md §9 и docs/PROMPTS.md «Версионирование промптов».
    stance_version: Mapped[str] = mapped_column(String(32), default="")
    retriever_version: Mapped[str] = mapped_column(String(64), default="")
    judge_version: Mapped[str] = mapped_column(String(32), default="")
    qa_version: Mapped[str] = mapped_column(String(32), default="")

    # Флаг последней версии — упрощает запрос «дай актуальное состояние»
    is_latest: Mapped[bool] = mapped_column(Boolean, default=True, index=True)

    # Краткие агрегаты — чтобы не считать каждый раз
    claims_count: Mapped[int] = mapped_column(Integer, default=0)
    false_count: Mapped[int] = mapped_column(Integer, default=0)
    misleading_count: Mapped[int] = mapped_column(Integer, default=0)
    conflicting_count: Mapped[int] = mapped_column(Integer, default=0)
    sophism_count: Mapped[int] = mapped_column(Integer, default=0)
    # Сколько claim'ов отфильтровал Stance Detector как «автор сам разобрал»
    # (то, что не показывается пользователю, но видно в метриках).
    debunked_drop_count: Mapped[int] = mapped_column(Integer, default=0)
    # Сколько Judge оставил без вердикта (источников не нашлось).
    unverifiable_count: Mapped[int] = mapped_column(Integer, default=0)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    video: Mapped[Video] = relationship(back_populates="analyses")
    claims: Mapped[list["Claim"]] = relationship(
        back_populates="analysis",
        cascade="all, delete-orphan",
        order_by="Claim.start",
    )

    __table_args__ = (
        Index("ix_analyses_video_latest", "video_id", "is_latest"),
    )


class Claim(Base):
    __tablename__ = "claims"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    analysis_id: Mapped[int] = mapped_column(
        ForeignKey("analyses.id", ondelete="CASCADE"), index=True
    )

    text: Mapped[str] = mapped_column(Text)
    start: Mapped[float] = mapped_column(Float, index=True)
    verdict: Mapped[str] = mapped_column(String(32))   # false / misleading / conflicting / unverifiable
    type: Mapped[str] = mapped_column(String(32))      # claim / sophism
    explanation: Mapped[str] = mapped_column(Text, default="")
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    sources: Mapped[list | None] = mapped_column(JSONB, nullable=True)

    # --- Аудит-поля от агентов (P0+) -----------------------------------
    # Что говорил Extractor до того, как Judge переоценил verdict.
    extractor_verdict: Mapped[str] = mapped_column(String(32), default="")
    # Риторическая роль claim'а в видео по Stance Detector.
    # "asserted" | "debunked_partially" | "quoted_neutral".
    # "debunked_fully" сюда никогда не доходит — отфильтровано раньше.
    stance: Mapped[str] = mapped_column(String(32), default="asserted")
    # Для debunked_partially — что именно автор упустил.
    stance_missing: Mapped[str] = mapped_column(Text, default="")
    # Объяснение Judge: почему verdict такой (1-2 предложения, для дебага).
    judge_notes: Mapped[str] = mapped_column(Text, default="")
    # Поисковые запросы, которые Query Former сгенерировал для этого claim'а.
    # JSONB: {"pubmed": [...], "who": [...], ...}.
    search_queries: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    analysis: Mapped[Analysis] = relationship(back_populates="claims")
