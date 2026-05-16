"""
detector.py — анализ транскрипта YouTube через YandexGPT.

Извлекает медицинские утверждения и софизмы, классифицирует их:
- verdict: "false" | "misleading" | "conflicting" | "true"
- type:    "claim" | "sophism"

Возвращает claims привязанные к таймкоду (start в секундах) из исходного
транскрипта, чтобы content_script.js мог поставить метки на плеере YouTube.

API: используется OpenAI-совместимый эндпоинт Yandex AI Studio.
Доступ: ключ из .env (YANDEX_API_KEY + YANDEX_FOLDER_ID).
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

from log_setup import setup_logging

load_dotenv()
setup_logging()

# Имя совпадает с тем, на которое навешан отдельный файл detector.log
log = logging.getLogger("detector")

# --- Конфигурация ---------------------------------------------------------

YANDEX_API_KEY = os.getenv("YANDEX_API_KEY", "").strip()
YANDEX_FOLDER_ID = os.getenv("YANDEX_FOLDER_ID", "").strip()
YANDEX_GPT_MODEL = os.getenv("YANDEX_GPT_MODEL", "yandexgpt-5.1/latest").strip()
YANDEX_BASE_URL = "https://ai.api.cloud.yandex.net/v1"

# Жёсткие лимиты, чтобы не упереться в контекст модели и не разоряться на токенах
CHUNK_CHAR_LIMIT = 3500          # ~ 1.5–2 минуты речи
MAX_OUTPUT_TOKENS = 1500
TEMPERATURE = 0.1                 # детектор должен быть максимально стабильным
REQUEST_TIMEOUT_S = 60.0

VALID_VERDICTS = {"false", "misleading", "conflicting", "true"}
VALID_TYPES = {"claim", "sophism"}


SYSTEM_PROMPT = """Ты — медицинский фактчекер на YouTube. На входе — фрагмент транскрипта
с таймкодами. Твоя задача — выписать только те утверждения, которые
ДЕЙСТВИТЕЛЬНО МОГУТ ПОВРЕДИТЬ ЗДОРОВЬЮ зрителя, если в них поверить.

ЧТО ВЫПИСЫВАТЬ:
- Конкретные ошибочные утверждения о здоровье, лечении, питании, лекарствах,
  процедурах, диагнозах, профилактике.
- Софизмы и логические уловки в медицинском контексте (апелляция к авторитету,
  «натуральное = безопасное», ложная дихотомия, постхок, страх и т.п.).

ЧТО НЕ ВЫПИСЫВАТЬ — ВАЖНО:
- Верные общеизвестные медицинские факты, даже если они упомянуты вскользь
  («менингит передаётся воздушно-капельным путём», «витамин С нужен организму»,
  «грипп вызывается вирусом»). Если утверждение СООТВЕТСТВУЕТ доказательной
  медицине — НЕ включай его в ответ. Список должен содержать только то,
  на что зрителю стоит обратить внимание.
- Шутки, лирические отступления, мнения о вкусе и т.п.
- Если в транскрипте нет ничего проверяемого — верни пустой массив claims.

ЗНАЧЕНИЯ verdict — выбирай по реальному соответствию доказательной медицине,
а НЕ по эмоциональной окраске фразы:
- "false"       — противоречит научному консенсусу, потенциально вредно
                  (пример: «рак лечится содой», «прививки вызывают аутизм»).
- "misleading"  — формально слова не ложь, но подача создаёт ложное впечатление,
                  опускает критичные оговорки или преувеличивает эффект
                  (пример: «БАД X укрепляет иммунитет» — реальный эффект не доказан).
- "conflicting" — научный консенсус неоднозначен или активно дискутируется
                  (пример: пороги употребления алкоголя, дозировки витамина D).
- "true"        — соответствует доказательной медицине. Используй ТОЛЬКО если
                  фраза действительно требует подтверждения для зрителя
                  (например, разоблачает популярный миф). Обычные верные
                  фразы вообще не включай в список.

ЗНАЧЕНИЯ type:
- "claim"       — фактическое утверждение.
- "sophism"     — логическая уловка/манипуляция, безотносительно того, верен
                  ли вывод.

ПРАВИЛА ВЫВОДА:
- `start` — время в секундах из ближайшего таймкода в квадратных скобках.
- `text` — короткая цитата или близкий пересказ, до 200 символов, по-русски.
- `explanation` — 1–2 предложения, объясняющие именно verdict
  (НЕ пересказывай само утверждение, а объясни, почему оно false/misleading/...).
- `confidence` — твоя уверенность 0.0..1.0.
- Если verdict="true", explanation должен начинаться с «Верно:».
- Если verdict="false", explanation должен начинаться с «Неверно:».
- Если verdict="misleading", explanation должен начинаться с «Вводит в заблуждение:».
- Если verdict="conflicting", explanation должен начинаться с «Неоднозначно:».

ФОРМАТ ОТВЕТА — строго валидный JSON, без markdown-обёртки, без комментариев,
verdict и type ВСЕГДА в кавычках как строки:
{
  "claims": [
    {
      "text": "string",
      "start": 12.34,
      "verdict": "false",
      "type": "claim",
      "explanation": "string",
      "confidence": 0.0
    }
  ]
}

ПРИМЕРЫ:

Транскрипт:
[12.00] Менингит передаётся воздушно-капельным путём.
[18.50] Поэтому от него спасает только серебряная вода.

Ответ:
{"claims":[{"text":"От менингита спасает только серебряная вода","start":18.50,"verdict":"false","type":"claim","explanation":"Неверно: серебряная вода не имеет доказанной эффективности против менингита; реальная профилактика — вакцинация и гигиена.","confidence":0.95}]}
(Первая фраза верная и не вводит в заблуждение — её НЕ выписываем.)

Транскрипт:
[03.20] Витамин С нужен организму.
[07.10] Один только витамин С не сможет защитить от высокого давления.

Ответ:
{"claims":[]}
(Оба утверждения верные и не требуют разоблачения.)

Транскрипт:
[55.00] Все врачи скрывают, что простуду лечит чеснок за день.

Ответ:
{"claims":[{"text":"Все врачи скрывают, что простуду лечит чеснок за день","start":55.00,"verdict":"false","type":"sophism","explanation":"Неверно: теория заговора + ложное утверждение о лечении. Простуда проходит сама за 5–7 дней, чеснок этот срок не сокращает.","confidence":0.9}]}"""


# --- Клиент ---------------------------------------------------------------

_client: AsyncOpenAI | None = None


def _get_client() -> AsyncOpenAI:
    """Ленивая инициализация клиента — чтобы не падать при импорте без .env."""
    global _client
    if _client is not None:
        return _client

    if not YANDEX_API_KEY or not YANDEX_FOLDER_ID:
        raise RuntimeError(
            "YANDEX_API_KEY / YANDEX_FOLDER_ID не заданы. "
            "Создай backend/.env по образцу .env.example."
        )

    _client = AsyncOpenAI(
        api_key=YANDEX_API_KEY,
        base_url=YANDEX_BASE_URL,
        project=YANDEX_FOLDER_ID,
        timeout=REQUEST_TIMEOUT_S,
    )
    return _client


def _model_uri() -> str:
    return f"gpt://{YANDEX_FOLDER_ID}/{YANDEX_GPT_MODEL}"


# --- Чанкование ----------------------------------------------------------

def _chunk_snippets(snippets: list[dict], char_limit: int = CHUNK_CHAR_LIMIT) -> list[list[dict]]:
    """
    Бьёт сниппеты на пакеты с сохранением порядка и таймкодов.
    Каждый пакет — не больше char_limit символов суммарно.
    """
    chunks: list[list[dict]] = []
    current: list[dict] = []
    current_len = 0

    for s in snippets:
        text_len = len(s.get("text", ""))
        if current and current_len + text_len > char_limit:
            chunks.append(current)
            current = []
            current_len = 0
        current.append(s)
        current_len += text_len + 1

    if current:
        chunks.append(current)
    return chunks


def _format_chunk_for_prompt(chunk: list[dict]) -> str:
    """Готовит человекочитаемый кусок транскрипта с таймкодами для модели."""
    lines = []
    for s in chunk:
        start = s.get("start", 0.0)
        text = s.get("text", "").replace("\n", " ").strip()
        if text:
            lines.append(f"[{start:.2f}] {text}")
    return "\n".join(lines)


# --- Парсинг ответа ------------------------------------------------------

_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)
_JSON_OBJ_RE = re.compile(r"\{[\s\S]*\}")


def _extract_json(raw: str) -> dict:
    """
    Достаём JSON из ответа модели. YandexGPT иногда оборачивает в ```json```
    или добавляет пояснение до/после. Пробуем по убыванию строгости.
    """
    raw = (raw or "").strip()
    if not raw:
        return {"claims": []}

    # 1) чистый JSON
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    # 2) ```json ... ```
    m = _JSON_FENCE_RE.search(raw)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass

    # 3) первая {...} группа
    m = _JSON_OBJ_RE.search(raw)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass

    log.warning("не удалось распарсить ответ модели; raw=%r", raw[:500])
    return {"claims": []}


# Синонимы, которые модель иногда возвращает вместо канонических значений.
# В новом промпте мы просим строго один из четырёх токенов, но реальность
# богаче: бывают англоязычные синонимы и русские варианты.
_VERDICT_ALIASES = {
    "false": "false", "wrong": "false", "incorrect": "false",
    "ложь": "false", "ложное": "false", "ложно": "false", "неверно": "false",
    "неверное": "false", "ошибочно": "false", "ошибочное": "false",

    "misleading": "misleading", "deceptive": "misleading",
    "вводит в заблуждение": "misleading", "вводящее в заблуждение": "misleading",
    "спорно": "misleading", "спорное": "misleading", "сомнительно": "misleading",

    "conflicting": "conflicting", "controversial": "conflicting", "disputed": "conflicting",
    "неоднозначно": "conflicting", "неоднозначное": "conflicting",
    "противоречиво": "conflicting", "противоречивое": "conflicting",

    "true": "true", "correct": "true", "accurate": "true", "verified": "true",
    "верно": "true", "верное": "true", "правда": "true", "достоверно": "true",
}


def _normalize_verdict(raw: object) -> str | None:
    """
    Приводит verdict к одному из VALID_VERDICTS. Возвращает None если не понял.
    Корректно обрабатывает случай, когда json.loads вернул булевое True/False
    (модель прислала `"verdict": true` без кавычек).
    """
    # json вернул булево
    if raw is True:
        return "true"
    if raw is False:
        return "false"
    if raw is None:
        return None

    s = str(raw).strip().lower().strip(".,;:!?'\"")
    if not s:
        return None
    if s in VALID_VERDICTS:
        return s
    return _VERDICT_ALIASES.get(s)


def _placeholder_sources(text: str, verdict: str, ctype: str) -> list[dict]:
    """
    Временные источники, пока retriever.py не написан.

    Это НЕ выдуманные URL — мы формируем поисковые запросы в PubMed и WHO
    с текстом claim'а, чтобы пользователь, кликнув, попал на реальную
    страницу с результатами. Когда появится настоящий retriever, эти
    заглушки заменятся конкретными статьями.

    Логика подбора по verdict (черновой mapping иерархии доверия):
      - false / misleading / sophism — медицина: PubMed + WHO;
      - conflicting                   — медицина: PubMed + Cochrane.

    Все ссылки помечаются mock=True, чтобы фронт мог их при желании отрисовать
    иначе и чтобы мы потом могли отличить «placeholder» от «настоящих».
    """
    from urllib.parse import quote_plus

    q = quote_plus(text[:120])

    pubmed = {
        "title": "PubMed",
        "url": f"https://pubmed.ncbi.nlm.nih.gov/?term={q}",
        "tier": "pubmed",
        "weight": 0.9,
        "mock": True,
    }
    who = {
        "title": "WHO",
        "url": f"https://www.who.int/home/search?indexCatalogue=genericsearchindex1&searchQuery={q}",
        "tier": "who",
        "weight": 0.85,
        "mock": True,
    }
    cochrane = {
        "title": "Cochrane",
        "url": f"https://www.cochranelibrary.com/search?searchBy=1&searchText={q}",
        "tier": "pubmed",
        "weight": 0.9,
        "mock": True,
    }

    if verdict == "conflicting":
        return [pubmed, cochrane]
    return [pubmed, who]


def _validate_claim(c: dict) -> dict | None:
    """
    Возвращает нормализованный claim или None если невалидный.
    Невалидные verdict'ы НЕ заглушаем в misleading — отбрасываем целиком,
    иначе верные утверждения превращаются в «спорные».
    """
    if not isinstance(c, dict):
        return None
    text = (c.get("text") or "").strip()
    if not text:
        return None

    try:
        start = float(c.get("start", 0.0))
    except (TypeError, ValueError):
        start = 0.0

    verdict = _normalize_verdict(c.get("verdict"))
    if verdict is None:
        log.warning("отбрасываю claim: непонятный verdict=%r text=%r", c.get("verdict"), text[:80])
        return None

    # По новому промпту true в выводе быть не должно. Если модель всё-таки
    # выдала true (вместо того чтобы просто не выписывать утверждение) —
    # не показываем такие claim'ы в оверлее, чтобы не пугать пользователя
    # «спорным» при верной фразе.
    if verdict == "true":
        log.info("пропускаю verdict=true (по политике в оверлей не показываем): %r", text[:80])
        return None

    ctype = (c.get("type") or "claim").strip().lower()
    if ctype not in VALID_TYPES:
        ctype = "claim"

    try:
        confidence = float(c.get("confidence", 0.5))
    except (TypeError, ValueError):
        confidence = 0.5
    confidence = max(0.0, min(1.0, confidence))

    explanation = (c.get("explanation") or "").strip()

    # Если модель сама приложила источники — берём их (после нормализации).
    # Иначе подставляем placeholder, чтобы UI не был пустым до retriever.py.
    raw_sources = c.get("sources")
    sources: list[dict] = []
    if isinstance(raw_sources, list):
        for s in raw_sources:
            if isinstance(s, dict) and s.get("url"):
                sources.append({
                    "title": str(s.get("title") or "source")[:120],
                    "url": str(s["url"])[:500],
                    "tier": str(s.get("tier") or "unknown")[:32],
                    "weight": float(s.get("weight") or 0.5),
                    "mock": bool(s.get("mock", False)),
                })
    if not sources:
        sources = _placeholder_sources(text, verdict, ctype)

    return {
        "text": text[:500],
        "start": round(start, 2),
        "verdict": verdict,
        "type": ctype,
        "explanation": explanation[:1000],
        "confidence": round(confidence, 2),
        "sources": sources,
    }


# --- Основная функция ----------------------------------------------------

async def _analyze_chunk(chunk: list[dict], chunk_idx: int = 0) -> list[dict]:
    """Один поход в YandexGPT по одному чанку транскрипта."""
    client = _get_client()
    transcript_block = _format_chunk_for_prompt(chunk)

    user_prompt = (
        "Транскрипт видео (в квадратных скобках — время начала фрагмента в секундах):\n\n"
        f"{transcript_block}\n\n"
        "Верни строго JSON по описанному формату."
    )

    t_start = chunk[0].get("start", 0.0) if chunk else 0.0
    t_end = chunk[-1].get("start", 0.0) if chunk else 0.0
    log.debug(
        "chunk #%d -> YandexGPT: %d сниппетов, %.1fs..%.1fs, prompt=%d симв.",
        chunk_idx, len(chunk), t_start, t_end, len(user_prompt),
    )

    try:
        response = await client.responses.create(
            model=_model_uri(),
            instructions=SYSTEM_PROMPT,
            input=user_prompt,
            temperature=TEMPERATURE,
            max_output_tokens=MAX_OUTPUT_TOKENS,
        )
    except OpenAIError as e:
        log.error("chunk #%d: ошибка YandexGPT: %s", chunk_idx, e)
        return []

    raw = getattr(response, "output_text", None) or ""
    log.debug("chunk #%d raw response (%d симв.): %s", chunk_idx, len(raw), raw)

    data = _extract_json(raw)
    raw_claims = data.get("claims") or []
    log.debug("chunk #%d: распарсено сырых claims=%d", chunk_idx, len(raw_claims))

    cleaned: list[dict] = []
    dropped = 0
    for c in raw_claims:
        v = _validate_claim(c)
        if v is None:
            dropped += 1
            log.debug("chunk #%d: claim отбракован: %r", chunk_idx, c)
            continue
        cleaned.append(v)
        log.info(
            "chunk #%d claim: [%.1fs] %s/%s conf=%.2f text=%s",
            chunk_idx, v["start"], v["verdict"], v["type"],
            v["confidence"], v["text"][:120],
        )

    log.info(
        "chunk #%d итог: валидных claims=%d, отброшено=%d",
        chunk_idx, len(cleaned), dropped,
    )
    return cleaned


async def analyze_claims(snippets: list[dict]) -> list[dict]:
    """
    Главная точка входа из main.py.

    Аргументы:
        snippets: [{"text": str, "start": float, "duration": float}, ...]

    Возвращает:
        список claims в формате, ожидаемом content_script.js:
        [{"text", "start", "verdict", "type", "explanation", "confidence", "sources"}]
    """
    if not snippets:
        return []

    chunks = _chunk_snippets(snippets)
    log.info(
        "старт анализа: сниппетов=%d, чанков=%d, модель=%s",
        len(snippets), len(chunks), _model_uri(),
    )

    # Запускаем параллельно, но без фанатизма — YandexGPT любит rate-limit
    sem = asyncio.Semaphore(3)

    async def run(idx: int, chunk: list[dict]) -> list[dict]:
        async with sem:
            return await _analyze_chunk(chunk, chunk_idx=idx)

    results = await asyncio.gather(
        *(run(i, c) for i, c in enumerate(chunks)),
        return_exceptions=True,
    )

    all_claims: list[dict] = []
    failed = 0
    for i, r in enumerate(results):
        if isinstance(r, Exception):
            failed += 1
            log.warning("чанк #%d упал с ошибкой: %s", i, r)
            continue
        all_claims.extend(r)

    # Сортируем по таймкоду, чтобы оверлей рисовал слева направо
    all_claims.sort(key=lambda c: c["start"])
    log.info(
        "анализ завершён: claims=%d (false=%d, misleading=%d, conflicting=%d, true=%d), упало чанков=%d",
        len(all_claims),
        sum(1 for c in all_claims if c["verdict"] == "false"),
        sum(1 for c in all_claims if c["verdict"] == "misleading"),
        sum(1 for c in all_claims if c["verdict"] == "conflicting"),
        sum(1 for c in all_claims if c["verdict"] == "true"),
        failed,
    )
    return all_claims


# --- CLI для отладки -----------------------------------------------------

if __name__ == "__main__":
    import sys
    from transcript import get_transcript

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    video_id = sys.argv[1] if len(sys.argv) > 1 else "kqtD5dpn9C8"

    async def _main() -> None:
        print(f"Получаю субтитры для: {video_id}")
        snippets = get_transcript(video_id)
        print(f"Сниппетов: {len(snippets)}")

        print("Анализирую через YandexGPT...")
        claims = await analyze_claims(snippets)
        print(f"Найдено утверждений: {len(claims)}\n")
        for c in claims:
            print(
                f"[{c['start']:.1f}s] ({c['verdict']}/{c['type']}, "
                f"conf={c['confidence']}) {c['text']}"
            )
            if c["explanation"]:
                print(f"   -> {c['explanation']}")

    asyncio.run(_main())
