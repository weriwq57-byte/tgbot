"""Настройка логирования.

Лог пишется в logs/levelup.log (rotating, ограниченный размер) и в консоль.
Все команды и ошибки попадают в лог; алерт владельцу при всплеске ошибок —
см. app/utils/errors.py.
"""
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from app.config import get_settings

LOG_DIR = Path(__file__).resolve().parent.parent.parent / "logs"
LOG_FILE = LOG_DIR / "levelup.log"

_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
_DATE_FORMAT = "%d.%m.%Y %H:%M:%S"


def _resolve_level(level_name: str | None) -> int:
    """Переводит имя уровня в число; неизвестное значение — INFO с warning.

    Неизвестный уровень из .env не должен ронять бот (фолбэк на INFO).
    """
    name = (level_name or "").upper()
    level = getattr(logging, name, None)
    if not isinstance(level, int):
        logging.getLogger(__name__).warning(
            "Неизвестный LOG_LEVEL %r — использую INFO", name
        )
        return logging.INFO
    return level


def _config_log_level() -> str:
    """Уровень из настроек, устойчивый к битому .env.

    Если настройки вообще не читаются (кривой ADMIN_IDS и т.п.) — вернём
    INFO: понятную ошибку покажет main.py при первой проверке настроек.
    """
    try:
        return get_settings().LOG_LEVEL
    except Exception:
        return "INFO"


def setup_logging(level: str | None = None) -> None:
    """Настраивает корневой логгер: файл + консоль. Вызывать один раз."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    if root.handlers:
        # Уже настроены (например, в тестах) — не пересоздаём.
        return

    root.setLevel(_resolve_level(level or _config_log_level()))

    formatter = logging.Formatter(_FORMAT, datefmt=_DATE_FORMAT)

    # Файловый хендлер: 5 МБ × 5 файлов
    file_handler = RotatingFileHandler(
        LOG_FILE,
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    # Консольный хендлер
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    root.addHandler(console_handler)
