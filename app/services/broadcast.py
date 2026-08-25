"""Рассылка (Заход 9): сбор получателей по категориям и отправка.

Сбор получателей — только активные пользователи:
- students — по Student/StudentSubject (режим "all" — все ученики,
  "subjects" — только ученики с активной записью по выбранному предмету);
- teachers/managers — по первичной роли ИЛИ role2 (совмещение ролей,
  см. users.role2): пользователь с обеими ролями получит одно сообщение.

Отправка: каждому получателю отдельное сообщение; сбой одного
получателя не роняет рассылку (failed). Пользователи без tg_id
и неактивные на момент отправки — skipped.
"""
import asyncio
import logging

from aiogram.exceptions import (
    TelegramAPIError,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Student, StudentSubject, Subject, User
from app.utils.format import esc
from app.utils.roles import user_roles

logger = logging.getLogger(__name__)

STUDENTS_MODE_ALL = "all"
STUDENTS_MODE_SUBJECTS = "subjects"

# Имена категорий в визарде (кнопки: students/teachers/managers) vs роли в БД
ROLE_BY_CATEGORY = {"students": "student", "teachers": "teacher", "managers": "manager"}

# Строки «Получателей: N» — в обработчике; итог рассылки здесь:
TEXT_REPORT = (
    "✅ Рассылка отправлена.\n"
    "Отправлено: {ok}\n"
    "Не удалось: {failed}\n"
    "Пропущено: {skipped}"
)


async def collect_recipients(
    session: AsyncSession,
    categories: list[str],
    students_mode: str = STUDENTS_MODE_ALL,
    subject_ids: list[int] | None = None,
) -> list[User]:
    """Активные получатели по категориям (без дублей).

    categories — подмножество {"students","teachers","managers"}.
    Возвращает пользователей (студенты — role="student", преподаватели
    и менеджеры — по user_roles(), т.е. учитывая role2).
    """
    want_students = "students" in categories
    want_staff = {ROLE_BY_CATEGORY[c] for c in categories if c in ROLE_BY_CATEGORY}
    want_staff.discard("student")  # «students» в staff-ветку не попадает
    if not want_students and not want_staff:
        return []

    users: dict[int, User] = {}

    if want_students:
        stmt = (
            select(User)
            .join(Student, Student.user_id == User.id)
            .where(User.is_active.is_(True))
        )
        if students_mode == STUDENTS_MODE_SUBJECTS and subject_ids:
            stmt = (
                stmt.join(StudentSubject, StudentSubject.student_id == Student.id)
                .join(Subject, Subject.id == StudentSubject.subject_id)
                .where(
                    StudentSubject.subject_id.in_(subject_ids),
                    StudentSubject.is_active.is_(True),
                    Subject.is_active.is_(True),
                )
            )
        for user in await session.scalars(stmt):
            users[user.id] = user

    if want_staff:
        stmt = select(User).where(
            User.is_active.is_(True),
            User.role.in_(want_staff)
            | (User.role2.is_not(None) & User.role2.in_(want_staff)),
        )
        for user in await session.scalars(stmt):
            if set(user_roles(user)) & want_staff:
                users[user.id] = user

    return list(users.values())


async def send_broadcast(
    message,
    bot,
    recipients: list[User],
    photo: bool,
    text: str = "",
    photo_file_id: str | None = None,
) -> dict[str, int]:
    """Отправка: по одному сообщению каждому получателю.

    photo=True — send_photo (caption=text или None); иначе send_message.
    Текст экранируется esc() (HTML parse_mode включён глобально).
    Сбой одного получателя ловится и учитывается:
    - forbidden / chat not found — skipped (человек не в чате с ботом);
    - retry after / сетевые таймауты — пауза и один повтор;
    - остальное — failed.
    Между сообщениями — пауза 0.05 с (антифлуд Telegram).
    """
    ok = failed = skipped = 0

    async def _send_one(user: User) -> bool:
        if photo:
            await bot.send_photo(
                chat_id=user.tg_id, photo=photo_file_id, caption=esc(text) or None
            )
        else:
            await bot.send_message(chat_id=user.tg_id, text=esc(text))
        return True

    for user in recipients:
        if not user.tg_id or not user.is_active:
            skipped += 1
            continue
        try:
            await _send_one(user)
            ok += 1
        except TelegramForbiddenError:
            # бот заблокирован / чат не найден — человек не в чате
            skipped += 1
            logger.warning("Рассылка: не в чате tg_id=%s", user.tg_id)
        except TelegramRetryAfter as exc:
            # лимит частоты: ждём retry_after и повторяем один раз
            await asyncio.sleep(exc.retry_after)
            try:
                await _send_one(user)
                ok += 1
            except Exception:
                failed += 1
                logger.exception("Рассылка: не удалось после retry tg_id=%s", user.tg_id)
        except (TelegramNetworkError, TimeoutError):
            # сеть/таймаут: короткая пауза и один повтор
            await asyncio.sleep(2)
            try:
                await _send_one(user)
                ok += 1
            except Exception:
                failed += 1
                logger.exception("Рассылка: не удалось после сети tg_id=%s", user.tg_id)
        except (TelegramAPIError, Exception):
            failed += 1
            logger.exception("Рассылка: не удалось отправить tg_id=%s", user.tg_id)
        await asyncio.sleep(0.05)
    return {"ok": ok, "failed": failed, "skipped": skipped}
