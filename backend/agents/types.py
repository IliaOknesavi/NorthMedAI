"""
agents/types.py — структурные контракты pipeline'а.

TypedDict выбран намеренно вместо dataclass: вся коммуникация в проекте
идёт через JSON (модель → JSON → dict, API → dict → JSON). TypedDict даёт
статическую проверку без сериализационного оверхэда.

Соответствие docs/RAG_ARCHITECTURE.md §8 «Контракты данных — сводно».
"""

from __future__ import annotations

from typing import Literal, TypedDict


# --- 0. Входной транскрипт (от transcript.py) ----------------------------

class Snippet(TypedDict):
    """Один сегмент субтитров с таймкодом."""

    text: str
    start: float        # секунды от начала видео
    duration: float


# --- 1. Шаг Extractor → RawClaim -----------------------------------------

# Verdict, который может выдать Extractor. После Stance и Judge варианты
# меняются: Stance ничего не добавляет, Judge может выдать "unverifiable".
ExtractorVerdict = Literal["false", "misleading", "conflicting"]
ClaimType = Literal["claim", "sophism"]


class RawClaim(TypedDict):
    """
    Claim в том виде, в котором его выдаёт Extractor (detector.py).

    sources здесь НЕТ — они появляются после Retriever. stance тоже
    появляется позже (Stance Detector).
    """

    text: str
    start: float
    verdict: ExtractorVerdict
    type: ClaimType
    explanation: str
    confidence: float       # 0..1 — мнение Extractor'а без источников


# --- 1.5. Шаг Stance Detector → StanceLabel ------------------------------

Stance = Literal[
    "asserted",            # автор утверждает это всерьёз (дефолт)
    "debunked_fully",      # автор сам полностью разоблачил миф → drop
    "debunked_partially",  # автор разобрал не полностью; missing — что упустил
    "quoted_neutral",      # автор цитирует чужое мнение
]


class StanceLabel(TypedDict):
    """Метка stance для одного claim'а от Stance Detector."""

    claim_index: int        # позиция в массиве RawClaim'ов на входе шага 1.5
    stance: Stance
    missing: str            # для debunked_partially — что упущено; иначе ""
    confidence: float       # 0..1


# --- 2. Шаг Query Former → ClaimQueries ---------------------------------

SourceTier = Literal[
    "pubmed",
    "who",
    "cdc_nejm",
    "minzdrav",
    "news_major",
    "unknown",
]


class QueriesPerSource(TypedDict, total=False):
    """Поисковые запросы по адаптерам. Все поля опциональны."""

    pubmed: list[str]
    who: list[str]
    cdc_nejm: list[str]
    minzdrav: list[str]
    news: list[str]


class ClaimQueries(TypedDict):
    """Запросы по источникам для одного claim'а."""

    claim_text: str
    topic: str              # 2-5 слов на en, для логов и кэширования
    queries: QueriesPerSource
    must_not_contain: list[str]
    use_news: bool


# --- 3. Шаг Retriever → Source / Evidence -------------------------------

class Source(TypedDict, total=False):
    """
    Один источник с релевантным сниппетом.

    На P0 stubs возвращают минимум полей. На P1+ заполняются все.
    """

    # Обязательные:
    title: str
    url: str
    tier: SourceTier
    weight: float           # 0..1 — априорная важность источника (по tier + свежесть)
    # Опциональные (появляются после реальной интеграции):
    relevance: float        # 0..1 — насколько ответ relevant к claim'у
    score: float            # = weight × relevance × freshness_factor
    snippet: str            # 1-3 предложения из источника для Judge
    published_at: str       # ISO8601
    language: str           # "ru" | "en" | ...
    mock: bool              # True для placeholder/stub — отличаем от настоящих


class Evidence(TypedDict):
    """Результат ретривала для одного claim'а."""

    claim_text: str
    sources: list[Source]   # отсортированы по итоговому скору (best-first)
    errors: list[str]       # какие адаптеры упали — для дебага
    retrieved_at: str       # ISO8601


# --- 4. Шаг Judge → FinalClaim ------------------------------------------

FinalVerdict = Literal["false", "misleading", "conflicting", "unverifiable"]
# На выходе Stance Detector debunked_fully сразу отфильтрован, до Judge
# доходит только эта тройка stance'ов.
FinalStance = Literal["asserted", "debunked_partially", "quoted_neutral"]


class FinalClaim(TypedDict):
    """
    Итоговый claim после всех агентов. Это то, что попадает в БД и
    (после фильтрации `unverifiable`) к пользователю в content_script.
    """

    text: str
    start: float
    verdict: FinalVerdict
    type: ClaimType
    explanation: str        # переписан Judge с учётом источников и stance
    confidence: float
    sources: list[Source]

    # Stance от шага 1.5 (debunked_fully сюда не доходит — отфильтрован).
    stance: FinalStance
    stance_missing: str     # для debunked_partially — что упустил автор

    # Аудит — что говорил Extractor до Judge.
    extractor_verdict: str
    judge_notes: str        # 1-2 фразы, почему Judge решил так
    # Запросы, которые Retriever использовал — для дебага.
    search_queries: QueriesPerSource | None
