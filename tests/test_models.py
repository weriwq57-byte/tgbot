"""Модели и схема БД (ТЗ раздел 4): таблицы, констрейнты, каскады."""
from datetime import date

import pytest
from sqlalchemy.exc import IntegrityError

from app.database import Base
from app.models import (
    Attempt,
    ReminderLog,
    Student,
    StudentSubject,
    Subject,
    Task,
    TaskProgress,
    TeacherSubject,
    Theme,
    User,
)

# Все 11 таблиц из ТЗ раздела 4
EXPECTED_TABLES = {
    "users",
    "subjects",
    "teacher_subjects",
    "themes",
    "subthemes",
    "tasks",
    "students",
    "student_subjects",
    "attempts",
    "task_progress",
    "reminder_log",
}


def test_all_tables_defined():
    """В Base.metadata зарегистрированы все таблицы из ТЗ."""
    actual = set(Base.metadata.tables)
    assert EXPECTED_TABLES <= actual


async def test_user_defaults(user_factory):
    """Дефолты users: guest, активен, стрики 0."""
    user = await user_factory()
    assert user.id is not None
    assert user.role == "guest"
    assert user.is_active is True
    assert user.streak_current == 0
    assert user.streak_best == 0
    assert user.last_solved_date is None
    assert user.last_reaction_id is None
    assert user.created_at is not None


async def test_user_role_check_constraint(session):
    """Недопустимая роль не вставляется."""
    session.add(User(role="admin"))
    with pytest.raises(IntegrityError):
        await session.commit()


async def test_user_tg_id_unique(session):
    """tg_id уникален: второй пользователь с тем же tg_id — ошибка."""
    session.add(User(tg_id=100500, role="student"))
    await session.commit()
    session.add(User(tg_id=100500, role="guest"))
    with pytest.raises(IntegrityError):
        await session.commit()


async def test_theme_defaults(session):
    """Дефолты themes: закрыта, sequential."""
    subject = Subject(name="Математика")
    session.add(subject)
    await session.flush()
    theme = Theme(subject_id=subject.id, title="Логарифмы")
    session.add(theme)
    await session.commit()
    assert theme.is_open is False
    assert theme.mode == "sequential"
    assert theme.order == 0


async def test_theme_mode_check_constraint(session):
    """Недопустимый режим темы не вставляется."""
    subject = Subject(name="Физика")
    session.add(subject)
    await session.flush()
    session.add(Theme(subject_id=subject.id, title="Кинематика", mode="shuffle"))
    with pytest.raises(IntegrityError):
        await session.commit()


async def test_student_defaults(session):
    """Дефолты students: pending, код уникален."""
    user = User(role="student", tg_username="ivan")
    session.add(user)
    await session.flush()
    student = Student(user_id=user.id, invite_code="ABC123")
    session.add(student)
    await session.commit()
    assert student.invite_status == "pending"
    assert student.access_until is None


async def test_student_invite_status_check(session):
    """Недопустимый статус приглашения не вставляется."""
    user = User(role="student")
    session.add(user)
    await session.flush()
    session.add(Student(user_id=user.id, invite_status="weird"))
    with pytest.raises(IntegrityError):
        await session.commit()


async def test_student_invite_code_unique(session):
    """Инвайт-код уникален."""
    u1 = User(role="student")
    session.add(u1)
    await session.flush()
    u2 = User(role="student")
    session.add(u2)
    await session.flush()
    session.add(Student(user_id=u1.id, invite_code="ABC123"))
    await session.commit()
    session.add(Student(user_id=u2.id, invite_code="ABC123"))
    with pytest.raises(IntegrityError):
        await session.commit()


async def test_task_progress_status_check(session):
    """Недопустимый статус прогресса не вставляется."""
    session.add(TaskProgress(student_id=999, task_id=999, status="skip"))
    with pytest.raises(IntegrityError):
        await session.commit()


async def test_task_options_json_roundtrip(session):
    """options хранятся как JSON со списком словарей."""
    subject = Subject(name="Математика")
    session.add(subject)
    await session.flush()
    theme = Theme(subject_id=subject.id, title="Производные")
    session.add(theme)
    await session.flush()
    options = [{"t": "2", "c": True}, {"t": "3", "c": False}]
    task = Task(
        theme_id=theme.id,
        question_text="Сколько будет 1+1?",
        options=options,
    )
    session.add(task)
    await session.commit()
    loaded = await session.get(Task, task.id)
    assert loaded.options == options


async def test_pair_primary_keys(session):
    """Связи-пары (teacher_subjects, student_subjects) не допускают дублей."""
    teacher = User(role="teacher")
    session.add(teacher)
    await session.flush()
    subject = Subject(name="Химия")
    session.add(subject)
    await session.flush()
    session.add(TeacherSubject(teacher_id=teacher.id, subject_id=subject.id))
    await session.commit()
    session.add(TeacherSubject(teacher_id=teacher.id, subject_id=subject.id))
    with pytest.raises(IntegrityError):
        await session.commit()


async def test_cascade_delete_theme_to_tasks(session):
    """Каскад: удаление темы удаляет задания (FK ondelete CASCADE)."""
    subject = Subject(name="Биология")
    session.add(subject)
    await session.flush()
    theme = Theme(subject_id=subject.id, title="Клетка")
    session.add(theme)
    await session.flush()
    task = Task(theme_id=theme.id, options=[{"t": "Да", "c": True}])
    session.add(task)
    await session.commit()

    await session.delete(theme)
    task_id = task.id
    await session.commit()
    session.expire_all()  # сбрасываем identity map, иначе get() вернёт кэш
    assert await session.get(Task, task_id) is None


async def test_cascade_delete_user_to_attempts(session):
    """Каскад: удаление users удаляет студента, а попытки — каскадом от студента."""
    user = User(role="student")
    session.add(user)
    await session.flush()
    student = Student(user_id=user.id, invite_code="XYZ999")
    session.add(student)
    await session.flush()
    subject = Subject(name="География")
    session.add(subject)
    await session.flush()
    theme = Theme(subject_id=subject.id, title="Страны")
    session.add(theme)
    await session.flush()
    task = Task(theme_id=theme.id, options=[{"t": "Да", "c": True}])
    session.add(task)
    await session.commit()

    attempt = Attempt(student_id=student.id, task_id=task.id, answer_index=0, is_correct=True)
    session.add(attempt)
    await session.commit()

    await session.delete(user)
    student_id, attempt_id = student.id, attempt.id
    await session.commit()
    session.expire_all()  # сбрасываем identity map, иначе get() вернёт кэш
    assert await session.get(Student, student_id) is None
    assert await session.get(Attempt, attempt_id) is None


async def test_reminder_log_unique(session):
    """reminder_log: на (студент, вид, дата) — одна запись."""
    user = User(role="student")
    session.add(user)
    await session.flush()
    student = Student(user_id=user.id, invite_code="QQQ111")
    session.add(student)
    await session.commit()

    session.add(ReminderLog(student_id=student.id, kind="3days", reminded_on=date(2026, 8, 10)))
    await session.commit()
    session.add(ReminderLog(student_id=student.id, kind="3days", reminded_on=date(2026, 8, 10)))
    with pytest.raises(IntegrityError):
        await session.commit()
