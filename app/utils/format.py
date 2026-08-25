"""Форматирование текстов: экранирование HTML, даты."""
import html
from datetime import date, datetime

from app.utils.dates import MINSK_TZ


def esc(value) -> str:
    """Экранирует текст пользователя для HTML-разметки Telegram.

    Использовать для ЛЮБОГО пользовательского ввода (имена, названия,
    тексты вопросов), чтобы он не ломал HTML-форматирование.
    """
    return html.escape(str(value), quote=False)


def format_date(d: date | None) -> str:
    """Дата в формате ДД.ММ.ГГГГ (или прочерк, если None)."""
    return d.strftime("%d.%m.%Y") if d else "—"


def format_datetime(dt: datetime | None) -> str:
    """Дата-время по Минску в формате ДД.ММ.ГГГГ ЧЧ:ММ."""
    if dt is None:
        return "—"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=MINSK_TZ)
    return dt.astimezone(MINSK_TZ).strftime("%d.%m.%Y %H:%M")
