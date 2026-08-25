"""Все таблицы проекта LevelUp (ТЗ, раздел 4).

Схема согласована с владельцем — не менять поля. Поля-закладки под v2
(subthemes, themes.mode, reminder_log) остаются в схеме, но в MVP
в интерфейсе не используются.
Правило: удаление сущностей в продакшене не используется —
только деактивация (is_active=false). Каскады нужны для гигиены данных
и тестов (например, каскад тема → задания).
"""
from datetime import date, datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    false,
    true,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


# --------------------------------------------------------------------------
# users
# --------------------------------------------------------------------------
class User(Base):
    """Пользователь бота. Роль: owner | teacher | manager | student | guest.

    guest — «гость»: ещё не привязан к профилю ученика. tg_id заполняется
    при привязке; один ТГ = одна запись.

    teacher и manager могут совмещаться (владелец назначил обе роли):
    роль2 хранит вторую роль, role_set() собирает полный набор.
    """

    __tablename__ = "users"

    @property
    def role_set(self) -> frozenset[str]:
        roles = {self.role}
        if self.role2:
            roles.add(self.role2)
        return frozenset(roles)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    tg_id: Mapped[int | None] = mapped_column(
        BigInteger, unique=True, nullable=True
    )
    tg_username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tg_full_name: Mapped[str | None] = mapped_column(String(128), nullable=True)

    role: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="guest",
        server_default="guest",
    )
    role2: Mapped[str | None] = mapped_column(String(16), nullable=True)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=true()
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    # Стрики (UTC+3, Минск)
    streak_current: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    streak_best: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    last_solved_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    # Последняя показанная реакция (id из services/reactions.py) —
    # чтобы не повторять две фразы подряд
    last_reaction_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    __table_args__ = (
        CheckConstraint(
            "role IN ('owner','teacher','manager','student','guest')",
            name="ck_users_role",
        ),
    )


# --------------------------------------------------------------------------
# subjects
# --------------------------------------------------------------------------
class Subject(Base):
    """Предмет (математика, русский и т.д.). Создаёт владелец."""

    __tablename__ = "subjects"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=true()
    )


# --------------------------------------------------------------------------
# teacher_subjects
# --------------------------------------------------------------------------
class TeacherSubject(Base):
    """Связь «преподаватель → предметы» (препод видит только свои)."""

    __tablename__ = "teacher_subjects"

    teacher_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    subject_id: Mapped[int] = mapped_column(
        ForeignKey("subjects.id", ondelete="CASCADE"), primary_key=True
    )


# --------------------------------------------------------------------------
# themes
# --------------------------------------------------------------------------
class Theme(Base):
    """Тема внутри предмета. Может быть закрыта (is_open) и в двух режимах."""

    __tablename__ = "themes"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    subject_id: Mapped[int] = mapped_column(
        ForeignKey("subjects.id", ondelete="CASCADE"), nullable=False
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    order: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    # Открыта ли тема для учеников
    is_open: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )
    # 'sequential' — по порядку, 'random' — «открыть все» [v2]
    mode: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="sequential",
        server_default="sequential",
    )
    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "mode IN ('sequential','random')", name="ck_themes_mode"
        ),
        Index("ix_themes_subject_id", "subject_id"),
    )


# --------------------------------------------------------------------------
# subthemes — [v2-закладка] (таблица остаётся, интерфейса в MVP нет)
# --------------------------------------------------------------------------
class Subtheme(Base):
    """Необязательная группа заданий внутри темы."""

    __tablename__ = "subthemes"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    theme_id: Mapped[int] = mapped_column(
        ForeignKey("themes.id", ondelete="CASCADE"), nullable=False
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    order: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )

    __table_args__ = (Index("ix_subthemes_theme_id", "theme_id"),)


# --------------------------------------------------------------------------
# tasks
# --------------------------------------------------------------------------
class Task(Base):
    """Задание. options — JSON: [{"t": текст, "c": правильный}, ...].

    Вопрос и объяснение могут быть текстом и/или фото (file_id Telegram).
    """

    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    # Задание может висеть прямо на теме (subtheme_id = NULL); подтемы — [v2]
    subtheme_id: Mapped[int | None] = mapped_column(
        ForeignKey("subthemes.id", ondelete="CASCADE"), nullable=True
    )
    theme_id: Mapped[int] = mapped_column(
        ForeignKey("themes.id", ondelete="CASCADE"), nullable=False
    )

    question_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    question_photo_id: Mapped[str | None] = mapped_column(
        String(256), nullable=True
    )

    # [{"t": str, "c": bool}] — до 4 вариантов, ровно один c=true
    options: Mapped[list | None] = mapped_column(
        JSON, nullable=False, default=list
    )

    feedback_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    feedback_photo_id: Mapped[str | None] = mapped_column(
        String(256), nullable=True
    )

    order: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=true()
    )
    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        Index("ix_tasks_theme_id", "theme_id"),
        Index("ix_tasks_subtheme_id", "subtheme_id"),
    )


# --------------------------------------------------------------------------
# students
# --------------------------------------------------------------------------
class Student(Base):
    """Запись «ученик»: привязка к users, доступ, инвайт-код."""

    __tablename__ = "students"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    # Одна дата доступа на ученика (действует на все предметы)
    access_until: Mapped[date | None] = mapped_column(Date, nullable=True)
    # Код-приглашение (6 символов; колонка 8 по ТЗ)
    invite_code: Mapped[str | None] = mapped_column(
        String(8), nullable=True, unique=True
    )
    invite_status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="pending",
        server_default="pending",
    )
    invited_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "invite_status IN ('pending','activated')",
            name="ck_students_invite_status",
        ),
    )


# --------------------------------------------------------------------------
# student_subjects
# --------------------------------------------------------------------------
class StudentSubject(Base):
    """Предметы ученика с ручным переключателем активности.

    Стрики — ПО ПРЕДМЕТУ (владение, 13.08.2026): каждый предмет ведёт
    свой streak_current / streak_best / last_solved_date; день без ответов
    по предмету сбрасывает его стрик. Общие поля на users остались только
    историей (больше не обновляются).
    """

    __tablename__ = "student_subjects"

    student_id: Mapped[int] = mapped_column(
        ForeignKey("students.id", ondelete="CASCADE"), primary_key=True
    )
    subject_id: Mapped[int] = mapped_column(
        ForeignKey("subjects.id", ondelete="CASCADE"), primary_key=True
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=true()
    )
    added_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    # Стрик по предмету (UTC+3, Минск); сброс, если день пропущен
    streak_current: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    streak_best: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    last_solved_date: Mapped[date | None] = mapped_column(Date, nullable=True)


# --------------------------------------------------------------------------
# attempts
# --------------------------------------------------------------------------
class Attempt(Base):
    """Журнал ответов. Пишется всегда (задел под статистику в v2)."""

    __tablename__ = "attempts"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    student_id: Mapped[int] = mapped_column(
        ForeignKey("students.id", ondelete="CASCADE"), nullable=False
    )
    task_id: Mapped[int] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False
    )
    answer_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_correct: Mapped[bool] = mapped_column(Boolean, nullable=False)
    answered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        Index("ix_attempts_student_id", "student_id"),
        Index("ix_attempts_answered_at", "answered_at"),
    )


# --------------------------------------------------------------------------
# task_progress
# --------------------------------------------------------------------------
class TaskProgress(Base):
    """Прогресс решения задания учеником (для «осталось N»).

    status: done — решено правильно, wrong — дан правильный ответ после ошибки.
    Первая запись фиксирует факт решения; повторы пишутся только в attempts.
    """

    __tablename__ = "task_progress"

    student_id: Mapped[int] = mapped_column(
        ForeignKey("students.id", ondelete="CASCADE"), primary_key=True
    )
    task_id: Mapped[int] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), primary_key=True
    )
    status: Mapped[str] = mapped_column(
        String(8), nullable=False, default="done", server_default="done"
    )
    solved_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('done','wrong')", name="ck_task_progress_status"
        ),
    )


# --------------------------------------------------------------------------
# reminder_log — [v2-закладка] (таблица остаётся, в MVP не пишется)
# --------------------------------------------------------------------------
class ReminderLog(Base):
    """Лог отправленных напоминаний (защита от дублей).

    kind: 'expired' | '7days' | '3days' | '1day' | 'last_day' |
          'code_unactivated'
    На (student, kind, дата) — не более одного напоминания.
    """

    __tablename__ = "reminder_log"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    student_id: Mapped[int] = mapped_column(
        ForeignKey("students.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(24), nullable=False)
    reminded_on: Mapped[date] = mapped_column(Date, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "student_id",
            "kind",
            "reminded_on",
            name="uq_reminder_log_student_kind_day",
        ),
        Index("ix_reminder_log_student_id", "student_id"),
    )
