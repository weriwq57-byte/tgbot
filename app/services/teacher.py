"""Сервисы преподавателя: предметы, темы, задания (ТЗ, раздел 6).

Бизнес-логика здесь; хендлеры (app/handlers/teacher.py) — только UI.
Права: преподаватель работает только со своими предметами (teacher_subjects),
владелец — с любыми. Роль для проверки прав сервис читает сам из БД
(users.role), чтобы проверка не зависела от того, что передал хендлер.

Ошибки валидации ввода (название темы) — через ValueError с человеческим
текстом (паттерн app/services/people.py). «Удалить тему» — единственный
случай физического удаления (ТЗ раздел 4: явный случай, каскад через FK).
"""
import logging

from sqlalchemy import func, select

from app.models import Subject, Subtheme, Task, TeacherSubject, Theme, User

logger = logging.getLogger(__name__)

# Максимум символов в названии темы (колонка String(200))
THEME_TITLE_MAX = 200
SUBTITLE_MAX = 200  # подтема — та же колонка String(200)


async def _is_owner(session, user_id: int) -> bool:
    user = await session.get(User, user_id)
    return user is not None and user.role == "owner"


def check_theme_title(raw: str) -> str:
    """Валидация названия темы. Raises ValueError с человеческим текстом."""
    return _check_title(raw, THEME_TITLE_MAX, "Название")


def check_subtheme_title(raw: str) -> str:
    """Валидация названия подтемы. Raises ValueError с человеческим текстом."""
    return _check_title(raw, SUBTITLE_MAX, "Название подтемы")


def _check_title(raw: str, max_len: int, what: str) -> str:
    title = (raw or "").strip()
    if not title:
        raise ValueError(f"{what} не может быть пустым.")
    if len(title) > max_len:
        raise ValueError(
            f"Слишком длинное {what.lower()} (максимум {max_len} символов)."
        )
    if title.startswith("/"):
        raise ValueError(f"{what} не может начинаться с «/» — это команда.")
    return title


async def can_manage_subject(session, user_id: int, subject_id: int) -> bool:
    """Может ли пользователь работать с предметом.

    Препод — только свои предметы (teacher_subjects), владелец — любой.
    Скрытый предмет (is_active=False) недоступен никому — устаревшие
    кнопки по скрытому предмету не работают (раздел 0, дефект 2).
    """
    subject = await session.get(Subject, subject_id)
    if subject is None or not subject.is_active:
        return False
    if await _is_owner(session, user_id):
        return True
    link = await session.get(TeacherSubject, (user_id, subject_id))
    return link is not None


async def list_teacher_subjects(session, teacher_id: int) -> list[dict]:
    """Предметы с темами для экрана «📚 Мои предметы».

    Для владельца — все активные предметы; для преподавателя — только
    свои (teacher_subjects). Элемент:
    {"subject": Subject, "themes": [(Theme, count_active_tasks)]}.
    Сортировка: предметы по имени, темы по порядку (order, id).
    """
    is_owner = await _is_owner(session, teacher_id)
    query = select(Subject).where(Subject.is_active.is_(True))
    if not is_owner:
        query = query.join(
            TeacherSubject, TeacherSubject.subject_id == Subject.id
        ).where(TeacherSubject.teacher_id == teacher_id)
    subjects = (
        await session.scalars(query.order_by(Subject.name, Subject.id))
    ).all()

    result = []
    for subject in subjects:
        themes = (
            await session.scalars(
                select(Theme)
                .where(Theme.subject_id == subject.id)
                .order_by(Theme.order, Theme.id)
            )
        ).all()
        result.append(
            {
                "subject": subject,
                "themes": [
                    (theme, await _count_active_tasks(session, theme.id))
                    for theme in themes
                ],
            }
        )
    return result


async def _count_active_tasks(session, theme_id: int) -> int:
    return await session.scalar(
        select(func.count(Task.id)).where(
            Task.theme_id == theme_id, Task.is_active.is_(True)
        )
    ) or 0


async def count_theme_tasks(session, theme_id: int) -> int:
    """Все задания темы (включая скрытые) — для счётчика в меню темы."""
    return await session.scalar(
        select(func.count(Task.id)).where(Task.theme_id == theme_id)
    ) or 0


async def add_theme(session, teacher_id: int, subject_id: int, title: str) -> Theme:
    """Создаёт тему: закрытой по умолчанию, режим 'sequential'.

    Raises ValueError: нет прав на предмет или некорректное название.
    """
    title = check_theme_title(title)
    if not await can_manage_subject(session, teacher_id, subject_id):
        raise ValueError("У тебя нет доступа к этому предмету.")
    theme = Theme(
        subject_id=subject_id,
        title=title,
        is_open=False,
        mode="sequential",
        created_by=teacher_id,
    )
    session.add(theme)
    await session.commit()
    logger.info(
        "Тема создана: id=%s subject_id=%s %r (скрыта)",
        theme.id, subject_id, title,
    )
    return theme


async def get_theme_for_teacher(
    session, teacher_id: int, theme_id: int
) -> Theme | None:
    """Тема с проверкой прав. None — нет темы или нет доступа к ней."""
    theme = await session.get(Theme, theme_id)
    if theme is None:
        return None
    if await _is_owner(session, teacher_id):
        return theme
    if await can_manage_subject(session, teacher_id, theme.subject_id):
        return theme
    return None


async def toggle_theme_open(session, theme_id: int) -> bool | None:
    """Открыть/закрыть тему. Новое состояние is_open или None (тема исчезла)."""
    theme = await session.get(Theme, theme_id)
    if theme is None:
        return None
    theme.is_open = not theme.is_open
    await session.commit()
    logger.info("Тема id=%s is_open=%s", theme_id, theme.is_open)
    return theme.is_open


async def toggle_theme_mode(session, theme_id: int) -> str | None:
    """Переключить режим темы sequential ↔ random («🎲 Открыть все»).

    Возвращает НОВЫЙ режим или None (тема исчезла).
    """
    theme = await session.get(Theme, theme_id)
    if theme is None:
        return None
    theme.mode = "random" if theme.mode == "sequential" else "sequential"
    await session.commit()
    logger.info("Тема id=%s mode=%s", theme_id, theme.mode)
    return theme.mode


async def rename_theme(session, theme_id: int, title: str) -> bool:
    """Переименовать тему (валидация названия). False — темы нет."""
    title = check_theme_title(title)
    theme = await session.get(Theme, theme_id)
    if theme is None:
        return False
    theme.title = title
    await session.commit()
    logger.info("Тема переименована: id=%s %r", theme_id, title)
    return True


async def delete_theme(session, theme_id: int) -> bool:
    """Удалить тему вместе с заданиями (каскад через FK ondelete CASCADE:
    subthemes, tasks, а от них — attempts и task_progress)."""
    theme = await session.get(Theme, theme_id)
    if theme is None:
        return False
    await session.delete(theme)
    await session.commit()
    logger.info("Тема удалена: id=%s", theme_id)
    return True


# --------------------------------------------------------------------------
# Подтемы (текущий заход): группы заданий внутри темы
# --------------------------------------------------------------------------
async def list_theme_subthemes(session, theme_id: int) -> list[dict]:
    """Подтемы темы по порядку. Элемент: {"subtheme": Subtheme, "count": N}
    — N заданий подтемы (включая скрытые)."""
    subthemes = (
        await session.scalars(
            select(Subtheme)
            .where(Subtheme.theme_id == theme_id)
            .order_by(Subtheme.order, Subtheme.id)
        )
    ).all()
    result = []
    for subtheme in subthemes:
        count = await session.scalar(
            select(func.count(Task.id)).where(Task.subtheme_id == subtheme.id)
        ) or 0
        result.append({"subtheme": subtheme, "count": count})
    return result


async def get_subtheme_for_teacher(
    session, teacher_id: int, subtheme_id: int
) -> Subtheme | None:
    """Подтема с проверкой прав (через её тему). None — нет/нет доступа."""
    subtheme = await session.get(Subtheme, subtheme_id)
    if subtheme is None:
        return None
    theme = await get_theme_for_teacher(session, teacher_id, subtheme.theme_id)
    if theme is None:
        return None
    return subtheme


async def add_subtheme(
    session, teacher_id: int, theme_id: int, title: str
) -> Subtheme:
    """Создаёт подтему в теме (order = следующий по порядку).

    Raises ValueError: нет доступа к теме или некорректное название.
    """
    title = check_subtheme_title(title)
    if await get_theme_for_teacher(session, teacher_id, theme_id) is None:
        raise ValueError("У тебя нет доступа к этой теме.")
    max_order = (
        await session.scalar(
            select(func.max(Subtheme.order)).where(Subtheme.theme_id == theme_id)
        )
    )
    if max_order is None:
        max_order = -1
    subtheme = Subtheme(
        theme_id=theme_id, title=title, order=max_order + 1
    )
    session.add(subtheme)
    await session.commit()
    logger.info("Подтема создана: id=%s theme_id=%s %r", subtheme.id, theme_id, title)
    return subtheme


async def rename_subtheme(session, subtheme_id: int, title: str) -> bool:
    """Переименовать подтему (валидация названия). False — подтемы нет."""
    title = check_subtheme_title(title)
    subtheme = await session.get(Subtheme, subtheme_id)
    if subtheme is None:
        return False
    subtheme.title = title
    await session.commit()
    logger.info("Подтема переименована: id=%s %r", subtheme_id, title)
    return True


async def count_subtheme_tasks(session, subtheme_id: int) -> int:
    """Все задания подтемы (включая скрытые) — для подтверждения удаления."""
    return await session.scalar(
        select(func.count(Task.id)).where(Task.subtheme_id == subtheme_id)
    ) or 0


async def delete_subtheme(session, subtheme_id: int) -> bool:
    """Удалить подтему вместе с её заданиями (каскад через FK ondelete
    CASCADE: tasks, а от них — attempts и task_progress)."""
    subtheme = await session.get(Subtheme, subtheme_id)
    if subtheme is None:
        return False
    await session.delete(subtheme)
    await session.commit()
    logger.info("Подтема удалена: id=%s", subtheme_id)
    return True


async def list_theme_tasks(session, theme_id: int) -> list[dict]:
    """Задания темы по порядку. Элемент: {"task": Task, "subtheme": Subtheme | None}."""
    rows = (
        await session.execute(
            select(Task, Subtheme)
            .outerjoin(Subtheme, Subtheme.id == Task.subtheme_id)
            .where(Task.theme_id == theme_id)
            .order_by(Task.order, Task.id)
        )
    ).all()
    return [{"task": task, "subtheme": subtheme} for task, subtheme in rows]


# --------------------------------------------------------------------------
# Задания (Заход 5, визард «Добавить задание»)
# --------------------------------------------------------------------------
# Минимум/максимум вариантов и лимиты длины (проверяются на шагах визарда)
OPTIONS_MIN = 2
OPTIONS_MAX = 4
OPTION_TEXT_MAX = 200
QUESTION_TEXT_MAX = 1000


def build_options_json(options: list[str], correct_index: int) -> list[dict]:
    """Собирает JSON вариантов задания: [{"t": текст, "c": правильный}, ...].

    Валидация: от 2 до 4 непустых вариантов (≤200 симв.), индекс
    правильного — в диапазоне. Raises ValueError с человеческим текстом.
    """
    if not (OPTIONS_MIN <= len(options) <= OPTIONS_MAX):
        raise ValueError(f"Нужно от {OPTIONS_MIN} до {OPTIONS_MAX} вариантов.")
    if not (0 <= correct_index < len(options)):
        raise ValueError("Некорректный номер правильного варианта.")
    result = []
    for i, text in enumerate(options):
        text = (text or "").strip()
        if not text:
            raise ValueError("Вариант не может быть пустым.")
        if len(text) > OPTION_TEXT_MAX:
            raise ValueError(
                f"Слишком длинный вариант (максимум {OPTION_TEXT_MAX} символов)."
            )
        result.append({"t": text, "c": i == correct_index})
    return result


async def create_task(
    session,
    theme_id: int,
    question_text: str | None,
    question_photo_id: str | None,
    options: list[dict],
    feedback_text: str | None,
    feedback_photo_id: str | None,
    created_by: int,
    subtheme_id: int | None = None,
) -> Task:
    """Создаёт задание в теме (order = следующий по порядку).

    subtheme_id — подтема из шага визарда (None — на тему напрямую).
    """
    max_order = (
        await session.scalar(
            select(func.max(Task.order)).where(Task.theme_id == theme_id)
        )
    )
    if max_order is None:
        max_order = -1
    task = Task(
        theme_id=theme_id,
        subtheme_id=subtheme_id,
        question_text=question_text,
        question_photo_id=question_photo_id,
        options=options,
        feedback_text=feedback_text,
        feedback_photo_id=feedback_photo_id,
        order=max_order + 1,
        is_active=True,
        created_by=created_by,
    )
    session.add(task)
    await session.commit()
    logger.info(
        "Задание создано: id=%s theme_id=%s subtheme_id=%s (order=%s)",
        task.id, theme_id, subtheme_id, task.order,
    )
    return task


async def update_task(
    session,
    task_id: int,
    question_text: str | None,
    question_photo_id: str | None,
    options: list[dict],
    feedback_text: str | None,
    feedback_photo_id: str | None,
    updated_by: int,
) -> Task | None:
    """Обновляет задание в режиме «✏️ Редактировать» (UPDATE, не INSERT).

    order/is_active/created_by и прогресс учеников не трогаются.
    Возвращает None, если задания больше нет.
    """
    task = await session.get(Task, task_id)
    if task is None:
        return None
    task.question_text = question_text
    task.question_photo_id = question_photo_id
    task.options = options
    task.feedback_text = feedback_text
    task.feedback_photo_id = feedback_photo_id
    task.created_by = updated_by
    await session.commit()
    logger.info("Задание обновлено: id=%s", task.id)
    return task


async def get_task_for_teacher(
    session, teacher_id: int, task_id: int
) -> Task | None:
    """Задание с проверкой прав (препод — через свой предмет, владелец — любой)."""
    task = await session.get(Task, task_id)
    if task is None:
        return None
    theme = await get_theme_for_teacher(session, teacher_id, task.theme_id)
    if theme is None:
        return None
    return task


async def toggle_task_active(session, task_id: int) -> bool | None:
    """Скрыть/показать задание. Новое состояние is_active или None (нет задания)."""
    task = await session.get(Task, task_id)
    if task is None:
        return None
    task.is_active = not task.is_active
    await session.commit()
    logger.info("Задание id=%s is_active=%s", task_id, task.is_active)
    return task.is_active


async def delete_task(session, task_id: int) -> bool:
    """Удалить задание (FK-каскад чистит attempts и task_progress)."""
    task = await session.get(Task, task_id)
    if task is None:
        return False
    await session.delete(task)
    await session.commit()
    logger.info("Задание удалено: id=%s", task_id)
    return True