"""
agents/query_former.py — построение поисковых запросов из claim'ов через
YandexGPT-lite. Батч-вызов: один LLM-запрос → запросы для всех claim'ов.

См. docs/PROMPTS.md → Query Former и docs/RAG_ARCHITECTURE.md §4.2.

Failure mode: при любой ошибке возвращаем «наивный» fallback — текст
claim'а как запрос в pubmed. Retriever работает, просто менее точно.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from typing import Any

from dotenv import load_dotenv
from openai import AsyncOpenAI, OpenAIError

from .prompts import SYSTEM_PROMPT_QUERY_FORMER
from .types import ClaimQueries, QueriesPerSource, RawClaim

load_dotenv()
log = logging.getLogger("agents.query_former")

# --- Конфигурация --------------------------------------------------------

YANDEX_API_KEY = os.getenv("YANDEX_API_KEY", "").strip()
YANDEX_FOLDER_ID = os.getenv("YANDEX_FOLDER_ID", "").strip()
# Query Former — лёгкая задача, lite-модели достаточно. Если хочется
# побольше точности — YANDEX_QF_MODEL=yandexgpt-5.1/latest в .env.
YANDEX_QF_MODEL = os.getenv("YANDEX_QF_MODEL", "yandexgpt-lite/latest").strip()
YANDEX_BASE_URL = "https://ai.api.cloud.yandex.net/v1"

MAX_OUTPUT_TOKENS = 1500
TEMPERATURE = 0.0
REQUEST_TIMEOUT_S = 30.0

# Конкуренция: даже при батче нам иногда нужен fallback на single-claim
_SEMAPHORE = asyncio.Semaphore(5)

VALID_SOURCE_KEYS = {"pubmed", "who", "cdc_nejm", "minzdrav", "news"}


# --- Клиент --------------------------------------------------------------

_client: AsyncOpenAI | None = None


def _get_client() -> AsyncOpenAI:
    global _client
    if _client is not None:
        return _client
    if not YANDEX_API_KEY or not YANDEX_FOLDER_ID:
        raise RuntimeError("YANDEX_API_KEY / YANDEX_FOLDER_ID не заданы")
    _client = AsyncOpenAI(
        api_key=YANDEX_API_KEY,
        base_url=YANDEX_BASE_URL,
        project=YANDEX_FOLDER_ID,
        timeout=REQUEST_TIMEOUT_S,
    )
    return _client


def _model_uri() -> str:
    return f"gpt://{YANDEX_FOLDER_ID}/{YANDEX_QF_MODEL}"


# --- Парсинг -------------------------------------------------------------

_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)
_JSON_OBJ_RE = re.compile(r"\{[\s\S]*\}")


def _extract_json(raw: str) -> dict[str, Any]:
    raw = (raw or "").strip()
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    m = _JSON_FENCE_RE.search(raw)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    m = _JSON_OBJ_RE.search(raw)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass
    log.warning("query_former: не удалось распарсить JSON; raw=%r", raw[:300])
    return {}


def _fallback_queries(claim: RawClaim) -> ClaimQueries:
    """Наивный fallback: текст claim'а → pubmed запрос. Лучше чем пусто."""
    text = claim["text"][:200]
    return ClaimQueries(
        claim_text=claim["text"],
        topic=text[:40],
        queries={
            "pubmed": [text],
            "who": [],
            "cdc_nejm": [],
            "minzdrav": [],
            "news": [],
        },
        must_not_contain=[],
        use_news=False,
    )


def _sanitize_queries(raw_q: Any) -> QueriesPerSource:
    """Привести queries-объект к QueriesPerSource."""
    out: QueriesPerSource = {
        "pubmed": [], "who": [], "cdc_nejm": [], "minzdrav": [], "news": [],
    }
    if not isinstance(raw_q, dict):
        return out
    for k in VALID_SOURCE_KEYS:
        v = raw_q.get(k)
        if isinstance(v, list):
            cleaned = [str(s).strip()[:200] for s in v if isinstance(s, str) and s.strip()]
            out[k] = cleaned[:3]  # хард-кап 3 запроса на источник
    return out


def _normalize_one(item: Any, fallback_claim: RawClaim) -> ClaimQueries:
    if not isinstance(item, dict):
        return _fallback_queries(fallback_claim)

    topic = str(item.get("topic") or fallback_claim["text"][:40])[:60]
    queries = _sanitize_queries(item.get("queries"))

    must_not = item.get("must_not_contain") or []
    if not isinstance(must_not, list):
        must_not = []
    must_not = [str(x).strip()[:60] for x in must_not if isinstance(x, str)][:10]

    use_news = bool(item.get("use_news", False))

    # Если все списки пусты — используем fallback (хотя бы pubmed)
    if not any(queries[k] for k in VALID_SOURCE_KEYS):
        log.warning("query_former: пустые queries для claim=%r — fallback",
                    fallback_claim["text"][:60])
        return _fallback_queries(fallback_claim)

    return ClaimQueries(
        claim_text=fallback_claim["text"],
        topic=topic,
        queries=queries,
        must_not_contain=must_not,
        use_news=use_news,
    )


# --- Основная функция: батч на все claim'ы ----------------------------

async def make_queries_batch(claims: list[RawClaim]) -> list[ClaimQueries]:
    """
    Один LLM-вызов на всю пачку claim'ов. Если падает — у каждого fallback.
    """
    if not claims:
        return []

    try:
        client = _get_client()
    except RuntimeError as e:
        log.warning("query_former: %s — fallback на наивные запросы", e)
        return [_fallback_queries(c) for c in claims]

    numbered = "\n".join(
        f"[{i}] {c['text']}  (verdict={c['verdict']})"
        for i, c in enumerate(claims)
    )
    user_prompt = (
        "Утверждения для проверки:\n\n"
        f"{numbered}\n\n"
        "(Каждое утверждение помечено индексом и черновым вердиктом "
        "экстрактора. Сформируй запросы для всех. Верни строго JSON.)"
    )

    log.info("query_former: вызываю %s для %d claim'ов", _model_uri(), len(claims))

    async with _SEMAPHORE:
        try:
            response = await client.responses.create(
                model=_model_uri(),
                instructions=SYSTEM_PROMPT_QUERY_FORMER,
                input=user_prompt,
                temperature=TEMPERATURE,
                max_output_tokens=MAX_OUTPUT_TOKENS,
            )
        except OpenAIError:
            log.exception("query_former: YandexGPT упал — fallback")
            return [_fallback_queries(c) for c in claims]
        except Exception:  # noqa: BLE001
            log.exception("query_former: непредвиденная ошибка — fallback")
            return [_fallback_queries(c) for c in claims]

    raw = getattr(response, "output_text", None) or ""
    log.debug("query_former raw (%d симв.): %s", len(raw), raw[:1000])

    data = _extract_json(raw)
    raw_items = data.get("queries") if isinstance(data, dict) else None
    if not isinstance(raw_items, list):
        log.warning("query_former: нет поля 'queries' — fallback")
        return [_fallback_queries(c) for c in claims]

    # Индексация по claim_index — LLM может вернуть в другом порядке
    by_index: dict[int, ClaimQueries] = {}
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        try:
            idx = int(item.get("claim_index"))
        except (TypeError, ValueError):
            continue
        if 0 <= idx < len(claims):
            by_index[idx] = _normalize_one(item, claims[idx])

    result: list[ClaimQueries] = []
    for i, c in enumerate(claims):
        result.append(by_index.get(i) or _fallback_queries(c))

    log.info(
        "query_former: подготовлено запросов для %d claim'ов "
        "(pubmed=%d, who=%d, cdc=%d, minzdrav=%d, news=%d)",
        len(result),
        sum(len(q["queries"].get("pubmed", [])) for q in result),
        sum(len(q["queries"].get("who", [])) for q in result),
        sum(len(q["queries"].get("cdc_nejm", [])) for q in result),
        sum(len(q["queries"].get("minzdrav", [])) for q in result),
        sum(len(q["queries"].get("news", [])) for q in result),
    )
    return result


# --- Совместимость с pipeline (один claim → один ClaimQueries) ----------

async def make_queries(claim: RawClaim) -> ClaimQueries:
    """
    Совместимая обёртка для pipeline'а, который вызывает per-claim.
    На практике pipeline должен переключиться на make_queries_batch
    для эффективности, но интерфейс сохраняем для безопасной миграции.
    """
    out = await make_queries_batch([claim])
    return out[0] if out else _fallback_queries(claim)
