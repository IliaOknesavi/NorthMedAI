"""
agents/sources/_embeddings.py — клиент Yandex Embeddings API.

Доки: https://yandex.cloud/ru/docs/foundation-models/concepts/embeddings
Эндпоинт: POST https://llm.api.cloud.yandex.net/foundationModels/v1/textEmbedding
Модели:
  - text-search-doc   — для документов (1024 dim)
  - text-search-query — для запросов (1024 dim)

Используется:
  - corpus_ingest.py — на этапе загрузки PDF (модель doc)
  - corpus.py        — для query-вектора при поиске (модель query)

Graceful fallback: без YANDEX_API_KEY / FOLDER_ID возвращает None.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Literal

import httpx
from dotenv import load_dotenv

load_dotenv()
log = logging.getLogger("agents.sources.embeddings")

YANDEX_API_KEY = os.getenv("YANDEX_API_KEY", "").strip()
YANDEX_FOLDER_ID = os.getenv("YANDEX_FOLDER_ID", "").strip()
YANDEX_EMB_BASE = "https://llm.api.cloud.yandex.net/foundationModels/v1/textEmbedding"

EMBEDDING_DIM = 1024
REQUEST_TIMEOUT = 20.0
# Yandex Embeddings — отдельный rate-limit, обычно мягкий
_SEMAPHORE = asyncio.Semaphore(3)

EmbeddingKind = Literal["doc", "query"]


def is_configured() -> bool:
    return bool(YANDEX_API_KEY and YANDEX_FOLDER_ID)


def _model_uri(kind: EmbeddingKind) -> str:
    name = "text-search-doc" if kind == "doc" else "text-search-query"
    return f"emb://{YANDEX_FOLDER_ID}/{name}/latest"


async def embed(text: str, *, kind: EmbeddingKind = "doc") -> list[float] | None:
    """Получить эмбеддинг для одного текста. Возвращает None при ошибке."""
    if not is_configured():
        log.debug("embeddings: нет YANDEX_API_KEY — возвращаю None")
        return None
    if not text or not text.strip():
        return None

    headers = {
        "Authorization": f"Api-Key {YANDEX_API_KEY}",
        "x-folder-id": YANDEX_FOLDER_ID,
        "Content-Type": "application/json",
    }
    payload = {"modelUri": _model_uri(kind), "text": text[:8000]}

    async with _SEMAPHORE:
        try:
            async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
                r = await client.post(YANDEX_EMB_BASE, json=payload, headers=headers)
                r.raise_for_status()
                data = r.json()
        except httpx.HTTPError as e:
            log.warning("embeddings: HTTP error — %s", e)
            return None
        except Exception:  # noqa: BLE001
            log.exception("embeddings: непредвиденная ошибка")
            return None

    vec = data.get("embedding")
    if not isinstance(vec, list) or len(vec) != EMBEDDING_DIM:
        log.warning("embeddings: неожиданная форма ответа (dim=%s)",
                    len(vec) if isinstance(vec, list) else "?")
        return None
    return [float(x) for x in vec]


async def embed_many(texts: list[str], *, kind: EmbeddingKind = "doc") -> list[list[float] | None]:
    """Параллельно по семафору. Размер = количество входов; None в позиции = неудача."""
    return await asyncio.gather(
        *(embed(t, kind=kind) for t in texts), return_exceptions=False,
    )
