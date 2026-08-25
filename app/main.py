"""Точка входа: long-polling.

Запуск: python -m app.main (из корня и из любого другого каталога —
см. ТЗ раздел 11, ошибка №1: путь к .env абсолютный, а sys.path
пополняется корнем проекта в самом начале).
"""
import asyncio
import logging
import sys
from pathlib import Path

from pydantic import ValidationError

# Корень проекта добавляем в sys.path, чтобы `python -m app.main` и
# `python app/main.py` работали из любого каталога.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from aiogram.utils.token import TokenValidationError  # noqa: E402

from app.bot import create_bot, dp, set_bot_commands  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.database import check_db_connection  # noqa: E402
from app.handlers import (  # noqa: E402
    student,
    commands,
    manager,
    owner,
    teacher,
    broadcast,
)
from app.utils.errors import register_error_handler  # noqa: E402
from app.utils.logging import setup_logging  # noqa: E402

logger = logging.getLogger(__name__)


def db_connection_error_text(exc: Exception) -> str:
    """Понятный текст ошибки недоступной БД (ТЗ, ошибка №4).

    Вынесен отдельно, чтобы сообщение было покрыто тестом дословно.
    """
    return f"Нет соединения с базой данных: {exc}"


async def main() -> None:
    """Старт бота: настройки, БД, роутеры, ошибки, поллинг."""
    setup_logging()

    # 1. Настройки: создание ленивое, поэтому кривые значения в .env
    #    (ADMIN_IDS=abc и т.п.) дают понятную ошибку здесь, а не при импорте.
    try:
        settings = get_settings()
        settings.validate()
    except ValidationError as exc:
        logger.error("Ошибка в настройках (.env): %s", exc.errors()[0]["msg"])
        raise SystemExit(1) from None
    except RuntimeError as exc:
        logger.error("%s", exc)
        raise SystemExit(1) from None

    if not settings.ADMIN_IDS:
        logger.warning(
            "ADMIN_IDS пуст — уведомления об ошибках владельцу уходить не будут"
        )

    # 2. Проверка подключения к БД (ТЗ, ошибка №4). Битый DATABASE_URL
    #    тоже даёт понятную ошибку здесь, а не при импорте модуля.
    try:
        await check_db_connection()
    except Exception as exc:
        logger.error("%s", db_connection_error_text(exc))
        raise SystemExit(1) from None

    try:
        bot = create_bot()
    except TokenValidationError as exc:
        logger.error("Некорректный BOT_TOKEN в .env: %s", exc)
        raise SystemExit(1) from None

    # Роутеры: ученик ПЕРВЫМ (перехват /start КОД и menu:back:student:0
    # для роли student; команды — затем: commands.py), владелец, менеджер,
    # преподаватель
    dp.include_router(student.router)
    dp.include_router(commands.router)
    dp.include_router(owner.router)
    dp.include_router(broadcast.router)
    dp.include_router(manager.router)
    dp.include_router(teacher.router)

    # Глобальный обработчик ошибок (лог + ответ + алерт владельцу)
    register_error_handler(dp, bot)

    # Гарантируем чистый старт long-polling (ТЗ, ошибка №2: не падаем)
    try:
        await bot.delete_webhook(drop_pending_updates=True)
    except Exception:
        logger.exception("Не удалось удалить webhook (продолжаем старт)")

    # Команды в нижнюю кнопку «Меню» Telegram; сбой не останавливает старт
    try:
        await set_bot_commands(bot)
    except Exception:
        logger.exception("Не удалось зарегистрировать команды меню (продолжаем)")

    logger.info("Бот LevelUp запускается...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Бот остановлен.")
    except Exception:
        logger.exception("Бот завершился с ошибкой")
        raise SystemExit(1) from None