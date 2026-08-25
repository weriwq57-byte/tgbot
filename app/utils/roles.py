"""Декораторы проверки ролей.

Правило №1: роль берётся ТОЛЬКО из data["db_user"], который загрузит
UserContextMiddleware из БД по tg_id из апдейта (Заход 2). Никогда
не доверяем callback-данным и не храним роли в кнопках.
"""
import inspect
import logging
from typing import Any, Awaitable, Callable

from aiogram.types import CallbackQuery, Message

logger = logging.getLogger(__name__)

MSG_NO_PERMISSION = "Эта команда не для твоей роли"


def user_roles(db_user) -> tuple[str, ...]:
    """Роли пользователя, первичная первой.

    teacher ↔ manager могут совмещаться (роль2): владелец назначил обе.
    owner/student/guest всегда одна роль. Для тестовых заглушек без role2
    возвращает (role,).
    """
    roles = [getattr(db_user, "role", "guest")]
    role2 = getattr(db_user, "role2", None)
    if role2 and role2 not in roles:
        roles.append(role2)
    return tuple(roles)


def has_any_role(db_user, *roles: str) -> bool:
    """Пересечение набора ролей пользователя с требуемыми."""
    return bool(set(user_roles(db_user)) & set(roles))


def require_role(*roles: str) -> Callable:
    """Разрешает хендлер только указанным ролям.

    Использование: @require_role("owner") / @require_role("teacher", "manager")
    Если роль не подходит — вежливый отказ, хендлер не выполняется.

    Хендлер получает ТОЛЬКО те kwargs, что объявлены в его сигнатуре.
    Обёртка сама принимает всё (**data), но наверх передаёт лишь
    совпадающие имена — иначе aiogram протащил бы внутрь ещё и bot,
    state, event_context… и хендлер упал бы с TypeError.
    """

    def decorator(handler: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
        try:
            handler_params = set(inspect.signature(handler).parameters)
        except (ValueError, TypeError):
            handler_params = set()

        # ВАЖНО: НЕ используем @wraps(handler) — иначе aiogram при
        # prepare_kwargs распакует сигнатуру ХЕНДЛЕРА и не передаст
        # обёртке db_user/bot/state (TypeError либо вечный None).
        async def wrapper(*args: Any, **data: Any) -> Any:
            db_user = data.get("db_user")
            if db_user is None:
                return None
            if not has_any_role(db_user, *roles):
                logger.info(
                    "Отказ в доступе: role=%s ждёт %s (tg_id=%s)",
                    user_roles(db_user),
                    ",".join(roles),
                    db_user.tg_id,
                )
                await _deny(*args, **data)
                return None
            allowed = {k: v for k, v in data.items() if k in handler_params}
            return await handler(*args, **allowed)

        return wrapper

    return decorator


async def _deny(*args: Any, **data: Any) -> None:
    """Вежливый отказ для сообщения или колбэка."""
    event = args[0] if args else None
    if isinstance(event, Message):
        try:
            await event.answer(MSG_NO_PERMISSION)
            return
        except Exception:
            logger.exception("Не удалось отказать через message")
    elif isinstance(event, CallbackQuery):
        try:
            await event.answer(MSG_NO_PERMISSION, show_alert=True)
            return
        except Exception:
            logger.exception("Не удалось отказать через callback")

    message: Message | None = data.get("message")
    if message is not None:
        try:
            await message.answer(MSG_NO_PERMISSION)
            return
        except Exception:
            logger.exception("Не удалось отказать через message")

    callback: CallbackQuery | None = data.get("callback_query")
    if callback is not None:
        try:
            await callback.answer(MSG_NO_PERMISSION, show_alert=True)
        except Exception:
            logger.exception("Не удалось отказать через callback")
