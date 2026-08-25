"""Инвайт-коды и пригласительные ссылки (ТЗ, раздел 7).

Код генерируется «до успеха» с проверкой уникальности по БД — строго
внутри той же транзакции/сессии, что и создание ученика (ошибка №10:
иначе два параллельных создания могут выдать один код).

Username бота кэшируем ТОЛЬКО при успешном get_me (ошибка №11: после
первого сбоя пустое значение не должно застревать в кэше).
"""
import logging
import random

from sqlalchemy import select

from app.models import Student

logger = logging.getLogger(__name__)

# Алфавит без похожих символов: нет 0/O, 1/I (ТЗ, раздел 7)
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
CODE_LENGTH = 6
MAX_ATTEMPTS = 100

# Фолбэк, если get_me недоступен (ошибка №11)
FALLBACK_BOT_USERNAME = "LevelUpBot"

# Кэш ТОЛЬКО успешного результата get_me
_bot_username_cache: str | None = None


def generate_code() -> str:
    """Случайный код приглашения: 6 символов из алфавита без похожих."""
    return "".join(random.choices(CODE_ALPHABET, k=CODE_LENGTH))


def looks_like_code(text: str | None) -> bool:
    """Похож ли текст на код приглашения: ровно CODE_LENGTH символов A-Z/0-9.

    Проверка ДО поиска по БД (онбординг): «привет»/мусор получают
    приветствие с подсказкой, а не «Такого кода нет». Формат приёма
    сознательно шире алфавита генерации (без 0/O/1/I): человек не знает
    алфавит, а «ABC123» из подсказок валиден.
    """
    if not text:
        return False
    return len(text) == CODE_LENGTH and text.isalnum() and text.isascii()


async def generate_unique_code(session) -> str:
    """Уникальный код: генерирует до успеха, проверяя по БД.

    Не более MAX_ATTEMPTS попыток — затем RuntimeError (ТЗ, раздел 7).
    Вызывается ДО commit: уникальность проверяется в той же транзакции,
    что и запись ученика (ошибка №10).
    """
    for _ in range(MAX_ATTEMPTS):
        code = generate_code()
        exists = await session.scalar(
            select(Student.id).where(Student.invite_code == code)
        )
        if exists is None:
            return code
    raise RuntimeError(
        f"Не удалось сгенерировать уникальный код за {MAX_ATTEMPTS} попыток"
    )


async def get_bot_username(bot) -> str:
    """Username бота с кэшем успешного результата.

    При сбое get_me возвращает фолбэк и НЕ кэширует — следующая попытка
    повторится (ошибка №11 старой версии).
    """
    global _bot_username_cache
    if _bot_username_cache is not None:
        return _bot_username_cache
    try:
        me = await bot.get_me()
    except Exception:
        logger.exception("get_me не удался — использую фолбэк %r", FALLBACK_BOT_USERNAME)
        return FALLBACK_BOT_USERNAME
    _bot_username_cache = me.username or FALLBACK_BOT_USERNAME
    return _bot_username_cache


def invite_link(bot_username: str, code: str) -> str:
    """Ссылка-приглашение: https://t.me/{username}?start={КОД}."""
    return f"https://t.me/{bot_username}?start={code}"
