"""Мидлварь контекста пользователя (регистрируется на dp.update).

Для каждого апдейта с from_user:
- находит пользователя по tg_id (роль ВСЕГДА берётся из БД, а не из
  данных кнопок — принцип ТЗ раздел 5);
- не нашёл → пытается привязать «телефонного» препода/менеджера
  (tg_id=NULL, is_active=True, роль teacher/manager) по @username;
- не нашёл и привязывать некого → создаёт guest (или owner, если tg_id
  в ADMIN_IDS из настроек);
- освежает tg_username / tg_full_name в БД;
- кладёт объект пользователя в data['db_user'];
- логирует команды (текст, роль, tg_id);
- деактивированного (is_active=False) блокирует: алерт для колбэка,
  текст для сообщения, событие дальше НЕ идёт (раздел 10 ТЗ).
"""
import logging
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, Update
from sqlalchemy import func, select

from app.config import get_settings
from app.database import get_session_factory
from app.models import User
from app.services.invite import looks_like_code

logger = logging.getLogger(__name__)

# Человеческий текст блокировки (заход «повторная привязка»): кикнутого
# человека менеджер мог пересоздать с НОВЫМ кодом — подсказываем путь.
MSG_BLOCKED = (
    "Твой доступ к курсу закрыт. Если тебе выдали новый код приглашения — "
    "напиши его в чат или нажми /start"
)

# Колбэки панели подтверждения привязки — часть пути повторной привязки
_REBIND_CB_PREFIXES = ("std:bind_yes:", "std:bind_no:")


def _is_rebind_event(event) -> bool:
    """Событие пути повторной привязки: НЕ блокируется деактивированному.

    Три пути, описанных в тексте блокировки и заходе:
    - /start (в т.ч. /start КОД — deep link ?start=КОД);
    - текст, похожий на код приглашения («напиши его в чат»);
    - кнопки «Да, это я» / «Нет, это не я» (std:bind_*) — иначе
      подтверждение привязки не дошло бы до хендлера.
    """
    data = getattr(event, "data", None)
    if data is not None:
        return str(data).startswith(_REBIND_CB_PREFIXES)
    text = getattr(event, "text", None)
    if not text:
        return False
    return text.lower().startswith("/start") or looks_like_code(text)


class UserContextMiddleware(BaseMiddleware):
    """Загружает/создаёт пользователя и защищает деактивированных."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        # Мидлварь на dp.update — приходит обёртка Update. Разворачиваем её
        # ТОЛЬКО для чтения (from_user/текст): дальше по цепочке aiogram
        # ожидает именно Update (update.event_type).
        source = event.event if isinstance(event, Update) else event
        from_user = getattr(source, "from_user", None)
        if from_user is None or getattr(from_user, "id", None) is None:
            # События без пользователя (poll_answer и т.п.) — пропускаем.
            return await handler(event, data)

        async with get_session_factory()() as session:
            db_user = await session.scalar(
                select(User).where(User.tg_id == from_user.id)
            )

            if db_user is not None and db_user.role == "guest":
                # Гость уже заходил в бота. Владелец мог добавить его по
                # @username как преподавателя/менеджера (запись с tg_id=NULL).
                # ТЗ (раздел 7, шаг 5): запись гостя удаляем, привязываем его.
                # Или он попал в ADMIN_IDS — повышаем до owner.
                staff = await self._find_unbound_staff(from_user, session)
                if staff is not None:
                    await session.delete(db_user)
                    # Физическое удаление ДО записи tg_id: иначе INSERT/UPDATE
                    # слабо в одной транзакции (UNIQUE на tg_id)
                    await session.flush()
                    db_user = staff
                    db_user.tg_id = from_user.id
                    db_user.tg_username = from_user.username
                    db_user.tg_full_name = from_user.full_name
                    await session.commit()
                    logger.info(
                        "Привязка незанятого профиля (гость убран): "
                        "role=%s tg_id=%s username=@%s",
                        db_user.role,
                        from_user.id,
                        from_user.username,
                    )
                elif from_user.id in get_settings().ADMIN_IDS:
                    db_user.role = "owner"
                    await session.commit()
                    logger.info(
                        "Гость стал владельцем: tg_id=%s (ADMIN_IDS)",
                        from_user.id,
                    )

            if db_user is None:
                # «Телефонный» препод/менеджер (добавлен владельцем, tg_id
                # ещё не заполнен) привязывается при первом входе по username.
                db_user = await self._find_unbound_staff(from_user, session)
                if db_user is None:
                    role = (
                        "owner"
                        if from_user.id in get_settings().ADMIN_IDS
                        else "guest"
                    )
                    db_user = User(
                        tg_id=from_user.id,
                        tg_username=from_user.username,
                        tg_full_name=from_user.full_name,
                        role=role,
                    )
                    session.add(db_user)
                    await session.commit()
                    logger.info(
                        "Новый пользователь → %s: tg_id=%s username=@%s",
                        role,
                        from_user.id,
                        from_user.username,
                    )
                else:
                    db_user.tg_id = from_user.id
                    db_user.tg_username = from_user.username
                    db_user.tg_full_name = from_user.full_name
                    await session.commit()
                    logger.info(
                        "Привязка незанятого профиля: role=%s tg_id=%s username=@%s",
                        db_user.role,
                        from_user.id,
                        from_user.username,
                    )
            else:
                # Освежаем имя/юзернейм (могут меняться)
                changed = False
                if db_user.tg_username != from_user.username:
                    db_user.tg_username = from_user.username
                    changed = True
                if db_user.tg_full_name != from_user.full_name:
                    db_user.tg_full_name = from_user.full_name
                    changed = True
                if changed:
                    await session.commit()

            data["db_user"] = db_user

            # ВАЖНО: db_user выходит из контекста сессии (async with ...) ниже.
            # Объект остаётся валидным только благодаря expire_on_commit=False
            # (см. app/database.py). Никаких lazy-полей у User добавлять нельзя
            # без явной загрузки до выхода из сессии — иначе DetachedInstanceError.

            # Логирование команд: текст, роль, tg_id (ТЗ раздел 13)
            text = getattr(source, "text", None)
            if text and str(text).startswith("/"):
                logger.info(
                    "Команда %s от tg_id=%s role=%s username=@%s",
                    text.split()[0],
                    db_user.tg_id,
                    db_user.role,
                    db_user.tg_username,
                )

            # Деактивированный пользователь (любой роли) — блокируем (ТЗ 10),
            # КРОМЕ пути повторной привязки: /start (в т.ч. ?start=КОД),
            # текст-код и кнопки подтверждения привязки — иначе кикнутый
            # человек с новым кодом не смог бы привязаться (заход).
            if not db_user.is_active and not _is_rebind_event(source):
                logger.info(
                    "Заблокирован деактивированный: tg_id=%s role=%s",
                    db_user.tg_id,
                    db_user.role,
                )
                await self._reply_blocked(source)
                return None

        return await handler(event, data)

    @staticmethod
    async def _find_unbound_staff(from_user, session) -> User | None:
        """Ищет «телефонного» препода/менеджера (tg_id IS NULL) по username.

        Такие записи создаёт владелец в разделе «Преподаватели/Менеджеры»;
        при первом входе человека привязываем tg_id. Никогда не
        привязываем чужие guest/student-записи другого человека.
        """
        if not from_user.username:
            return None
        # Telegram username регистронезависим: «Ivanov» == «ivanov».
        return await session.scalar(
            select(User).where(
                func.lower(User.tg_username) == from_user.username.lower(),
                User.tg_id.is_(None),
                User.role.in_(["teacher", "manager"]),
                User.is_active.is_(True),
            )
        )

    @staticmethod
    async def _reply_blocked(event) -> None:
        """Вежливо сообщаем о блокировке, не роняя событие.

        Колбэк отвечает алертом (не ломаем интерфейс), сообщение — текстом.
        Duck-typing вместо isinstance: одинаково работает с реальными
        объектами aiogram и с тестовыми двойниками.
        """
        try:
            if getattr(event, "data", None) is not None:
                await event.answer(MSG_BLOCKED, show_alert=True)
            elif hasattr(event, "answer"):
                await event.answer(MSG_BLOCKED)
            else:
                pass
        except Exception:
            logger.exception("Не удалось ответить о блокировке")