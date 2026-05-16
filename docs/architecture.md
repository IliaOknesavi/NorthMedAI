# Архитектура NorthMedAI

См. README.md для общего описания.

## Схема потока данных

```
extension/background.js
  → fetchTranscript() из transcript.js
      → POST youtube.com/youtubei/v1/player  (InnerTube API, IP пользователя)
      → GET субтитры в формате json3
  → POST backend/analyze  { video_id, transcript: [{text, start, duration}] }
      → detector.py  (YandexGPT: claim detection + sophism)
      → retriever.py (PubMed, Yandex Search)
      → JSON с таймкодами
  → content_script.js → оверлей поверх видеоплеера
```

## Решение по транскрипции

**Транскрипт получаем на стороне расширения, не сервера.**

Причина: YouTube банит IP облачных провайдеров (Railway, Yandex Cloud, AWS и т.д.)
при запросах к субтитрам. С IP пользователя блокировок нет.

Реализация: `extension/transcript.js` — прямой запрос к InnerTube API YouTube,
без сторонних библиотек, только нативный `fetch`.

### Поведение при отсутствии субтитров

YouTube не всегда генерирует авто-субтитры (музыкальный фон, короткие видео и т.д.).

**MVP (хакатон):** если субтитры недоступны — показываем сообщение в попапе,
анализ не запускаем.

**V2 (постхакатон):** кнопка «Попробовать через SpeechKit» — скачиваем аудио
через yt-dlp на бэкенде и прогоняем через Yandex SpeechKit STT.

### Рекомендуемые каналы для демо (субтитры гарантированы)

Выбирать видео у крупных научно-медицинских каналов с ручными субтитрами:
Kurzgesagt, SciShow, Doctor Mike, Khan Academy Medicine — EN;
для RU — искать каналы с включённой функцией субтитров YouTube.

## Формат JSON-ответа

См. README.md → «Формат ответа API»

## Открытые вопросы

- Финальное название (Faktus / Scrutin / MedLens / Claimen)
- Что показывать при нескольких одновременных пометках
- Язык интерфейса: только русский или билингвально
- Узкая ниша для демо: онкология / вакцины / питание / БАДы
- Нужен ли логин для хакатон-версии
