"""Заход 6: ученик — привязка, меню, решение заданий, стрики (ТЗ 6, 7, 9, 10).

Покрывает 9 критериев готовности: полный флоу (код → подтверждение →
приветствие → тема → задание → ответ → ещё → итог → повтор), условия
task_progress/attempts, стрики, seq-защита, «текущее досматривает»,
привязка (гость/повторный код/чужая роль), права владельца.
"""
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import func, insert, select

from app.handlers import student as s_h
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
from app.services import student as student_svc
from app.services import students as students_svc
from app.utils.dates import today_minsk

TODAY = today_minsk()
DOY = student_svc.issue_day()  # день выдачи в кнопках ответа (фикс perm)
_SEQ = {"n": 0}
_LETTERS = "АБВГ"


class FakeMessage:
    def __init__(self, text="", chat_id=42, from_user_id=42):
        self.text = text
        self.chat = SimpleNamespace(id=chat_id)
        self.from_user = SimpleNamespace(id=from_user_id)
        self.answers = []
        self.answers_photo = []
        self.edits = []

    async def answer(self, content="", reply_markup=None, **kwargs):
        self.answers.append((content, reply_markup))

    async def answer_photo(self, photo, caption=None, reply_markup=None, **kwargs):
        self.answers_photo.append((photo, caption, reply_markup))

    async def edit_text(self, text, reply_markup=None, **kwargs):
        self.edits.append((text, reply_markup))


def make_cb(data: str, message: FakeMessage, from_user_id=42):
    answers = []

    async def answer(content="", show_alert=False, **kwargs):
        answers.append((content, show_alert))

    return SimpleNamespace(
        data=data,
        message=message,
        answer=answer,
        answers=answers,
        from_user=SimpleNamespace(id=from_user_id),
    )


def cb_buttons(markup):
    if markup is None:
        return []
    return [(b.text, b.callback_data) for row in markup.inline_keyboard for b in row]


async def make_fsm():
    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.storage.base import StorageKey
    from aiogram.fsm.storage.memory import MemoryStorage

    storage = MemoryStorage()
    key = StorageKey(chat_id=42, user_id=42, bot_id=42424242, destiny="test")
    return FSMContext(storage=storage, key=key)


def db_user(role: str, user_id: int) -> SimpleNamespace:
    return SimpleNamespace(
        role=role, id=user_id, tg_id=user_id, streak_current=0, streak_best=0
    )


def db_user_inactive(role: str, user_id: int) -> SimpleNamespace:
    return SimpleNamespace(
        role=role,
        id=user_id,
        tg_id=user_id,
        streak_current=0,
        streak_best=0,
        is_active=False,
    )


async def _mk_user(session_factory, role="teacher"):
    _SEQ["n"] += 1
    async with session_factory() as session:
        user = User(
            tg_id=400000000 + _SEQ["n"],
            tg_username=f"s6_{role}_{_SEQ['n']}",
            role=role,
            is_active=True,
        )
        session.add(user)
        await session.commit()
        return user


async def _mk_subject(session_factory, name="Математика") -> Subject:
    async with session_factory() as session:
        subject = Subject(name=name, is_active=True)
        session.add(subject)
        await session.commit()
        return subject


async def _mk_theme(
    session_factory, subject_id: int, title="Уравнения", is_open=True
) -> Theme:
    async with session_factory() as session:
        theme = Theme(
            subject_id=subject_id, title=title, is_open=is_open, mode="sequential"
        )
        session.add(theme)
        await session.commit()
        return theme


async def _mk_task(
    session_factory,
    theme_id: int,
    question="2+2?",
    n_options: int = 4,
    correct: int = 0,
    order: int | None = None,
    is_active: bool = True,
) -> Task:
    async with session_factory() as session:
        task = Task(
            theme_id=theme_id,
            question_text=question,
            options=[
                {"t": f"Вариант {i}", "c": i == correct} for i in range(n_options)
            ],
            order=order if order is not None else 0,
            is_active=is_active,
        )
        session.add(task)
        await session.commit()
        return task


async def _mk_student_stub(session_factory, subject_id: int, name="Иван Иванов"):
    """Ученик стандартным путём (менеджер создаёт запись). (user, student)."""
    mgr = await _mk_user(session_factory, role="manager")
    async with session_factory() as session:
        user, student, _ = await students_svc.create_student_record(
            session, name, {subject_id}, mgr.id, TODAY + timedelta(days=30)
        )
        user.tg_full_name = name
        await session.commit()
        return user, student


async def _mk_guest(session_factory, tg_id=777001) -> User:
    async with session_factory() as session:
        guest = User(
            tg_id=tg_id,
            tg_username="guest_s6",
            tg_full_name="Гость",
            role="guest",
            is_active=True,
        )
        session.add(guest)
        await session.commit()
        return guest


# ---------------------------------------------------------------------------
# Привязка: bind_by_code / confirm_bind (ТЗ раздел 7)
# ---------------------------------------------------------------------------
async def test_bind_code_not_found(session_factory):
    async with session_factory() as session:
        result = await student_svc.bind_by_code(session, 42, "ZZZZZZ")
    assert result["status"] == student_svc.BIND_CODE_NOT_FOUND


async def test_bind_code_normalized(session_factory):
    """Код с пробелами/нижним регистром приводится к верхнему."""
    subj = await _mk_subject(session_factory)
    _user, student = await _mk_student_stub(session_factory, subj.id)
    async with session_factory() as session:
        result = await student_svc.bind_by_code(
            session, 42, f"  {student.invite_code.lower()}  "
        )
    assert result["status"] == student_svc.BIND_READY


async def test_bind_already_activated(session_factory):
    subj = await _mk_subject(session_factory)
    _user, student = await _mk_student_stub(session_factory, subj.id)
    async with session_factory() as session:
        await student_svc.confirm_bind(session, 42, student.invite_code)
        result = await student_svc.bind_by_code(session, 42, student.invite_code)
    assert result["status"] == student_svc.BIND_ALREADY_ACTIVATED


@pytest.mark.parametrize("role", ["teacher", "manager"])
async def test_bind_tg_taken_by_serious_role(session_factory, role):
    """Активные чужие серьёзные роли, занявшие tg_id, — отказ (ТЗ 7 п.5):
    активного препода/менеджера привязка НЕ затирает (дополнение)."""
    staff = await _mk_user(session_factory, role=role)
    subj = await _mk_subject(session_factory)
    _user, student = await _mk_student_stub(session_factory, subj.id)
    async with session_factory() as session:
        result = await student_svc.bind_by_code(
            session, staff.tg_id, student.invite_code
        )
    assert result["status"] == student_svc.BIND_TG_ALREADY_BOUND


async def test_bind_code_rejected_for_bound_student(session_factory):
    """Один ТГ = один профиль (ТЗ 7): привязанный ученик пробует ЧУЖОЙ
    код → «Этот Telegram уже привязан…», перепривязка невозможна
    до деактивации/удаления."""
    subj = await _mk_subject(session_factory)
    user, student = await _mk_student_stub(session_factory, subj.id)
    other_subj = await _mk_subject(session_factory, name="Физика")
    _u2, other = await _mk_student_stub(session_factory, other_subj.id)
    async with session_factory() as session:
        u = await session.get(User, user.id)
        u.tg_id = 424242
        await session.commit()
        result = await student_svc.bind_by_code(
            session, 424242, other.invite_code
        )
    assert result["status"] == student_svc.BIND_TG_ALREADY_BOUND


async def test_confirm_bind_removes_guest(session_factory):
    """Гость, заходивший ранее в бота, удаляется (ТЗ 7 п.5)."""
    guest = await _mk_guest(session_factory, tg_id=777001)
    subj = await _mk_subject(session_factory)
    user, student = await _mk_student_stub(session_factory, subj.id)
    async with session_factory() as session:
        ok = await student_svc.confirm_bind(session, guest.tg_id, student.invite_code)
        assert ok is True
        assert await session.get(User, guest.id) is None
        fresh = await session.get(User, user.id)
        assert fresh.tg_id == guest.tg_id
        fresh_student = await session.get(Student, student.id)
        assert fresh_student.invite_status == "activated"


async def test_confirm_bind_serious_occupant_refused(session_factory):
    teacher = await _mk_user(session_factory, role="teacher")
    subj = await _mk_subject(session_factory)
    _user, student = await _mk_student_stub(session_factory, subj.id)
    async with session_factory() as session:
        ok = await student_svc.confirm_bind(session, teacher.tg_id, student.invite_code)
    assert ok is False


async def test_confirm_bind_serious_occupant_refused_manager(session_factory):
    manager = await _mk_user(session_factory, role="manager")
    subj = await _mk_subject(session_factory)
    _user, student = await _mk_student_stub(session_factory, subj.id)
    async with session_factory() as session:
        ok = await student_svc.confirm_bind(
            session, manager.tg_id, student.invite_code
        )
    assert ok is False


# ---------------------------------------------------------------------------
# Повторная привязка деактивированного (заход «новый код приглашения»)
# ---------------------------------------------------------------------------
async def _mk_kicked_student(session_factory, subj_id, name="Кикнутый Ученик"):
    """Кикнутый ученик с историей (привязан, деактивирован, есть прогресс).
    Возвращает (user, student, task) для проверки каскада удаления."""
    user, student = await _mk_student_stub(session_factory, subj_id, name=name)
    theme = await _mk_theme(session_factory, subj_id)
    task = await _mk_task(session_factory, theme.id)
    async with session_factory() as session:
        u = await session.get(User, user.id)
        u.tg_id = 424242
        u.is_active = False
        session.add(TaskProgress(student_id=student.id, task_id=task.id, status="wrong"))
        session.add(
            Attempt(
                student_id=student.id,
                task_id=task.id,
                answer_index=0,
                is_correct=False,
            )
        )
        await session.commit()
        return user, student, task


async def test_bind_ready_for_deactivated_student(session_factory):
    """Кикнутый ученик вводит НОВЫЙ код → tg_id свободен → BIND_READY
    (старая запись уступит место каскадным удалением при подтверждении)."""
    subj = await _mk_subject(session_factory)
    _kicked_user, _kicked_st, _task = await _mk_kicked_student(session_factory, subj.id)
    other_subj = await _mk_subject(session_factory, name="Физика")
    _u2, new_student = await _mk_student_stub(session_factory, other_subj.id)
    async with session_factory() as session:
        result = await student_svc.bind_by_code(session, 424242, new_student.invite_code)
    assert result["status"] == student_svc.BIND_READY


async def test_confirm_bind_replaces_deactivated_student(session_factory):
    """Подтверждение: старая запись кикнутого удаляется КАСКАДОМ
    (User → Student → Attempt/TaskProgress/StudentSubject), tg_id
    переходит новому ученику, его код — activated."""
    subj = await _mk_subject(session_factory)
    kicked_user, kicked_st, task = await _mk_kicked_student(session_factory, subj.id)
    other_subj = await _mk_subject(session_factory, name="Физика")
    new_user, new_student = await _mk_student_stub(session_factory, other_subj.id)
    async with session_factory() as session:
        ok = await student_svc.confirm_bind(session, 424242, new_student.invite_code)
        assert ok is True
        # старая запись удалена каскадом
        assert await session.get(User, kicked_user.id) is None
        assert await session.get(Student, kicked_st.id) is None
        assert (
            await session.scalar(
                select(TaskProgress).where(TaskProgress.student_id == kicked_st.id)
            )
            is None
        )
        assert (
            await session.scalar(
                select(Attempt).where(Attempt.student_id == kicked_st.id)
            )
            is None
        )
        # новый ученик привязан
        fresh = await session.get(User, new_user.id)
        assert fresh.tg_id == 424242
        assert fresh.is_active is True
        fresh_student = await session.get(Student, new_student.id)
        assert fresh_student.invite_status == "activated"


async def _mk_kicked_staff(session_factory, role, subj_id):
    """Кикнутый препод/менеджер со связью предмета (TeacherSubject).
    Возвращает (user, teacher_subject)."""
    staff = await _mk_user(session_factory, role=role)
    async with session_factory() as session:
        u = await session.get(User, staff.id)
        u.is_active = False
        ts = TeacherSubject(teacher_id=staff.id, subject_id=subj_id)
        session.add(ts)
        await session.commit()
        return staff, ts


async def test_rebind_ready_for_deactivated_staff(session_factory):
    """Дополнение: кикнутого ПРЕПОДА/МЕНЕДЖЕРА тоже можно перепривязать
    учеником — владелец создал ему ученика, /start КОД (или текст кода)
    не должен упираться в tg_already_bound."""
    for role in ("teacher", "manager"):
        subj = await _mk_subject(session_factory)
        staff, _ts = await _mk_kicked_staff(session_factory, role, subj.id)
        other_subj = await _mk_subject(session_factory, name="Физика")
        _u2, student = await _mk_student_stub(session_factory, other_subj.id)
        async with session_factory() as session:
            result = await student_svc.bind_by_code(
                session, staff.tg_id, student.invite_code
            )
        assert result["status"] == student_svc.BIND_READY
        assert result["occupant_role"] == role


async def test_confirm_bind_replaces_deactivated_staff(session_factory):
    """Подтверждение: кикнутый staff удаляется КАСКАДОМ (включая
    TeacherSubject), tg_id переходит новому ученику, его код — activated."""
    for role in ("teacher", "manager"):
        subj = await _mk_subject(session_factory)
        staff, ts = await _mk_kicked_staff(session_factory, role, subj.id)
        other_subj = await _mk_subject(session_factory, name="Физика")
        new_user, new_student = await _mk_student_stub(session_factory, other_subj.id)
        async with session_factory() as session:
            ok = await student_svc.confirm_bind(
                session, staff.tg_id, new_student.invite_code
            )
            assert ok is True
            # старая запись staff удалена каскадом (TeacherSubject — CASCADE)
            assert await session.get(User, staff.id) is None
            assert await session.get(TeacherSubject, (staff.id, subj.id)) is None
            # новый ученик привязан
            fresh = await session.get(User, new_user.id)
            assert fresh.tg_id == staff.tg_id
            assert fresh.role == "student"
            assert fresh.is_active is True
            fresh_student = await session.get(Student, new_student.id)
            assert fresh_student.invite_status == "activated"


# ---------------------------------------------------------------------------
# Выдача: issue_task (последовательность, all_done, empty, блокировки)
# ---------------------------------------------------------------------------
async def test_issue_empty_theme(session_factory):
    subj = await _mk_subject(session_factory)
    _user, student = await _mk_student_stub(session_factory, subj.id)
    theme = await _mk_theme(session_factory, subj.id)
    async with session_factory() as session:
        result = await student_svc.issue_task(session, student.user_id, theme.id)
    assert result["status"] == student_svc.TASK_ISSUE_EMPTY


async def test_issue_first_unsolved_by_order(session_factory):
    subj = await _mk_subject(session_factory)
    _user, student = await _mk_student_stub(session_factory, subj.id)
    theme = await _mk_theme(session_factory, subj.id)
    t1 = await _mk_task(session_factory, theme.id, "Первый", order=2)
    t2 = await _mk_task(session_factory, theme.id, "Второй", order=1)
    async with session_factory() as session:
        result = await student_svc.issue_task(session, student.user_id, theme.id)
    assert result["status"] == student_svc.TASK_ISSUE_OK
    assert result["task"].id == t2.id
    assert result["seq"] == 0
    assert result["progress"] == {"solved": 0, "total": 2, "remaining": 2}


async def test_issue_skips_solved_and_hidden(session_factory):
    subj = await _mk_subject(session_factory)
    _user, student = await _mk_student_stub(session_factory, subj.id)
    theme = await _mk_theme(session_factory, subj.id)
    t1 = await _mk_task(session_factory, theme.id, "Первый")
    t2 = await _mk_task(session_factory, theme.id, "Второй")
    hidden = await _mk_task(session_factory, theme.id, "Скрытый", is_active=False)
    async with session_factory() as session:
        done_id = t1.id
        session.add(TaskProgress(student_id=student.id, task_id=done_id, status="done"))
        await session.commit()
        result = await student_svc.issue_task(session, student.user_id, theme.id)
    assert result["status"] == student_svc.TASK_ISSUE_OK
    assert result["task"].id == t2.id
    assert result["progress"] == {"solved": 1, "total": 2, "remaining": 1}


async def test_issue_all_done(session_factory):
    subj = await _mk_subject(session_factory)
    _user, student = await _mk_student_stub(session_factory, subj.id)
    theme = await _mk_theme(session_factory, subj.id)
    t1 = await _mk_task(session_factory, theme.id, "Первый")
    async with session_factory() as session:
        session.add(TaskProgress(student_id=student.id, task_id=t1.id, status="done"))
        await session.commit()
        result = await student_svc.issue_task(session, student.user_id, theme.id)
    assert result["status"] == student_svc.TASK_ISSUE_ALL_DONE
    assert result["summary"] == {"correct": 1, "wrong": 0}
    assert result["theme_id"] == theme.id


async def test_issue_all_done_after_hidden_solved(session_factory):
    """Решённое задание скрыто -> ALL_DONE, а не StopIteration.

    Без фильтра is_active в подсчёте solved скрытая решённая задача дала бы
    remaining=-1 и краш next() при выборе первого нерешённого.
    """
    subj = await _mk_subject(session_factory)
    _user, student = await _mk_student_stub(session_factory, subj.id)
    theme = await _mk_theme(session_factory, subj.id)
    t1 = await _mk_task(session_factory, theme.id, "Первый")
    t2 = await _mk_task(session_factory, theme.id, "Второй")
    async with session_factory() as session:
        session.add(TaskProgress(student_id=student.id, task_id=t1.id, status="done"))
        session.add(TaskProgress(student_id=student.id, task_id=t2.id, status="done"))
        await session.commit()
    async with session_factory() as session:
        task = await session.get(Task, t2.id)
        task.is_active = False
        await session.commit()
        result = await student_svc.issue_task(session, student.user_id, theme.id)
    assert result["status"] == student_svc.TASK_ISSUE_ALL_DONE
    assert result["theme_id"] == theme.id


async def test_issue_blocked_expired(session_factory):
    subj = await _mk_subject(session_factory)
    _user, student = await _mk_student_stub(session_factory, subj.id)
    theme = await _mk_theme(session_factory, subj.id)
    await _mk_task(session_factory, theme.id)
    async with session_factory() as session:
        rec = await session.get(Student, student.id)
        rec.access_until = TODAY - timedelta(days=1)
        await session.commit()
        result = await student_svc.issue_task(session, student.user_id, theme.id)
    assert result["status"] == student_svc.TASK_ISSUE_EXPIRED


async def test_issue_blocked_theme_closed(session_factory):
    subj = await _mk_subject(session_factory)
    _user, student = await _mk_student_stub(session_factory, subj.id)
    theme = await _mk_theme(session_factory, subj.id, is_open=False)
    async with session_factory() as session:
        result = await student_svc.issue_task(session, student.user_id, theme.id)
    assert result["status"] == student_svc.TASK_ISSUE_THEME_CLOSED


async def test_issue_blocked_theme_missing(session_factory):
    subj = await _mk_subject(session_factory)
    _user, student = await _mk_student_stub(session_factory, subj.id)
    async with session_factory() as session:
        result = await student_svc.issue_task(session, student.user_id, 999999)
    assert result["status"] == student_svc.TASK_ISSUE_NOT_FOUND


# ---------------------------------------------------------------------------
# Ответ: check_answer (Attempt/progress/reaction/streak/seq/gone)
# ---------------------------------------------------------------------------
async def test_answer_writes_attempt_and_progress(session_factory):
    subj = await _mk_subject(session_factory)
    _user, student = await _mk_student_stub(session_factory, subj.id)
    theme = await _mk_theme(session_factory, subj.id)
    task = await _mk_task(session_factory, theme.id, correct=1)
    async with session_factory() as session:
        perm = student_svc.options_permutation(task.id, student.id, 4)
        result = await student_svc.check_answer(
            session, student.user_id, task.id, perm.index(1), seq=0
        )
        assert result["status"] == student_svc.ANSWER_OK
        assert result["is_correct"] is True
        assert "Вариант 1" in result["correct_answer"]
        attempts = (
            await session.scalars(
                select(Attempt).where(
                    Attempt.student_id == student.id, Attempt.task_id == task.id
                )
            )
        ).all()
        progress = await session.get(TaskProgress, (student.id, task.id))
        user = await session.get(User, student.user_id)
        link = await session.get(StudentSubject, (student.id, subj.id))
    assert len(attempts) == 1
    assert attempts[0].is_correct is True
    assert progress is not None and progress.status == "done"
    assert user.last_reaction_id is not None
    assert link is not None and link.streak_current == 1  # стрик ПО ПРЕДМЕТУ


async def test_answer_wrong_writes_wrong_progress(session_factory):
    subj = await _mk_subject(session_factory)
    _user, student = await _mk_student_stub(session_factory, subj.id)
    theme = await _mk_theme(session_factory, subj.id)
    task = await _mk_task(session_factory, theme.id, correct=2)
    async with session_factory() as session:
        perm = student_svc.options_permutation(task.id, student.id, 4)
        wrong_pos = next(i for i in range(4) if perm[i] != 2)
        result = await student_svc.check_answer(
            session, student.user_id, task.id, wrong_pos, seq=0
        )
        assert result["is_correct"] is False
        assert result["correct_answer"].startswith(f"{_LETTERS[perm.index(2)]}. ")
        progress = await session.get(TaskProgress, (student.id, task.id))
    assert progress is not None and progress.status == "wrong"


async def test_answer_progress_created_only_once(session_factory):
    """Attempt пишется всегда, TaskProgress — только первый раз (ТЗ 9 п.3)."""
    subj = await _mk_subject(session_factory)
    _user, student = await _mk_student_stub(session_factory, subj.id)
    theme = await _mk_theme(session_factory, subj.id)
    task = await _mk_task(session_factory, theme.id)
    # правильный вариант с учётом перемешивания (perm) — позиция в карточке
    correct_pos = student_svc.options_permutation(task.id, student.id, 4).index(0)
    async with session_factory() as session:
        for seq in (0, 1):  # две попытки (прямой вызов, без UI)
            result = await student_svc.check_answer(
                session, student.user_id, task.id, correct_pos, seq=seq
            )
            assert result["status"] == student_svc.ANSWER_OK
        attempts = (
            await session.scalars(
                select(Attempt).where(
                    Attempt.student_id == student.id, Attempt.task_id == task.id
                )
            )
        ).all()
        progress = await session.get(TaskProgress, (student.id, task.id))
    assert len(attempts) == 2
    assert progress is not None and progress.status == "done"


async def test_answer_stale_seq(session_factory):
    """Старая кнопка (seq не совпал) → stale, ничего не пишется."""
    subj = await _mk_subject(session_factory)
    _user, student = await _mk_student_stub(session_factory, subj.id)
    theme = await _mk_theme(session_factory, subj.id)
    task = await _mk_task(session_factory, theme.id)
    async with session_factory() as session:
        await student_svc.check_answer(session, student.user_id, task.id, 0, seq=0)
        stale = await student_svc.check_answer(
            session, student.user_id, task.id, 0, seq=0
        )
        assert stale["status"] == student_svc.ANSWER_STALE
        attempts = (
            await session.scalars(
                select(Attempt).where(
                    Attempt.student_id == student.id, Attempt.task_id == task.id
                )
            )
        ).all()
    assert len(attempts) == 1


async def test_answer_index_out_of_range_is_stale(session_factory):
    subj = await _mk_subject(session_factory)
    _user, student = await _mk_student_stub(session_factory, subj.id)
    theme = await _mk_theme(session_factory, subj.id)
    task = await _mk_task(session_factory, theme.id, n_options=2)
    async with session_factory() as session:
        result = await student_svc.check_answer(
            session, student.user_id, task.id, 5, seq=0
        )
    assert result["status"] == student_svc.ANSWER_STALE


async def test_answer_task_gone(session_factory):
    subj = await _mk_subject(session_factory)
    _user, student = await _mk_student_stub(session_factory, subj.id)
    theme = await _mk_theme(session_factory, subj.id)
    task = await _mk_task(session_factory, theme.id, is_active=False)
    async with session_factory() as session:
        result = await student_svc.check_answer(
            session, student.user_id, task.id, 0, seq=0
        )
    assert result["status"] == student_svc.ANSWER_GONE


async def test_current_handouts_finish_after_expiry(session_factory):
    """«Текущее досматривает»: доступ истёк ПОСЛЕ выдачи — ответ принимается."""
    subj = await _mk_subject(session_factory)
    _user, student = await _mk_student_stub(session_factory, subj.id)
    theme = await _mk_theme(session_factory, subj.id)
    task = await _mk_task(session_factory, theme.id)
    # документ уже на руках у ученика; доступ истекает ПОСЛЕ выдачи
    async with session_factory() as session:
        rec = await session.get(Student, student.id)
        rec.access_until = TODAY - timedelta(days=1)
        await session.commit()
        result = await student_svc.check_answer(
            session, student.user_id, task.id, 0, seq=0
        )
        assert result["status"] == student_svc.ANSWER_OK
        # а новая выдача уже блокируется
        blocked = await student_svc.issue_task(session, student.user_id, theme.id)
    assert blocked["status"] == student_svc.TASK_ISSUE_EXPIRED


# ---------------------------------------------------------------------------
# Итог и повтор (ТЗ 9 п.4, 10)
# ---------------------------------------------------------------------------
async def test_retry_resets_progress_keeps_attempts(session_factory):
    subj = await _mk_subject(session_factory)
    _user, student = await _mk_student_stub(session_factory, subj.id)
    theme = await _mk_theme(session_factory, subj.id)
    t1 = await _mk_task(session_factory, theme.id, "Первый")
    t2 = await _mk_task(session_factory, theme.id, "Второй")
    async with session_factory() as session:
        # два решения (seq растёт: 0 → 1) — как в реальном флоу
        await student_svc.check_answer(session, student.user_id, t1.id, 0, seq=0)
        await student_svc.check_answer(session, student.user_id, t2.id, 0, seq=1)
        after = await student_svc.issue_task(session, student.user_id, theme.id)
        assert after["status"] == student_svc.TASK_ISSUE_ALL_DONE

        retried = await student_svc.retry_theme(session, student.user_id, theme.id)
        assert retried["status"] == student_svc.TASK_ISSUE_OK
        assert retried["task"].id == t1.id
        assert retried["progress"]["remaining"] == 2

        attempts = (
            await session.scalars(
                select(Attempt).where(Attempt.student_id == student.id)
            )
        ).all()
        progress = (
            await session.scalars(
                select(TaskProgress).where(TaskProgress.student_id == student.id)
            )
        ).all()
    assert len(attempts) == 2  # attempts остаются
    assert progress == []  # task_progress сброшен


async def test_retry_closed_theme_does_not_reset(session_factory):
    subj = await _mk_subject(session_factory)
    _user, student = await _mk_student_stub(session_factory, subj.id)
    theme = await _mk_theme(session_factory, subj.id, is_open=False)
    t1 = await _mk_task(session_factory, theme.id)
    async with session_factory() as session:
        session.add(
            TaskProgress(student_id=student.id, task_id=t1.id, status="done")
        )
        await session.commit()
        result = await student_svc.retry_theme(session, student.user_id, theme.id)
        progress = (
            await session.scalars(
                select(TaskProgress).where(TaskProgress.student_id == student.id)
            )
        ).all()
    assert result["status"] == student_svc.TASK_ISSUE_THEME_CLOSED
    assert len(progress) == 1  # прогресс НЕ сброшен


async def test_retry_theme_rolls_back_progress_on_issue_failure(
    session_factory, monkeypatch
):
    """Повтор темы — одна транзакция: падение выдачи ПОСЛЕ сброса не
    оставляет тему без прогресса (сброс откатывается, попытка не потеряна)."""
    subj = await _mk_subject(session_factory)
    _user, student = await _mk_student_stub(session_factory, subj.id)
    theme = await _mk_theme(session_factory, subj.id)
    t1 = await _mk_task(session_factory, theme.id)
    async with session_factory() as session:
        session.add(
            TaskProgress(student_id=student.id, task_id=t1.id, status="done")
        )
        await session.commit()

        async def boom(*_args, **_kwargs):
            raise RuntimeError("db down")

        monkeypatch.setattr(student_svc, "issue_task", boom)
        with pytest.raises(RuntimeError):
            await student_svc.retry_theme(session, student.user_id, theme.id)
    async with session_factory() as session:
        remains = (
            await session.scalars(
                select(TaskProgress).where(TaskProgress.student_id == student.id)
            )
        ).all()
    assert len(remains) == 1  # сброс откатился вместе с упавшей выдачей


async def test_double_tap_answer_duplicate_progress_is_stale(
    session_factory, monkeypatch
):
    """Гонка: два параллельных check_answer по одной кнопке. Оба видят
    «прогресса нет» → дубль PK TaskProgress падает IntegrityError'ом на
    втором коммите. НЕ должно быть исключения: второй ответ → stale
    (перерисовать карточку), попыток и прогресса — по одной записи."""
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models import TaskProgress

    subj = await _mk_subject(session_factory)
    _user, student = await _mk_student_stub(session_factory, subj.id)
    theme = await _mk_theme(session_factory, subj.id)
    t1 = await _mk_task(session_factory, theme.id)
    async with session_factory() as session:
        # первая «половина» даблтапа уже закоммичена (seq 0 → 1)
        assert (
            await student_svc.check_answer(session, student.user_id, t1.id, 0, seq=0)
        )["status"] == student_svc.ANSWER_OK

        real_get = AsyncSession.get

        async def fake_get(self, model, ident, *args, **kwargs):
            if model is TaskProgress:
                # Вторая «половина» гонки — отдельная сессия, её ORM добавит
                # задачу INSERT'ом напрямую в БД (в нашей сессии identity map
                # уже знает PK — аномалия add() не сымитирует конкуренцию).
                await self.execute(
                    insert(TaskProgress)
                    .values(student_id=ident[0], task_id=ident[1], status="wrong")
                )
                return None  # не видит свежий прогресс
            return await real_get(self, model, ident, *args, **kwargs)

        monkeypatch.setattr(AsyncSession, "get", fake_get)
        result = await student_svc.check_answer(
            session, student.user_id, t1.id, 0, seq=1
        )
        assert result["status"] == student_svc.ANSWER_STALE

        attempts = (
            await session.scalars(
                select(Attempt).where(Attempt.student_id == student.id)
            )
        ).all()
        progress = (
            await session.scalars(
                select(TaskProgress).where(TaskProgress.student_id == student.id)
            )
        ).all()
    assert len(attempts) == 1  # лишняя попытка откатилась
    assert len(progress) == 1


async def test_solver_shadow_creation_race_recovers(session_factory, monkeypatch):
    """Гонка: два одновременных входа staff в тему (теневой профиль ещё не
    создан) — первое создание уходит, второе падает на unique
    (students.user_id), откатывается и возвращает СУЩЕСТВУЮЩИЙ профиль."""
    user = await _mk_user(session_factory, role="owner")
    async with session_factory() as session:
        # первая «половина» гонки уже создала профиль и закоммитила
        session.add(Student(user_id=user.id, access_until=None, invite_code=None))
        await session.commit()

        real_get_student = student_svc._get_student
        calls = {"n": 0}

        async def fake_get_student(s, user_id):
            calls["n"] += 1
            if calls["n"] == 1:
                return None  # вторая «половина» не видит свежий профиль
            return await real_get_student(s, user_id)

        monkeypatch.setattr(student_svc, "_get_student", fake_get_student)
        student, db_user = await student_svc._solver(session, user.id)
        assert student is not None and student.user_id == user.id
        assert db_user is not None and db_user.id == user.id
    async with session_factory() as session:
        n_students = (
            await session.scalar(
                select(func.count()).select_from(Student)
            )
        )
    assert n_students == 1  # дубль не сохранился


async def test_theme_summary_counts(session_factory):
    subj = await _mk_subject(session_factory)
    _user, student = await _mk_student_stub(session_factory, subj.id)
    theme = await _mk_theme(session_factory, subj.id)
    t1 = await _mk_task(session_factory, theme.id)
    t2 = await _mk_task(session_factory, theme.id)
    t3 = await _mk_task(session_factory, theme.id)
    async with session_factory() as session:
        session.add_all(
            [
                TaskProgress(student_id=student.id, task_id=t1.id, status="done"),
                TaskProgress(student_id=student.id, task_id=t2.id, status="done"),
                TaskProgress(student_id=student.id, task_id=t3.id, status="wrong"),
            ]
        )
        await session.commit()
        summary = await student_svc.theme_summary(session, student.id, theme.id)
    assert summary == {"correct": 2, "wrong": 1}


# ---------------------------------------------------------------------------
# Хендлеры: полный флоу ученика (критерий готовности 1)
# ---------------------------------------------------------------------------
async def test_full_student_flow(session_factory, monkeypatch):
    """Код → подтверждение → приветствие → меню → тема → задание → ответ
    → ещё задание → итог → повтор (все тексты дословно из ТЗ)."""
    # Фиксируем случайную реакцию — детерминированный вывод
    monkeypatch.setattr(
        student_svc.reactions.random, "choice", lambda seq: seq[0]
    )

    subj = await _mk_subject(session_factory)
    user, student = await _mk_student_stub(session_factory, subj.id)
    theme = await _mk_theme(session_factory, subj.id, "Уравнения")
    t1 = await _mk_task(session_factory, theme.id, "2+2?", correct=1)
    t2 = await _mk_task(session_factory, theme.id, "3+3?", correct=0)

    # 1. /start с кодом → подтверждение
    msg = FakeMessage(text=f"/start {student.invite_code}")
    await s_h.cmd_start_with_code(msg, state=None, db_user=db_user("guest", 0))
    assert len(msg.answers) == 1
    confirm_text, kb = msg.answers[0]
    assert confirm_text == f"Подтверди, что ты — {user.tg_full_name}?"
    buttons = cb_buttons(kb)
    assert ("Да, это я", f"std:bind_yes:{student.invite_code}:0") in buttons
    assert ("Нет, это не я", f"std:bind_no:{student.invite_code}:0") in buttons

    # 2. «Да, это я» → приветствие + меню
    msg2 = FakeMessage()
    cb = make_cb(f"std:bind_yes:{student.invite_code}:0", msg2)
    await s_h.cb_bind_yes(cb, db_user=db_user("guest", 0))
    assert cb.answers == [("", False)]
    assert len(msg2.answers) == 2
    greeting, kb = msg2.answers[0]
    assert greeting.startswith(f"Привет, {user.tg_full_name}! 🎉")
    assert "Математика" in greeting
    assert "Погнали? 🔥" in greeting
    # reply-клавиатура команд прикреплена к приветствию (без лишнего
    # текстового сообщения; переключатель возле стикеров)
    assert kb.keyboard[0][0].text == "/start"
    assert kb.keyboard[1][0].text == "/help"
    menu_text, menu_kb = msg2.answers[1]
    # шапка меню — стрики ПО ПРЕДМЕТАМ (владелец, 13.08)
    assert "Математика — 🔥 0 (рекорд: 0)" in menu_text
    # шапка меню — с приветствием по имени (см. «Привет, {имя}!»)
    assert menu_text.startswith(f"Привет, {user.tg_full_name}!")
    assert ("🎯 Решать задания", "menu:student:subjects:0") in cb_buttons(menu_kb)
    assert ("📊 Статистика", "menu:student:stats:0") in cb_buttons(menu_kb)

    # 3. «Решать задания» → выбор предмета → темы предмета с прогрессом
    msg3 = FakeMessage()
    cb3 = make_cb("menu:student:subjects:0", msg3)
    await s_h.cb_menu_student_subjects(cb3, db_user=db_user("student", user.id))
    assert len(msg3.answers) == 1
    pick_text, pick_kb = msg3.answers[0]
    assert pick_text.startswith("🎯 <b>Решать задания</b>")
    pick_buttons = cb_buttons(pick_kb)
    assert ("Математика", f"std:subj:{subj.id}:0") in pick_buttons
    assert ("← Назад", "menu:back:student:0") in pick_buttons

    msg3b = FakeMessage()
    cb3b = make_cb(f"std:subj:{subj.id}:0", msg3b)
    await s_h.cb_student_subject(cb3b, db_user=db_user("student", user.id))
    assert len(msg3b.answers) == 1
    topics_text, topics_kb = msg3b.answers[0]
    assert "<b>Математика</b>" in topics_text
    topic_buttons = cb_buttons(topics_kb)
    assert ("🔓 Уравнения — осталось 2", f"std:theme:{theme.id}:0") in topic_buttons
    assert ("← Назад", "std:subjects:0") in topic_buttons

    # 4. Вход в тему → карточка задания «Решено 0 из 2 · Осталось 2».
    #    Варианты перемешаны по дню (options_permutation): кнопка несёт
    #    ПОЗИЦИЮ в карточке, текст — options[perm[position]].
    msg4 = FakeMessage()
    cb4 = make_cb(f"std:theme:{theme.id}:0", msg4)
    await s_h.cb_student_theme(cb4, db_user=db_user("student", user.id))
    assert len(msg4.answers) == 1
    card_text, card_kb = msg4.answers[0]
    assert card_text.startswith("Решено 0 из 2 · Осталось 2")
    card_buttons = cb_buttons(card_kb)
    perm1 = student_svc.options_permutation(t1.id, student.id, 4)
    correct_pos1 = perm1.index(1)  # у t1 правильный вариант — «Вариант 1»
    for pos, db_i in enumerate(perm1):
        assert (f"{_LETTERS[pos]}. Вариант {db_i}", f"task:{t1.id}:ans:{pos}:0:{DOY}") in card_buttons

    # 5. Ответ правильным вариантом → реакция + кнопки «Ещё/Вернуться»
    msg5 = FakeMessage()
    cb5 = make_cb(f"task:{t1.id}:ans:{correct_pos1}:0:{DOY}", msg5)
    await s_h.cb_task_answer(cb5, db_user=db_user("student", user.id))
    assert cb5.answers == [("", False)]
    assert len(msg5.answers) == 1
    reaction_text, actions_kb = msg5.answers[0]
    assert reaction_text == "Вот это точно! Так держать"
    actions_buttons = cb_buttons(actions_kb)
    assert ("⏭ Следующее задание", f"std:again:{theme.id}:0") in actions_buttons
    assert ("📚 Вернуться к темам", f"std:topics:{theme.id}:0") in actions_buttons

    # 6. «Ещё задание» → второе задание, «Решено 1 из 2 · Осталось 1», seq=1
    msg6 = FakeMessage()
    cb6 = make_cb(f"std:again:{theme.id}:0", msg6)
    await s_h.cb_student_again(cb6, db_user=db_user("student", user.id))
    card2_text, card2_kb = msg6.answers[0]
    assert card2_text.startswith("Решено 1 из 2 · Осталось 1")
    perm2 = student_svc.options_permutation(t2.id, student.id, 4)
    correct_pos2 = perm2.index(0)  # у t2 правильный вариант — «Вариант 0»
    assert (
        f"{_LETTERS[correct_pos2]}. Вариант 0",
        f"task:{t2.id}:ans:{correct_pos2}:1:{DOY}",
    ) in cb_buttons(card2_kb)

    # 7. Ошибочный ответ → «Правильный ответ: …» (реакция после первой,
    #    с учётом last_reaction_id=1 из пула мотивационных — «Ошибка — …»)
    wrong_pos = next(i for i in range(4) if perm2[i] != 0)
    msg7 = FakeMessage()
    cb7 = make_cb(f"task:{t2.id}:ans:{wrong_pos}:1:{DOY}", msg7)
    await s_h.cb_task_answer(cb7, db_user=db_user("student", user.id))
    wrong_text, _kb = msg7.answers[0]
    assert "Ошибка — единственный способ запомнить навсегда" in wrong_text
    assert f"Правильный ответ: {_LETTERS[correct_pos2]}. Вариант 0" in wrong_text

    # 8. «Ещё задание» → итог темы + рекорд стрика + «Повторить/Другие темы»
    msg8 = FakeMessage()
    cb8 = make_cb(f"std:again:{theme.id}:0", msg8)
    await s_h.cb_student_again(cb8, db_user=db_user("student", user.id))
    result_text, result_kb = msg8.answers[0]
    assert "🏁 <b>Тема пройдена!</b>" in result_text
    assert "✅ 1 правильных, ❌ 1 неправильных" in result_text
    assert "И новый рекорд стрика: 1 дней! 🔥" in result_text
    result_buttons = cb_buttons(result_kb)
    assert ("🔁 Повторить тему", f"std:retry:{theme.id}:0") in result_buttons
    assert ("📚 Другие темы", f"std:topics:{theme.id}:0") in result_buttons

    # 9. «Повторить тему» → «🔁 Начинаем тему заново!» + карточка с нуля
    msg9 = FakeMessage()
    cb9 = make_cb(f"std:retry:{theme.id}:0", msg9)
    await s_h.cb_student_retry(cb9, db_user=db_user("student", user.id))
    assert msg9.answers[0][0] == "🔁 Начинаем тему заново!"
    retry_card_text, retry_kb = msg9.answers[1]
    assert retry_card_text.startswith("Решено 0 из 2 · Осталось 2")
    # attempts (2) не сброшены — seq продолжает расти; вариант «А» карточки —
    # options[perm[0]] (перемешивание стабильно в рамках дня)
    assert (
        f"А. Вариант {perm1[0]}",
        f"task:{t1.id}:ans:0:2:{DOY}",
    ) in cb_buttons(retry_kb)

    # 10. attempts хранятся (2 ответа), прогресс сброшен после повтора
    async with session_factory() as session:
        attempts = (
            await session.scalars(
                select(Attempt).where(Attempt.student_id == student.id)
            )
        ).all()
        progress = (
            await session.scalars(
                select(TaskProgress).where(TaskProgress.student_id == student.id)
            )
        ).all()
    assert len(attempts) == 2
    assert progress == []


async def test_answer_stale_redraws_current_card(session_factory):
    """seq не совпал → alert «Кнопка устарела…» + перерисовка (ТЗ 9 п.5)."""
    subj = await _mk_subject(session_factory)
    user, student = await _mk_student_stub(session_factory, subj.id)
    theme = await _mk_theme(session_factory, subj.id)
    t1 = await _mk_task(session_factory, theme.id)
    t2 = await _mk_task(session_factory, theme.id, "Второе")
    async with session_factory() as session:
        await student_svc.check_answer(session, user.id, t1.id, 0, seq=0)

    msg = FakeMessage()
    cb = make_cb(f"task:{t1.id}:ans:0:0:{DOY}", msg)
    await s_h.cb_task_answer(cb, db_user=db_user("student", user.id))
    assert cb.answers == [
        ("Кнопка устарела — возьми задание заново.", True)
    ]
    # перерисовка: карточка следующего нерешённого задания
    assert len(msg.answers) == 1
    assert msg.answers[0][0].startswith("Решено 1 из 2 · Осталось 1")
    assert f"task:{t2.id}:ans:" in cb_buttons(msg.answers[0][1])[0][1]


async def test_answer_gone_alert(session_factory):
    subj = await _mk_subject(session_factory)
    user, student = await _mk_student_stub(session_factory, subj.id)
    theme = await _mk_theme(session_factory, subj.id)
    task = await _mk_task(session_factory, theme.id, is_active=False)
    msg = FakeMessage()
    cb = make_cb(f"task:{task.id}:ans:0:0:{DOY}", msg)
    await s_h.cb_task_answer(cb, db_user=db_user("student", user.id))
    assert cb.answers == [("Задание больше недоступно.", True)]
    assert msg.answers == []


async def test_student_menu_none_for_outsider(session_factory):
    """Чужая роль (менеджер) не открывает ученический контур (require_role),
    босс/препод — теневой контур: видят предметы сами, «← Назад» по роли."""
    mgr = await _mk_user(session_factory, role="manager")
    msg = FakeMessage()
    cb = make_cb("menu:student:subjects:0", msg)
    await s_h.cb_menu_student_subjects(cb, db_user=db_user("manager", mgr.id))
    assert msg.answers == []  # роль не разрешена — ничего не отправлено

    owner = await _mk_user(session_factory, role="owner")
    msg2 = FakeMessage()
    cb2 = make_cb("menu:student:subjects:0", msg2)
    await s_h.cb_menu_student_subjects(cb2, db_user=db_user("owner", owner.id))
    # у владельца нет теневого профиля/предметов — «Тебе пока не назначены…»
    assert msg2.answers[0][0] == "Тебе пока не назначены предметы — напиши менеджеру."


async def test_back_student_intercepts_only_student(session_factory):
    user, _student = await _mk_student_stub(session_factory, (await _mk_subject(session_factory)).id)
    # владелец (старая кнопка после смены роли) — меню СВОЕЙ роли из БД
    msg_own = FakeMessage()
    cb_own = make_cb("menu:back:student:0", msg_own)
    await s_h.cb_back_student(cb_own, db_user=db_user("owner", user.id))
    assert len(msg_own.edits) == 1
    assert msg_own.edits[0][0] == "Меню:"
    assert ("👨🏫 Преподаватели", "menu:owner:teachers:0") in cb_buttons(msg_own.edits[0][1])
    # студент — шапка со стриком
    msg_st = FakeMessage()
    cb_st = make_cb("menu:back:student:0", msg_st)
    await s_h.cb_back_student(cb_st, db_user=db_user("student", user.id))
    assert len(msg_st.edits) == 1
    assert "Математика — 🔥 0 (рекорд: 0)" in msg_st.edits[0][0]
    kb = msg_st.edits[0][1]
    assert ("🎯 Решать задания", "menu:student:subjects:0") in cb_buttons(kb)
    assert ("📋 Мои команды", "menu:help:student:0") in cb_buttons(kb)


async def test_guest_menu_code_and_text_bind(session_factory):
    """«🔑 Ввести код»: state ставится; ввод кода текстом → подтверждение."""
    guest = await _mk_guest(session_factory)
    subj = await _mk_subject(session_factory)
    _user, student = await _mk_student_stub(session_factory, subj.id)

    state = await make_fsm()
    msg = FakeMessage()
    cb = make_cb("menu:guest:code:0", msg)
    await s_h.cb_menu_guest_code(cb, db_user=db_user("guest", guest.tg_id), state=state)
    assert await state.get_state() == "GuestBindStates:code"

    msg2 = FakeMessage(text=student.invite_code.lower(), from_user_id=guest.tg_id)
    await s_h.on_bind_code_text(msg2, state=state, db_user=db_user("guest", guest.tg_id))
    assert msg2.answers[0][0].startswith("Подтверди, что ты —")
    assert await state.get_state() is None  # визард закрыт


async def test_guest_code_media_hint(session_factory):
    """Медиа в GuestBindStates.code — подсказка, состояние не теряется."""
    state = await make_fsm()
    await state.set_state("GuestBindStates:code")
    msg = FakeMessage()
    await s_h.on_bind_non_text(msg, state=state, db_user=db_user("guest", 42))
    assert msg.answers[0][0] == "Отправь код текстом, например ABC123"
    assert await state.get_state() == "GuestBindStates:code"


async def test_role_denied_for_manager(session_factory):
    """Доступ ученических экранов только student/owner (require_role)."""
    msg = FakeMessage()
    cb = make_cb("menu:student:subjects:0", msg)
    result = await s_h.cb_menu_student_subjects(cb, db_user=db_user("manager", 999))
    assert result is None  # хендлер не выполнился
    assert msg.answers == []  # сообщения не отправлялись


# ---------------------------------------------------------------------------
# Карточка с фото-вопросом (ТЗ 9 п.2)
# ---------------------------------------------------------------------------
async def test_photo_task_card_uses_answer_photo(session_factory):
    subj = await _mk_subject(session_factory)
    user, student = await _mk_student_stub(session_factory, subj.id)
    theme = await _mk_theme(session_factory, subj.id)
    async with session_factory() as session:
        task = Task(
            theme_id=theme.id,
            question_photo_id="PHOTO123",
            options=[{"t": "А", "c": True}, {"t": "Б", "c": False}],
            is_active=True,
        )
        session.add(task)
        await session.commit()
    msg = FakeMessage()
    cb = make_cb(f"std:theme:{theme.id}:0", msg)
    await s_h.cb_student_theme(cb, db_user=db_user("student", user.id))
    assert len(msg.answers_photo) == 1
    photo, caption, kb = msg.answers_photo[0]
    assert photo == "PHOTO123"
    assert caption.startswith("Решено 0 из 1 · Осталось 1")
    perm = student_svc.options_permutation(task.id, student.id, 2)
    assert (
        f"А. {perm[0] and 'Б' or 'А'}",
        f"task:{task.id}:ans:0:0:{DOY}",
    ) in cb_buttons(kb)


async def test_theme_result_not_shown_until_all_done(session_factory):
    """Пустая тема при входе — «Заданий пока нет…» + клавиатура БЕЗ
    «🔁 Повторить тему» (0.3 Захода 8: петля, пока препод не добавит
    заданий): «📚 Другие темы» + «← Назад» на «Мои предметы»."""
    subj = await _mk_subject(session_factory)
    user, student = await _mk_student_stub(session_factory, subj.id)
    theme = await _mk_theme(session_factory, subj.id)
    msg = FakeMessage()
    cb = make_cb(f"std:theme:{theme.id}:0", msg)
    await s_h.cb_student_theme(cb, db_user=db_user("student", user.id))
    assert msg.answers[0][0] == "Заданий пока нет. Препод скоро добавит 👩🏫"
    buttons = cb_buttons(msg.answers[0][1])
    assert ("📚 Другие темы", f"std:topics:{theme.id}:0") in buttons
    assert ("← Назад", f"std:topics:{theme.id}:0") in buttons
    assert not any(
        "Повторить" in label or (data or "").startswith("std:retry:")
        for label, data in buttons
    )


# ---------------------------------------------------------------------------
# Раздел 0 Захода 6 (фиксы владельца, 09.08–10.08.2026)
# ---------------------------------------------------------------------------
async def test_answer_accepted_after_theme_closed(session_factory):
    """ТЗ 10 «текущее досматривает»: тему закрыли ПОСЛЕ выдачи —
    ответ по выданному заданию всё равно принимается (Баг 1)."""
    subj = await _mk_subject(session_factory)
    user, student = await _mk_student_stub(session_factory, subj.id)
    theme = await _mk_theme(session_factory, subj.id)
    task = await _mk_task(session_factory, theme.id)
    async with session_factory() as session:
        rec = await session.get(Theme, theme.id)
        rec.is_open = False
        await session.commit()
        result = await student_svc.check_answer(
            session, user.id, task.id, 0, seq=0
        )
        assert result["status"] == student_svc.ANSWER_OK
        progress = await session.get(TaskProgress, (student.id, task.id))
    assert progress is not None  # прогресс записан
    # а новая выдача уже блокируется
    async with session_factory() as session:
        blocked = await student_svc.issue_task(session, user.id, theme.id)
    assert blocked["status"] == student_svc.TASK_ISSUE_THEME_CLOSED


async def test_record_line_not_shown_on_catch_up(
    session_factory, monkeypatch
):
    """Баг 2: догон до старого рекорда (best=7, было 6, стало 7) —
    строки «И новый рекорд стрика» в итоге НЕТ."""
    monkeypatch.setattr(
        student_svc.reactions.random, "choice", lambda seq: seq[0]
    )
    subj = await _mk_subject(session_factory)
    user, student = await _mk_student_stub(session_factory, subj.id)
    theme = await _mk_theme(session_factory, subj.id)
    task = await _mk_task(session_factory, theme.id)
    # стрик «догоняет» прошлый рекорд: вчера 6, рекорд 7 (по предмету)
    async with session_factory() as session:
        link = await session.get(StudentSubject, (student.id, subj.id))
        link.streak_current = 6
        link.streak_best = 7
        link.last_solved_date = TODAY - timedelta(days=1)
        await session.commit()

    # ответ сегодня → текущий 7, рекорд 7 (догон, НЕ побитие)
    msg1 = FakeMessage()
    cb1 = make_cb(f"task:{task.id}:ans:0:0:{DOY}", msg1)
    await s_h.cb_task_answer(cb1, db_user=db_user("student", user.id))
    # «Ещё задание» → итог темы
    msg2 = FakeMessage()
    cb2 = make_cb(f"std:again:{theme.id}:0", msg2)
    await s_h.cb_student_again(cb2, db_user=db_user("student", user.id))
    result_text, _kb = msg2.answers[0]
    assert "🏁 <b>Тема пройдена!</b>" in result_text
    assert "И новый рекорд стрика" not in result_text


async def test_record_line_shown_on_real_break(session_factory, monkeypatch):
    """Баг 2: реальное побитие (best=1, было 1, стало 2) —
    строкa «И новый рекорд стрика: 2 дней! 🔥» есть."""
    monkeypatch.setattr(
        student_svc.reactions.random, "choice", lambda seq: seq[0]
    )
    subj = await _mk_subject(session_factory)
    user, student = await _mk_student_stub(session_factory, subj.id)
    theme = await _mk_theme(session_factory, subj.id)
    task = await _mk_task(session_factory, theme.id)
    async with session_factory() as session:
        link = await session.get(StudentSubject, (student.id, subj.id))
        link.streak_current = 1
        link.streak_best = 1
        link.last_solved_date = TODAY - timedelta(days=1)
        await session.commit()

    msg1 = FakeMessage()
    cb1 = make_cb(f"task:{task.id}:ans:0:0:{DOY}", msg1)
    await s_h.cb_task_answer(cb1, db_user=db_user("student", user.id))
    msg2 = FakeMessage()
    cb2 = make_cb(f"std:again:{theme.id}:0", msg2)
    await s_h.cb_student_again(cb2, db_user=db_user("student", user.id))
    result_text, _kb = msg2.answers[0]
    assert "И новый рекорд стрика: 2 дней! 🔥" in result_text


async def test_guest_code_button_stale_for_non_guest(session_factory):
    """Мелочь: кнопка «🔑 Ввести код» у не-гостя — тост, без сообщений."""
    msg = FakeMessage()
    cb = make_cb("menu:guest:code:0", msg)
    await s_h.cb_menu_guest_code(cb, db_user=db_user("student", 42))
    assert cb.answers == [("Кнопка устарела.", True)]
    assert msg.answers == []


async def test_retry_empty_theme_no_retry_button(session_factory):
    """0.3 Захода 8: повтор пустой темы — «Заданий пока нет…» + клавиатура
    БЕЗ «🔁 Повторить тему» (петля): «📚 Другие темы» + «← Назад»."""
    subj = await _mk_subject(session_factory)
    user, student = await _mk_student_stub(session_factory, subj.id)
    theme = await _mk_theme(session_factory, subj.id)
    msg = FakeMessage()
    cb = make_cb(f"std:retry:{theme.id}:0", msg)
    await s_h.cb_student_retry(cb, db_user=db_user("student", user.id))
    assert msg.answers[0][0] == "Заданий пока нет. Препод скоро добавит 👩🏫"
    buttons = cb_buttons(msg.answers[0][1])
    assert ("📚 Другие темы", f"std:topics:{theme.id}:0") in buttons
    assert ("← Назад", f"std:topics:{theme.id}:0") in buttons
    assert not any(
        "Повторить" in label or (data or "").startswith("std:retry:")
        for label, data in buttons
    )


async def test_task_card_rendered_by_single_function(session_factory, monkeypatch):
    """0.1 Захода 8: карточка задания рендерится ЕДИНОЙ функцией _send_task_card
    (и при выдаче, и при повторе) — нет двух дублирующихся блоков."""
    calls = []

    async def fake_send(message, result):
        calls.append(result["status"])
        await FakeMessage().answer("card")

    monkeypatch.setattr(s_h, "_send_task_card", fake_send)
    subj = await _mk_subject(session_factory)
    user, student = await _mk_student_stub(session_factory, subj.id)
    theme = await _mk_theme(session_factory, subj.id)
    await _mk_task(session_factory, theme.id)

    msg = FakeMessage()
    await s_h.cb_student_theme(make_cb(f"std:theme:{theme.id}:0", msg),
                               db_user=db_user("student", user.id))
    msg2 = FakeMessage()
    await s_h.cb_student_retry(make_cb(f"std:retry:{theme.id}:0", msg2),
                               db_user=db_user("student", user.id))
    assert calls == [student_svc.TASK_ISSUE_OK, student_svc.TASK_ISSUE_OK]


async def test_guest_plain_text_binds_without_wizard(session_factory, monkeypatch):
    """Мелочь: гость шлёт код текстом ВНЕ визарда — подтверждение (не молчок)."""
    monkeypatch.setattr(
        student_svc.reactions.random, "choice", lambda seq: seq[0]
    )
    guest = await _mk_guest(session_factory)
    subj = await _mk_subject(session_factory)
    user, student = await _mk_student_stub(session_factory, subj.id)
    msg = FakeMessage(text=student.invite_code, from_user_id=guest.tg_id)
    await s_h.on_guest_any_text(msg, state=None, db_user=db_user("guest", guest.tg_id))
    assert len(msg.answers) == 1
    assert msg.answers[0][0] == f"Подтверди, что ты — {user.tg_full_name}?"
    assert ("Да, это я", f"std:bind_yes:{student.invite_code}:0") in cb_buttons(msg.answers[0][1])


async def test_guest_plain_text_unknown_code_hint(session_factory):
    """Мелочь: мусорный текст гостя без визарда → приветствие гостя
    (формат кода не известен человеку), а НЕ «Такого кода нет…»."""
    guest = await _mk_guest(session_factory)
    msg = FakeMessage(text="какой-то мусор", from_user_id=guest.tg_id)
    await s_h.on_guest_any_text(msg, state=None, db_user=db_user("guest", guest.tg_id))
    assert len(msg.answers) == 1
    assert msg.answers[0][0].startswith("Привет! Ты из LevelUp? 👋")
    assert ("🔑 Ввести код", "menu:guest:code:0") in cb_buttons(msg.answers[0][1])


# ---------------------------------------------------------------------------
# Дополнение: предупреждение при повторной привязке деактивированного staff
# ---------------------------------------------------------------------------
async def test_rebind_staff_warning_in_confirm(session_factory):
    """Кикнутый препод с новым кодом: подтверждение СОДЕРЖИТ предупреждение
    «Старый аккаунт (преподаватель/менеджер) будет удалён вместе с данными»."""
    staff = await _mk_user(session_factory, role="teacher")
    subj = await _mk_subject(session_factory)
    user, student = await _mk_student_stub(session_factory, subj.id)
    async with session_factory() as session:
        u = await session.get(User, staff.id)
        u.is_active = False
        await session.commit()
    msg = FakeMessage(text=student.invite_code, from_user_id=staff.tg_id)
    await s_h.on_guest_any_text(
        msg, state=None, db_user=db_user_inactive("teacher", staff.tg_id)
    )
    assert len(msg.answers) == 1
    confirm = msg.answers[0][0]
    assert f"Подтверди, что ты — {user.tg_full_name}?" in confirm
    assert s_h.MSG_REBIND_STAFF_WARNING in confirm
    assert ("Да, это я", f"std:bind_yes:{student.invite_code}:0") in cb_buttons(msg.answers[0][1])


async def test_rebind_no_warning_for_guest(session_factory):
    """Гость с новым кодом: предупреждения НЕТ (обычный флоу, ТЗ 7)."""
    guest = await _mk_guest(session_factory)
    subj = await _mk_subject(session_factory)
    _user, student = await _mk_student_stub(session_factory, subj.id)
    msg = FakeMessage(text=student.invite_code, from_user_id=guest.tg_id)
    await s_h.on_guest_any_text(msg, state=None, db_user=db_user("guest", guest.tg_id))
    assert len(msg.answers) == 1
    assert s_h.MSG_REBIND_STAFF_WARNING not in msg.answers[0][0]


async def test_rebind_no_warning_for_kicked_student(session_factory):
    """Кикнутый ученик с новым кодом: предупреждения НЕТ (только staff)."""
    subj = await _mk_subject(session_factory)
    kicked_user, _kicked_st, _task = await _mk_kicked_student(session_factory, subj.id)
    other_subj = await _mk_subject(session_factory, name="Физика")
    _u2, student = await _mk_student_stub(session_factory, other_subj.id)
    msg = FakeMessage(text=student.invite_code, from_user_id=kicked_user.tg_id)
    await s_h.on_guest_any_text(
        msg, state=None, db_user=db_user_inactive("student", kicked_user.tg_id)
    )
    assert len(msg.answers) == 1
    assert s_h.MSG_REBIND_STAFF_WARNING not in msg.answers[0][0]


# ---------------------------------------------------------------------------
# Заход 7: доступ и edge-кейсы (ТЗ 10, 7, 11, 14)
# ---------------------------------------------------------------------------
async def test_issue_blocked_for_deactivated_student(session_factory):
    """Деактивированный ученик не получает НИ ОДНОЙ выдачи (ТЗ 10):
    issue_task и retry_theme — через can_access → not_for_you."""
    subj = await _mk_subject(session_factory)
    user, student = await _mk_student_stub(session_factory, subj.id)
    theme = await _mk_theme(session_factory, subj.id)
    await _mk_task(session_factory, theme.id)
    async with session_factory() as session:
        u = await session.get(User, user.id)
        u.is_active = False
        await session.commit()
        issued = await student_svc.issue_task(session, user.id, theme.id)
        retried = await student_svc.retry_theme(session, user.id, theme.id)
    assert issued["status"] == student_svc.TASK_ISSUE_NOT_FOR_YOU
    assert retried["status"] == student_svc.TASK_ISSUE_NOT_FOR_YOU
    # прогресс темы не тронут
    async with session_factory() as session:
        progress = (
            await session.scalars(
                select(TaskProgress).where(TaskProgress.student_id == student.id)
            )
        ).all()
    assert progress == []


async def test_feedback_photo_sent_after_answer(session_factory, monkeypatch):
    """ТЗ 9 п.3: объяснение уходит ОДНИМ сообщением ПОСЛЕ реакции; фото+текст
    — фотка с подписью (caption), кнопки действий — НА объяснении, а не на
    реакции (правка владельца)."""
    monkeypatch.setattr(
        student_svc.reactions.random, "choice", lambda seq: seq[0]
    )
    subj = await _mk_subject(session_factory)
    user, student = await _mk_student_stub(session_factory, subj.id)
    theme = await _mk_theme(session_factory, subj.id)
    async with session_factory() as session:
        task = Task(
            theme_id=theme.id,
            question_text="2+2?",
            options=[{"t": "А", "c": True}, {"t": "Б", "c": False}],
            feedback_text="Объяснение текстом",
            feedback_photo_id="FEEDBACK_PHOTO_1",
            is_active=True,
        )
        session.add(task)
        await session.commit()
        task_id = task.id
    correct_pos = student_svc.options_permutation(task_id, student.id, 2).index(0)
    msg = FakeMessage()
    cb = make_cb(f"task:{task_id}:ans:{correct_pos}:0:{DOY}", msg)
    await s_h.cb_task_answer(cb, db_user=db_user("student", user.id))
    # 1. реакция БЕЗ кнопок (кнопки переехали к объяснению)
    assert len(msg.answers) == 1
    reaction_text, actions_kb = msg.answers[0]
    assert actions_kb is None
    assert "Правильный ответ:" not in reaction_text  # ответ верный
    # 2. объяснение — ОДНО фото-сообщение с подписью и кнопками
    assert len(msg.answers_photo) == 1
    photo, caption, kb = msg.answers_photo[0]
    assert photo == "FEEDBACK_PHOTO_1"
    assert caption == "💡 <b>Объяснение:</b>\nОбъяснение текстом"
    actions_buttons = cb_buttons(kb)
    assert ("⏭ Следующее задание", f"std:again:{theme.id}:0") in actions_buttons
    assert ("📚 Вернуться к темам", f"std:topics:{theme.id}:0") in actions_buttons


async def test_feedback_text_only_sent_with_actions(session_factory, monkeypatch):
    """Объяснение ТОЛЬКО текстом: реакция без кнопок → сообщение
    «💡 Объяснение:» с кнопками."""
    monkeypatch.setattr(
        student_svc.reactions.random, "choice", lambda seq: seq[0]
    )
    subj = await _mk_subject(session_factory)
    user, student = await _mk_student_stub(session_factory, subj.id)
    theme = await _mk_theme(session_factory, subj.id)
    async with session_factory() as session:
        task = Task(
            theme_id=theme.id,
            question_text="2+2?",
            options=[{"t": "А", "c": True}, {"t": "Б", "c": False}],
            feedback_text="Разбор по шагам",
            is_active=True,
        )
        session.add(task)
        await session.commit()
        task_id = task.id
    correct_pos = student_svc.options_permutation(task_id, student.id, 2).index(0)
    msg = FakeMessage()
    cb = make_cb(f"task:{task_id}:ans:{correct_pos}:0:{DOY}", msg)
    await s_h.cb_task_answer(cb, db_user=db_user("student", user.id))
    assert len(msg.answers) == 2
    assert msg.answers[0][1] is None  # реакция без кнопок
    exp_text, exp_kb = msg.answers[1]
    assert exp_text == "💡 <b>Объяснение:</b>\nРазбор по шагам"
    assert ("⏭ Следующее задание", f"std:again:{theme.id}:0") in cb_buttons(exp_kb)
    assert msg.answers_photo == []


async def test_feedback_photo_only_sent_with_actions(session_factory, monkeypatch):
    """Объяснение ТОЛЬКО фото: реакция без кнопок → голое фото (без
    подписи) с кнопками действий."""
    monkeypatch.setattr(
        student_svc.reactions.random, "choice", lambda seq: seq[0]
    )
    subj = await _mk_subject(session_factory)
    user, student = await _mk_student_stub(session_factory, subj.id)
    theme = await _mk_theme(session_factory, subj.id)
    async with session_factory() as session:
        task = Task(
            theme_id=theme.id,
            question_text="2+2?",
            options=[{"t": "А", "c": True}, {"t": "Б", "c": False}],
            feedback_photo_id="FEEDBACK_PHOTO_2",
            is_active=True,
        )
        session.add(task)
        await session.commit()
        task_id = task.id
    correct_pos = student_svc.options_permutation(task_id, student.id, 2).index(0)
    msg = FakeMessage()
    cb = make_cb(f"task:{task_id}:ans:{correct_pos}:0:{DOY}", msg)
    await s_h.cb_task_answer(cb, db_user=db_user("student", user.id))
    assert len(msg.answers) == 1
    assert msg.answers[0][1] is None  # реакция без кнопок
    assert len(msg.answers_photo) == 1
    photo, caption, kb = msg.answers_photo[0]
    assert photo == "FEEDBACK_PHOTO_2"
    assert caption is None  # только фото — без подписи
    assert ("⏭ Следующее задание", f"std:again:{theme.id}:0") in cb_buttons(kb)

# ---------------------------------------------------------------------------
# Босс и преподаватель решают задания (владелец: «дай боссу и преподу решать»)
# ---------------------------------------------------------------------------
async def test_owner_solves_all_subjects(session_factory, monkeypatch):
    """Босс: «📚 Мои предметы» → ВСЕ предметы, темы (включая закрытые),
    решение и прогресс на теневом профиле."""
    monkeypatch.setattr(
        student_svc.reactions.random, "choice", lambda seq: seq[0]
    )
    owner = await _mk_user(session_factory, role="owner")
    subj1 = await _mk_subject(session_factory, "Математика")
    subj2 = await _mk_subject(session_factory, "Физика")
    theme = await _mk_theme(session_factory, subj1.id, "Уравнения", is_open=False)
    task = await _mk_task(session_factory, theme.id)

    # меню: выбор предмета (оба) → темы предмета (закрытая тема видна)
    msg = FakeMessage()
    await s_h.cb_menu_student_subjects(
        make_cb("menu:student:subjects:0", msg), db_user=db_user("owner", owner.id)
    )
    pick_buttons = cb_buttons(msg.answers[0][1])
    assert ("Математика", f"std:subj:{subj1.id}:0") in pick_buttons
    assert ("Физика", f"std:subj:{subj2.id}:0") in pick_buttons
    msg1b = FakeMessage()
    await s_h.cb_student_subject(
        make_cb(f"std:subj:{subj1.id}:0", msg1b), db_user=db_user("owner", owner.id)
    )
    buttons = cb_buttons(msg1b.answers[0][1])
    assert ("🔓 Уравнения — осталось 1", f"std:theme:{theme.id}:0") in buttons

    # вход в тему (даже закрытую) + решение; позиция правильного варианта
    # читается из карточки (перемешивание по дню, perm теневого профиля)
    msg2 = FakeMessage()
    await s_h.cb_student_theme(
        make_cb(f"std:theme:{theme.id}:0", msg2), db_user=db_user("owner", owner.id)
    )
    card_text, card_kb = msg2.answers[0]
    assert card_text.startswith("Решено 0 из 1 · Осталось 1")
    correct_pos = next(
        int(cb.split(":")[3])
        for text, cb in cb_buttons(card_kb)
        if cb.startswith("task:") and text.endswith("Вариант 0")
    )
    msg3 = FakeMessage()
    await s_h.cb_task_answer(
        make_cb(f"task:{task.id}:ans:{correct_pos}:0:{DOY}", msg3),
        db_user=db_user("owner", owner.id),
    )
    assert msg3.answers[0][0] == "Вот это точно! Так держать"
    async with session_factory() as session:
        progress = (
            await session.scalars(
                select(TaskProgress).where(TaskProgress.student_id.in_(
                    select(Student.id).where(Student.user_id == owner.id)
                ))
            )
        ).all()
    assert len(progress) == 1


async def test_teacher_solves_only_own_subjects(session_factory, monkeypatch):
    """Препод: «🎯 Решать задания» — ТОЛЬКО свои предметы (teacher_subjects),
    чужих нет; свои решает без ограничений."""
    monkeypatch.setattr(
        student_svc.reactions.random, "choice", lambda seq: seq[0]
    )
    teacher = await _mk_user(session_factory, role="teacher")
    mine = await _mk_subject(session_factory, "Мой предмет")
    alien = await _mk_subject(session_factory, "Чужой предмет")
    from app.models import TeacherSubject
    async with session_factory() as session:
        session.add(TeacherSubject(teacher_id=teacher.id, subject_id=mine.id))
        await session.commit()
    theme = await _mk_theme(session_factory, mine.id, "Тема", is_open=False)
    task = await _mk_task(session_factory, theme.id)

    msg = FakeMessage()
    await s_h.cb_menu_student_subjects(
        make_cb("menu:student:subjects:0", msg), db_user=db_user("teacher", teacher.id)
    )
    buttons = cb_buttons(msg.answers[0][1])
    assert ("Мой предмет", f"std:subj:{mine.id}:0") in buttons
    assert not any("Чужой" in label for label, _ in buttons)
    assert ("← Назад", "menu:back:teacher:0") in buttons

    # свой предмет → темы (закрытая видна) → вход в тему
    msg1b = FakeMessage()
    await s_h.cb_student_subject(
        make_cb(f"std:subj:{mine.id}:0", msg1b), db_user=db_user("teacher", teacher.id)
    )
    assert ("🔓 Тема — осталось 1", f"std:theme:{theme.id}:0") in cb_buttons(msg1b.answers[0][1])

    msg2 = FakeMessage()
    await s_h.cb_student_theme(
        make_cb(f"std:theme:{theme.id}:0", msg2), db_user=db_user("teacher", teacher.id)
    )
    assert msg2.answers[0][0].startswith("Решено 0 из 1 · Осталось 1")


# ---------------------------------------------------------------------------
# Правки владельца: teacher-гейт по teacher_subjects, битая тема у staff,
# косметика «— 0 заданий» в self_mode
# ---------------------------------------------------------------------------
async def test_teacher_loses_access_to_unlinked_theme(session_factory):
    """Отвязали предмет от препода → старые кнопки гаснут: issue/retry по
    чужой теме → not_for_you, прогресс НЕ сбрасывается."""
    from app.models import TeacherSubject

    teacher = await _mk_user(session_factory, role="teacher")
    subj = await _mk_subject(session_factory, "Мой предмет")
    theme = await _mk_theme(session_factory, subj.id)
    await _mk_task(session_factory, theme.id)
    async with session_factory() as session:
        session.add(TeacherSubject(teacher_id=teacher.id, subject_id=subj.id))
        await session.commit()

    # своя тема — выдаётся
    async with session_factory() as session:
        result = await student_svc.issue_task(session, teacher.id, theme.id)
    assert result["status"] == student_svc.TASK_ISSUE_OK

    # предмет отвязан — старая кнопка гаснет
    async with session_factory() as session:
        await session.execute(
            TeacherSubject.__table__.delete().where(
                TeacherSubject.teacher_id == teacher.id
            )
        )
        await session.commit()
        result = await student_svc.issue_task(session, teacher.id, theme.id)
        retried = await student_svc.retry_theme(session, teacher.id, theme.id)
    assert result["status"] == student_svc.TASK_ISSUE_NOT_FOR_YOU
    assert retried["status"] == student_svc.TASK_ISSUE_NOT_FOR_YOU


async def test_staff_broken_theme_button_is_not_found(session_factory):
    """Битая кнопка (темы нет) у staff → «Тема не найдена», а НЕ
    «Заданий пока нет»."""
    owner = await _mk_user(session_factory, role="owner")
    async with session_factory() as session:
        result = await student_svc.issue_task(session, owner.id, 999999)
    assert result["status"] == student_svc.TASK_ISSUE_NOT_FOUND


async def test_staff_empty_theme_label_self_mode(session_factory):
    """self_mode (босс/препод): пустая тема подписана «— {Тема} — 0 заданий»,
    а не активным «осталось 0»."""
    owner = await _mk_user(session_factory, role="owner")
    subj = await _mk_subject(session_factory, "Математика")
    theme = await _mk_theme(session_factory, subj.id, "Пустая тема")

    msg = FakeMessage()
    await s_h.cb_menu_student_subjects(
        make_cb("menu:student:subjects:0", msg), db_user=db_user("owner", owner.id)
    )
    msg1b = FakeMessage()
    await s_h.cb_student_subject(
        make_cb(f"std:subj:{subj.id}:0", msg1b), db_user=db_user("owner", owner.id)
    )
    buttons = cb_buttons(msg1b.answers[0][1])
    assert ("— Пустая тема — 0 заданий", f"std:theme:{theme.id}:0") in buttons
    assert not any("осталось 0" in label for label, _ in buttons)


# ---------------------------------------------------------------------------
# Перемешивание вариантов (фича 2): детерминизм по (task, student, дата Минск)
# ---------------------------------------------------------------------------
async def test_options_permutation_stable_and_varied():
    """Порядок стабилен в рамках дня (карточка и перевыдача совпадают),
    меняется между днями и между учениками; perm — перестановка."""
    perm_a = student_svc.options_permutation(1, 2, 4, day=TODAY)
    perm_b = student_svc.options_permutation(1, 2, 4, day=TODAY)
    assert perm_a == perm_b
    assert sorted(perm_a) == [0, 1, 2, 3]
    # hash-коллизия для конкретного завтрашнего дня возможна (seed из sha1),
    # поэтому проверяем вариативность по дням в целом, а не строго +1 день
    varied_days = [
        student_svc.options_permutation(1, 2, 4, day=TODAY + timedelta(days=d))
        for d in range(1, 16)
    ]
    assert any(d != perm_a for d in varied_days)
    assert student_svc.options_permutation(1, 3, 4, day=TODAY) != perm_a


async def test_check_answer_uses_permuted_positions(session_factory):
    """Ответ в check_answer сверяется по options[perm[index]]: нажатие
    позиции правильного варианта — правильно, БД-порядок не меняется."""
    subj = await _mk_subject(session_factory)
    user, student = await _mk_student_stub(session_factory, subj.id)
    theme = await _mk_theme(session_factory, subj.id)
    task = await _mk_task(session_factory, theme.id, correct=1)
    perm = student_svc.options_permutation(task.id, student.id, 4)
    correct_pos = perm.index(1)
    wrong_pos = next(i for i in range(4) if i != correct_pos)
    async with session_factory() as session:
        ok = await student_svc.check_answer(session, user.id, task.id, correct_pos, seq=0)
        bad = await student_svc.check_answer(session, user.id, task.id, wrong_pos, seq=1)
    assert ok["status"] == student_svc.ANSWER_OK and ok["is_correct"] is True
    assert bad["status"] == student_svc.ANSWER_OK and bad["is_correct"] is False
    async with session_factory() as session:
        opts = (await session.get(Task, task.id)).options
        assert opts[1]["c"] is True and opts[0]["c"] is False  # БД не тронут


# ---------------------------------------------------------------------------
# Режим «🔁 Ошибки» (фича 1): wrong-задания повторяются до «разобраны»
# ---------------------------------------------------------------------------
async def test_errors_theme_flow(session_factory, monkeypatch):
    """Кнопка «🔁 Повторить ошибки» в «Мои предметы» (wrong_count>0) →
    карточка wrong-задания (`:e`-кнопки, perm как в обычной выдаче,
    шапка «Ошибки · осталось N») → правильный ответ переводит wrong→done →
    «🔁 Следующая ошибка» → «Ошибки разобраны! 🎉»."""
    monkeypatch.setattr(
        student_svc.reactions.random, "choice", lambda seq: seq[0]
    )
    subj = await _mk_subject(session_factory)
    user, student = await _mk_student_stub(session_factory, subj.id)
    theme = await _mk_theme(session_factory, subj.id, "Уравнения")
    t1 = await _mk_task(session_factory, theme.id, "2+2?", correct=1)
    await _mk_task(session_factory, theme.id, "3+3?", correct=0)

    # 1. неправильный ответ по t1 → TaskProgress.status = wrong
    perm = student_svc.options_permutation(t1.id, student.id, 4)
    wrong_pos = next(i for i in range(4) if perm[i] != 1)
    async with session_factory() as session:
        result = await student_svc.check_answer(session, user.id, t1.id, wrong_pos, seq=0)
    assert result["status"] == student_svc.ANSWER_OK
    assert result["is_correct"] is False

    # 2. «Мои предметы»: выбор предмета → кнопка «🔁 Повторить ошибки «Уравнения»»
    msg = FakeMessage()
    await s_h.cb_menu_student_subjects(
        make_cb("menu:student:subjects:0", msg), db_user=db_user("student", user.id)
    )
    msg1b = FakeMessage()
    await s_h.cb_student_subject(
        make_cb(f"std:subj:{subj.id}:0", msg1b), db_user=db_user("student", user.id)
    )
    buttons = cb_buttons(msg1b.answers[0][1])
    assert ("🔁 Повторить ошибки «Уравнения»", f"std:errors:{theme.id}:0") in buttons

    # 3. вход в ошибки: карточка wrong-задания, шапка, кнопки с «:e»
    msg2 = FakeMessage()
    await s_h.cb_student_errors(
        make_cb(f"std:errors:{theme.id}:0", msg2), db_user=db_user("student", user.id)
    )
    err_text, err_kb = msg2.answers[0]
    assert err_text.startswith("🔁 Ошибки · осталось 1")
    correct_pos = perm.index(1)
    buttons2 = cb_buttons(err_kb)
    assert (
        f"{_LETTERS[correct_pos]}. Вариант 1",
        f"task:{t1.id}:ans:{correct_pos}:1:{DOY}:e",
    ) in buttons2

    # 4. правильный ответ по wrong-заданию: wrong→done, «Следующая ошибка»
    msg3 = FakeMessage()
    await s_h.cb_task_answer(
        make_cb(f"task:{t1.id}:ans:{correct_pos}:1:{DOY}:e", msg3),
        db_user=db_user("student", user.id),
    )
    reaction_text, actions_kb = msg3.answers[0]
    assert "Правильный ответ:" not in reaction_text  # ответ верный
    act = cb_buttons(actions_kb)
    assert ("🔁 Следующая ошибка", f"std:err_next:{theme.id}:0") in act
    assert "🎯 Ещё задание" not in [b[0] for b in act]
    async with session_factory() as session:
        prog = await session.get(TaskProgress, (student.id, t1.id))
        assert prog.status == "done"

    # 5. «Следующая ошибка» — новых wrong нет → «Ошибки разобраны!»
    msg4 = FakeMessage()
    await s_h.cb_student_err_next(
        make_cb(f"std:err_next:{theme.id}:0", msg4), db_user=db_user("student", user.id)
    )
    assert msg4.answers[0][0] == "Ошибки разобраны! 🎉"
    done_buttons = cb_buttons(msg4.answers[0][1])
    assert ("📚 Вернуться к темам", f"std:topics:{theme.id}:0") in done_buttons

    # 6. кнопка во «Мн предметы» пропала (wrong_count = 0)
    msg5 = FakeMessage()
    await s_h.cb_menu_student_subjects(
        make_cb("menu:student:subjects:0", msg5), db_user=db_user("student", user.id)
    )
    buttons5 = cb_buttons(msg5.answers[0][1])
    assert all("Ошибки" not in b[0] for b in buttons5)


async def test_errors_done_when_no_wrong_tasks(session_factory):
    """«🔁 Ошибки» без wrong-записей → сразу «Ошибки разобраны!»."""
    subj = await _mk_subject(session_factory)
    user, student = await _mk_student_stub(session_factory, subj.id)
    theme = await _mk_theme(session_factory, subj.id)
    await _mk_task(session_factory, theme.id)
    msg = FakeMessage()
    await s_h.cb_student_errors(
        make_cb(f"std:errors:{theme.id}:0", msg), db_user=db_user("student", user.id)
    )
    assert msg.answers[0][0] == "Ошибки разобраны! 🎉"


async def test_errors_mode_respects_theme_closed(session_factory):
    """Доступ при выдаче ошибок — как у обычной выдачи: закрытая тема
    → «Тема закрыта, задания пока не выдаём.» (без ИЗМЕНЕНИЙ прогресса)."""
    subj = await _mk_subject(session_factory)
    user, student = await _mk_student_stub(session_factory, subj.id)
    theme = await _mk_theme(session_factory, subj.id, is_open=False)
    task = await _mk_task(session_factory, theme.id)
    async with session_factory() as session:
        await student_svc.check_answer(session, user.id, task.id, 0, seq=0)
    msg = FakeMessage()
    await s_h.cb_student_errors(
        make_cb(f"std:errors:{theme.id}:0", msg), db_user=db_user("student", user.id)
    )
    assert msg.answers[0][0] == "Тема закрыта, задания пока не выдаём."
    assert msg.answers[0][1] is None
