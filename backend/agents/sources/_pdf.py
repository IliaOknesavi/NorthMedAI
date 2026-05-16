"""
agents/sources/_pdf.py — асинхронный fetch + parse PDF для адаптеров
WHO / Минздрав, у которых guidelines часто живут в PDF.

Подход прагматичный:
  - httpx скачивает байты (с тайм-аутом и хард-лимитом на размер);
  - pdfplumber вытаскивает текст первых N страниц (PDF могут быть толстые,
    нам нужны введение/summary);
  - возвращаем сжатый snippet до 600 символов.

Тяжёлая работа pdfplumber выполняется в `asyncio.to_thread` — иначе
блокирует event loop.
"""

from __future__ import annotations

import asyncio
import logging
from io import BytesIO

import httpx

log = logging.getLogger("agents.sources.pdf")


MAX_PDF_BYTES = 8 * 1024 * 1024     # 8 MB — typical WHO guideline fits
MAX_PAGES = 2                        # читаем только первые 2 страницы
MAX_SNIPPET_CHARS = 600
FETCH_TIMEOUT = 15.0


async def fetch_pdf_snippet(url: str) -> str:
    """
    Скачать PDF, прочитать первые MAX_PAGES страниц, вернуть короткий
    snippet. На любую ошибку — пустая строка.
    """
    try:
        async with httpx.AsyncClient(
            timeout=FETCH_TIMEOUT, follow_redirects=True,
        ) as client:
            r = await client.get(url, headers={
                "User-Agent": "Mozilla/5.0 (compatible; NorthMedAI/1.0)"
            })
            r.raise_for_status()
            data = r.content
    except httpx.HTTPError as e:
        log.debug("pdf fetch failed for %s: %s", url, e)
        return ""
    except Exception:  # noqa: BLE001
        log.debug("pdf fetch unexpected error for %s", url, exc_info=True)
        return ""

    if len(data) > MAX_PDF_BYTES:
        log.debug("pdf %s слишком большой (%d байт), пропускаю", url, len(data))
        return ""

    # Тяжёлая операция → в thread'е
    def _extract(buf: bytes) -> str:
        try:
            import pdfplumber  # ленивый import — pdfplumber тянет PIL
        except ImportError:
            return ""
        try:
            with pdfplumber.open(BytesIO(buf)) as pdf:
                pages = pdf.pages[:MAX_PAGES]
                texts: list[str] = []
                for p in pages:
                    t = p.extract_text() or ""
                    texts.append(t.strip())
                joined = "\n".join(t for t in texts if t)
                # Сжимаем повторяющиеся пробелы
                cleaned = " ".join(joined.split())
                return cleaned[:MAX_SNIPPET_CHARS]
        except Exception:  # noqa: BLE001
            log.debug("pdfplumber упал для %s", url, exc_info=True)
            return ""

    return await asyncio.to_thread(_extract, data)


def is_pdf_url(url: str) -> bool:
    """Эвристика: ссылка похожа на PDF."""
    if not url:
        return False
    u = url.lower().split("?", 1)[0].split("#", 1)[0]
    return u.endswith(".pdf")
