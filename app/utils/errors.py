"""Глобальный обработчик ошибок.

Ловит все исключения из хендлеров:
- пишет трейсбек в лог;
- отвечает пользователю «Что-то пошло не так…»;
- при 10+ ошибках за час шлёт одно уведомление владельцам из ADMIN_IDS
  (счётчик в памяти, окно 1 час).
"""
import logging
import time
from collections import deque

from aiogram import Bot, Dispatcher
from aiogram.types import ErrorEvent

from app.config import get_settings

logger = logging.getLogger(__name__)

# Метки времени последних ошибок (для скользящего окна в 1 час)
_error_times: deque[float] = deque(maxlen=1000)
# Когда в последний раз уведомляли владельца об ошибках
_last_notify_ts: float = 0.0

MSG_TECHNICAL_ERROR = "Что-то пошло не так, попробуй ещё раз"


def _extract_chat_id(event: ErrorEvent) -> int | None:
    """Достаёт chat_id из апдейта, в котором упал хендлер."""
    update = event.update
    if update.message is not None:
        return update.message.chat.id
    if update.callback_query is not None:
        return update.callback_query.message.chat.id if update.callback_query.message else None
    if update.channel_post is not None:
        return update.channel_post.chat.id
    return None


def register_error_handler(dp: Dispatcher, bot: Bot) -> None:
    """Регистрирует глобальный обработчик ошибок на диспетчере."""

    @dp.errors()
    async def on_error(event: ErrorEvent) -> None:
        await handle_error(bot, event)


async def handle_error(bot: Bot, event: ErrorEvent) -> None:
    """Обработчик одной ошибки (вынесен для тестируемости)."""
    global _last_notify_ts
    # Защита от рекурсии: если падает сам обработчик ошибок —
    # пишем в лог и выходим.
    try:
        logger.error(
            "Исключение в хендлере: %s", event.exception, exc_info=event.exception
        )
    except Exception:
        return

    try:
        # Считаем ошибки за последний час
        now = time.time()
        _error_times.append(now)
        while _error_times and now - _error_times[0] > 3600:
            _error_times.popleft()

        # 10+ ошибок за час и не уведомляли в течение часа — алерт владельцу
        if len(_error_times) >= 10 and now - _last_notify_ts > 3600:
            for admin_id in get_settings().ADMIN_IDS:
                try:
                    await bot.send_message(
                        admin_id,
                        f"⚠️ У бота LevelUp {len(_error_times)} ошибок "
                        f"за последний час. Проверь логи.",
                    )
                except Exception:
                    logger.exception("Не удалось уведомить владельца %s", admin_id)
            _last_notify_ts = now
    except Exception:
        logger.exception("Ошибка в счётчике ошибок")

    # Вежливо сообщаем пользователю
    chat_id = _extract_chat_id(event)
    if chat_id is not None:
        try:
            await bot.send_message(chat_id, MSG_TECHNICAL_ERROR)
        except Exception:
            logger.exception("Не удалось ответить пользователю %s", chat_id)
