# NorthMedAI

> AI-фактчекер медицинского контента для YouTube и статей

Браузерное расширение (Chromium / Яндекс Браузер), которое анализирует медицинский контент в видео и показывает ненавязчивые пометки о спорных или ложных утверждениях прямо поверх видеоплеера.

**Хакатон:** Алиса AI / Yandex AI Studio

---

## Как это работает

Пользователь вставляет URL видео → система скачивает транскрипт → YandexGPT анализирует каждый фрагмент → при воспроизведении оверлей синхронизируется по таймкодам.

**Два типа проверки:**
- **Фактические ошибки** — верификация утверждений против научных источников (PubMed, ВОЗ, CDC)
- **Логические ошибки / софизмы** — анализ структуры аргумента (ad hominem, appeal to nature, false dichotomy и др.)

---

## Архитектура

```
extension/          Chrome Extension (Manifest v3)
│  popup.js        — UI: поле для URL, кнопка «Проверить»
│  background.js   — Service worker: запрос к бэкенду
│  content_script.js — Оверлей поверх видеоплеера
│
backend/            Python / FastAPI
│  main.py         — /analyze, /reanalyze, /video/{id}, /history endpoints
│  transcript.py   — YouTube Transcript API → субтитры + таймкоды
│  detector.py     — Extractor: YandexGPT извлекает claim'ы из транскрипта
│  agents/         — мульти-агент pipeline (см. docs/RAG_ARCHITECTURE.md)
│  │  pipeline.py    — enrich(snippets, claims): главная точка входа
│  │  stance.py      — Stance Detector (drop claim'ов которые автор сам разбирает)
│  │  query_former.py— Query Former (формирует search-запросы под источники)
│  │  retriever.py   — оркестратор адаптеров + conflict classification
│  │  conflict_classifier.py — supports/contradicts/neutral для каждого source
│  │  judge.py       — финальный verdict с учётом источников
│  │  final_qa.py    — whole-video QA: drop / repair / dedup
│  │  prompts.py     — все системные промпты в одном месте
│  │  sources/       — адаптеры источников
│  │     pubmed.py        — NCBI E-utilities
│  │     who.py           — World Health Organization (Yandex Search + PDF)
│  │     cdc_nejm.py      — CDC + крупные журналы (NEJM/JAMA/BMJ/Lancet)
│  │     minzdrav.py      — Минздрав РФ + клинрекомендации (PDF)
│  │     news_major.py    — Reuters/AP/BBC/ТАСС (только когда use_news)
│  │     corpus.py        — локальный RAG через pgvector
│  │     _yandex_search.py  — общий клиент Yandex Search XML API
│  │     _pdf.py            — async PDF fetch + parse (pdfplumber)
│  │     _embeddings.py     — клиент Yandex Embeddings (text-search-doc/query)
│  │     _whitelist.py      — базовый класс для whitelist-адаптеров
│  models.py       — SQLAlchemy ORM
│  migrations.py   — in-place ALTER TABLE миграции
│  db.py           — async SQLAlchemy слой
│  corpus_ingest.py — CLI: загрузка PDF → chunks → embeddings → corpus_chunks
│
docs/               Документация (RAG_ARCHITECTURE.md, PROMPTS.md)
```

---

## Стек

| Компонент | Технология |
|---|---|
| Расширение | JavaScript, Chrome Extensions Manifest v3 |
| Бэкенд | Python, FastAPI |
| LLM | YandexGPT (через Yandex AI Studio) |
| Транскрипты | youtube-transcript-api |
| Научный поиск | NCBI E-utilities (PubMed) |
| Веб-поиск | Yandex Search API |
| RAG | LangChain + локальный векторный индекс |
| Хостинг | Railway / Yandex Cloud |

---

## Формат ответа API

```json
[
  {
    "timestamp": 142,
    "type": "fact",
    "claim": "Витамин C лечит простуду",
    "verdict": "misleading",
    "confidence": 0.82,
    "sources": [
      {"title": "Cochrane Review 2023", "url": "...", "trust": 0.9}
    ]
  },
  {
    "timestamp": 310,
    "type": "sophism",
    "subtype": "appeal_to_nature",
    "claim": "Природное значит безопасное",
    "explanation": "Апелляция к природе — логическая ошибка..."
  }
]
```

---

## Запуск

### Бэкенд

```bash
# 1) Postgres с pgvector (для локального RAG)
cd ~/Documents/Claude/Projects/NortMedAI
docker compose up -d        # поднимет pgvector/pgvector:pg16

# 2) Backend
cd backend
python -m venv venv
source venv/bin/activate    # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env        # заполнить YANDEX_* ключи
uvicorn main:app --reload
# При старте увидишь применённые миграции M0001..M0003 в логах.
```

### Локальный RAG-корпус (опционально)

```bash
# Скачать какой-нибудь WHO guideline (PDF) и загрузить в БД:
cd backend
python -m corpus_ingest path/to/who_vaccines_position.pdf \
    --tier who --url https://www.who.int/...
# Чанки и embeddings уйдут в corpus_chunks.
# С этого момента CorpusAdapter будет находить релевантные куски.
```

### Тесты

```bash
cd backend
TMPDIR=/tmp python -m pytest tests/ -v
```

### Расширение

1. Открыть `chrome://extensions/`
2. Включить «Режим разработчика»
3. «Загрузить распакованное» → выбрать папку `extension/`

---

## Переменные окружения

Скопируй `.env.example` в `.env` и заполни:

```
# Обязательные:
YANDEX_API_KEY=              # Yandex AI Studio (для GPT, Embeddings)
YANDEX_FOLDER_ID=            # ID каталога в Yandex Cloud
DATABASE_URL=postgresql+asyncpg://nmai:nmai@localhost:5432/nmai

# Опциональные (без них соответствующий источник вернёт []):
YANDEX_SEARCH_API_KEY=       # для WHO/CDC/Minzdrav/News адаптеров
NCBI_API_KEY=                # NCBI E-utilities, для повышения rate-limit'а
NCBI_EMAIL=                  # рекомендация NCBI, не обязательно

# Опционально — кастомные модели (по умолчанию yandexgpt-5.1/latest):
YANDEX_GPT_MODEL=
YANDEX_STANCE_MODEL=
YANDEX_QA_MODEL=
YANDEX_JUDGE_MODEL=
YANDEX_QF_MODEL=
YANDEX_CC_MODEL=
```

---

## Источники данных и веса доверия

| Источник | Вес |
|---|---|
| PubMed / Cochrane | 0.9 |
| WHO (who.int) | 0.85 |
| CDC, NEJM | 0.8 |
| Минздрав РФ | 0.7 |
| Крупные новостные агентства | 0.6 |

> При конфликте источников система показывает конфликт, не выбирает сторону. Вердикт `unverifiable` при отсутствии источников ≠ `false`.

---

## Команда

<!-- Добавьте себя -->

---

*MVP для хакатона Алиса AI / Yandex AI Studio*
