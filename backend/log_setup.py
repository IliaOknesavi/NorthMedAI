"""
log_setup.py — единая настройка логирования для бэкенда NorthMedAI.

Пишет в:
  - stderr (как и раньше, чтобы видно было в терминале uvicorn);
  - backend/logs/backend.log — общий лог приложения;
  - backend/logs/detector.log — отдельный лог детектора (raw-ответы YandexGPT,
    распарсенные claims, ошибки разбора).

Файлы ротируются (5 МБ × 3 файла), чтобы не съесть диск во время отладки.

Использование:
    from log_setup import setup_logging
    setup_logging()                       # один раз на старте приложения
    log = logging.getLogger("detector")   # обычным способом
"""

from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path

LOG_DIR = Path(__file__).parent / "logs"
APP_LOG = LOG_DIR / "backend.log"
DETECTOR_LOG = LOG_DIR / "detector.log"

MAX_BYTES = 5 * 1024 * 1024  # 5 МБ
BACKUP_COUNT = 3

_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"

_configured = False


def _make_file_handler(path: Path, level: int) -> logging.Handler:
    handler = logging.handlers.RotatingFileHandler(
        path,
        maxBytes=MAX_BYTES,
        backupCount=BACKUP_COUNT,
        encoding="utf-8",
    )
    handler.setLevel(level)
    handler.setFormatter(logging.Formatter(_FORMAT, datefmt=_DATEFMT))
    return handler


def setup_logging(level: int = logging.INFO) -> None:
    """Настраивает корневой логгер + отдельный файл для логгера 'detector'.

    Идемпотентна: повторный вызов ничего не делает.
    """
    global _configured
    if _configured:
        return

    LOG_DIR.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(level)

    # Консоль — оставляем как было.
    if not any(isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler)
               for h in root.handlers):
        console = logging.StreamHandler()
        console.setLevel(level)
        console.setFormatter(logging.Formatter(_FORMAT, datefmt=_DATEFMT))
        root.addHandler(console)

    # Общий файл — пишут все логгеры.
    root.addHandler(_make_file_handler(APP_LOG, level))

    # Отдельный файл только для детектора: DEBUG, чтобы видеть raw-ответы.
    detector_logger = logging.getLogger("detector")
    detector_logger.setLevel(logging.DEBUG)
    detector_logger.addHandler(_make_file_handler(DETECTOR_LOG, logging.DEBUG))
    # propagate=True по умолчанию — запись попадёт и в backend.log тоже.

    _configured = True
    logging.getLogger(__name__).info(
        "logging инициализировано: %s, %s", APP_LOG, DETECTOR_LOG
    )
