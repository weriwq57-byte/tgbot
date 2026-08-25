"""Регрессионные тесты найденных багов каркаса.

- safe_edit не падает на «message is not modified» (любой регистр);
- setup_logging переживает неизвестный LOG_LEVEL (фолбэк на INFO);
- validate() срезает пробелы вокруг BOT_TOKEN.
"""
import logging

import pytest
from aiogram.exceptions import TelegramBadRequest
import aiogram.methods

from app.config import MSG_NO_TOKEN, Settings
from app.utils.logging import _resolve_level
from app.utils.messages import safe_edit


# --------------------------------------------------------------------------
# safe_edit
# --------------------------------------------------------------------------
class FakeMessage:
    """Заглушка Message: edit_text кидает заданное исключение."""

    def __init__(self, exc):
        self._exc = exc

    async def edit_text(self, text, reply_markup=None):
        if self._exc is not None:
            raise self._exc
        return None


def _bad_request(description: str) -> TelegramBadRequest:
    return TelegramBadRequest(method=aiogram.methods.DeleteWebhook(), message=description)


async def test_safe_edit_message_not_modified_lowercase():
    """«message is not modified» (классика) — молча пропускаем."""
    msg = FakeMessage(_bad_request("Bad Request: message is not modified"))
    await safe_edit(msg, "текст")  # не должно упасть


async def test_safe_edit_message_not_modified_uppercase():
    """Другой регистр и длинное описание — тоже не падаем."""
    msg = FakeMessage(_bad_request("Bad Request: Message is Not Modified: specified new message content and reply markup are the same as a current content"))
    await safe_edit(msg, "текст")


async def test_safe_edit_other_error_does_not_raise():
    """Любая другая ошибка редактирования логируется, но не роняет бота."""
    msg = FakeMessage(_bad_request("Bad Request: chat not found"))
    await safe_edit(msg, "текст")  # warning в лог, исключение не наружу


async def test_safe_edit_success():
    """Успешное редактирование — без изменений."""
    msg = FakeMessage(None)
    await safe_edit(msg, "новый текст")


# --------------------------------------------------------------------------
# setup_logging: неизвестный LOG_LEVEL
# --------------------------------------------------------------------------
@pytest.mark.parametrize("name,expected", [("DEBUG", logging.DEBUG), ("INFO", logging.INFO), ("ERROR", logging.ERROR)])
def test_resolve_level_valid(name, expected):
    from app.utils.logging import _resolve_level
    assert _resolve_level(name) == expected


def test_resolve_level_unknown_falls_back(caplog):
    """LOG_LEVEL=BOGUS не роняет бот: фолбэк на INFO + warning в лог."""
    from app.utils.logging import _resolve_level
    assert _resolve_level("BOGUS") == logging.INFO
    assert "BOGUS" in caplog.text


def test_resolve_level_empty_falls_back():
    from app.utils.logging import _resolve_level
    assert _resolve_level("") == logging.INFO
    assert _resolve_level(None) == logging.INFO


# --------------------------------------------------------------------------
# config.validate: пробелы вокруг BOT_TOKEN
# --------------------------------------------------------------------------
def test_validate_strips_token(monkeypatch):
    monkeypatch.delenv("BOT_TOKEN", raising=False)
    settings = Settings(BOT_TOKEN="  123456:ABCD  ", _env_file=None)
    settings.validate()
    assert settings.BOT_TOKEN == "123456:ABCD"


def test_validate_whitespace_only_token(monkeypatch):
    monkeypatch.delenv("BOT_TOKEN", raising=False)
    settings = Settings(BOT_TOKEN="   ", _env_file=None)
    with pytest.raises(RuntimeError) as exc_info:
        settings.validate()
    assert str(exc_info.value) == MSG_NO_TOKEN


# --------------------------------------------------------------------------
# parse_date_input: границы формата (ТЗ раздел 6 — даты «ДД.ММ.ГГГГ»)
# --------------------------------------------------------------------------
from app.utils.dates import parse_date_input  # noqa: E402


@pytest.mark.parametrize(
    "raw,day,month,year",
    [
        ("31.05.2027", 31, 5, 2027),
        ("31/05/2027", 31, 5, 2027),
        ("31 05 2027", 31, 5, 2027),
        ("1.1.2027", 1, 1, 2027),          # без ведущих нулей
        ("31.05.27", 31, 5, 2027),         # двузначный год → 2000+
        (" 31.05.2027 ", 31, 5, 2027),     # пробелы по краям
    ],
)
def test_parse_date_valid(raw, day, month, year):
    from datetime import date
    assert parse_date_input(raw) == date(year, month, day)


@pytest.mark.parametrize(
    "raw",
    [
        "31.02.2027",   # несуществующая дата
        "31.05",        # нет года
        "abc",          # мусор
        "05.31.2027",   # месяц на первом месте не поддерживается
        "2027.05.31",   # ISO — не поддерживается
        "",             # пусто
        "32.05.2027",   # день вне диапазона
        "31.13.2027",   # месяц вне диапазона
    ],
)
def test_parse_date_invalid(raw):
    assert parse_date_input(raw) is None


# --------------------------------------------------------------------------
# Глобальный обработчик ошибок (ТЗ 13): счётчик 10+/час + уведомление
# --------------------------------------------------------------------------
from types import SimpleNamespace  # noqa: E402

from app.utils import errors as errors_mod  # noqa: E402


def _make_error_event(chat_id=42):
    """Фейковый ErrorEvent: exception + update с сообщением."""
    update = SimpleNamespace(
        message=SimpleNamespace(chat=SimpleNamespace(id=chat_id)),
        callback_query=None,
        channel_post=None,
    )
    return SimpleNamespace(exception=RuntimeError("boom"), update=update)


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text):
        self.sent.append((chat_id, text))


@pytest.fixture(autouse=True)
def _reset_error_state():
    """Глобальный счётчик ошибок — пер-процессный, чистим до/после теста."""
    errors_mod._error_times.clear()
    errors_mod._last_notify_ts = 0.0
    yield
    errors_mod._error_times.clear()
    errors_mod._last_notify_ts = 0.0


async def test_error_handler_replies_user_each_time(monkeypatch):
    bot = FakeBot()
    monkeypatch.setattr(
        errors_mod, "get_settings", lambda: SimpleNamespace(ADMIN_IDS=[555])
    )
    event = _make_error_event()
    for _ in range(9):
        await errors_mod.handle_error(bot, event)
    # до 10 ошибок — только ответ пользователю, уведомлений владельцу нет
    assert [t for _, t in bot.sent] == [errors_mod.MSG_TECHNICAL_ERROR] * 9
    assert not any("⚠️ У бота" in t for _, t in bot.sent)


async def test_error_handler_notifies_owner_once_per_hour(monkeypatch):
    """10-я ошибка за час — уведомление владельцу; дальше — не чаще раза в час."""
    bot = FakeBot()
    monkeypatch.setattr(
        errors_mod, "get_settings", lambda: SimpleNamespace(ADMIN_IDS=[555])
    )
    event = _make_error_event(chat_id=777)
    for _ in range(10):
        await errors_mod.handle_error(bot, event)
    assert (555, "⚠️ У бота LevelUp 10 ошибок за последний час. Проверь логи.") in bot.sent
    # 11-я и 15-я в том же часу — повторного уведомления нет
    for _ in range(5):
        await errors_mod.handle_error(bot, event)
    alerts = [t for _, t in bot.sent if "⚠️ У бота" in t]
    assert len(alerts) == 1
