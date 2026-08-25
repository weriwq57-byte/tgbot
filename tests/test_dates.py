"""Даты (Минск, UTC+3) и форматирование: parse_date_input, esc, format_date."""
from datetime import date, datetime, timedelta

import pytest

from app.utils.dates import (
    MINSK_TZ,
    end_of_day_minsk,
    now_minsk,
    parse_date_input,
    today_minsk,
)
from app.utils.format import esc, format_date, format_datetime


def test_minsk_tz_offset():
    """Минск — UTC+3, без перехода на летнее время."""
    assert now_minsk().utcoffset() == timedelta(hours=3)
    assert MINSK_TZ.key == "Europe/Minsk"


def test_now_minsk_is_aware():
    assert now_minsk().tzinfo is not None


def test_today_minsk_is_date():
    assert isinstance(today_minsk(), date)


def test_end_of_day_minsk():
    d = end_of_day_minsk(date(2026, 5, 31))
    assert d.date() == date(2026, 5, 31)
    assert d.hour == 23 and d.minute == 59
    assert d.tzinfo is not None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("31.05.2027", date(2027, 5, 31)),
        ("31.05.27", date(2027, 5, 31)),
        ("31/05/2027", date(2027, 5, 31)),
        ("31 05 2027", date(2027, 5, 31)),
        ("1.2.26", date(2026, 2, 1)),
        ("01.02.2026", date(2026, 2, 1)),
        (" 05.06.2026 ", date(2026, 6, 5)),
        ("31.12.99", date(2099, 12, 31)),
        ("01.01.00", date(2000, 1, 1)),
        ("01.01.1900", date(1900, 1, 1)),
    ],
)
def test_parse_date_input_valid(text, expected):
    assert parse_date_input(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "31.05",            # нет года
        "2026.05.31",       # год на первом месте
        "05.2026",          # нет дня
        "31.13.2026",       # 13-й месяц
        "31.02.2026",       # 31 февраля
        "32.01.2026",       # 32-е число
        "abc",
        "",
        None,
        "31.05.2027.05",
    ],
)
def test_parse_date_input_invalid(text):
    assert parse_date_input(text) is None


def test_esc_escapes_html():
    assert esc("<b>bold</b> & <i>ital</i>") == "&lt;b&gt;bold&lt;/b&gt; &amp; &lt;i&gt;ital&lt;/i&gt;"


def test_esc_on_non_string():
    assert esc(42) == "42"


def test_format_date():
    assert format_date(date(2026, 8, 8)) == "08.08.2026"
    assert format_date(None) == "—"


def test_format_datetime():
    assert format_datetime(datetime(2026, 8, 8, 9, 5, tzinfo=MINSK_TZ)) == "08.08.2026 09:05"
    assert format_datetime(None) == "—"


def test_format_datetime_naive_treated_as_minsk():
    naive = datetime(2026, 8, 8, 9, 5)
    assert format_datetime(naive) == "08.08.2026 09:05"
