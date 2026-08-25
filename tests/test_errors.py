"""Понятные ошибки при старте (ТЗ раздел 11, ошибка №1).

Запуск без BOT_TOKEN должен дать понятную ошибку, а не падение с 401.
Кривые значения в .env (ADMIN_IDS, DATABASE_URL) — тоже понятная ошибка,
и настройки создаются лениво (не при импорте).
"""
import pytest
from pydantic import ValidationError

from app.config import (
    MSG_BAD_ADMIN_IDS,
    MSG_BAD_DATABASE_URL,
    MSG_NO_TOKEN,
    Settings,
    get_settings,
)


def test_empty_token_raises_friendly_error(monkeypatch):
    """Пустой BOT_TOKEN → RuntimeError с понятным текстом."""
    monkeypatch.delenv("BOT_TOKEN", raising=False)
    settings = Settings(BOT_TOKEN="", _env_file=None)
    with pytest.raises(RuntimeError) as exc_info:
        settings.validate()
    assert str(exc_info.value) == MSG_NO_TOKEN


def test_token_present_passes(monkeypatch):
    """Заполненный BOT_TOKEN проходит валидацию."""
    monkeypatch.delenv("BOT_TOKEN", raising=False)
    settings = Settings(BOT_TOKEN="123456:ABCDEFGH", _env_file=None)
    settings.validate()  # не бросает


def test_error_message_text():
    """Текст ошибки — дословно из ТЗ."""
    assert MSG_NO_TOKEN == "BOT_TOKEN не задан. Заполни .env"


# --------------------------------------------------------------------------
# Баг: кривой ADMIN_IDS ронял бот при импорте (settings на уровне модуля)
# --------------------------------------------------------------------------
def test_settings_import_is_lazy_with_bad_admin_ids(monkeypatch):
    """Настройки создаются лениво: мусор в ADMIN_IDS падает при первом
    обращении (понятный ValidationError), а не при import app.config.

    Имитируем .env с мусором через переменные окружения и сбрасываем кэш.
    """
    import app.config

    app.config._settings = None
    monkeypatch.setenv("ADMIN_IDS", "abc")
    with pytest.raises(ValidationError) as exc_info:
        get_settings()
    msg = exc_info.value.errors()[0]["msg"]
    assert "ADMIN_IDS" in msg and "не число" in msg


def test_bad_admin_ids_message_text():
    assert "ADMIN_IDS" in MSG_BAD_ADMIN_IDS


def test_admin_ids_parsed_correctly(monkeypatch):
    """«1, 2,3» → [1, 2, 3]."""
    settings = Settings(ADMIN_IDS="1, 2,3", _env_file=None)
    assert settings.ADMIN_IDS == [1, 2, 3]


# --------------------------------------------------------------------------
# Баг/замечание: DATABASE_URL не проверялся при старте
# --------------------------------------------------------------------------
def test_validate_bad_database_url(monkeypatch):
    """Не asyncpg-URL → RuntimeError с понятным текстом."""
    monkeypatch.delenv("BOT_TOKEN", raising=False)
    settings = Settings(BOT_TOKEN="123456:ABC", DATABASE_URL="sqlite:///x.db", _env_file=None)
    with pytest.raises(RuntimeError) as exc_info:
        settings.validate()
    assert str(exc_info.value) == MSG_BAD_DATABASE_URL


def test_validate_empty_database_url(monkeypatch):
    monkeypatch.delenv("BOT_TOKEN", raising=False)
    settings = Settings(BOT_TOKEN="123456:ABC", DATABASE_URL="", _env_file=None)
    with pytest.raises(RuntimeError) as exc_info:
        settings.validate()
    assert str(exc_info.value) == MSG_BAD_DATABASE_URL


def test_validate_good_database_url(monkeypatch):
    monkeypatch.delenv("BOT_TOKEN", raising=False)
    settings = Settings(
        BOT_TOKEN="123456:ABC",
        DATABASE_URL="postgresql+asyncpg://u:p@localhost:5432/db",
        _env_file=None,
    )
    settings.validate()  # не бросает


def test_get_settings_returns_cached_instance():
    """get_settings() кешируется."""
    first = get_settings()
    second = get_settings()
    assert first is second


# --------------------------------------------------------------------------
# Заход 7: check_db_connection с недоступной БД (ошибка №4)
# --------------------------------------------------------------------------
async def test_check_db_connection_unavailable(monkeypatch):
    """Недоступная БД → check_db_connection бросает (в main.py — понятная
    ошибка и выход; здесь проверяем сам бросок без реального сервера)."""
    import app.database as database

    class _FakeSettings:
        DATABASE_URL = (
            "postgresql+asyncpg://user:pass@127.0.0.1:9/nope"
        )

    monkeypatch.setattr(database, "engine", None)
    monkeypatch.setattr(database, "SessionFactory", None)
    monkeypatch.setattr(database, "get_settings", lambda: _FakeSettings())

    with pytest.raises(Exception):
        await database.check_db_connection()

    # глобальный engine снова свободен — не влияет на другие тесты
    monkeypatch.setattr(database, "engine", None)
    monkeypatch.setattr(database, "SessionFactory", None)


# --------------------------------------------------------------------------
# Заход 8, п.0.2: сообщение «Нет соединения с базой данных» — в функции
# --------------------------------------------------------------------------
def test_db_connection_error_text_message():
    """Формирование сообщения вынесено в функцию (покрыто дословно):
    «Нет соединения с базой данных: …» + причина недоступности."""
    from app.main import db_connection_error_text

    exc = ConnectionRefusedError("refused")
    assert db_connection_error_text(exc) == (
        "Нет соединения с базой данных: refused"
    )
    assert db_connection_error_text(exc).startswith("Нет соединения с базой данных:")
