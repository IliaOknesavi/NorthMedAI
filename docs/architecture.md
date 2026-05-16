# Архитектура NorthMedAI

См. README.md для общего описания.

## Схема потока данных

URL → background.js → POST /analyze → transcript.py → detector.py → retriever.py → JSON → content_script.js → оверлей

## Формат JSON-ответа

См. README.md → «Формат ответа API»

## Открытые вопросы

- Финальное название (Faktus / Scrutin / MedLens / Claimen)
- Что показывать при нескольких одновременных пометках
- Язык интерфейса: только русский или билингвально
- Узкая ниша для демо: онкология / вакцины / питание / БАДы
- Нужен ли логин для хакатон-версии
