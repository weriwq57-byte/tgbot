"""Сервисы ученика (ТЗ разделы 6, 7, 10): привязка по коду, меню,
выдача заданий, проверка ответов, итог темы, повтор.

Возвращают словари со статусом — тексты статусов живут в хендлере.

Правило «текущее досматривает»: доступ (can_access) проверяется при
выдаче (вход в тему, «Ещё задание», «Повторить тему»), НЕ при ответе.
"""
import hashlib
import random

from sqlalchemy import delete, func, select, true
from sqlalchemy.exc import IntegrityError

from app.utils.dates import today_minsk

from app.models import (
    Attempt,
    Student,
    StudentSubject,
    Subject,
    Task,
    TaskProgress,
    TeacherSubject,
    Theme,
    User,
)
from app.services import access, reactions, streaks

LETTERS = "АБВГ"

# Статусы привязки (ТЗ раздел 7)
BIND_CODE_NOT_FOUND = "code_not_found"
BIND_ALREADY_ACTIVATED = "already_activated"
BIND_TG_ALREADY_BOUND = "tg_already_bound"
BIND_READY = "ready"

# Статусы выдачи задания (ТЗ раздел 10)
TASK_ISSUE_OK = "ok"
TASK_ISSUE_EMPTY = "empty"
TASK_ISSUE_ALL_DONE = "all_done"
TASK_ISSUE_EXPIRED = "expired"
TASK_ISSUE_THEME_CLOSED = "theme_closed"
TASK_ISSUE_NOT_FOR_YOU = "not_for_you"
TASK_ISSUE_NOT_FOUND = "not_found"

# Статусы ответа
ANSWER_OK = "ok"
ANSWER_STALE = "stale"
ANSWER_GONE = "gone"

# Режим «🔁 Ошибки»: ошибок больше нет
ERRORS_DONE = "errors_done"


def issue_day() -> int:
    """Метка дня выдачи: today_minsk().toordinal() (глобальный номер дня).

    Монотонно растёт без повторений между годами (в отличие от номера
    дня в году). Кодируется в кнопки ответа task:{id}:ans:{i}:{seq}:{doy} —
    кнопка, выпущенная в другой день, считается устаревшей (перемешивание
    вариантов зависит от даты: perm вчера ≠ perm сегодня, позиция «А»
    вчера может оказаться другой опцией сегодня).
    """
    return today_minsk().toordinal()


def options_permutation(
    task_id: int, student_id: int, n_options: int, day=None
) -> list[int]:
    """Детерминированное перемешивание позиций вариантов для карточки.

    Порядок зависит от (task_id, student_id, дата Минск): в один день
    ученик видит один и тот же порядок (стабильно для устаревших кнопок
    и seq-сверки), в разные дни — разный (не заучивает позицию «А»).
    БД-порядок options НЕ меняется: карточка показывает options[perm[i]],
    проверка ответа идёт по options[perm[answer_index]] (кнопка несёт
    ПОЗИЦИЮ в карточке, как раньше).
    """
    day = day or today_minsk()
    digest = hashlib.sha1(
        f"{task_id}:{student_id}:{day.isoformat()}".encode("utf-8")
    ).digest()
    seed = int.from_bytes(digest[:8], "big")
    perm = list(range(n_options))
    random.Random(seed).shuffle(perm)
    return perm


def prepare_code(raw: str | None) -> str | None:
    """Нормализация кода приглашения: пробелы убраны, верхний регистр."""
    if not raw:
        return None
    code = raw.strip().upper()
    return code or None


async def _get_student(session, user_id: int) -> Student | None:
    return await session.scalar(
        select(Student).where(Student.user_id == user_id)
    )


# Роли, которые решают задания «для себя»: босс (все предметы) и
# преподаватель (свои предметы). Для них заводится скрытый «теневой»
# профиль ученика (без кода и даты доступа) — иначе попытки и прогресс
# некуда писать (FK attempts/task_progress → students.id).
SELF_SOLVER_ROLES = ("owner", "teacher")


async def _solver(session, user_id: int) -> tuple[Student | None, User | None]:
    """Решатель: обычный ученик или теневой профиль staff (создаётся лениво)."""
    user = await session.get(User, user_id)
    if user is None:
        return None, None
    student = await _get_student(session, user_id)
    if student is None and user.role in SELF_SOLVER_ROLES:
        student = Student(user_id=user_id, access_until=None, invite_code=None)
        session.add(student)
        try:
            await session.commit()  # своя транзакция: профиль должен пережить сессию
        except IntegrityError:
            # Гонка: два одновременных входа в тему создали теневой профиль
            # (students.user_id unique). Откатываемся и берём существующий;
            # user после rollback протухает — перезагружаем свежим.
            await session.rollback()
            user = await session.get(User, user_id)
            student = await _get_student(session, user_id)
    return student, user


async def _teacher_owns_theme(session, teacher_id: int, theme_id: int) -> bool:
    """Принадлежит ли тема преподавателю (teacher_subjects → subject)."""
    theme = await session.get(Theme, theme_id)
    if theme is None:
        return False
    subj = await session.scalar(
        select(TeacherSubject).where(
            TeacherSubject.teacher_id == teacher_id,
            TeacherSubject.subject_id == theme.subject_id,
        )
    )
    return subj is not None


async def _staff_theme_gate(
    session, user: User, theme_id: int
) -> str | None:
    """Проверки staff перед выдачей. None — можно выдавать, иначе статус.

    - Тема обязана существовать (битая кнопка → «Тема не найдена», а не
      «Заданий пока нет»).
    - Препод — только свои предметы; после отвязки предмета старые кнопки
      гаснут («Предмет больше не доступен»).
    """
    if await session.get(Theme, theme_id) is None:
        return TASK_ISSUE_NOT_FOUND
    if user.role == "teacher" and not await _teacher_owns_theme(
        session, user.id, theme_id
    ):
        return TASK_ISSUE_NOT_FOR_YOU
    return None


async def _theme_rows_for_owner(session) -> list[Subject]:
    """Босс: все активные предметы."""
    return (
        await session.scalars(
            select(Subject)
            .where(Subject.is_active.is_(True))
            .order_by(Subject.name.asc(), Subject.id.asc())
        )
    ).all()


async def _theme_rows_for_teacher(session, user_id: int) -> list[Subject]:
    """Препод: только свои предметы (teacher_subjects)."""
    return (
        await session.scalars(
            select(Subject)
            .join(TeacherSubject, TeacherSubject.subject_id == Subject.id)
            .where(
                TeacherSubject.teacher_id == user_id,
                Subject.is_active.is_(True),
            )
            .order_by(Subject.name.asc(), Subject.id.asc())
        )
    ).all()


async def _count_attempts_theme(session, student_id: int, theme_id: int) -> int:
    attempts = (
        select(func.count())
        .select_from(Attempt)
        .join(Task, Task.id == Attempt.task_id)
        .where(Attempt.student_id == student_id, Task.theme_id == theme_id)
        .scalar_subquery()
    )
    return (await session.scalar(select(attempts))) or 0


async def _theme_active_tasks(session, theme_id: int) -> list[Task]:
    return list(
        (
            await session.scalars(
                select(Task)
                .where(Task.theme_id == theme_id, Task.is_active.is_(True))
                .order_by(Task.order.asc(), Task.id.asc())
            )
        ).all()
    )


async def _theme_progress(
    session, student_id: int, theme_id: int
) -> dict:
    """{solved, total, remaining} по активным заданиям темы."""
    tasks = await _theme_active_tasks(session, theme_id)
    total = len(tasks)
    if not total:
        return {"solved": 0, "total": 0, "remaining": 0}
    solved = await session.scalar(
        select(func.count())
        .select_from(TaskProgress)
        .join(Task, Task.id == TaskProgress.task_id)
        .where(
            TaskProgress.student_id == student_id,
            Task.theme_id == theme_id,
            Task.is_active.is_(True),
        )
    )
    solved = solved or 0
    return {"solved": solved, "total": total, "remaining": total - solved}


async def _progress_bulk(
    session, student_id: int, theme_ids: list[int]
) -> dict[int, dict]:
    """Прогресс по пачке тем одним заходом (3 запроса вместо per-theme).

    {theme_id: {solved, total, remaining, wrong}} по АКТИВНЫМ заданиям.
    """
    if not theme_ids:
        return {}
    total_rows = (
        await session.execute(
            select(Task.theme_id, func.count())
            .where(
                Task.theme_id.in_(theme_ids),
                Task.is_active.is_(True),
            )
            .group_by(Task.theme_id)
        )
    ).all()
    total_by = dict(total_rows)
    progress_rows = (
        await session.execute(
            select(Task.theme_id, TaskProgress.status)
            .select_from(TaskProgress)
            .join(Task, Task.id == TaskProgress.task_id)
            .where(
                TaskProgress.student_id == student_id,
                Task.theme_id.in_(theme_ids),
                Task.is_active.is_(True),
            )
        )
    ).all()
    solved_by = dict.fromkeys(theme_ids, 0)
    wrong_by = dict.fromkeys(theme_ids, 0)
    for tid, status in progress_rows:
        solved_by[tid] += 1
        if status == "wrong":
            wrong_by[tid] += 1
    result = {}
    for tid in theme_ids:
        total = total_by.get(tid, 0)
        solved = solved_by[tid]
        result[tid] = {
            "solved": solved,
            "total": total,
            "remaining": total - solved,
            "wrong": wrong_by[tid],
        }
    return result


# --------------------------------------------------------------------------
# Привязка (ТЗ раздел 7)
# --------------------------------------------------------------------------
async def _user_by_tg_id(session, tg_id: int) -> User | None:
    """Пользователь по Telegram id (не путать с первичным ключом id)."""
    return await session.scalar(select(User).where(User.tg_id == tg_id))


def _tg_id_is_free(user: User | None) -> bool:
    """Можно ли перепривязать tg_id новому ученику.

    Свободен: пользователя нет, активный гость (освобождается при
    привязке, ТЗ 7 п.5) или ЛЮБОЙ ДЕАКТИВИРОВАННЫЙ пользователь
    (кикнули препод/менеджера/ученика/гостя — владелец выдал новый
    код, старая запись удаляется каскадом Student/Attempt/TaskProgress/
    TeacherSubject). Отказ — только АКТИВНОМУ аккаунту (кроме гостя):
    чужой профиль не трогаем.
    """
    if user is None:
        return True
    if user.role == "guest":
        return True  # гость освобождается при привязке (ТЗ 7 п.5)
    return not user.is_active


async def bind_by_code(session, tg_id: int, raw_code: str) -> dict:
    """Превращает код приглашения в профиль ученика.

    Статусы: code_not_found / already_activated / tg_already_bound /
    ready (с полями student + occupant_role — роль того, кому сейчас
    принадлежит tg_id, для предупреждения при привязке). Привязку не
    выполняет — только проверку, подтверждение делает confirm_bind
    (кнопкой «Да, это я»).
    """
    code = prepare_code(raw_code)
    if code is None:
        return {"status": BIND_CODE_NOT_FOUND}
    student = await session.scalar(
        select(Student)
        .where(Student.invite_code == code)
        .order_by(Student.id.desc())  # последний, если вдруг дубли
    )
    if student is None:
        return {"status": BIND_CODE_NOT_FOUND}
    if student.invite_status != "pending":
        return {"status": BIND_ALREADY_ACTIVATED}
    user = await _user_by_tg_id(session, tg_id)
    if not _tg_id_is_free(user):
        return {"status": BIND_TG_ALREADY_BOUND}
    return {
        "status": BIND_READY,
        "student": student,
        "occupant_role": user.role if user is not None else None,
    }


async def confirm_bind(session, tg_id: int, raw_code: str) -> bool:
    """Подтверждает привязку: ученик получает tg_id, код — activated.

    Прежний владелец tg_id удаляется, если он свободен по _tg_id_is_free:
    гость (ТЗ 7 п.5), ДЕАКТИВИРОВАННЫЙ ученик/гость/staff (кикнули,
    выдали новый код) — вместе с историей (каскады Student/Attempt/
    TaskProgress/TeacherSubject). Если tg_id занят АКТИВНЫМ аккаунтом —
    False (менять чужой профиль нельзя). Коммитит.
    """
    code = prepare_code(raw_code)
    if code is None:
        return False
    student = await session.scalar(
        select(Student).where(Student.invite_code == code)
    )
    if student is None or student.invite_status != "pending":
        return False
    occupant = await _user_by_tg_id(session, tg_id)
    if not _tg_id_is_free(occupant):
        return False
    if occupant is not None:  # гость или деактивированный — освобождаем tg_id
        await session.delete(occupant)
        await session.flush()
    user = await session.get(User, student.user_id)
    if user is None:
        return False
    user.tg_id = tg_id
    student.invite_status = "activated"
    await session.commit()
    return True


# --------------------------------------------------------------------------
# Меню ученика (ТЗ раздел 6)
# --------------------------------------------------------------------------
async def student_menu(session, user_id: int) -> dict | None:
    """Данные для «📚 Мои предметы» и меню ученика.

    Возвращает None, если к user_id нет записи ученика.
    subjects: [{subject, themes: [{theme, progress: {solved,total,remaining},
    all_done}]}].

    Обычный ученик: только активные предметы (StudentSubject) и открытые
    темы. Staff (владелец/преподаватель, теневой профиль): без ограничений
    доступа — босс видит ВСЕ предметы, препод свои (teacher_subjects);
    темы показываются все (свои задания можно решать, даже закрытые).
    """
    student, user = await _solver(session, user_id)
    if student is None or user is None:
        return None

    if user.role == "owner":
        subject_rows = await _theme_rows_for_owner(session)
    elif user.role == "teacher":
        subject_rows = await _theme_rows_for_teacher(session, user_id)
    else:
        subject_rows = (
            await session.execute(
                select(Subject)
                .join(StudentSubject, StudentSubject.subject_id == Subject.id)
                .where(
                    StudentSubject.student_id == student.id,
                    Subject.is_active.is_(True),
                    StudentSubject.is_active.is_(True),
                )
                .order_by(Subject.name.asc(), Subject.id.asc())
            )
        ).scalars().all()

    subjects = []
    by_theme = {}
    theme_items_by_subject: dict[int, list] = {}
    all_theme_ids = []
    for subj in subject_rows:
        themes = (
            await session.scalars(
                select(Theme)
                .where(
                    Theme.subject_id == subj.id,
                    (
                        Theme.is_open.is_(True)
                        if user.role not in SELF_SOLVER_ROLES
                        else true()
                    ),
                )
                .order_by(Theme.order.asc(), Theme.id.asc())
            )
        ).all()
        theme_items_by_subject[subj.id] = themes
        all_theme_ids.extend(t.id for t in themes)
    progress_map = await _progress_bulk(session, student.id, all_theme_ids)
    for subj in subject_rows:
        theme_items = []
        for theme in theme_items_by_subject[subj.id]:
            progress = progress_map[theme.id]
            all_done = progress["total"] > 0 and progress["remaining"] == 0
            theme_items.append(
                {
                    "theme": theme,
                    "progress": progress,
                    "all_done": all_done,
                    "wrong_count": progress["wrong"],
                }
            )
            by_theme[theme.id] = progress
        subjects.append({"subject": subj, "themes": theme_items})

    return {
        "user": user,
        "student": student,
        "subjects": subjects,
        "by_theme": by_theme,
    }


# --------------------------------------------------------------------------
# Стрики по предметам (доработка владельца 13.08)
# --------------------------------------------------------------------------
async def subject_streaks(session, user_id: int) -> list[dict]:
    """Стрики ПО ПРЕДМЕТАМ ученика: [{subject_id, name, current, best}].

    Только активные предметы (StudentSubject.is_active), порядок по
    названию. [] — ученика нет или предметов нет.
    """
    student = await _get_student(session, user_id)
    if student is None:
        return []
    rows = (
        await session.execute(
            select(
                Subject.id,
                Subject.name,
                StudentSubject.streak_current,
                StudentSubject.streak_best,
            )
            .join(StudentSubject, StudentSubject.subject_id == Subject.id)
            .where(
                StudentSubject.student_id == student.id,
                StudentSubject.is_active.is_(True),
            )
            .order_by(Subject.name.asc(), Subject.id.asc())
        )
    ).all()
    return [
        {
            "subject_id": row[0],
            "name": row[1],
            "current": row[2],
            "best": row[3],
        }
        for row in rows
    ]


# --------------------------------------------------------------------------
# Выдача заданий (ТЗ раздел 10)
# --------------------------------------------------------------------------
async def _theme_access_status(
    session, user: User, student: Student, theme_id: int
) -> str | None:
    """Доступ к теме для ВЫДАЧИ; None — можно выдавать.

    Возвращает статус блокировки (expired / theme_closed / not_for_you /
    not_found). Staff решают без ограничений доступа, но с проверкой
    темы и принадлежности предмета (см. issue_task).
    """
    if user.role in SELF_SOLVER_ROLES:
        return await _staff_theme_gate(session, user, theme_id)
    ok, reason = await access.can_access(session, student.id, theme_id)
    if not ok:
        status_map = {
            access.REASON_ACCESS_EXPIRED: TASK_ISSUE_EXPIRED,
            access.REASON_THEME_CLOSED: TASK_ISSUE_THEME_CLOSED,
            access.REASON_THEME_NOT_FOUND: TASK_ISSUE_NOT_FOUND,
        }
        return status_map.get(reason, TASK_ISSUE_NOT_FOR_YOU)
    return None


def _task_perm(task: Task, student_id: int) -> list[int]:
    """Позиции вариантов для карточки; [] — опции битые (не выдавать)."""
    try:
        return options_permutation(task.id, student_id, len(task.options or []))
    except (TypeError, ValueError):
        return []


async def issue_task(session, user_id: int, theme_id: int) -> dict:
    """Выдаёт первое нерешённое задание темы (или итог, статусы блокировок).

    ok: {status, task, seq, theme, progress, doy} (+ random_mode для
    тем в режиме «🎲 Открыть все»: случайное активное задание, повторы
    возможны, итога нет).
    all_done: {status, summary, streak_current, streak_best}
    блокировки: {status} — expired / theme_closed / not_for_you / not_found.
    seq — число ответов ученика по теме на момент выдачи; включается
    в кнопки ответа как защита от устаревших карточек. doy — день выдачи
    (issue_day), кодируется в кнопки ответа: perm зависит от даты.
    """
    student, user = await _solver(session, user_id)
    if student is None or user is None:
        return {"status": TASK_ISSUE_NOT_FOR_YOU}
    blocked = await _theme_access_status(session, user, student, theme_id)
    if blocked is not None:
        return {"status": blocked}

    tasks = await _theme_active_tasks(session, theme_id)
    progress = await _theme_progress(session, student.id, theme_id)
    if not tasks:
        return {"status": TASK_ISSUE_EMPTY}
    theme = await session.get(Theme, theme_id)
    if theme is not None and theme.mode == "random":
        # «🎲 Открыть все»: случайное активное задание (повторы возможны),
        # без прогресса/итога — «Следующее задание» даёт новую случайную.
        seq = await _count_attempts_theme(session, student.id, theme_id)
        task = random.choice(tasks)
        return {
            "status": TASK_ISSUE_OK,
            "task": task,
            "seq": seq,
            "theme": theme,
            "progress": progress,
            "perm": _task_perm(task, student.id),
            "doy": issue_day(),
            "random_mode": True,
        }
    if progress["remaining"] == 0:
        summary = await theme_summary(session, student.id, theme_id)
        theme = await session.get(Theme, theme_id)
        subject_id = theme.subject_id if theme is not None else None
        # Стрик по предмету темы (StudentSubject); нет связки — без стрика
        link = (
            await session.get(StudentSubject, (student.id, subject_id))
            if subject_id is not None
            else None
        )
        return {
            "status": TASK_ISSUE_ALL_DONE,
            "summary": summary,
            "theme_id": theme_id,
            "subject_id": subject_id,
            "streak_current": link.streak_current if link else 0,
            "streak_best": link.streak_best if link else 0,
            # рекорд побит именно сегодня (факт из register_solved)
            "record_broken": (
                streaks.record_broken_today((student.id, subject_id))
                if subject_id is not None
                else False
            ),
        }

    progress_rows = (
        await session.scalars(
            select(TaskProgress.task_id).where(
                TaskProgress.student_id == student.id
            )
        )
    ).all()
    solved_ids = set(progress_rows)
    task = next(t for t in tasks if t.id not in solved_ids)
    seq = await _count_attempts_theme(session, student.id, theme_id)
    return {
        "status": TASK_ISSUE_OK,
        "task": task,
        "seq": seq,
        "theme": theme,
        "progress": progress,
        "perm": _task_perm(task, student.id),
        "doy": issue_day(),
    }


async def issue_next_wrong(session, user_id: int, theme_id: int) -> dict:
    """«🔁 Ошибки»: выдаёт первое нерешённое wrong-задание темы.

    ok: {status, task, seq, theme, progress, perm, wrong_remaining}
    errors_done: {status, theme_id} — неправильных больше не осталось.
    Блокировки — те же, что у issue_task (can_access при выдаче).
    """
    student, user = await _solver(session, user_id)
    if student is None or user is None:
        return {"status": TASK_ISSUE_NOT_FOR_YOU}
    blocked = await _theme_access_status(session, user, student, theme_id)
    if blocked is not None:
        return {"status": blocked}

    wrong_tasks = (
        await session.scalars(
            select(Task)
            .join(TaskProgress, TaskProgress.task_id == Task.id)
            .where(
                TaskProgress.student_id == student.id,
                Task.theme_id == theme_id,
                Task.is_active.is_(True),
                TaskProgress.status == "wrong",
            )
            .order_by(Task.order.asc(), Task.id.asc())
        )
    ).all()
    if not wrong_tasks:
        return {
            "status": ERRORS_DONE,
            "theme_id": theme_id,
            "summary": await theme_summary(session, student.id, theme_id),
        }

    task = wrong_tasks[0]
    seq = await _count_attempts_theme(session, student.id, theme_id)
    theme = await session.get(Theme, theme_id)
    progress = await _theme_progress(session, student.id, theme_id)
    return {
        "status": TASK_ISSUE_OK,
        "task": task,
        "seq": seq,
        "theme": theme,
        "progress": progress,
        "perm": _task_perm(task, student.id),
        "wrong_remaining": len(wrong_tasks),
        "doy": issue_day(),
    }


# --------------------------------------------------------------------------
# Ответ (ТЗ разделы 9, 10)
# --------------------------------------------------------------------------
async def check_answer(
    session, user_id: int, task_id: int, answer_index: int, seq: int, doy=None
) -> dict:
    """Проверяет ответ, пишет Attempt/TaskProgress, обновляет стрик/реакцию.

    stale — карточка устарела (кнопка с другой seq, другой день выдачи
    или индекс вне вариантов); gone — задания/темы больше нет или ученика
    нет. Для stale/gone ничего не пишется. Для ok пишется попытка всегда
    и прогресс — только если задания ещё не было в TaskProgress. Повторный
    ПРАВИЛЬНЫЙ ответ по заданию со статусом wrong переводит его в done
    (режим «🔁 Ошибки»).

    doy — день выдачи из кнопки (issue_day): не совпал с сегодняшним →
    stale (перемешивание вариантов зависит от даты). None — не проверять
    (прямые вызовы/тесты); в проде хендлер всегда передаёт doy из кнопки.
    """
    student = await _get_student(session, user_id)
    if student is None:
        return {"status": ANSWER_GONE}
    task = await session.get(Task, task_id)
    if task is None or not task.is_active:
        return {"status": ANSWER_GONE}
    # «Текущее досматривает» (ТЗ раздел 10): is_open НЕ проверяется —
    # тему могли закрыть ПОСЛЕ выдачи, ответ по выданному заданию
    # принимается. Проверка доступа только при выдаче, не при ответе.
    theme = await session.get(Theme, task.theme_id)
    if theme is None:
        return {"status": ANSWER_GONE}
    if doy is not None and doy != issue_day():
        return {"status": ANSWER_STALE}

    try:
        options = list(task.options or [])
    except (TypeError, ValueError):
        return {"status": ANSWER_GONE}
    if answer_index < 0 or answer_index >= len(options):
        return {"status": ANSWER_STALE}
    current_seq = await _count_attempts_theme(
        session, student.id, task.theme_id
    )
    if current_seq != seq:
        return {"status": ANSWER_STALE}

    # Кнопка несёт ПОЗИЦИЮ в карточке; карточка показывает options[perm[i]],
    # правильность и ответ — по options[perm[answer_index]] (перемешивание
    # только для показа, БД-порядок options не меняется — ТЗ 8.2).
    perm = _task_perm(task, student.id)
    if answer_index >= len(perm):
        return {"status": ANSWER_STALE}
    is_correct = bool(options[perm[answer_index]].get("c"))
    try:
        session.add(
            Attempt(
                student_id=student.id,
                task_id=task.id,
                answer_index=answer_index,
                is_correct=is_correct,
            )
        )
        existing_progress = await session.get(
            TaskProgress, (student.id, task.id)
        )
        if existing_progress is None:
            session.add(
                TaskProgress(
                    student_id=student.id,
                    task_id=task.id,
                    status="done" if is_correct else "wrong",
                )
            )
        elif existing_progress.status == "wrong" and is_correct:
            # «🔁 Ошибки»: правильный ответ по решавшейся-wrong задаче → done
            existing_progress.status = "done"
    except IntegrityError:
        # Даблтап по кнопке ответа: два параллельных check_answer видят
        # existing_progress=None и оба вставляют TaskProgress с одним PK —
        # второй падает уже на autoflush/commit. Откатываемся (и лишняя
        # Attempt уходит) и отвечаем stale: карточка перерисуется заново.
        await session.rollback()
        return {"status": ANSWER_STALE}

    user = await session.get(User, user_id)
    if user is not None:
        if is_correct:
            reaction_id, reaction_text = reactions.positive_reaction(
                user.last_reaction_id
            )
        else:
            reaction_id, reaction_text = reactions.motivational_reaction(
                user.last_reaction_id
            )
        user.last_reaction_id = reaction_id
    else:
        reaction_id, reaction_text = 0, ""
    # Стрик — ПО ПРЕДМЕТУ задания (StudentSubject); если связки нет
    # (теневой профиль owner/teacher без предметов) — стрик не ведём.
    link = await session.get(StudentSubject, (student.id, theme.subject_id))
    if link is not None:
        streak_current, streak_best, _best_updated = streaks.register_solved(
            session, link, (student.id, link.subject_id)
        )
    else:
        streak_current = streak_best = 0

    last_id = None  # правильный вариант
    correct_pos = 0
    for i, opt in enumerate(options):
        if opt.get("c"):
            correct_pos = perm.index(i)
            last_id = opt.get("t")
            break
    correct_answer = f"{LETTERS[correct_pos]}. {last_id}" if last_id else ""

    await session.commit()
    return {
        "status": ANSWER_OK,
        "is_correct": is_correct,
        "reaction": reaction_text,
        "correct_answer": correct_answer,
        "feedback_text": task.feedback_text,
        "feedback_photo_id": task.feedback_photo_id,
        "theme_id": task.theme_id,
        "subject_id": theme.subject_id,
        "streak_current": streak_current,
        "streak_best": streak_best,
    }


# --------------------------------------------------------------------------
# Итог темы и повтор (ТЗ разделы 9, 10)
# --------------------------------------------------------------------------
async def theme_summary(session, student_id: int, theme_id: int) -> dict:
    """{correct: K, wrong: M} — по TaskProgress записей темы."""
    correct = await session.scalar(
        select(func.count())
        .select_from(TaskProgress)
        .join(Task, Task.id == TaskProgress.task_id)
        .where(
            TaskProgress.student_id == student_id,
            Task.theme_id == theme_id,
            TaskProgress.status == "done",
        )
    )
    wrong = await session.scalar(
        select(func.count())
        .select_from(TaskProgress)
        .join(Task, Task.id == TaskProgress.task_id)
        .where(
            TaskProgress.student_id == student_id,
            Task.theme_id == theme_id,
            TaskProgress.status == "wrong",
        )
    )
    return {"correct": correct or 0, "wrong": wrong or 0}


async def reset_theme_progress(session, student_id: int, theme_id: int) -> int:
    """Сбрасывает прогресс темы (повтор): удаляет TaskProgress, но НЕ attempts.

    Коммит делает вызывающий (retry_theme) — сброс и выдача идут одним
    транзакционным блоком: сбой выдачи не оставляет тему без прогресса.
    Возвращает число сброшенных записей.
    """
    task_ids = select(Task.id).where(Task.theme_id == theme_id)
    result = await session.execute(
        delete(TaskProgress).where(
            TaskProgress.student_id == student_id,
            TaskProgress.task_id.in_(task_ids),
        )
    )
    return result.rowcount or 0


async def retry_theme(session, user_id: int, theme_id: int) -> dict:
    """«🔁 Повторить тему»: сначала доступ, потом сброс, потом выдача.

    Возвращает dict из issue_task (ok/блокировки). При заблокированном
    доступе прогресс НЕ сбрасывается. Сброс и выдача — одна транзакция:
    коммит в конце, падение выдачи откатывает сброс.
    """
    student, user = await _solver(session, user_id)
    if student is None:
        return {"status": TASK_ISSUE_NOT_FOR_YOU}
    self_solve = user.role in SELF_SOLVER_ROLES
    if self_solve:
        blocked = await _staff_theme_gate(session, user, theme_id)
        if blocked is not None:
            return {"status": blocked}
    else:
        ok, reason = await access.can_access(session, student.id, theme_id)
        if not ok:
            status_map = {
                access.REASON_ACCESS_EXPIRED: TASK_ISSUE_EXPIRED,
                access.REASON_THEME_CLOSED: TASK_ISSUE_THEME_CLOSED,
                access.REASON_THEME_NOT_FOUND: TASK_ISSUE_NOT_FOUND,
            }
            return {"status": status_map.get(reason, TASK_ISSUE_NOT_FOR_YOU)}
    await reset_theme_progress(session, student.id, theme_id)
    result = await issue_task(session, user_id, theme_id)
    await session.commit()
    return result