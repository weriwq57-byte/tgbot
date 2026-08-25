"""Преподаватель: предметы и темы (Заход 4): сервисы teacher.py и handlers.

Сервисные тесты — на SQLite in-memory (session_factory); хендлеры —
прямыми вызовами с фейками (как в test_owner.py/test_students.py),
db_user: SimpleNamespace(role, id) — роль всегда из БД.
Каскадное удаление проверяется на реальных записях task/attempt/progress.
"""
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.handlers import teacher as t_h
from app.models import (
    Attempt,
    Subtheme,
    Subject,
    Task,
    TaskProgress,
    TeacherSubject,
    Theme,
    User,
)
from app.services import students as students_svc
from app.services import teacher as teacher_svc
from app.states import AddThemeStates, DeleteThemeStates, RenameThemeStates
from app.utils.dates import today_minsk

TODAY = today_minsk()


class FakeMessage:
    """Message: answer пишет в .answers, edit_text — в .edits."""

    def __init__(self, text="", chat_id=42):
        self.text = text
        self.chat = SimpleNamespace(id=chat_id)
        self.answers = []
        self.edits = []

    async def answer(self, content="", reply_markup=None, **kwargs):
        self.answers.append((content, reply_markup))

    async def edit_text(self, text, reply_markup=None, **kwargs):
        self.edits.append((text, reply_markup))


def make_cb(data: str, message: FakeMessage):
    answers = []

    async def answer(content="", show_alert=False, **kwargs):
        answers.append((content, show_alert))

    return SimpleNamespace(data=data, message=message, answer=answer, answers=answers)


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


_SEQ = {"n": 0}


def db_user(role: str, user_id: int | None = None) -> SimpleNamespace:
    return SimpleNamespace(role=role, id=user_id)


async def _mk_user(session_factory, role="teacher") -> User:
    _SEQ["n"] += 1
    async with session_factory() as session:
        user = User(
            tg_id=200000000 + _SEQ["n"],
            tg_username=f"{role}_{_SEQ['n']}",
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


async def _grant(session_factory, teacher_id: int, subject_id: int) -> None:
    async with session_factory() as session:
        session.add(TeacherSubject(teacher_id=teacher_id, subject_id=subject_id))
        await session.commit()


async def _mk_theme(
    session_factory, subject_id: int, title="Уравнения", is_open=False
) -> Theme:
    async with session_factory() as session:
        theme = Theme(
            subject_id=subject_id, title=title, is_open=is_open, mode="sequential"
        )
        session.add(theme)
        await session.commit()
        return theme


# ---------------------------------------------------------------------------
# Сервисы: list_teacher_subjects (свои/все, counts, сортировка)
# ---------------------------------------------------------------------------
async def test_teacher_subjects_only_own(session_factory):
    teacher = await _mk_user(session_factory)
    own = await _mk_subject(session_factory, "Математика")
    other = await _mk_subject(session_factory, "Физика")
    await _grant(session_factory, teacher.id, own.id)
    await _mk_theme(session_factory, own.id, title="Уравнения")
    await _mk_theme(session_factory, other.id, title="Чужая тема")

    async with session_factory() as session:
        data = await teacher_svc.list_teacher_subjects(session, teacher.id)
    assert [item["subject"].id for item in data] == [own.id]
    assert len(data[0]["themes"]) == 1
    assert data[0]["themes"][0][0].title == "Уравнения"


async def test_list_teacher_subjects_owner_sees_all(session_factory):
    owner = await _mk_user(session_factory, role="owner")
    s1 = await _mk_subject(session_factory, "Математика")
    s2 = await _mk_subject(session_factory, "Физика")
    async with session_factory() as session:
        data = await teacher_svc.list_teacher_subjects(session, owner.id)
    assert {item["subject"].id for item in data} == {s1.id, s2.id}


async def test_list_subjects_hides_inactive_subject(session_factory):
    teacher = await _mk_user(session_factory)
    active = await _mk_subject(session_factory, "Математика")
    hidden = await _mk_subject(session_factory, "Физика")
    await _grant(session_factory, teacher.id, active.id)
    await _grant(session_factory, teacher.id, hidden.id)
    async with session_factory() as session:
        subject = await session.get(Subject, hidden.id)
        subject.is_active = False
        await session.commit()
    async with session_factory() as session:
        data = await teacher_svc.list_teacher_subjects(session, teacher.id)
    assert [item["subject"].id for item in data] == [active.id]


async def test_list_subjects_task_counts(session_factory):
    teacher = await _mk_user(session_factory)
    subject = await _mk_subject(session_factory)
    await _grant(session_factory, teacher.id, subject.id)
    theme = await _mk_theme(session_factory, subject.id)
    async with session_factory() as session:
        session.add(Task(theme_id=theme.id, options=[{"t": "1", "c": True}]))
        session.add(Task(theme_id=theme.id, options=[{"t": "2", "c": True}]))
        session.add(Task(theme_id=theme.id, options=[{"t": "3", "c": True}], is_active=False))
        await session.commit()
    async with session_factory() as session:
        data = await teacher_svc.list_teacher_subjects(session, teacher.id)
    theme2, count = data[0]["themes"][0]
    assert theme2.id == theme.id
    assert count == 2  # скрытое задание не в счётчике


# ---------------------------------------------------------------------------
# Сервисы: add_theme
# ---------------------------------------------------------------------------
async def test_add_theme_created_closed_sequential(session_factory):
    teacher = await _mk_user(session_factory)
    subject = await _mk_subject(session_factory)
    await _grant(session_factory, teacher.id, subject.id)
    async with session_factory() as session:
        theme = await teacher_svc.add_theme(
            session, teacher.id, subject.id, "Теория чисел"
        )
    assert theme.subject_id == subject.id
    assert theme.title == "Теория чисел"
    assert theme.is_open is False
    assert theme.mode == "sequential"
    assert theme.created_by == teacher.id


async def test_add_theme_foreign_subject_forbidden(session_factory):
    teacher = await _mk_user(session_factory)
    subject = await _mk_subject(session_factory)
    with pytest.raises(ValueError):
        async with session_factory() as session:
            await teacher_svc.add_theme(session, teacher.id, subject.id, "Тема")


async def test_add_theme_owner_any_subject(session_factory):
    owner = await _mk_user(session_factory, role="owner")
    subject = await _mk_subject(session_factory)
    async with session_factory() as session:
        theme = await teacher_svc.add_theme(session, owner.id, subject.id, "Тема")
    assert theme.subject_id == subject.id


async def test_add_theme_title_validation(session_factory):
    teacher = await _mk_user(session_factory)
    subject = await _mk_subject(session_factory)
    await _grant(session_factory, teacher.id, subject.id)
    for bad in ["", "   ", "/command", "Т" * 201]:
        with pytest.raises(ValueError):
            async with session_factory() as session:
                await teacher_svc.add_theme(session, teacher.id, subject.id, bad)


# ---------------------------------------------------------------------------
# Сервисы: get_theme_for_teacher, toggle, rename
# ---------------------------------------------------------------------------
async def test_get_theme_rights(session_factory):
    teacher = await _mk_user(session_factory)
    owner = await _mk_user(session_factory, role="owner")
    own_subject = await _mk_subject(session_factory, "Математика")
    other_subject = await _mk_subject(session_factory, "Физика")
    await _grant(session_factory, teacher.id, own_subject.id)
    own_theme = await _mk_theme(session_factory, own_subject.id, "Своя")
    other_theme = await _mk_theme(session_factory, other_subject.id, "Чужая")

    async with session_factory() as session:
        got = await teacher_svc.get_theme_for_teacher(
            session, teacher.id, own_theme.id
        )
        assert got is not None and got.title == "Своя"
        assert (
            await teacher_svc.get_theme_for_teacher(
                session, teacher.id, other_theme.id
            )
            is None
        )
        assert (
            await teacher_svc.get_theme_for_teacher(session, owner.id, other_theme.id)
            is not None
        )
        assert (
            await teacher_svc.get_theme_for_teacher(session, teacher.id, 99999)
            is None
        )


async def test_toggle_theme_open(session_factory):
    subject = await _mk_subject(session_factory)
    theme = await _mk_theme(session_factory, subject.id)
    async with session_factory() as session:
        assert await teacher_svc.toggle_theme_open(session, theme.id) is True
    async with session_factory() as session:
        assert (await session.get(Theme, theme.id)).is_open is True
    async with session_factory() as session:
        assert await teacher_svc.toggle_theme_open(session, theme.id) is False
    async with session_factory() as session:
        assert await teacher_svc.toggle_theme_open(session, 99999) is None


async def test_rename_theme(session_factory):
    subject = await _mk_subject(session_factory)
    theme = await _mk_theme(session_factory, subject.id)
    async with session_factory() as session:
        assert await teacher_svc.rename_theme(session, theme.id, "Новое имя") is True
    async with session_factory() as session:
        assert (await session.get(Theme, theme.id)).title == "Новое имя"
    async with session_factory() as session:
        assert await teacher_svc.rename_theme(session, 99999, "Х") is False
    with pytest.raises(ValueError):
        async with session_factory() as session:
            await teacher_svc.rename_theme(session, theme.id, "")


# ---------------------------------------------------------------------------
# Сервисы: delete_theme — каскад заданий, прогресса и попыток
# ---------------------------------------------------------------------------
async def test_delete_theme_cascades(session_factory):
    """Удаление темы убирает задания, подтемы, attempts и task_progress."""
    subject = await _mk_subject(session_factory)
    theme = await _mk_theme(session_factory, subject.id)
    manager = await _mk_user(session_factory, role="manager")
    async with session_factory() as session:
        _, student, _ = await students_svc.create_student_record(
            session, "Иван", {subject.id}, manager.id, TODAY + timedelta(days=30)
        )
        sid = student.id
    async with session_factory() as session:
        subtheme = Subtheme(theme_id=theme.id, title="Подтема")
        session.add(subtheme)
        await session.flush()
        task1 = Task(
            theme_id=theme.id,
            subtheme_id=subtheme.id,
            options=[{"t": "а", "c": True}],
        )
        task2 = Task(theme_id=theme.id, options=[{"t": "б", "c": True}])
        session.add_all([task1, task2])
        await session.flush()
        session.add(Attempt(student_id=sid, task_id=task1.id, is_correct=True))
        session.add(TaskProgress(student_id=sid, task_id=task2.id, status="done"))
        await session.commit()

    async with session_factory() as session:
        assert await teacher_svc.delete_theme(session, theme.id) is True
    async with session_factory() as session:
        assert await session.get(Theme, theme.id) is None
        assert (await session.scalars(select(Task))).all() == []
        assert (await session.scalars(select(Subtheme))).all() == []
        assert (await session.scalars(select(Attempt))).all() == []
        assert (await session.scalars(select(TaskProgress))).all() == []
    async with session_factory() as session:
        assert await teacher_svc.delete_theme(session, 99999) is False


async def test_list_theme_tasks_order(session_factory):
    subject = await _mk_subject(session_factory)
    theme = await _mk_theme(session_factory, subject.id)
    async with session_factory() as session:
        for text, order in [("Первый", 2), ("Второй", 1), ("Третий", 0)]:
            session.add(
                Task(
                    theme_id=theme.id,
                    question_text=text,
                    order=order,
                    options=[{"t": "1", "c": True}],
                )
            )
        await session.commit()
    async with session_factory() as session:
        rows = await teacher_svc.list_theme_tasks(session, theme.id)
    assert [r["task"].question_text for r in rows] == ["Третий", "Второй", "Первый"]
    assert all(r["subtheme"] is None for r in rows)


# ---------------------------------------------------------------------------
# Хендлеры: «Мои предметы»
# ---------------------------------------------------------------------------
async def test_cmd_my_subjects_text_and_buttons(session_factory):
    teacher = await _mk_user(session_factory)
    subject = await _mk_subject(session_factory, "Математика")
    await _grant(session_factory, teacher.id, subject.id)
    await _mk_theme(session_factory, subject.id, "Уравнения", is_open=True)
    await _mk_theme(session_factory, subject.id, "Теория чисел")

    msg = FakeMessage()
    await t_h.cmd_my_subjects(msg, db_user=db_user("teacher", teacher.id))
    text, kb = msg.answers[-1]
    assert "📚 Мои предметы" in text
    assert "Математика" in text
    assert "🔓 по порядку Уравнения" in text
    assert "🔒 закрыта Теория чисел" in text
    cbs = [c for _, c in cb_buttons(kb)]
    assert f"tch:subj:{subject.id}:0" in cbs
    assert sum(c.startswith("tch:theme:") for c in cbs) == 2


async def test_cmd_my_subjects_empty_by_role(session_factory):
    teacher = await _mk_user(session_factory)
    owner = await _mk_user(session_factory, role="owner")

    msg = FakeMessage()
    await t_h.cmd_my_subjects(msg, db_user=db_user("owner", owner.id))
    assert msg.answers[-1][0] == t_h.TEXT_NO_SUBJECTS_OWNER

    msg2 = FakeMessage()
    await t_h.cmd_my_subjects(msg2, db_user=db_user("teacher", teacher.id))
    assert msg2.answers[-1][0] == t_h.TEXT_NO_SUBJECTS_TEACHER


async def test_cb_my_subjects_edits(session_factory):
    owner = await _mk_user(session_factory, role="owner")
    msg = FakeMessage()
    cb = make_cb("menu:teacher:subjects:0", msg)
    await t_h.cb_my_subjects(cb, db_user=db_user("owner", owner.id))
    assert msg.edits


async def test_cb_theme_list(session_factory):
    teacher = await _mk_user(session_factory)
    subject = await _mk_subject(session_factory)
    await _grant(session_factory, teacher.id, subject.id)
    theme = await _mk_theme(session_factory, subject.id, "Уравнения")

    msg = FakeMessage()
    cb = make_cb(f"tch:subj:{subject.id}:0", msg)
    await t_h.cb_theme_list(cb, db_user=db_user("teacher", teacher.id))
    text, kb = msg.edits[-1]
    assert "Математика" in text
    assert "🔒 закрыта Уравнения" in text
    assert ("➕ Добавить тему", f"tch:add_theme:{subject.id}:0") in cb_buttons(kb)


async def test_cb_theme_list_foreign_subject_alert(session_factory):
    teacher = await _mk_user(session_factory)
    subject = await _mk_subject(session_factory)
    msg = FakeMessage()
    cb = make_cb(f"tch:subj:{subject.id}:0", msg)
    await t_h.cb_theme_list(cb, db_user=db_user("teacher", teacher.id))
    assert any("Предмет не найден" in t for t, _ in cb.answers)


async def test_cb_theme_list_stale(session_factory):
    msg = FakeMessage()
    cb = make_cb("tch:subj:99999:0", msg)
    await t_h.cb_theme_list(cb, db_user=db_user("teacher", 1))
    assert any("Предмет не найден" in t for t, _ in cb.answers)


# ---------------------------------------------------------------------------
# Хендлеры: визард «Добавить тему»
# ---------------------------------------------------------------------------
async def test_add_theme_wizard_full_flow(session_factory):
    teacher = await _mk_user(session_factory)
    subject = await _mk_subject(session_factory, "Математика")
    await _grant(session_factory, teacher.id, subject.id)

    msg = FakeMessage()
    state = await make_fsm()
    cb = make_cb(f"tch:add_theme:{subject.id}:0", msg)
    await t_h.cb_add_theme(cb, state, db_user=db_user("teacher", teacher.id))
    assert msg.answers[-1][0] == t_h.ASK_THEME_TITLE
    assert await state.get_state() == AddThemeStates.name

    msg2 = FakeMessage(text="Уравнения")
    await t_h.on_add_theme_name(msg2, state, db_user=db_user("teacher", teacher.id))
    assert any("✅ Тема «Уравнения» создана." in t for t, _ in msg2.answers)
    body, kb = msg2.answers[-1]
    assert "📚 Уравнения" in body

    async with session_factory() as session:
        theme = await session.scalar(select(Theme).where(Theme.title == "Уравнения"))
        assert theme is not None
        assert theme.is_open is False
        assert theme.mode == "sequential"
        assert ("📝 Задания", f"tch:tasks:{theme.id}:0") in cb_buttons(kb)
    assert await state.get_state() is None  # state.clear() после успеха


async def test_add_theme_wizard_bad_title(session_factory):
    teacher = await _mk_user(session_factory)
    subject = await _mk_subject(session_factory)
    await _grant(session_factory, teacher.id, subject.id)
    msg0 = FakeMessage()
    state = await make_fsm()
    cb = make_cb(f"tch:add_theme:{subject.id}:0", msg0)
    await t_h.cb_add_theme(cb, state, db_user=db_user("teacher", teacher.id))

    for bad in ["", "/command", "Т" * 201]:
        msg = FakeMessage(text=bad)
        await t_h.on_add_theme_name(msg, state, db_user=db_user("teacher", teacher.id))
        assert not any("✅ Тема" in t for t, _ in msg.answers)
        assert await state.get_state() == AddThemeStates.name  # остаёмся в визарде


async def test_add_theme_wizard_no_rights(session_factory):
    teacher = await _mk_user(session_factory)
    subject = await _mk_subject(session_factory)  # не назначен учителю
    msg = FakeMessage()
    state = await make_fsm()
    cb = make_cb(f"tch:add_theme:{subject.id}:0", msg)
    await t_h.cb_add_theme(cb, state, db_user=db_user("teacher", teacher.id))
    assert msg.answers[-1][0] == "У тебя нет доступа к этому предмету."
    assert await state.get_state() is None


async def test_cmd_add_theme_single_subject_goes_to_wizard(session_factory):
    teacher = await _mk_user(session_factory)
    subject = await _mk_subject(session_factory)
    await _grant(session_factory, teacher.id, subject.id)
    msg = FakeMessage()
    state = await make_fsm()
    await t_h.cmd_add_theme(msg, state, db_user=db_user("teacher", teacher.id))
    assert msg.answers[-1][0] == t_h.ASK_THEME_TITLE
    assert await state.get_state() == AddThemeStates.name


async def test_cmd_add_theme_choose_subject(session_factory):
    teacher = await _mk_user(session_factory)
    s1 = await _mk_subject(session_factory, "Математика")
    s2 = await _mk_subject(session_factory, "Физика")
    await _grant(session_factory, teacher.id, s1.id)
    await _grant(session_factory, teacher.id, s2.id)
    msg = FakeMessage()
    state = await make_fsm()
    await t_h.cmd_add_theme(msg, state, db_user=db_user("teacher", teacher.id))
    text, kb = msg.answers[-1]
    assert "В какой предмет добавить тему?" in text
    assert f"tch:add_theme:{s1.id}:0" in [c for _, c in cb_buttons(kb)]


async def test_cmd_add_theme_no_subjects(session_factory):
    teacher = await _mk_user(session_factory)
    msg = FakeMessage()
    state = await make_fsm()
    await t_h.cmd_add_theme(msg, state, db_user=db_user("teacher", teacher.id))
    assert msg.answers[-1][0] == t_h.TEXT_NO_SUBJECTS_TEACHER


# ---------------------------------------------------------------------------
# Хендлеры: меню темы и открытие/закрытие
# ---------------------------------------------------------------------------
async def test_cb_theme_menu(session_factory):
    teacher = await _mk_user(session_factory)
    subject = await _mk_subject(session_factory)
    await _grant(session_factory, teacher.id, subject.id)
    theme = await _mk_theme(session_factory, subject.id, "Уравнения")
    async with session_factory() as session:
        for _ in range(3):
            session.add(Task(theme_id=theme.id, options=[{"t": "1", "c": True}]))
        await session.commit()

    msg = FakeMessage()
    cb = make_cb(f"tch:theme:{theme.id}:0", msg)
    await t_h.cb_theme_menu(cb, db_user=db_user("teacher", teacher.id))
    text, kb = msg.edits[-1]
    assert "📚 Уравнения" in text
    assert "📝 Заданий: 3" in text
    buttons = cb_buttons(kb)
    assert ("🔓 Открыть тему", f"tch:th_open:{theme.id}:0") in buttons
    assert ("📝 Задания", f"tch:tasks:{theme.id}:0") in buttons
    assert ("✏️ Переименовать", f"tch:rename:{theme.id}:0") in buttons
    assert ("🗑 Удалить тему", f"tch:delete:{theme.id}:0") in buttons


async def test_cb_theme_menu_foreign_alert(session_factory):
    teacher = await _mk_user(session_factory)
    subject = await _mk_subject(session_factory)
    theme = await _mk_theme(session_factory, subject.id)
    msg = FakeMessage()
    cb = make_cb(f"tch:theme:{theme.id}:0", msg)
    await t_h.cb_theme_menu(cb, db_user=db_user("teacher", teacher.id))
    assert any("Тема не найдена" in t for t, _ in cb.answers)


async def test_toggle_open_closed(session_factory):
    teacher = await _mk_user(session_factory)
    subject = await _mk_subject(session_factory)
    await _grant(session_factory, teacher.id, subject.id)
    theme = await _mk_theme(session_factory, subject.id)

    msg = FakeMessage()
    cb = make_cb(f"tch:th_open:{theme.id}:0", msg)
    await t_h.cb_theme_toggle_open(cb, db_user=db_user("teacher", teacher.id))
    # перерисовка меню темы + тост «открыта»
    assert "📚 Уравнения" in msg.edits[-1][0]
    assert any(t == t_h.TEXT_THEME_OPEN and not alert for t, alert in cb.answers)
    assert len(cb.answers) == 1  # один answer: тост, без двойных
    async with session_factory() as session:
        assert (await session.get(Theme, theme.id)).is_open is True

    # повторный клик (по кнопке того же экрана) — закрывает обратно
    msg2 = FakeMessage()
    cb2 = make_cb(f"tch:th_open:{theme.id}:0", msg2)
    await t_h.cb_theme_toggle_open(cb2, db_user=db_user("teacher", teacher.id))
    assert any(t == t_h.TEXT_THEME_CLOSED for t, _ in cb2.answers)
    async with session_factory() as session:
        assert (await session.get(Theme, theme.id)).is_open is False


async def test_toggle_open_stale(session_factory):
    msg = FakeMessage()
    cb = make_cb("tch:th_open:99999:0", msg)
    await t_h.cb_theme_toggle_open(cb, db_user=db_user("teacher", 1))
    assert any("Тема не найдена" in t for t, _ in cb.answers)


# ---------------------------------------------------------------------------
# Хендлеры: переименование
# ---------------------------------------------------------------------------
async def test_rename_theme_flow(session_factory):
    teacher = await _mk_user(session_factory)
    subject = await _mk_subject(session_factory)
    await _grant(session_factory, teacher.id, subject.id)
    theme = await _mk_theme(session_factory, subject.id, "Старое")

    msg = FakeMessage()
    state = await make_fsm()
    cb = make_cb(f"tch:rename:{theme.id}:0", msg)
    await t_h.cb_rename_theme(cb, state, db_user=db_user("teacher", teacher.id))
    assert "Старое" in msg.edits[-1][0]  # промпт — перерисовкой (safe_edit)
    assert await state.get_state() == RenameThemeStates.name

    msg2 = FakeMessage(text="Новое")
    await t_h.on_rename_theme_name(msg2, state, db_user=db_user("teacher", teacher.id))
    assert any("✅ Тема переименована в «Новое»." in t for t, _ in msg2.answers)
    assert await state.get_state() is None
    async with session_factory() as session:
        assert (await session.get(Theme, theme.id)).title == "Новое"


async def test_rename_theme_bad_title_stays(session_factory):
    teacher = await _mk_user(session_factory)
    subject = await _mk_subject(session_factory)
    await _grant(session_factory, teacher.id, subject.id)
    theme = await _mk_theme(session_factory, subject.id)

    msg = FakeMessage()
    state = await make_fsm()
    cb = make_cb(f"tch:rename:{theme.id}:0", msg)
    await t_h.cb_rename_theme(cb, state, db_user=db_user("teacher", teacher.id))
    msg2 = FakeMessage(text="/bad")
    await t_h.on_rename_theme_name(msg2, state, db_user=db_user("teacher", teacher.id))
    assert not any("переименована" in t for t, _ in msg2.answers)
    assert await state.get_state() == RenameThemeStates.name


# ---------------------------------------------------------------------------
# Хендлеры: удаление темы
# ---------------------------------------------------------------------------
async def test_delete_theme_wrong_name_stays(session_factory):
    teacher = await _mk_user(session_factory)
    subject = await _mk_subject(session_factory)
    await _grant(session_factory, teacher.id, subject.id)
    theme = await _mk_theme(session_factory, subject.id, "Уравнения")

    state = await make_fsm()
    msg = FakeMessage()
    await t_h.cb_delete_theme(
        make_cb(f"tch:delete:{theme.id}:0", msg), state,
        db_user=db_user("teacher", teacher.id),
    )
    assert await state.get_state() == DeleteThemeStates.confirm
    assert ("Отмена", f"tch:del_no:{theme.id}:0") in cb_buttons(msg.edits[-1][1])

    msg2 = FakeMessage(text="Другое название")
    await t_h.on_delete_confirm(msg2, state, db_user=db_user("teacher", teacher.id))
    assert any(t_h.TEXT_DEL_THEME_NOT_MATCH in t for t, _ in msg2.answers)
    assert await state.get_state() == DeleteThemeStates.confirm  # можно повторить


async def test_delete_theme_cancel_returns_menu(session_factory):
    teacher = await _mk_user(session_factory)
    subject = await _mk_subject(session_factory)
    await _grant(session_factory, teacher.id, subject.id)
    theme = await _mk_theme(session_factory, subject.id)

    state = await make_fsm()
    msg = FakeMessage()
    await t_h.cb_delete_theme(
        make_cb(f"tch:delete:{theme.id}:0", msg), state,
        db_user=db_user("teacher", teacher.id),
    )
    msg2 = FakeMessage()
    await t_h.cb_delete_cancel(
        make_cb(f"tch:del_no:{theme.id}:0", msg2), state,
        db_user=db_user("teacher", teacher.id),
    )
    assert await state.get_state() is None
    assert "📚 Уравнения" in msg2.edits[-1][0]  # вернулось меню темы
    async with session_factory() as session:
        assert await session.get(Theme, theme.id) is not None


async def test_delete_theme_exact_name(session_factory):
    teacher = await _mk_user(session_factory)
    subject = await _mk_subject(session_factory)
    await _grant(session_factory, teacher.id, subject.id)
    theme = await _mk_theme(session_factory, subject.id, "Уравнения")

    state = await make_fsm()
    msgStub = FakeMessage()
    await t_h.cb_delete_theme(
        make_cb(f"tch:delete:{theme.id}:0", msgStub), state,
        db_user=db_user("teacher", teacher.id),
    )
    exact = FakeMessage(text="Уравнения")
    await t_h.on_delete_confirm(exact, state, db_user=db_user("teacher", teacher.id))
    assert any(
        "🗑 Тема «Уравнения» удалена вместе с заданиями." in t
        for t, _ in exact.answers
    )
    async with session_factory() as session:
        assert await session.get(Theme, theme.id) is None
    assert await state.get_state() is None


# ---------------------------------------------------------------------------
# Хендлеры: /tasks и список заданий
# ---------------------------------------------------------------------------
async def test_cmd_tasks_pick(session_factory):
    teacher = await _mk_user(session_factory)
    subject = await _mk_subject(session_factory)
    await _grant(session_factory, teacher.id, subject.id)
    theme = await _mk_theme(session_factory, subject.id, "Уравнения")

    msg = FakeMessage()
    await t_h.cmd_tasks(msg, db_user=db_user("teacher", teacher.id))
    text, kb = msg.answers[-1]
    assert "📝 Выбери тему:" in text
    assert f"tch:tasks:{theme.id}:0" in [c for _, c in cb_buttons(kb)]


async def test_cmd_tasks_no_themes(session_factory):
    teacher = await _mk_user(session_factory)
    subject = await _mk_subject(session_factory)
    await _grant(session_factory, teacher.id, subject.id)
    msg = FakeMessage()
    await t_h.cmd_tasks(msg, db_user=db_user("teacher", teacher.id))
    assert "Тем пока нет" in msg.answers[-1][0]


async def test_cb_theme_tasks_list(session_factory):
    teacher = await _mk_user(session_factory)
    subject = await _mk_subject(session_factory)
    await _grant(session_factory, teacher.id, subject.id)
    theme = await _mk_theme(session_factory, subject.id, "Уравнения")
    async with session_factory() as session:
        session.add(Task(theme_id=theme.id, question_text="Сколько будет 2+2?",
                         options=[{"t": "4", "c": True}], order=0))
        session.add(Task(theme_id=theme.id, question_photo_id="photo123",
                         options=[{"t": "x", "c": True}], order=1))
        session.add(Task(theme_id=theme.id, question_text="Скрытый",
                         options=[{"t": "x", "c": True}], order=2, is_active=False))
        await session.commit()

    msg = FakeMessage()
    cb = make_cb(f"tch:tasks:{theme.id}:0", msg)
    await t_h.cb_theme_tasks(cb, db_user=db_user("teacher", teacher.id))
    text, kb = msg.edits[-1]
    assert "📝 <b>Задания темы «Уравнения»</b>" in text
    assert "1. ✅ Сколько будет 2+2?" in text
    assert "2. ✅ фото-вопрос" in text
    assert "3. 🚫 Скрытый" in text
    buttons = cb_buttons(kb)
    assert ("➕ Добавить задание", f"tch:add_task:{theme.id}:0") in buttons
    assert ("← В меню темы", f"tch:theme:{theme.id}:0") in buttons


async def test_cb_theme_tasks_empty(session_factory):
    teacher = await _mk_user(session_factory)
    subject = await _mk_subject(session_factory)
    await _grant(session_factory, teacher.id, subject.id)
    theme = await _mk_theme(session_factory, subject.id)

    msg = FakeMessage()
    cb = make_cb(f"tch:tasks:{theme.id}:0", msg)
    await t_h.cb_theme_tasks(cb, db_user=db_user("teacher", teacher.id))
    text, _ = msg.edits[-1]
    assert t_h.TEXT_TASKS_EMPTY in text


async def test_cb_theme_tasks_foreign_alert(session_factory):
    teacher = await _mk_user(session_factory)
    subject = await _mk_subject(session_factory)
    theme = await _mk_theme(session_factory, subject.id)
    msg = FakeMessage()
    cb = make_cb(f"tch:tasks:{theme.id}:0", msg)
    await t_h.cb_theme_tasks(cb, db_user=db_user("teacher", teacher.id))
    assert any("Тема не найдена" in t for t, _ in cb.answers)


async def test_cb_theme_tasks_stale(session_factory):
    msg = FakeMessage()
    cb = make_cb("tch:tasks:99999:0", msg)
    await t_h.cb_theme_tasks(cb, db_user=db_user("teacher", 1))
    assert any("Тема не найдена" in t for t, _ in cb.answers)