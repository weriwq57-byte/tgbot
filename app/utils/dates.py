"""Работа с датами и временем.

Все даты/времена — Минск, UTC+3, без летнего времени (Беларусь не
переводит часы). Стрики и «конец дня» считаются по Минску.
"""
import re
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

# ZoneInfo создаётся один раз, в одном месте (ТЗ раздел 11, ошибка №3:
# без пакета tzdata импорт падал на Linux; tzdata в requirements.txt).
MINSK_TZ = ZoneInfo("Europe/Minsk")

# ДД.ММ.ГГГГ или ДД.ММ.ГГ (разделители: точка, слеш, пробел)
_DATE_RE = re.compile(r"^\s*(\d{1,2})[./\s]+(\d{1,2})[./\s]+(\d{2,4})\s*$")


def now_minsk() -> datetime:
    """Текущее время по Минску (aware)."""
    return datetime.now(MINSK_TZ)


def today_minsk() -> date:
    """Сегодняшняя дата по Минску."""
    return now_minsk().date()


def end_of_day_minsk(d: date) -> datetime:
    """Конец календарного дня по Минску (23:59:59.999999) — включительно."""
    return datetime.combine(d, time.max, tzinfo=MINSK_TZ)


def parse_date_input(text: str) -> date | None:
    """Парсит пользовательский ввод даты: «31.05.2027» или «31.05.27».

    Разделители — точка, слеш или пробел. Двузначный год → 2000+ («27» → 2027).
    Возвращает None, если формат не распознан (или дата несуществующая).
    """
    match = _DATE_RE.match(text or "")
    if match is None:
        return None
    day, month, year = (int(part) for part in match.groups())
    if year < 100:
        year += 2000  # «27» → 2027
    try:
        return date(year, month, day)
    except ValueError:
        return None
