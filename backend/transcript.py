"""
transcript.py — получение субтитров с YouTube через youtube-transcript-api.
Запускается локально (не в облаке), поэтому блокировок по IP нет.
"""

from youtube_transcript_api import YouTubeTranscriptApi, NoTranscriptFound, TranscriptsDisabled
import re


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


def get_transcript(video_id: str, preferred_langs: list[str] = None) -> list[dict]:
    """
    Возвращает список сниппетов: [{"text": str, "start": float, "duration": float}, ...]

    Приоритет языков: ru -> en -> любой доступный.
    Raises:
        TranscriptsDisabled -- субтитры отключены для видео
        NoTranscriptFound   -- субтитры не найдены
        ValueError          -- неверный video_id
    """
    if preferred_langs is None:
        preferred_langs = ["ru", "en"]

    ytt = YouTubeTranscriptApi()

    try:
        transcript = ytt.fetch(video_id, languages=preferred_langs)
    except NoTranscriptFound:
        # Берём любой доступный трек
        transcript_list = ytt.list(video_id)
        available = list(transcript_list)
        if not available:
            raise NoTranscriptFound(video_id, preferred_langs, [])
        transcript = available[0].fetch()

    snippets = [
        {
            "text": snippet.text.replace("\n", " ").strip(),
            "start": round(snippet.start, 2),
            "duration": round(snippet.duration, 2),
        }
        for snippet in transcript
        if snippet.text.strip()
    ]

    return snippets


def get_full_text(video_id: str, preferred_langs: list[str] = None) -> str:
    """Возвращает полный текст транскрипта одной строкой (для отладки)."""
    snippets = get_transcript(video_id, preferred_langs)
    return " ".join(s["text"] for s in snippets)


# --- Быстрый тест ---
if __name__ == "__main__":
    import sys

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
