"""
transcript.py — получение субтитров с YouTube через youtube-transcript-api.
Запускается локально (не в облаке), но YouTube всё равно периодически
сбрасывает TCP-соединение под нагрузкой/по rate-limit'у — поэтому встроены
ретраи с экспоненциальным бэкоффом и кастомный User-Agent.
"""

from __future__ import annotations

import logging
import random
import re
import time

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from youtube_transcript_api import (
    YouTubeTranscriptApi,
    NoTranscriptFound,
    TranscriptsDisabled,
)

log = logging.getLogger("transcript")

# Имитируем нормальный десктопный браузер — дефолтный UA библиотеки иногда
# триггерит сетевой блок у YouTube.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# Сколько раз перепробуем при сетевых сбоях (Connection reset, таймауты, 5xx).
MAX_ATTEMPTS = 4
# База бэкоффа в секундах; на k-ой попытке ждём 2^k * BASE + джиттер.
BACKOFF_BASE_S = 0.8


def _build_session() -> requests.Session:
    """requests.Session с ретраями на уровне urllib3 и нормальным UA."""
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Accept-Language": "ru,en;q=0.9",
        }
    )
    retry = Retry(
        total=3,
        connect=3,
        read=3,
        status=3,
        backoff_factor=0.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET", "POST"]),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=4, pool_maxsize=8)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


def extract_video_id(url_or_id: str) -> str:
    """Извлекает video_id из URL или возвращает как есть если уже ID."""
    patterns = [
        r"(?:v=|youtu\.be/|embed/|shorts/)([A-Za-z0-9_-]{11})",
    ]
    for pattern in patterns:
        match = re.search(pattern, url_or_id)
        if match:
            return match.group(1)
    if re.match(r"^[A-Za-z0-9_-]{11}$", url_or_id):
        return url_or_id
    raise ValueError(f"Не удалось извлечь video_id из: {url_or_id}")


# Классы исключений, которые имеет смысл ретраить.
# requests.ConnectionError ловит и ConnectionResetError (errno 54), и ProtocolError.
_RETRYABLE_EXC = (
    requests.exceptions.ConnectionError,
    requests.exceptions.ChunkedEncodingError,
    requests.exceptions.Timeout,
    ConnectionResetError,
)


def _new_ytt() -> YouTubeTranscriptApi:
    """
    Создаёт клиент с нашей requests.Session если эта версия библиотеки
    такое поддерживает (>=1.0), иначе — дефолтный конструктор.
    """
    session = _build_session()
    try:
        return YouTubeTranscriptApi(http_client=session)
    except TypeError:
        # Старая версия библиотеки — http_client не принимается.
        return YouTubeTranscriptApi()


def _fetch_with_retries(video_id: str, preferred_langs: list[str]):
    """Один вызов fetch() с ретраями по сетевым сбоям."""
    last_exc: Exception | None = None

    for attempt in range(1, MAX_ATTEMPTS + 1):
        ytt = _new_ytt()
        try:
            log.info(
                "fetch transcript: video_id=%s lang=%s попытка %d/%d",
                video_id, preferred_langs, attempt, MAX_ATTEMPTS,
            )
            return ytt.fetch(video_id, languages=preferred_langs)
        except NoTranscriptFound:
            # Это не сетевой сбой — отдаём наверх, пусть вызывающий
            # выберет любой доступный трек.
            raise
        except TranscriptsDisabled:
            # Тоже не ретраим — у видео реально отключены субтитры.
            raise
        except _RETRYABLE_EXC as e:
            last_exc = e
            if attempt == MAX_ATTEMPTS:
                break
            sleep_s = BACKOFF_BASE_S * (2 ** (attempt - 1))
            sleep_s += random.uniform(0, 0.3)  # джиттер
            log.warning(
                "сетевой сбой (%s), жду %.2fs и повторяю", e.__class__.__name__, sleep_s,
            )
            time.sleep(sleep_s)
        except Exception as e:
            # Неожиданная ошибка — пробуем ещё раз, мало ли.
            last_exc = e
            if attempt == MAX_ATTEMPTS:
                break
            sleep_s = BACKOFF_BASE_S * (2 ** (attempt - 1))
            log.warning("ошибка %s: %s — повтор через %.2fs", e.__class__.__name__, e, sleep_s)
            time.sleep(sleep_s)

    # Дошли сюда — все попытки исчерпаны.
    assert last_exc is not None
    raise RuntimeError(
        "YouTube сбрасывает соединение при попытке получить субтитры. "
        "Чаще всего это временный rate-limit от YouTube или активный VPN/прокси. "
        "Попробуйте: 1) подождать 30-60 секунд и повторить; "
        "2) выключить VPN/прокси; "
        "3) проверить, открывается ли само видео в браузере. "
        f"Исходная ошибка: {last_exc.__class__.__name__}: {last_exc}"
    ) from last_exc


TARGET_LANG = "ru"


def _pick_best_track(transcript_list, preferred_langs: list[str]):
    """
    Выбирает лучший трек. Гарантирует, что итог будет на русском, если хотя бы
    один трек у видео переводится на русский.

    Приоритет:
      1) ручной русский — без перевода;
      2) ASR русский — без перевода;
      3) ручной на любом языке + перевод на ru;
      4) ASR на любом языке + перевод на ru;
      5) если перевод на ru вообще нигде не сработал — ручной на одном из
         preferred_langs как есть (последний шанс хоть что-то отдать);
      6) совсем уж последний — первый доступный трек.
    """
    tracks = list(transcript_list)
    if not tracks:
        return None

    manual = [t for t in tracks if not t.is_generated]
    auto = [t for t in tracks if t.is_generated]

    # 1) ручной русский
    for t in manual:
        if t.language_code == TARGET_LANG:
            log.info("выбран ручной русский трек")
            return t

    # 2) ASR русский
    for t in auto:
        if t.language_code == TARGET_LANG:
            log.info("выбран ASR русский трек")
            return t

    # 3) ручной (предпочтительно en, потом любой) + перевод на ru
    manual_sorted = sorted(
        manual,
        key=lambda t: 0 if t.language_code == "en" else 1,
    )
    for t in manual_sorted:
        if t.is_translatable:
            try:
                tr = t.translate(TARGET_LANG)
                log.info("выбран ручной %s + перевод -> %s", t.language_code, TARGET_LANG)
                return tr
            except Exception as e:
                log.warning("перевод ручного %s -> %s не удался: %s",
                            t.language_code, TARGET_LANG, e)

    # 4) ASR (предпочтительно en, потом любой) + перевод на ru
    auto_sorted = sorted(
        auto,
        key=lambda t: 0 if t.language_code == "en" else 1,
    )
    for t in auto_sorted:
        if t.is_translatable:
            try:
                tr = t.translate(TARGET_LANG)
                log.info("выбран ASR %s + перевод -> %s", t.language_code, TARGET_LANG)
                return tr
            except Exception as e:
                log.warning("перевод ASR %s -> %s не удался: %s",
                            t.language_code, TARGET_LANG, e)

    # 5) перевод нигде не сработал — отдадим оригинал на одном из preferred_langs
    by_lang = {t.language_code: t for t in tracks}
    for lang in preferred_langs:
        if lang in by_lang:
            log.warning("перевод на %s невозможен, отдаю оригинал %s",
                        TARGET_LANG, lang)
            return by_lang[lang]

    # 6) хоть что-то
    log.warning("выбран первый доступный трек: %s (is_generated=%s)",
                tracks[0].language_code, tracks[0].is_generated)
    return tracks[0]


def _list_with_retries(video_id: str):
    """То же что и _fetch_with_retries, но для list()."""
    last_exc: Exception | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        ytt = _new_ytt()
        try:
            log.info("list tracks: video_id=%s попытка %d/%d",
                     video_id, attempt, MAX_ATTEMPTS)
            return ytt.list(video_id)
        except TranscriptsDisabled:
            raise
        except _RETRYABLE_EXC as e:
            last_exc = e
            if attempt == MAX_ATTEMPTS:
                break
            sleep_s = BACKOFF_BASE_S * (2 ** (attempt - 1)) + random.uniform(0, 0.3)
            log.warning("list: сетевой сбой (%s), жду %.2fs",
                        e.__class__.__name__, sleep_s)
            time.sleep(sleep_s)
        except Exception as e:
            last_exc = e
            if attempt == MAX_ATTEMPTS:
                break
            sleep_s = BACKOFF_BASE_S * (2 ** (attempt - 1))
            log.warning("list: ошибка %s — повтор через %.2fs",
                        e.__class__.__name__, sleep_s)
            time.sleep(sleep_s)
    assert last_exc is not None
    raise RuntimeError(
        f"Не удалось получить список субтитров: "
        f"{last_exc.__class__.__name__}: {last_exc}"
    ) from last_exc


def get_transcript(video_id: str, preferred_langs: list[str] | None = None) -> list[dict]:
    """
    Возвращает список сниппетов: [{"text": str, "start": float, "duration": float}, ...]

    Логика: смотрим все доступные треки видео и через _pick_best_track выбираем
    либо русский (ручной/ASR), либо ЛЮБОЙ переводимый трек с переводом на русский.
    Делает до MAX_ATTEMPTS попыток при ConnectionReset/таймаутах.

    Raises:
        TranscriptsDisabled -- субтитры отключены для видео
        NoTranscriptFound   -- субтитры не найдены ни на одном языке
        RuntimeError        -- сетевая ошибка не ушла после ретраев
        ValueError          -- неверный video_id
    """
    if preferred_langs is None:
        preferred_langs = ["ru", "en"]

    try:
        transcript_list = _list_with_retries(video_id)
    except TranscriptsDisabled:
        raise

    best = _pick_best_track(transcript_list, preferred_langs)
    if best is None:
        raise NoTranscriptFound(video_id, preferred_langs, [])

    # Сам fetch тоже бывает дёргает YouTube — оборачиваем в простой ретрай.
    last_exc: Exception | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            transcript = best.fetch()
            break
        except _RETRYABLE_EXC as e:
            last_exc = e
            if attempt == MAX_ATTEMPTS:
                raise RuntimeError(
                    f"YouTube сбросил соединение при fetch: {e}"
                ) from e
            sleep_s = BACKOFF_BASE_S * (2 ** (attempt - 1)) + random.uniform(0, 0.3)
            log.warning("fetch выбранного трека упал (%s), повтор через %.2fs",
                        e.__class__.__name__, sleep_s)
            time.sleep(sleep_s)

    snippets = [
        {
            "text": snippet.text.replace("\n", " ").strip(),
            "start": round(snippet.start, 2),
            "duration": round(snippet.duration, 2),
        }
        for snippet in transcript
        if snippet.text.strip()
    ]
    log.info("получено сниппетов: %d", len(snippets))
    return snippets


def get_full_text(video_id: str, preferred_langs: list[str] | None = None) -> str:
    """Возвращает полный текст транскрипта одной строкой (для отладки)."""
    snippets = get_transcript(video_id, preferred_langs)
    return " ".join(s["text"] for s in snippets)


# --- Быстрый тест ---
if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    video_id = sys.argv[1] if len(sys.argv) > 1 else "kqtD5dpn9C8"
    print(f"Получаю субтитры для: {video_id}")
    try:
        snippets = get_transcript(video_id)
        print(f"Получено {len(snippets)} сниппетов")
        print("Первые 5:")
        for s in snippets[:5]:
            print(f"  [{s['start']:.1f}s] {s['text']}")
        print("\nПолный текст (первые 500 символов):")
        print(get_full_text(video_id)[:500])
    except Exception as e:
        print(f"Ошибка: {e}")
