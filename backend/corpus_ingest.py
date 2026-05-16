"""
corpus_ingest.py — CLI для загрузки PDF в локальный RAG-корпус.

Использование:
  cd backend
  # Один PDF:
  python -m corpus_ingest /path/to/who_guideline.pdf --tier who --url https://who.int/...
  # Папка с PDF (рекурсивно):
  python -m corpus_ingest /path/to/corpus_dir --tier minzdrav --base-url https://cr.minzdrav.gov.ru
  # Перезагрузить (стереть старые chunks этого doc_url):
  python -m corpus_ingest <path> --reset

Шаги:
  1) Извлекаем текст из PDF (pdfplumber).
  2) Чанкуем на куски ~800-1000 символов с overlap 100 символов.
  3) Для каждого чанка получаем embedding через Yandex Embeddings (text-search-doc).
  4) Сохраняем в corpus_chunks.

Требует:
  - pgvector в Postgres (миграция M0003 применена).
  - YANDEX_API_KEY и YANDEX_FOLDER_ID в .env (для эмбеддингов).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from io import BytesIO
from pathlib import Path

from sqlalchemy import delete, text as sql_text
from sqlalchemy.ext.asyncio import AsyncSession

# Делаем backend/ в sys.path при прямом запуске
sys.path.insert(0, str(Path(__file__).resolve().parent))

from agents.sources._embeddings import embed_many, is_configured  # noqa: E402
from db import SessionLocal, init_db, engine  # noqa: E402
from log_setup import setup_logging  # noqa: E402
from models import CorpusChunk  # noqa: E402

setup_logging()
log = logging.getLogger("corpus_ingest")


CHUNK_SIZE = 900
CHUNK_OVERLAP = 100


def _extract_text(pdf_path: Path) -> str:
    """pdfplumber → текст всех страниц, конкатенированный."""
    try:
        import pdfplumber
    except ImportError:
        log.error("pdfplumber не установлен — pip install pdfplumber")
        return ""
    try:
        with pdfplumber.open(pdf_path) as pdf:
            parts = []
            for p in pdf.pages:
                t = (p.extract_text() or "").strip()
                if t:
                    parts.append(t)
            return "\n".join(parts)
    except Exception:  # noqa: BLE001
        log.exception("pdfplumber упал на %s", pdf_path)
        return ""


def _chunk(text: str, *, size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """Грубое sliding-window чанкование. Сжимает белые пробелы перед нарезкой."""
    text = " ".join(text.split())
    if not text:
        return []
    chunks: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        end = min(n, i + size)
        chunks.append(text[i:end])
        if end == n:
            break
        i = end - overlap
    return chunks


async def _delete_existing(session: AsyncSession, doc_url: str) -> int:
    """Удалить все chunks с данным doc_url. Возвращает количество."""
    res = await session.execute(
        delete(CorpusChunk).where(CorpusChunk.doc_url == doc_url)
    )
    return res.rowcount or 0


async def _insert_chunks(
    session: AsyncSession, *,
    doc_url: str, doc_title: str, doc_tier: str,
    texts: list[str], embeddings: list[list[float] | None],
) -> int:
    """Вставить chunks с embedding'ами через сырой SQL (pgvector литерал)."""
    inserted = 0
    for idx, (txt, vec) in enumerate(zip(texts, embeddings)):
        if vec is None:
            log.warning("chunk %d: embed=None, пропускаю", idx)
            continue
        vec_literal = "[" + ",".join(f"{x:.6f}" for x in vec) + "]"
        await session.execute(
            sql_text("""
                INSERT INTO corpus_chunks
                  (doc_url, doc_title, doc_tier, chunk_idx, text, chunk_metadata, embedding, created_at)
                VALUES
                  (:doc_url, :doc_title, :doc_tier, :chunk_idx, :text, NULL,
                   CAST(:vec AS vector), NOW())
            """),
            {
                "doc_url": doc_url, "doc_title": doc_title,
                "doc_tier": doc_tier, "chunk_idx": idx, "text": txt,
                "vec": vec_literal,
            },
        )
        inserted += 1
    return inserted


async def ingest_one(
    pdf_path: Path, *, tier: str, doc_url: str, title: str | None = None, reset: bool = False,
) -> tuple[int, int]:
    """Ингестит один PDF. Возвращает (inserted, total_chunks)."""
    log.info("ingest: %s → tier=%s, url=%s", pdf_path, tier, doc_url)
    text = _extract_text(pdf_path)
    if not text:
        log.warning("ingest: пустой текст из %s", pdf_path)
        return 0, 0

    chunks = _chunk(text)
    log.info("ingest: %s → %d чанков, %d симв.", pdf_path.name, len(chunks), len(text))

    embeddings = await embed_many(chunks, kind="doc")
    successful = sum(1 for e in embeddings if e is not None)
    log.info("ingest: получено %d/%d эмбеддингов", successful, len(chunks))

    doc_title = title or pdf_path.stem.replace("_", " ")
    async with SessionLocal() as session:
        if reset:
            removed = await _delete_existing(session, doc_url)
            log.info("ingest: reset — удалено %d старых chunks для %s", removed, doc_url)
        inserted = await _insert_chunks(
            session, doc_url=doc_url, doc_title=doc_title, doc_tier=tier,
            texts=chunks, embeddings=embeddings,
        )
        await session.commit()
    log.info("ingest: %s → %d chunks записано", pdf_path.name, inserted)
    return inserted, len(chunks)


async def main_async(args: argparse.Namespace) -> None:
    if not is_configured():
        log.error("YANDEX_API_KEY / YANDEX_FOLDER_ID не заданы — embeddings недоступны")
        return

    await init_db()  # на случай если миграции не катились

    path = Path(args.path).expanduser().resolve()
    if not path.exists():
        log.error("Путь не существует: %s", path)
        return

    pdfs: list[Path] = []
    if path.is_file() and path.suffix.lower() == ".pdf":
        pdfs = [path]
    elif path.is_dir():
        pdfs = sorted(path.rglob("*.pdf"))
    else:
        log.error("Ожидался .pdf файл или папка с PDF")
        return

    if not pdfs:
        log.error("Не найдено ни одного .pdf")
        return

    log.info("ingest: найдено %d PDF файлов", len(pdfs))

    total_inserted = 0
    for pdf in pdfs:
        # doc_url: если задан --url — используем; если папка с --base-url —
        # дописываем относительный путь; иначе берём абсолютный путь файла.
        if args.url:
            doc_url = args.url
        elif args.base_url:
            rel = pdf.relative_to(path) if path.is_dir() else pdf.name
            doc_url = args.base_url.rstrip("/") + "/" + str(rel).replace(os.sep, "/")
        else:
            doc_url = f"file://{pdf}"

        inserted, _total = await ingest_one(
            pdf, tier=args.tier, doc_url=doc_url, title=args.title, reset=args.reset,
        )
        total_inserted += inserted

    log.info("ingest: ГОТОВО. Всего записано %d chunks из %d PDF", total_inserted, len(pdfs))
    await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description="Загрузить PDF(ы) в локальный RAG-корпус")
    parser.add_argument("path", help="Путь к .pdf файлу или папке с PDF (рекурсивно)")
    parser.add_argument("--tier", default="corpus",
                        choices=["who", "cdc_nejm", "minzdrav", "pubmed", "corpus"],
                        help="Тир документов (влияет на вес в Retriever'е)")
    parser.add_argument("--url", default="",
                        help="Каноническая ссылка на документ (для одного PDF)")
    parser.add_argument("--base-url", default="",
                        help="Базовый URL, который дополнится относительным путём (для папки)")
    parser.add_argument("--title", default="",
                        help="Человеко-читаемое название документа")
    parser.add_argument("--reset", action="store_true",
                        help="Перед ингестом удалить существующие chunks этого doc_url")
    args = parser.parse_args()

    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
