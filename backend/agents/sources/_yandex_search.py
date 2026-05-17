"""
agents/sources/_yandex_search.py — общий клиент Yandex Search API.

Используется адаптерами WHO, CDC/NEJM, Минздрав, News. Все они работают
по одной схеме: вызов Yandex Search с `site:` ограничением по whitelist,
получение списка URL + snippet, опциональный фетч/парс контента.

Документация API: https://yandex.cloud/ru/docs/search-api/operations/search.

Без ключа (YANDEX_SEARCH_API_KEY не задан в .env) клиент возвращает []
и пишет WARNING — это требование на сегодня (см. диалог с Ильёй):
адаптеры должны работать с graceful fallback'ом.

Формат: используем синхронный XML-endpoint, потому что он самый простой
и не требует polling'а как у /searchAsync.
"""

from __future__ import annotations

import asyncio
import logging
import os
import xml.etree.ElementTree as ET
from datetime import datetime
from typing import Any

import httpx
from dotenv import load_dotenv

load_dotenv()
log = logging.getLogger("agents.sources.yandex_search")


# В Yandex Cloud один API key работает для всех сервисов AI Studio
# (YandexGPT + Embeddings + Search API), если у Service Account есть
# соответствующие роли. Поэтому если YANDEX_SEARCH_API_KEY не задан
# отдельно — fallback на основной YANDEX_API_KEY, который уже точно
# есть в .env (без него YandexGPT не работал бы). Если у Service Account
# нет роли search-api.executor — Yandex Search вернёт 401, и в логах
# увидим warning.
YANDEX_SEARCH_API_KEY = (
    os.getenv("YANDEX_SEARCH_API_KEY", "").strip()
    or os.getenv("YANDEX_API_KEY", "").strip()
)
# Yandex Search хочет folder_id; используем тот же, что у YandexGPT
YANDEX_FOLDER_ID = os.getenv("YANDEX_FOLDER_ID", "").strip()

# Альтернативный domain: yandex.com работает за рубежом.
YANDEX_SEARCH_BASE = os.getenv(
    "YANDEX_SEARCH_BASE",
    "https://yandex.ru/search/xml",
).strip()

REQUEST_TIMEOUT = 15.0
PER_QUERY_LIMIT = 5            # сколько top-результатов берём из выдачи
_SEMAPHORE = asyncio.Semaphore(5)


def is_configured() -> bool:
    return bool(YANDEX_SEARCH_API_KEY and YANDEX_FOLDER_ID)


# --- Парсинг XML ----------------------------------------------------------

def _strip_xml_tags(element: ET.Element) -> str:
    """Yandex кладёт <hlword> внутри passages — вытаскиваем чистый текст."""
    parts: list[str] = []
    if element.text:
        parts.append(element.text)
    for child in element:
        parts.append(_strip_xml_tags(child))
        if child.tail:
            parts.append(child.tail)
    return "".join(parts).strip()


def _parse_yandex_search_xml(xml_text: str) -> list[dict[str, Any]]:
    """Достать структурированный список результатов из XML-выдачи Yandex."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        log.warning("yandex_search: невалидный XML: %s", e)
        return []

    # Проверяем на error в ответе
    error = root.find(".//response/error")
    if error is not None:
        code = error.get("code", "?")
        text = (error.text or "").strip()
        log.warning("yandex_search: API error code=%s message=%r", code, text)
        return []

    out: list[dict[str, Any]] = []
    for doc in root.iter("doc"):
        url_el = doc.find("url")
        title_el = doc.find("title")
        url = (url_el.text or "").strip() if url_el is not None else ""
        if not url:
            continue
        title = _strip_xml_tags(title_el) if title_el is not None else url

        passages: list[str] = []
        for p in doc.iter("passage"):
            text = _strip_xml_tags(p)
            if text:
                passages.append(text)
        snippet = " ".join(passages)[:600]

        modtime_el = doc.find("modtime")
        modtime = (modtime_el.text or "").strip() if modtime_el is not None else ""

        out.append({
            "url": url,
            "title": title[:200],
            "snippet": snippet,
            "modtime": modtime,
        })
    return out


# --- Главная функция: search() -------------------------------------------

def _join_sites(domains: list[str]) -> str:
    """Превратить whitelist доменов в фильтр для Yandex Search.

    Yandex понимает синтаксис `site:domain.com | site:other.com` — это OR.
    """
    if not domains:
        return ""
    return " | ".join(f"site:{d}" for d in domains)


async def search(
    queries: list[str],
    *,
    domains: list[str] | None = None,
    lang: str = "ru",
    per_query: int = PER_QUERY_LIMIT,
) -> list[dict[str, Any]]:
    """
    Один или несколько поисковых запросов с опциональным whitelist'ом доменов.

    Возвращает список dict'ов: {url, title, snippet, modtime, query}.
    На любых ошибках — [] и WARNING.

    Если YANDEX_SEARCH_API_KEY не задан — сразу [].
    """
    if not is_configured():
        log.warning(
            "yandex_search: ни YANDEX_SEARCH_API_KEY, ни YANDEX_API_KEY не заданы — fallback на []",
        )
        return []

    if not queries:
        return []

    unique = list(dict.fromkeys(q.strip() for q in queries if q and q.strip()))
    if not unique:
        return []

    domain_filter = _join_sites(domains or [])

    async def _one_query(query: str) -> list[dict[str, Any]]:
        final_query = f"{query} {domain_filter}" if domain_filter else query
        params = {
            "folderid": YANDEX_FOLDER_ID,
            "apikey": YANDEX_SEARCH_API_KEY,
            "query": final_query,
            "l10n": lang,
            "groupby": (
                f"attr=d.mode=deep.groups-on-page={per_query}.docs-in-group=1"
            ),
        }
        async with _SEMAPHORE:
            try:
                async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
                    r = await client.get(YANDEX_SEARCH_BASE, params=params)
                    r.raise_for_status()
            except httpx.HTTPError as e:
                log.warning("yandex_search: HTTP error для %r: %s", final_query[:80], e)
                return []
            except Exception:  # noqa: BLE001
                log.exception("yandex_search: непредвиденная ошибка для %r", final_query[:80])
                return []

        items = _parse_yandex_search_xml(r.text)
        for it in items:
            it["query"] = query
        return items

    all_results = await asyncio.gather(
        *(_one_query(q) for q in unique), return_exceptions=False,
    )

    # Дедупим по URL, сохраняем порядок (по relevance)
    seen: set[str] = set()
    flat: list[dict[str, Any]] = []
    for batch in all_results:
        for item in batch:
            url = item["url"]
            if url in seen:
                continue
            seen.add(url)
            flat.append(item)

    log.info("yandex_search: %d уникальных результатов для %d запросов (домены=%r)",
             len(flat), len(unique), domains or "any")
    return flat


# --- Утилита: ranges score / freshness ----------------------------------

def parse_modtime_year(modtime: str) -> int | None:
    """Yandex отдаёт modtime типа '20210315T143000' — берём год."""
    if not modtime or len(modtime) < 4:
        return None
    try:
        y = int(modtime[:4])
        if 1990 <= y <= 2100:
            return y
    except ValueError:
        return None
    return None


def freshness_modifier(year: int | None) -> float:
    """То же правило что у PubMed: +0.05 if ≤5 лет, -0.10 if ≥15."""
    if year is None:
        return 0.0
    current = datetime.utcnow().year
    age = current - year
    if age <= 5:
        return 0.05
    if age >= 15:
        return -0.10
    return 0.0
