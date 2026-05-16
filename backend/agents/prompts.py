"""
agents/prompts.py — версии и тексты системных промптов агентов.

Источник правды по содержимому — docs/PROMPTS.md. Этот модуль импортирует
константы и пишет их версии в Analysis.{stance,retriever,judge}_version
при сохранении.

P0: тексты — заглушки (агенты ещё не зовут LLM). P1 заполнит реальными
промптами из docs/PROMPTS.md.
"""

from __future__ import annotations

# --- Версии (бампать при ЛЮБОМ изменении соответствующего промпта) -----
# Формат: <agent>-v<MAJOR>.<MINOR>. Major — несовместимый формат I/O,
# minor — правки текста промпта.
STANCE_VERSION = "stance-v0.0-stub"
QUERY_FORMER_VERSION = "qf-v0.0-stub"
CONFLICT_CLASSIFIER_VERSION = "cc-v0.0-stub"
JUDGE_VERSION = "judge-v0.0-stub"

# Композитная версия Retriever: query_former + conflict_classifier.
# Сама retrieval-логика (адаптеры источников) версионируется в коде
# через _ADAPTERS_VERSION в retriever.py, см. ниже в pipeline.
RETRIEVER_VERSION = f"{QUERY_FORMER_VERSION};{CONFLICT_CLASSIFIER_VERSION}"

# --- Тексты промптов (P0 — пустые, реальные тексты приедут в P1) -------
# Когда дойдём до P1: копировать из docs/PROMPTS.md без изменений
# (док — источник правды). При редактировании промптов править ОБА места.

SYSTEM_PROMPT_STANCE = ""
SYSTEM_PROMPT_QUERY_FORMER = ""
SYSTEM_PROMPT_JUDGE = ""
SYSTEM_PROMPT_CONFLICT_CLASSIFIER = ""
