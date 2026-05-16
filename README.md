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
│  main.py         — /analyze endpoint
│  transcript.py   — YouTube Transcript API → субтитры + таймкоды
│  detector.py     — Claim + Sophism detector (YandexGPT)
│  retriever.py    — Поиск по источникам (PubMed, Yandex Search)
│  rag/            — Локальный векторный индекс (PDF ВОЗ, клинрекомендации)
│
docs/               Документация
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
cd backend
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env      # добавить API-ключи
uvicorn main:app --reload
```

### Расширение

1. Открыть `chrome://extensions/`
2. Включить «Режим разработчика»
3. «Загрузить распакованное» → выбрать папку `extension/`

---

## Переменные окружения

Скопируй `.env.example` в `.env` и заполни:

```
YANDEX_API_KEY=       # Yandex AI Studio
YANDEX_FOLDER_ID=     # ID каталога в Yandex Cloud
NCBI_API_KEY=         # NCBI E-utilities (PubMed)
YANDEX_SEARCH_API_KEY=
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
