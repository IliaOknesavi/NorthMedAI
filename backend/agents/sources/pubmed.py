"""
agents/sources/pubmed.py — PubMed/PMC через NCBI E-utilities.

API публичный, без ключа работает (3 req/sec), с ключом 10 req/sec.
Документация: https://www.ncbi.nlm.nih.gov/books/NBK25497/

Используем два эндпоинта:
  - esearch.fcgi  — поиск PMID по запросу
  - esummary.fcgi — метаданные статей (заголовок, авторы, дата)

Абстракт через efetch есть, но он дорогой (отдельный запрос на статью).
В P1 берём только metadata + заголовок — этого хватает Judge'у. В P2
можно добавить efetch с кэшированием snippet'ов.
"""

from __future__ import annotations

import asyncio
import logging
import os
import xml.etree.ElementTree as ET  # esummary возвращает JSON, efetch — XML
from datetime import datetime
from typing import Any

import httpx
from dotenv import load_dotenv

from ..types import Source

load_dotenv()
log = logging.getLogger("agents.sources.pubmed")


NCBI_API_KEY = os.getenv("NCBI_API_KEY", "").strip()
NCBI_TOOL = os.getenv("NCBI_TOOL", "northmedai").strip()
NCBI_EMAIL = os.getenv("NCBI_EMAIL", "").strip()  # рекомендуется по гайду NCBI

BASE_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"

# Параметры запроса
SEARCH_RETMAX = 5            # максимум кандидатов на ОДИН search query
PER_ADAPTER_LIMIT = 5        # максимум возвращаемых Source'ов в total
REQUEST_TIMEOUT = 12.0

# Rate limit: без ключа 3 req/sec, с ключом 10 req/sec
# Делаем по 3 одновременных запроса даже без ключа — внутри клиента httpx
# серверный rate limit редко достигается, у нас в среднем <3 RPS.
_SEMAPHORE = asyncio.Semaphore(3 if not NCBI_API_KEY else 10)


def _common_params() -> dict[str, str]:
    p = {"tool": NCBI_TOOL, "retmode": "json"}
    if NCBI_API_KEY:
        p["api_key"] = NCBI_API_KEY
    if NCBI_EMAIL:
        p["email"] = NCBI_EMAIL
    return p


# --- esearch -------------------------------------------------------------

async def _esearch(client: httpx.AsyncClient, query: str) -> list[str]:
    """Найти PMID'ы по поисковому запросу. Возвращает до SEARCH_RETMAX."""
    if not query.strip():
        return []
    params = {
        **_common_params(),
        "db": "pubmed",
        "term": query,
        "retmax": str(SEARCH_RETMAX),
        "sort": "relevance",
    }
    try:
        async with _SEMAPHORE:
            r = await client.get(f"{BASE_URL}/esearch.fcgi", params=params)
            r.raise_for_status()
            data = r.json()
    except httpx.HTTPError as e:
        log.warning("PubMed esearch failed for %r: %s", query[:80], e)
        return []
    except Exception:  # noqa: BLE001
        log.exception("PubMed esearch unexpected error for %r", query[:80])
        return []

    result = data.get("esearchresult", {})
    ids = result.get("idlist", [])
    if not isinstance(ids, list):
        return []
    return [str(x) for x in ids if x]


# --- esummary ------------------------------------------------------------

def _parse_pubdate_year(pubdate: str) -> int | None:
    """Парсим год из 'YYYY [Mon DD]' формата esummary."""
    if not pubdate:
        return None
    head = pubdate.strip().split()[0]
    try:
        y = int(head[:4])
        if 1900 <= y <= 2100:
            return y
    except ValueError:
        return None
    return None


def _freshness_modifier(year: int | None) -> float:
    """+0.05 если ≤5 лет, -0.10 если ≥15 лет, иначе 0. См. RAG_ARCHITECTURE.md §2."""
    if year is None:
        return 0.0
    current = datetime.utcnow().year
    age = current - year
    if age <= 5:
        return 0.05
    if age >= 15:
        return -0.10
    return 0.0


async def _esummary(
    client: httpx.AsyncClient, pmids: list[str],
) -> list[dict[str, Any]]:
    """Метаданные пачки статей. Возвращает список dict'ов с базовой инфой."""
    if not pmids:
        return []
    params = {
        **_common_params(),
        "db": "pubmed",
        "id": ",".join(pmids),
    }
    try:
        async with _SEMAPHORE:
            r = await client.get(f"{BASE_URL}/esummary.fcgi", params=params)
            r.raise_for_status()
            data = r.json()
    except httpx.HTTPError as e:
        log.warning("PubMed esummary failed: %s", e)
        return []
    except Exception:  # noqa: BLE001
        log.exception("PubMed esummary unexpected error")
        return []

    result = data.get("result", {})
    out: list[dict[str, Any]] = []
    for pmid in pmids:
        item = result.get(pmid)
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip() or "PubMed article"
        pubdate = str(item.get("pubdate") or "")
        year = _parse_pubdate_year(pubdate)
        source = str(item.get("source") or "").strip()  # журнал
        # Доп. snippet — журнал + год, если нет абстракта
        snippet_parts: list[str] = []
        if source:
            snippet_parts.append(source)
        if year:
            snippet_parts.append(str(year))
        snippet = " — ".join(snippet_parts) if snippet_parts else ""
        out.append({
            "pmid": pmid,
            "title": title[:200],
            "year": year,
            "snippet": snippet,
            "pubdate": pubdate,
        })
    return out


# --- Adapter -------------------------------------------------------------

class PubMedAdapter:
    """См. agents/sources/_base.py → SourceAdapter (Protocol)."""

    tier = "pubmed"
    base_weight = 0.90

    async def search(self, queries: list[str], lang: str = "en") -> list[Source]:
        if not queries:
            return []
        # Дедуп: одинаковые claim'ы дают одинаковые queries — не дёргаем NCBI дважды
        unique_queries = list(dict.fromkeys(q.strip() for q in queries if q and q.strip()))
        if not unique_queries:
            return []

        log.info("PubMed adapter: %d уникальных query (%r...)",
                 len(unique_queries), unique_queries[0][:60])

        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            # esearch на каждый запрос параллельно
            pmid_lists = await asyncio.gather(
                *(_esearch(client, q) for q in unique_queries),
                return_exceptions=True,
            )

            # Собираем PMID'ы с сохранением порядка релевантности
            all_pmids: list[str] = []
            seen: set[str] = set()
            for plist in pmid_lists:
                if isinstance(plist, BaseException):
                    continue
                for pmid in plist:
                    if pmid not in seen:
                        seen.add(pmid)
                        all_pmids.append(pmid)

            if not all_pmids:
                log.info("PubMed adapter: ни одного PMID не найдено")
                return []

            # Берём топ-N для esummary
            top_pmids = all_pmids[: PER_ADAPTER_LIMIT * 2]
            summaries = await _esummary(client, top_pmids)

        # Превращаем в Source'ы. relevance = позиция в результате (0 — самый верх).
        # Считаем score = base_weight + freshness — простое и читаемое правило.
        out: list[Source] = []
        for rank, item in enumerate(summaries):
            position_score = 1.0 - (rank / max(len(summaries), 1)) * 0.5  # 1.0..0.5
            fresh_mod = _freshness_modifier(item["year"])
            weight = max(0.0, min(1.0, self.base_weight + fresh_mod))
            score = round(weight * position_score, 3)

            pmid = item["pmid"]
            src: Source = {
                "title": item["title"],
                "url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
                "tier": "pubmed",
                "weight": weight,
                "relevance": round(position_score, 3),
                "score": score,
                "snippet": item.get("snippet", ""),
                "published_at": item.get("pubdate", ""),
                "language": "en",
                "mock": False,
            }
            out.append(src)

        # Сортируем по score (по убыванию) и режем до лимита
        out.sort(key=lambda s: s.get("score", 0.0), reverse=True)
        return out[:PER_ADAPTER_LIMIT]


# Экспортируемый экземпляр-синглтон для регистрации в ADAPTERS.
ADAPTER = PubMedAdapter()
