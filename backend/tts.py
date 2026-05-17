"""
tts.py — синтез речи через Yandex SpeechKit v1 (REST, синхронный).

Документация: https://yandex.cloud/docs/speechkit/tts/request

POST https://tts.api.cloud.yandex.net/speech/v1/tts:synthesize
  - Authorization: Api-Key <key>
  - body: form-encoded (text, lang, voice, format, folderId)
  - response: бинарный аудио-поток (mp3/oggopus в зависимости от format)

Используется content_script.js — в конце окна показа метки расширение
ставит паузу видео, дёргает /tts с explanation, играет mp3, потом play().
"""

from __future__ import annotations

import logging
import os
from typing import Literal

import httpx
from dotenv import load_dotenv

load_dotenv()
log = logging.getLogger("tts")

YANDEX_API_KEY = os.getenv("YANDEX_API_KEY", "").strip()
YANDEX_FOLDER_ID = os.getenv("YANDEX_FOLDER_ID", "").strip()
TTS_BASE = "https://tts.api.cloud.yandex.net/speech/v1/tts:synthesize"

# alena — нейтральный женский голос, хорошо звучит на медицинских пояснениях.
# Альтернативы: filipp, ermil, jane, omazh.
DEFAULT_VOICE = os.getenv("YANDEX_TTS_VOICE", "alena").strip()
DEFAULT_LANG = "ru-RU"
DEFAULT_FORMAT: Literal["mp3", "oggopus"] = "mp3"   # mp3 проще играть через <audio>

MAX_TEXT_CHARS = 1000        # SpeechKit лимит 5000, но обычно нам не нужно больше 500
REQUEST_TIMEOUT = 15.0


def is_configured() -> bool:
    return bool(YANDEX_API_KEY and YANDEX_FOLDER_ID)


async def synthesize(text: str, *, voice: str | None = None) -> bytes | None:
    """
    Синтезирует речь и возвращает бинарные данные mp3.
    Возвращает None при отсутствии credentials или ошибке API.
    """
    if not is_configured():
        log.warning("tts: нет YANDEX_API_KEY / YANDEX_FOLDER_ID — возвращаю None")
        return None
    text = (text or "").strip()
    if not text:
        return None
    if len(text) > MAX_TEXT_CHARS:
        text = text[:MAX_TEXT_CHARS]

    voice_name = (voice or DEFAULT_VOICE).strip() or DEFAULT_VOICE

    headers = {"Authorization": f"Api-Key {YANDEX_API_KEY}"}
    data = {
        "text": text,
        "lang": DEFAULT_LANG,
        "voice": voice_name,
        "format": DEFAULT_FORMAT,
        "folderId": YANDEX_FOLDER_ID,
    }

    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            r = await client.post(TTS_BASE, data=data, headers=headers)
            r.raise_for_status()
            audio = r.content
    except httpx.HTTPError as e:
        log.warning("tts: HTTP error: %s", e)
        return None
    except Exception:  # noqa: BLE001
        log.exception("tts: непредвиденная ошибка")
        return None

    log.info("tts: синтезировано %d байт (voice=%s, %d симв.)",
             len(audio), voice_name, len(text))
    return audio
