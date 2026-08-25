"""Сервисы менеджера: ученики, доступ, инвайт-коды (ТЗ, разделы 6–7).

Вся бизнес-логика здесь; хендлеры (app/handlers/manager.py) — только UI.
Изменения коммитятся здесь же; сессия закрывается в хендлере — поэтому
все функции читают данные явно, без lazy-полей (expire_on_commit=False).

Сортировка списка (ТЗ, раздел 6): не привязанные → истёкшие → остальные,
внутри групп — по имени.
"""
import logging

from datetime import date, timedelta
from sqlalchemy import delete, select

from app.models import Student, StudentSubject, Subject, User
from app.services.invite import generate_unique_code
from app.utils.dates import today_minsk

logger = logging.getLogger(__name__)

# Длина имени ученика в визарде
STUDENT_NAME_MAX = 100


# --------------------------------------------------------------------------
# Создание ученика (код и запись — в одной транзакции, ошибка №10)
# --------------------------------------------------------------------------
async def create_student_record(
    session, name: str, subject_ids: set[int], invited_by_id: int, access_until: date
) -> tuple[User, Student, str]:
    """Создаёт ученика целиком: User + Student + StudentSubject.

    Инвайт-код генерируется ДО commit в этой же сессии — уникальность
    гарантируется в одной транзакции (ошибка №10). Возвращает
    (user, student, code). Коммит — в конце.
    """
    code = await generate_unique_code(session)

    user = User(
        tg_id=None,
        tg_username=None,
        tg_full_name=name,
        role="student",
        is_active=True,
    )
    session.add(user)
    await session.flush()  # нужен user.id

    student = Student(
        user_id=user.id,
        access_until=access_until,
        invite_code=code,
        invite_status="pending",
        invited_by=invited_by_id,
    )
    session.add(student)
    await session.flush()  # нужен student.id

    for sid in sorted(subject_ids):
        session.add(StudentSubject(student_id=student.id, subject_id=sid, is_active=True))

    await session.commit()
    logger.info(
        "Ученик создан: id=%s name=%r код=%s предметы=%s до=%s",
        student.id, name, code, sorted(subject_ids), access_until,
    )
    return user, student, code


# --------------------------------------------------------------------------
# Список и карточка
# --------------------------------------------------------------------------
async def list_students(session) -> list[dict]:
    """Все ученики со сводкой для списка.

    Элемент: {id, name, streak, subject_names, access_until, linked, active}.
    Сортировка: не привязанные → истёкшие → остальные; по имени внутри групп.
    """
    rows = (
        await session.execute(
            select(Student, User).join(User, Student.user_id == User.id)
            .where(User.role == "student")
        )
    ).all()
    today = today_minsk()

    students = []
    for student, user in rows:
        # Предметы ученика (названия) — одним запросом
        subject_names = list(
            (
                await session.scalars(
                    select(Subject.name)
                    .join(StudentSubject, StudentSubject.subject_id == Subject.id)
                    .where(StudentSubject.student_id == student.id)
                    .order_by(Subject.name, Subject.id)
                )
            ).all()
        )
        # Стрики ПО ПРЕДМЕТАМ (владелец, 13.08): все current в одну строку
        streak_row = (
            await session.execute(
                select(
                    Subject.name,
                    StudentSubject.streak_current,
                    StudentSubject.streak_best,
                )
                .join(StudentSubject, StudentSubject.subject_id == Subject.id)
                .where(
                    StudentSubject.student_id == student.id,
                    StudentSubject.is_active.is_(True),
                )
                .order_by(Subject.name, Subject.id)
            )
        ).all()
        subject_streaks = [
            {"name": r[0], "current": r[1] or 0, "best": r[2] or 0}
            for r in streak_row
        ]
        linked = user.tg_id is not None
        expired = linked and student.access_until is not None and student.access_until < today
        students.append(
            {
                "id": student.id,
                "name": user.tg_full_name or "",
                "streak": user.streak_current or 0,
                "subject_names": subject_names,
                "subject_streaks": subject_streaks,
                "access_until": student.access_until,
                "linked": linked,
                "expired": expired,
                "active": user.is_active,
            }
        )

    def group_key(row: dict) -> int:
        if not row["linked"]:
            return 0
        if row["expired"]:
            return 1
        return 2

    return sorted(
        students,
        key=lambda row: (group_key(row), (row["name"] or "").lower(), row["id"]),
    )


async def get_student_card(session, student_id: int) -> dict | None:
    """Данные карточки ученика.

    Возвращает {student, user, subjects: [(Subject, StudentSubject)],
    subject_streaks: [{name, current, best}]} или None, если ученик не найден.
    """
    student = await session.get(Student, student_id)
    if student is None:
        return None
    user = await session.get(User, student.user_id)
    subjects = list(
        (
            await session.execute(
                select(Subject, StudentSubject)
                .join(StudentSubject, StudentSubject.subject_id == Subject.id)
                .where(StudentSubject.student_id == student.id)
                .order_by(Subject.name, Subject.id)
            )
        ).all()
    )
    streak_rows = (
        await session.execute(
            select(
                Subject.name,
                StudentSubject.streak_current,
                StudentSubject.streak_best,
            )
            .join(StudentSubject, StudentSubject.subject_id == Subject.id)
            .where(
                StudentSubject.student_id == student.id,
                StudentSubject.is_active.is_(True),
            )
            .order_by(Subject.name, Subject.id)
        )
    ).all()
    subject_streaks = [
        {"name": r[0], "current": r[1] or 0, "best": r[2] or 0}
        for r in streak_rows
    ]
    return {
        "student": student,
        "user": user,
        "subjects": subjects,
        "subject_streaks": subject_streaks,
    }


# --------------------------------------------------------------------------
# Действия над учеником
# --------------------------------------------------------------------------
async def extend_access(session, student_id: int, new_date: date) -> bool:
    """Продлить доступ: новая дата (остальное у ученика не меняется)."""
    student = await session.get(Student, student_id)
    if student is None:
        return False
    student.access_until = new_date
    await session.commit()
    logger.info("Доступ продлён: student_id=%s до=%s", student_id, new_date)
    return True


async def toggle_subject_active(
    session, student_id: int, subject_id: int
) -> bool | None:
    """Закрыть/открыть предмет ученика вручную. None — связки нет."""
    link = await session.get(StudentSubject, (student_id, subject_id))
    if link is None:
        return None
    link.is_active = not link.is_active
    await session.commit()
    logger.info(
        "Предмет ученика: student_id=%s subject_id=%s is_active=%s",
        student_id, subject_id, link.is_active,
    )
    return link.is_active


async def regenerate_invite_code(session, student_id: int) -> str | None:
    """Новый код приглашения. None — ученик не найден или уже привязан."""
    student = await session.get(Student, student_id)
    if student is None:
        return None
    user = await session.get(User, student.user_id)
    if user is None or user.tg_id is not None:
        return None
    code = await generate_unique_code(session)
    student.invite_code = code
    student.invite_status = "pending"
    await session.commit()
    logger.info("Новый код приглашения: student_id=%s код=%s", student_id, code)
    return code


async def set_student_active(session, student_id: int, is_active: bool) -> bool:
    """Деактивировать/активировать ученика (доступ закрыт, данные целы)."""
    student = await session.get(Student, student_id)
    if student is None:
        return False
    user = await session.get(User, student.user_id)
    if user is None:
        return False
    user.is_active = is_active
    await session.commit()
    logger.info(
        "Ученик %s: student_id=%s", "деактивирован" if not is_active else "активирован", student_id
    )
    return True


async def delete_student(session, student_id: int) -> bool:
    """Полное удаление ученика НАВСЕГДА (UX-пакет).

    Удаляется запись User — каскады FK уносят Student, StudentSubject,
    Attempt, TaskProgress и reminder_log (модель, ondelete=CASCADE).
    Освобождаются tg_id и код приглашения. False — ученик не найден
    (повторное удаление безопасно).
    """
    student = await session.get(Student, student_id)
    if student is None:
        return False
    user = await session.get(User, student.user_id)
    if user is not None:
        await session.delete(user)
    else:
        await session.delete(student)
    await session.commit()
    logger.info("Ученик удалён навсегда: student_id=%s", student_id)
    return True


# --------------------------------------------------------------------------
# Истекающие
# --------------------------------------------------------------------------
def _expiring_group(days_left: int) -> str:
    """Группа по количеству дней до конца доступа (ТЗ, раздел 6):
    «7» — 4–7 дней, «3» — 2–3 дня, «1» — 1 день, «0» — сегодня, «expired»."""
    if days_left < 0:
        return "expired"
    if days_left <= 1:
        return str(days_left)
    if days_left <= 3:
        return "3"
    return "7"


async def list_expiring(session) -> dict[str, list[dict]]:
    """Ученики с близким концом доступа, сгруппированные.

    Ключи групп: "7", "3", "1", "0", "expired".
    Элемент: {id, name, access_until, overdue_days}.
    """
    today = today_minsk()
    rows = (
        await session.execute(
            select(Student, User).join(User, Student.user_id == User.id)
            .where(User.role == "student")
        )
    ).all()

    groups: dict[str, list[dict]] = {"7": [], "3": [], "1": [], "0": [], "expired": []}
    for student, user in rows:
        if student.access_until is None:
            continue
        days_left = (student.access_until - today).days
        key = _expiring_group(days_left)
        if key == "7" and days_left > 7:
            continue  # дальше недели — не «истекающий»
        groups[key].append(
            {
                "id": student.id,
                "name": user.tg_full_name or "",
                "access_until": student.access_until,
                "overdue_days": max(0, -days_left),
            }
        )

    for key in groups:
        groups[key].sort(
            key=lambda row: ((row["name"] or "").lower(), row["id"])
        )
    return groups
