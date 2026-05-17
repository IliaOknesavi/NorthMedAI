# NorthMedAI

> AI-фактчекер медицинского контента для YouTube

Браузерное расширение, которое анализирует медицинские утверждения в YouTube-видео и показывает ненавязчивые метки прямо на плеере — со ссылками на PubMed, WHO, CDC, Минздрав. На паузе Алиса (Yandex SpeechKit) голосом зачитывает возражение со ссылкой на источник.

**Хакатон:** ИТМО × Яндекс Образование — Алиса AI / Yandex AI Studio

---

## Как это работает

1. Пользователь открывает YouTube-ролик → расширение запрашивает `/analyze` у бэкенда.
2. Бэкенд тянет транскрипт (`youtube-transcript-api`), прогоняет через **7-этапный pipeline LLM-агентов**.
3. Результат — список claim'ов с вердиктами `false / misleading / conflicting / unverifiable` и подобранными источниками.
4. Расширение рисует кружок-метку в углу плеера и тики на таймлайне; по клику открывает тултип с источниками. На паузе — голосовое возражение Алисы.

---

## Pipeline (7 LLM-агентов на Yandex AI Studio)

```
транскрипт
   │
   ▼
1. Extractor         — извлекает спорные claim'ы (YandexGPT 5.1)
2. Stance Detector   — незаметно отбрасывает claim'ы, которые автор разбирает самостоятельно
3. Query Former v0.2 — формирует поисковые запросы (+ скептические для verdict=false)
4. Retriever         — параллельно зовёт 6 source-адаптеров
5. Conflict Class.   — для каждого source: supports/contradicts/neutral
6. Judge v0.3        — финальный verdict с опорой на источники
7. Final QA v0.3     — целостный проход: drop false-positives, dedup, repair
   │
   ▼
{claims, pipeline_stats, versions} → DB → UI
```

Каждый агент логирует версию промпта в `analyses.{stance,retriever,judge,qa}_version` — можно сравнивать поколения на `/history`.

Подробная архитектура — `docs/RAG_ARCHITECTURE.md`. Все промпты — `docs/PROMPTS.md`.

---

## Архитектура

```
extension/                Chrome Extension (Manifest v3)
│  popup.html/.js        — UI: тумблеры, история, Pipeline-воронка
│  background.js         — service worker
│  content_script.js     — оверлей: метки, тики, тултип, иконка Алисы + эквалайзер
│  i18n.js               — ru/en интерфейс
│
backend/                  Python / FastAPI
│  main.py               — /analyze /reanalyze /video/{id} /history /tts
│  transcript.py         — YouTube транскрипт + hard socket timeouts
│  detector.py           — Extractor (YandexGPT 5.1)
│  tts.py                — Yandex SpeechKit (синтез голоса Алисы)
│  agents/               — мульти-агент pipeline
│  │  pipeline.py        — enrich(snippets, claims): главная точка входа
│  │  stance.py          — Stance Detector v0.3
│  │  query_former.py    — Query Former v0.2 (скептические запросы для false)
│  │  retriever.py       — оркестратор + ранжирование
│  │  conflict_classifier.py
│  │  judge.py           — Judge v0.3
│  │  final_qa.py        — Final QA v0.3 (локальные окна ±120s)
│  │  prompts.py         — все системные промпты + версии
│  │  sources/
│  │     pubmed.py       — NCBI E-utilities (weight 0.90)
│  │     who.py          — WHO + on-demand PDF (weight 0.85)
│  │     cdc_nejm.py     — CDC/NEJM/JAMA/BMJ/Lancet (weight 0.80)
│  │     minzdrav.py     — Минздрав + клинреки PDF (weight 0.70)
│  │     news_major.py   — Reuters/AP/BBC/ТАСС (weight 0.60)
│  │     corpus.py       — локальный pgvector RAG
│  │     _yandex_search.py  — Cloud Search API v2
│  │     _pdf.py            — async PDF fetch + pdfplumber
│  │     _embeddings.py     — Yandex Embeddings (256-dim)
│  │     _whitelist.py      — базовый класс для whitelist-адаптеров
│  models.py             — SQLAlchemy ORM
│  migrations.py         — in-place ALTER TABLE (M0001..M0005)
│  db.py                 — async SQLAlchemy
│  corpus_ingest.py      — CLI: PDF → chunks → embeddings → corpus_chunks
│
docs/                     Документация
```

---

## Стек

| Компонент | Технология |
|---|---|
| Расширение | JavaScript, Chrome Extensions Manifest v3 |
| Бэкенд | Python 3.13, FastAPI, async SQLAlchemy 2.0 |
| База | PostgreSQL 16 + pgvector |
| **LLM** | **YandexGPT 5.1 / lite** (Extractor, Stance, Judge, QA / Query Former, Conflict) |
| **Эмбеддинги** | **Yandex Embeddings** (text-search-doc/query, 256-dim) |
| **Поиск** | **Yandex Cloud Search API v2** (WHO/CDC/Минздрав whitelist) |
| **TTS** | **Yandex SpeechKit** (голос alena) |
| Научный поиск | NCBI E-utilities (PubMed) |
| Транскрипты | youtube-transcript-api |
| PDF | pdfplumber (on-demand парсинг клинреков и WHO guidelines) |

---

## Ключевые фичи

**Метки на спорных утверждениях.** Кружок в правом нижнем углу плеера: красный (false), жёлтый (misleading / unverifiable). На таймлайне — тики на нужных секундах.

**Тултип с источниками в один клик.** PubMed, WHO, Минздрав, CDC с заголовком статьи и кратким объяснением вердикта. Тултип адаптивно масштабируется через `ResizeObserver` + CSS `scale` — одинаково читается в маленьком плеере и в fullscreen.

**«Алиса возражает» голосом.** На паузе после метки расширение показывает иконку Алисы с эквалайзером и зачитывает explanation через `/tts` → SpeechKit. Особенно полезно пожилым зрителям.

**Stance Detector.** Если врач-блогер сам разбирает миф — расширение молчит. Один батч-вызов YandexGPT с целым транскриптом и списком claim'ов размечает каждое утверждение: `asserted / debunked_fully / debunked_partially / quoted_neutral`. `debunked_fully` и `quoted_neutral` дропаются молча.

**RAG из 6 источников.** Yandex Search находит PDF клинрекомендаций Минздрава / WHO guidelines — мы качаем и парсим первые страницы прямо во время `/analyze` (on-demand RAG). Локальный pgvector корпус подключается, если в нём что-то загружено через `corpus_ingest`.

**Pipeline-секция в попапе.** Воронка in→stance→judge→QA→final с цифрами и версии всех агентов — видно прогресс работы pipeline'а на конкретном видео.

---

## Запуск

### Бэкенд

```bash
# 1) Postgres с pgvector (для локального RAG)
cd ~/Documents/Claude/Projects/NortMedAI
docker compose up -d        # pgvector/pgvector:pg16

# 2) Backend
cd backend
python -m venv venv
source venv/bin/activate    # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env        # заполнить YANDEX_* ключи
uvicorn main:app --reload
# При старте: миграции M0001..M0005 в логах.
```

### Локальный RAG-корпус (опционально)

```bash
cd backend
python -m corpus_ingest path/to/who_vaccines_position.pdf \
    --tier who --url https://www.who.int/...
# Чанки и embeddings → corpus_chunks.
```

### Тесты

```bash
cd backend
TMPDIR=/tmp python -m pytest tests/ -v
```

### Расширение

1. `chrome://extensions/` → «Режим разработчика».
2. «Загрузить распакованное» → выбрать `extension/`.
3. В попапе расширения — включить «Озвучка ошибок», если хочешь голос Алисы.

---

## Переменные окружения

Скопируй `.env.example` в `.env` и заполни:

```
# Обязательные:
YANDEX_API_KEY=              # Yandex AI Studio (GPT, Embeddings, SpeechKit)
YANDEX_FOLDER_ID=            # ID каталога в Yandex Cloud
DATABASE_URL=postgresql+asyncpg://nmai:nmai@localhost:5432/nmai

# Опциональные (без них соответствующий источник вернёт []):
YANDEX_SEARCH_API_KEY=       # WHO/CDC/Минздрав/news адаптеры
NCBI_API_KEY=                # PubMed rate-limit ×3 (3 → 10 RPS)
NCBI_EMAIL=                  # рекомендация NCBI

# Опционально — кастомные модели (по умолчанию yandexgpt-5.1/latest):
YANDEX_GPT_MODEL=
YANDEX_STANCE_MODEL=
YANDEX_QA_MODEL=
YANDEX_JUDGE_MODEL=
YANDEX_QF_MODEL=             # default: yandexgpt-lite
YANDEX_CC_MODEL=             # default: yandexgpt-lite
```

---

## Источники данных и веса доверия

| Источник | Вес | Транспорт |
|---|---|---|
| PubMed / Cochrane | 0.90 | NCBI E-utilities |
| WHO (who.int + EMRO) | 0.85 | Yandex Search + on-demand PDF |
| CDC, NEJM, JAMA, BMJ, Lancet | 0.80 | Yandex Search whitelist |
| Минздрав РФ + Росздравнадзор | 0.70 | Yandex Search + on-demand PDF |
| Reuters, AP, BBC, ТАСС | 0.60 | Yandex Search (только при `use_news`) |
| Локальный pgvector корпус | — | Yandex Embeddings cosine |

> При конфликте источников система показывает конфликт, не выбирает сторону. Вердикт `unverifiable` при отсутствии источников ≠ `false` — мы честно говорим пользователю «нет подтверждений», а не дотягиваем до ложного утверждения о ложности.

---

## API

```
POST /analyze       { "video_id": "..." }         → анализ или кеш из БД
POST /reanalyze     { "video_id": "..." }         → принудительный пересчёт (новая версия)
GET  /video/{id}                                  → последний анализ
GET  /history/{id}                                → все версии анализов
POST /tts           { "text": "..." }             → audio/ogg (SpeechKit)
```

Ответ `/analyze`:

```json
{
  "video_id": "...",
  "claims": [
    {
      "text": "Соду пить от рака",
      "start": 142.0,
      "verdict": "unverifiable",
      "type": "claim",
      "explanation": "Нет подтверждений: ни одно из исследований PubMed... (PubMed, 2019)",
      "confidence": 0.42,
      "sources": [{"title": "...", "url": "...", "tier": "pubmed", "stance": "neutral"}]
    }
  ],
  "pipeline_stats": {
    "claims_before_stance": 5, "stance_dropped": 0,
    "claims_before_qa": 5, "qa_kept": 5, "qa_dropped": 0, "qa_repaired": 0
  },
  "versions": {
    "stance": "stance-v0.3",
    "retriever": "qf-v0.2;cc-v0.1",
    "judge": "judge-v0.3",
    "qa": "qa-v0.3"
  }
}
```

---

## Команда NorthBridge AI

Сокомандники: Аугушту Рикарду Мутомба, Ивасенко Илья Даниилович, Махфудх Ахмед Айнин, Чуйко Алексей Игоревич

---

*MVP для хакатона ИТМО × Яндекс Образование. Алиса как полноценный голосовой компонент продукта, а не чат-обёртка.*
