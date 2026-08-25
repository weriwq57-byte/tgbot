"""Хелперы отправки/редактирования сообщений."""
import logging

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import Message

logger = logging.getLogger(__name__)


async def safe_edit(message: Message, text: str, reply_markup=None) -> bool:
    """Редактирование сообщения без падения на «message is not modified».

    ТГ считает ошибкой редактирование, когда и текст, и клавиатура
    не изменились — это не настоящая ошибка, молча пропускаем.
    True — сообщение отредактировано (или и так актуально); False — ошибка.
    """
    try:
        await message.edit_text(text, reply_markup=reply_markup)
    except TelegramBadRequest as exc:
        # ТГ присылает разные формулировки («message is not modified»,
        # «message is not modified: specified new message content...») —
        # ищем по ключевым словам, регистр не важен.
        if "message is not modified" in str(exc).lower():
            return True
        logger.warning("Не удалось отредактировать сообщение: %s", exc)
        return False
    return True
