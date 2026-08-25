"""Сервисы людей и предметов для блока владельца.

Бизнес-логика вынесена из хендлеров, чтобы покрыть её юнит-тестами:
- добавить/убрать преподавателя (с предметами) и менеджера;
- списки активных людей по ролям;
- создать/скрыть предмет, переключатель активности.

Ошибки бизнес-правил — через ValueError с человеческим текстом
(хендлеры ловят и показывают его пользователю). «Убрать» = is_active=False
(данные и история сохраняются; физических удалений нет — ТЗ раздел 4).
teacher ↔ manager совмещаются: «Добавить преподавателя» менеджеру не
затирает роль менеджера — она уходит в role2, и наоборот.
"""
import logging

from sqlalchemy import delete, func, or_, select

from app.models import Subject, TeacherSubject, User

logger = logging.getLogger(__name__)

# Максимум символов в названии предмета (колонка String(128))
SUBJECT_NAME_MAX = 128


def _grant_role(user: User, role: str) -> None:
    """Назначить роль, сохраняя вторую (teacher ↔ manager совмещение).

    Первичная роль не перетирается, пока у пользователя есть вторая:
    новая роль уходит в role2. Роль «гарантированно есть» после вызова.
    """
    if user.role == role or user.role2 == role:
        return
    if user.role == "guest":
        user.role = role
        user.role2 = None
        return
    user.role2 = role


def clean_username(raw: str) -> str:
    """Убирает «@», пробелы и лишние символы из username.

    Telegram-username регистронезависим: приводим к нижнему регистру,
    чтобы «@Ivanov» и «@ivanov» искались и не создавали дублей.
    """
    return (raw or "").strip().lstrip("@").strip().lower()


def check_username_input(raw: str) -> str | None:
    """Валидация ввода владельца: начинается с @, после @ не пусто.

    Возвращает чистый username (без @) или None, если ввод некорректен.
    Латиница/прочие проверки не нужны — это юзернейм Telegram.
    """
    value = (raw or "").strip()
    if not value.startswith("@"):
        return None
    username = clean_username(value)
    if not username:
        return None
    return username


async def add_teacher(
    session, username: str, subject_ids: set[int]
) -> User:
    """Создаёт (или активирует существующего) преподавателя и связи.

    Raises ValueError с человеческим текстом при конфликте ролей.
    Telegram-запись появляется ещё без tg_id — привяжется при первом
    входе человека в бота (мидлварь по @username).
    """
    existing = await session.scalar(
        select(User).where(func.lower(User.tg_username) == username.lower())
    )
    if existing is not None:
        if existing.role == "owner":
            raise ValueError("Владельца нельзя назначить преподавателем.")
        if existing.role == "student" and existing.is_active:
            raise ValueError(
                f"Пользователь @{username} сейчас ученик — его роль нельзя "
                "заменить на преподавателя. Деактивируй его сначала "
                "(это появится в менеджерском разделе → следующий заход)."
            )
        # Активный препод, менеджер, гость или деактивированный:
        # «добавить преподавателя» = создать/обновить. Менеджеру роль
        # НЕ затирается: он получает обе (совмещение ролей владельцем).
        _grant_role(existing, "teacher")
        existing.is_active = True
        teacher = existing
    else:
        teacher = User(tg_username=clean_username(username), role="teacher", is_active=True)
        session.add(teacher)
    await session.flush()

    # Перезаписываем связи: старые предметы удаляем, новые добавляем
    await session.execute(
        delete(TeacherSubject).where(TeacherSubject.teacher_id == teacher.id)
    )
    for sid in subject_ids:
        session.add(TeacherSubject(teacher_id=teacher.id, subject_id=sid))
    await session.commit()
    logger.info("Преподаватель добавлен: id=%s @%s предметы=%s",
                teacher.id, username, sorted(subject_ids))
    return teacher


async def add_manager(session, username: str) -> User:
    """Создать (активировать) менеджера. Raises ValueError при конфликте."""
    existing = await session.scalar(
        select(User).where(func.lower(User.tg_username) == username.lower())
    )
    if existing is not None:
        if existing.role == "owner":
            raise ValueError("Владельца нельзя назначить менеджером.")
        if existing.role == "student" and existing.is_active:
            raise ValueError(
                f"Пользователь @{username} сейчас ученик — деактивируй его "
                "сначала (это появится в менеджерском разделе → следующий заход)."
            )
        # Активный менеджер, препод, гость или деактивированный:
        # «добавить менеджера» = создать/обновить. Преподу роль НЕ
        # затирается: он получает обе (совмещение ролей владельцем).
        _grant_role(existing, "manager")
        existing.is_active = True
        manager = existing
    else:
        manager = User(tg_username=clean_username(username), role="manager", is_active=True)
        session.add(manager)
    await session.commit()
    logger.info("Менеджер добавлен: id=%s @%s", manager.id, username)
    return manager


async def deactivate_person(session, user_id: int) -> bool:
    """Деактивация пользователя (любой роли). True — произошла.

    False: пользователя нет или он уже деактивирован (идемпотентность —
    повторный клик не падает и не создаёт дублей).
    """
    user = await session.get(User, user_id)
    if user is None or not user.is_active:
        return False
    user.is_active = False
    await session.commit()
    logger.info("Пользователь деактивирован: id=%s role=%s tg_id=%s",
                user.id, user.role, user.tg_id)
    return True


async def remove_role(session, user_id: int, role: str) -> str | None:
    """Снять ОДНУ роль («Убрать преподавателя/менеджера»).

    - «deactivated» — у человека была только эта роль: доступ закрыт
      (как раньше, «убрать» = деактивация);
    - «stripped» — осталась вторая роль (teacher ↔ manager совмещение):
      снимаем только её, человек продолжает работать другой ролью;
    - None — пользователя нет / неактивен / роли нет (идемпотентно).
    """
    user = await session.get(User, user_id)
    if user is None or not user.is_active or role not in user.role_set:
        return None
    if user.role == role and user.role2:
        user.role, user.role2 = user.role2, None
        await session.commit()
        logger.info("Роль %s снята (осталась %s): id=%s", role, user.role, user_id)
        return "stripped"
    if user.role == role:
        user.role2 = None
        user.is_active = False
        await session.commit()
        logger.info("Роль %s убрана (деактивация): id=%s", role, user_id)
        return "deactivated"
    user.role2 = None
    await session.commit()
    logger.info("Роль %s снята: id=%s", role, user_id)
    return "stripped"


async def list_active_people(session, role: str) -> list[User]:
    """Активные пользователи роли, по юзернейму (рабочие кнопки).

    Совмещённые (role2 == role) тоже попадают в список.
    """
    return list(
        await session.scalars(
            select(User)
            .where(
                or_(User.role == role, User.role2 == role),
                User.is_active.is_(True),
            )
            .order_by(User.tg_username.nulls_last(), User.id)
        )
    )


async def create_subject(session, name: str) -> Subject:
    """Создать предмет. Raises ValueError при дубле/пустом имени."""
    name = (name or "").strip()
    if not name:
        raise ValueError("Название не может быть пустым.")
    if len(name) > SUBJECT_NAME_MAX:
        raise ValueError(
            f"Слишком длинное название (максимум {SUBJECT_NAME_MAX} символов)."
        )
    dup = await session.scalar(select(Subject).where(Subject.name.ilike(name)))
    if dup is not None:
        raise ValueError(f"Предмет «{name}» уже существует.")
    subject = Subject(name=name, is_active=True)
    session.add(subject)
    await session.commit()
    logger.info("Предмет создан: id=%s %r", subject.id, name)
    return subject


async def delete_subject(session, subject_id: int) -> bool:
    """Удалить предмет навсегда (каскад themes/tasks/attempts/task_progress,
    teacher_subjects, student_subjects, subthemes — FK ondelete CASCADE)."""
    subject = await session.get(Subject, subject_id)
    if subject is None:
        return False
    await session.delete(subject)
    await session.commit()
    logger.info("Предмет удалён: id=%s", subject_id)
    return True


async def toggle_subject_active(session, subject_id: int) -> Subject | None:
    """Скрыть/показать предмет. None — предмет не найден (удалён)."""
    subject = await session.get(Subject, subject_id)
    if subject is None:
        return None
    subject.is_active = not subject.is_active
    await session.commit()
    logger.info("Предмет id=%s is_active=%s", subject_id, subject.is_active)
    return subject


async def list_subjects(session) -> list[Subject]:
    """Все предметы (включая скрытые) — для переключателя активности."""
    return list(
        await session.scalars(select(Subject).order_by(Subject.name, Subject.id))
    )


async def list_active_subjects(session) -> list[Subject]:
    """Только активные предметы — для мультивыбора преподавателя."""
    return list(
        await session.scalars(
            select(Subject).where(Subject.is_active.is_(True))
            .order_by(Subject.name, Subject.id)
        )
    )


async def list_guests(session) -> list[User]:
    """Гости, уже заходившие в бота (tg_id заполнен) — для привязки без username.

    Список выбирается владельцем в визарде «Добавить преподавателя/менеджера».
    """
    return list(
        await session.scalars(
            select(User)
            .where(User.role == "guest", User.tg_id.is_not(None))
            .order_by(User.created_at.desc(), User.id)
        )
    )


def parse_target_input(raw: str) -> str | int | None:
    """Разбор ввода владельца на шаге «@username или tg_id».

    - «@ivanov» / «@Ivanov» → чистый username (lowercase);
    - «123456789» (только цифры) → tg_id (int);
    - всё остальное → None (невалидно).
    """
    text = (raw or "").strip()
    if not text:
        return None
    if text.startswith("@"):
        username = clean_username(text)
        return username or None
    # Только числа и разумная длина: tg_id умещается в 19 цифр (BigInteger)
    if text.isdigit() and len(text) <= 19:
        return int(text)
    return None


async def add_teacher_by_tg_id(
    session, tg_id: int, subject_ids: set[int]
) -> User:
    """Создаёт преподавателя, привязанного к tg_id напрямую (без username).

    Если запись с этим tg_id уже есть (гость, деактивированный препод и
    т.п.) — переиспользует её, меняя роль (активные чужие роли — ValueError).
    """
    return await _upsert_staff_by_tg_id(
        session, "teacher", tg_id, subject_ids=subject_ids
    )


async def add_manager_by_tg_id(session, tg_id: int) -> User:
    """Создаёт менеджера, привязанного к tg_id (без username)."""
    return await _upsert_staff_by_tg_id(session, "manager", tg_id, subject_ids=set())


async def _upsert_staff_by_tg_id(
    session, role: str, tg_id: int, *, subject_ids: set[int]
) -> User:
    """Общая логика «добавить сотрудника по tg_id».

    Находит запись по tg_id; случаи:
    - owner → ValueError (нельзя перевести владельца);
    - активный ученик → ValueError (роль ученика защищена);
    - препод ↔ менеджер, гость, деактивированный, та же роль и нет записи →
      переиспользуем/создаём, ставим роль role, активируем,
      (для teacher) перезаписываем связи — «убрать» заранее не нужно.
    """
    existing = await session.scalar(
        select(User).where(User.tg_id == tg_id)
    )
    if existing is not None and existing.is_active and existing.role != "guest":
        if existing.role == "owner":
            raise ValueError("Владельца нельзя назначить сотрудником.")
        if existing.role == "student":
            raise ValueError(
                "Пользователь с этим tg_id сейчас ученик — его роль нельзя "
                "заменить на преподавателя. Деактивируй его сначала "
                "(это появится в менеджерском разделе → следующий заход)."
            )

    if existing is None:
        staff = User(
            tg_id=tg_id,
            role=role,
            is_active=True,
        )
        session.add(staff)
    else:
        staff = existing
        _grant_role(staff, role)
        staff.is_active = True
    await session.flush()

    if role == "teacher":
        await session.execute(
            delete(TeacherSubject).where(TeacherSubject.teacher_id == staff.id)
        )
        for sid in subject_ids:
            session.add(TeacherSubject(teacher_id=staff.id, subject_id=sid))
    await session.commit()
    logger.info(
        "%s добавлен по tg_id: id=%s tg_id=%s",
        role.capitalize(), staff.id, tg_id,
    )
    return staff