"""Текущий заход: подтемы (список/добавить/переименовать/удалить, шаг визарда
задания) и режим «🎲 Открыть все» (random) — сервисы teacher.py и handlers.

Хендлеры вызываются напрямую с фейковыми Message/CallbackQuery (паттерн
test_teacher_tasks.py); db_user — SimpleNamespace(role, id), роль из БД.
Поведение ученика в random-режиме и фикс perm (doy) — в test_student_flow.py
и test_routing.py.
"""
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.handlers import teacher as t_h
from app.models import (
    Attempt,
    Student,
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
from app.states import (
    AddSubthemeStates,
    AddTaskStates,
    DeleteSubthemeStates,
    RenameSubthemeStates,
)
from app.utils.dates import today_minsk

TODAY = today_minsk()


class FakeMessage:
    """Message: answer/answer_photo/edit_text/edit_reply_markup пишут в списки."""

    def __init__(self, text="", chat_id=42, photo=None, caption=None):
        self.text = text
        self.chat = SimpleNamespace(id=chat_id)
        self.photo = photo
        self.caption = caption
        self.answers = []
        self.answers_photo = []
        self.edits = []
        self.edited_markup = []

    async def answer(self, content="", reply_markup=None, **kwargs):
        self.answers.append((content, reply_markup))

    async def answer_photo(self, photo, caption=None, reply_markup=None, **kwargs):
        self.answers_photo.append((photo, caption, reply_markup))

    async def edit_text(self, text, reply_markup=None, **kwargs):
        self.edits.append((text, reply_markup))

    async def edit_reply_markup(self, reply_markup=None, **kwargs):
        self.edited_markup.append(reply_markup)


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
            tg_id=900000000 + _SEQ["n"],
            tg_username=f"sub_{role}_{_SEQ['n']}",
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
    session_factory, subject_id: int, title="Уравнения", is_open=True, mode="sequential"
) -> Theme:
    async with session_factory() as session:
        theme = Theme(subject_id=subject_id, title=title, is_open=is_open, mode=mode)
        session.add(theme)
        await session.commit()
        return theme


async def _mk_subtheme(
    session_factory, theme_id: int, title="Линейные"
) -> Subtheme:
    async with session_factory() as session:
        subtheme = Subtheme(theme_id=theme_id, title=title)
        session.add(subtheme)
        await session.commit()
        return subtheme


async def _mk_task(
    session_factory,
    theme_id: int,
    subtheme_id: int | None = None,
    question="Вопрос?",
    created_by: int | None = None,
) -> Task:
    if created_by is None:
        created_by = (await _mk_user(session_factory)).id
    async with session_factory() as session:
        return await teacher_svc.create_task(
            session,
            theme_id,
            question,
            None,
            teacher_svc.build_options_json(["А", "Б"], 0),
            None,
            None,
            created_by,
            subtheme_id=subtheme_id,
        )


# ---------------------------------------------------------------------------
# Сервисы: check_subtheme_title / add_subtheme / get_subtheme_for_teacher
# ---------------------------------------------------------------------------
async def test_check_subtheme_title():
    with pytest.raises(ValueError):
        teacher_svc.check_subtheme_title("")
    with pytest.raises(ValueError):
        teacher_svc.check_subtheme_title("x" * 201)
    with pytest.raises(ValueError):
        teacher_svc.check_subtheme_title("/start")
    assert teacher_svc.check_subtheme_title("  Линейные  ") == "Линейные"


async def test_add_subtheme_order_and_rights(session_factory):
    teacher = await _mk_user(session_factory)
    owner = await _mk_user(session_factory, role="owner")
    alien = await _mk_user(session_factory)
    subject = await _mk_subject(session_factory)
    theme = await _mk_theme(session_factory, subject.id)
    await _grant(session_factory, teacher.id, subject.id)

    async with session_factory() as session:
        s1 = await teacher_svc.add_subtheme(session, teacher.id, theme.id, "А")
        s2 = await teacher_svc.add_subtheme(session, teacher.id, theme.id, "Б")
    assert (s1.order, s2.order) == (0, 1)

    async with session_factory() as session:
        s3 = await teacher_svc.add_subtheme(session, owner.id, theme.id, "В")
    assert s3.order == 2

    with pytest.raises(ValueError):
        async with session_factory() as session:
            await teacher_svc.add_subtheme(session, alien.id, theme.id, "Г")


async def test_get_subtheme_for_teacher_rights(session_factory):
    teacher = await _mk_user(session_factory)
    owner = await _mk_user(session_factory, role="owner")
    alien = await _mk_user(session_factory)
    subject = await _mk_subject(session_factory)
    theme = await _mk_theme(session_factory, subject.id)
    subtheme = await _mk_subtheme(session_factory, theme.id)
    await _grant(session_factory, teacher.id, subject.id)

    async with session_factory() as session:
        assert await teacher_svc.get_subtheme_for_teacher(
            session, teacher.id, subtheme.id
        ) is not None
        assert await teacher_svc.get_subtheme_for_teacher(
            session, owner.id, subtheme.id
        ) is not None
        assert await teacher_svc.get_subtheme_for_teacher(
            session, alien.id, subtheme.id
        ) is None
        assert await teacher_svc.get_subtheme_for_teacher(
            session, teacher.id, 999999
        ) is None


async def test_rename_and_count_subtheme(session_factory):
    teacher = await _mk_user(session_factory)
    subject = await _mk_subject(session_factory)
    theme = await _mk_theme(session_factory, subject.id)
    subtheme = await _mk_subtheme(session_factory, theme.id, "Старое")
    await _grant(session_factory, teacher.id, subject.id)
    await _mk_task(session_factory, theme.id, subtheme_id=subtheme.id)

    async with session_factory() as session:
        assert await teacher_svc.rename_subtheme(session, subtheme.id, "Новое") is True
        assert (await session.get(Subtheme, subtheme.id)).title == "Новое"
        assert await teacher_svc.rename_subtheme(session, 99999, "Х") is False
        assert await teacher_svc.count_subtheme_tasks(session, subtheme.id) == 1
        assert await teacher_svc.count_subtheme_tasks(session, 99999) == 0
    with pytest.raises(ValueError):
        async with session_factory() as session:
            await teacher_svc.rename_subtheme(session, subtheme.id, "")


async def test_delete_subtheme_cascades_tasks(session_factory):
    """Каскад заданий подтемы (attempts/progress тоже); задания «на тему»
    и другие подтемы остаются."""
    subject = await _mk_subject(session_factory)
    theme = await _mk_theme(session_factory, subject.id)
    sub1 = await _mk_subtheme(session_factory, theme.id, "Первая")
    sub2 = await _mk_subtheme(session_factory, theme.id, "Вторая")
    manager = await _mk_user(session_factory, role="manager")
    async with session_factory() as session:
        _, student, _ = await students_svc.create_student_record(
            session, "Иван", {subject.id}, manager.id, TODAY + timedelta(days=30)
        )
        sid = student.id
    t_in_sub1 = await _mk_task(session_factory, theme.id, subtheme_id=sub1.id)
    await _mk_task(session_factory, theme.id, subtheme_id=sub2.id)
    t_plain = await _mk_task(session_factory, theme.id)
    async with session_factory() as session:
        session.add(Attempt(student_id=sid, task_id=t_in_sub1.id, is_correct=True))
        session.add(TaskProgress(student_id=sid, task_id=t_in_sub1.id, status="done"))
        await session.commit()

    async with session_factory() as session:
        assert await teacher_svc.delete_subtheme(session, sub1.id) is True
    async with session_factory() as session:
        assert await session.get(Subtheme, sub1.id) is None
        assert await session.get(Subtheme, sub2.id) is not None
        assert await session.get(Task, t_in_sub1.id) is None
        assert await session.get(Task, t_plain.id) is not None
        assert (await session.scalars(select(Attempt))).all() == []
        assert (await session.scalars(select(TaskProgress))).all() == []
    async with session_factory() as session:
        assert await teacher_svc.delete_subtheme(session, 99999) is False


async def test_list_theme_subthemes_counts(session_factory):
    subject = await _mk_subject(session_factory)
    theme = await _mk_theme(session_factory, subject.id)
    sub1 = await _mk_subtheme(session_factory, theme.id, "Первая")
    await _mk_subtheme(session_factory, theme.id, "Вторая")
    await _mk_task(session_factory, theme.id, subtheme_id=sub1.id)
    await _mk_task(session_factory, theme.id, subtheme_id=sub1.id)
    await _mk_task(session_factory, theme.id)

    async with session_factory() as session:
        rows = await teacher_svc.list_theme_subthemes(session, theme.id)
    assert [r["subtheme"].title for r in rows] == ["Первая", "Вторая"]
    assert [r["count"] for r in rows] == [2, 0]


async def test_create_task_keeps_subtheme_id(session_factory):
    subject = await _mk_subject(session_factory)
    theme = await _mk_theme(session_factory, subject.id)
    subtheme = await _mk_subtheme(session_factory, theme.id)
    await _mk_task(session_factory, theme.id, subtheme_id=subtheme.id)
    task = await _mk_task(session_factory, theme.id, subtheme_id=subtheme.id)
    assert task.subtheme_id == subtheme.id
    async with session_factory() as session:
        assert (await session.get(Task, task.id)).order == 1


async def test_toggle_theme_mode(session_factory):
    subject = await _mk_subject(session_factory)
    theme = await _mk_theme(session_factory, subject.id, mode="sequential")
    async with session_factory() as session:
        assert await teacher_svc.toggle_theme_mode(session, theme.id) == "random"
        assert await teacher_svc.toggle_theme_mode(session, theme.id) == "sequential"
        assert await teacher_svc.toggle_theme_mode(session, 99999) is None


# ---------------------------------------------------------------------------
# Хендлеры: экран «🔖 Подтемы» и меню подтемы
# ---------------------------------------------------------------------------
async def test_cb_subthemes_shows_list(session_factory):
    teacher = await _mk_user(session_factory)
    subject = await _mk_subject(session_factory)
    theme = await _mk_theme(session_factory, subject.id, "Уравнения")
    await _grant(session_factory, teacher.id, subject.id)
    sub1 = await _mk_subtheme(session_factory, theme.id, "Линейные")
    sub2 = await _mk_subtheme(session_factory, theme.id, "Квадратные")
    await _mk_task(session_factory, theme.id, subtheme_id=sub1.id)

    m = FakeMessage()
    cb = make_cb(f"tch:subs:{theme.id}:0", m)
    await t_h.cb_subthemes(cb, db_user=db_user("teacher", teacher.id))

    assert len(m.edits) == 1
    assert "Подтемы темы «Уравнения»" in m.edits[0][0]
    buttons = cb_buttons(m.edits[0][1])
    assert (f"tch:sub:{sub1.id}:0", f"tch:sub:{sub2.id}:0") == (
        buttons[0][1],
        buttons[1][1],
    )
    assert any(b[1] == f"tch:sub_add:{theme.id}:0" for b in buttons)


async def test_cb_subthemes_empty(session_factory):
    teacher = await _mk_user(session_factory)
    subject = await _mk_subject(session_factory)
    theme = await _mk_theme(session_factory, subject.id)
    await _grant(session_factory, teacher.id, subject.id)

    m = FakeMessage()
    await t_h.cb_subthemes(make_cb(f"tch:subs:{theme.id}:0", m), db_user=db_user("teacher", teacher.id))

    assert len(m.edits) == 1
    assert "Подтем пока нет" in m.edits[0][0]
    assert any(b[1] == f"tch:sub_add:{theme.id}:0" for b in cb_buttons(m.edits[0][1]))


async def test_cb_subthemes_alien_alert(session_factory):
    teacher = await _mk_user(session_factory)
    alien = await _mk_user(session_factory)
    subject = await _mk_subject(session_factory)
    theme = await _mk_theme(session_factory, subject.id)
    await _grant(session_factory, teacher.id, subject.id)

    m = FakeMessage()
    cb = make_cb(f"tch:subs:{theme.id}:0", m)
    await t_h.cb_subthemes(cb, db_user=db_user("teacher", alien.id))

    assert m.edits == []
    assert cb.answers == [("Тема не найдена", True)] or (
        len(cb.answers) == 1 and cb.answers[0][1] is True
    )


async def test_cb_subtheme_menu(session_factory):
    teacher = await _mk_user(session_factory)
    subject = await _mk_subject(session_factory)
    theme = await _mk_theme(session_factory, subject.id)
    await _grant(session_factory, teacher.id, subject.id)
    sub = await _mk_subtheme(session_factory, theme.id, "Линейные")

    m = FakeMessage()
    await t_h.cb_subtheme_menu(make_cb(f"tch:sub:{sub.id}:0", m), db_user=db_user("teacher", teacher.id))

    assert len(m.edits) == 1
    assert "Линейные" in m.edits[0][0]
    buttons = cb_buttons(m.edits[0][1])
    assert any(b[1] == f"tch:sub_rename:{sub.id}:0" for b in buttons)
    assert any(b[1] == f"tch:sub_del:{sub.id}:0" for b in buttons)


# ---------------------------------------------------------------------------
# Визарды подтем: добавить / переименовать / удалить
# ---------------------------------------------------------------------------
async def test_cb_sub_add_wizard(session_factory):
    teacher = await _mk_user(session_factory)
    subject = await _mk_subject(session_factory)
    theme = await _mk_theme(session_factory, subject.id)
    await _grant(session_factory, teacher.id, subject.id)

    m = FakeMessage()
    state = await make_fsm()
    await t_h.cb_sub_add(make_cb(f"tch:sub_add:{theme.id}:0", m), state, db_user=db_user("teacher", teacher.id))
    assert await state.get_state() == AddSubthemeStates.name

    m2 = FakeMessage("Интегралы")
    await t_h.on_sub_add_name(m2, state, db_user=db_user("teacher", teacher.id))
    assert await state.get_state() is None
    assert any("создана" in text for text, _ in m2.answers)
    async with session_factory() as session:
        sub = (await session.scalars(select(Subtheme))).one()
    assert sub.title == "Интегралы" and sub.order == 0


async def test_cb_sub_add_alien_alert(session_factory):
    teacher = await _mk_user(session_factory)
    alien = await _mk_user(session_factory)
    subject = await _mk_subject(session_factory)
    theme = await _mk_theme(session_factory, subject.id)
    await _grant(session_factory, teacher.id, subject.id)

    state = await make_fsm()
    cb = make_cb(f"tch:sub_add:{theme.id}:0", FakeMessage())
    await t_h.cb_sub_add(cb, state, db_user=db_user("teacher", alien.id))
    assert await state.get_state() is None
    assert cb.answers and cb.answers[0][1] is True


async def test_cb_sub_rename_flow(session_factory):
    teacher = await _mk_user(session_factory)
    subject = await _mk_subject(session_factory)
    theme = await _mk_theme(session_factory, subject.id)
    await _grant(session_factory, teacher.id, subject.id)
    sub = await _mk_subtheme(session_factory, theme.id, "Старое")

    m = FakeMessage()
    state = await make_fsm()
    await t_h.cb_sub_rename(make_cb(f"tch:sub_rename:{sub.id}:0", m), state, db_user=db_user("teacher", teacher.id))
    assert await state.get_state() == RenameSubthemeStates.name

    m2 = FakeMessage("Новое")
    await t_h.on_sub_rename_name(m2, state, db_user=db_user("teacher", teacher.id))
    assert await state.get_state() is None
    async with session_factory() as session:
        assert (await session.get(Subtheme, sub.id)).title == "Новое"
    assert any("переименована" in text for text, _ in m2.answers)


async def test_cb_sub_rename_alien_alert(session_factory):
    teacher = await _mk_user(session_factory)
    alien = await _mk_user(session_factory)
    subject = await _mk_subject(session_factory)
    theme = await _mk_theme(session_factory, subject.id)
    await _grant(session_factory, teacher.id, subject.id)
    sub = await _mk_subtheme(session_factory, theme.id)

    state = await make_fsm()
    cb = make_cb(f"tch:sub_rename:{sub.id}:0", FakeMessage())
    await t_h.cb_sub_rename(cb, state, db_user=db_user("teacher", alien.id))
    assert await state.get_state() is None
    assert cb.answers and cb.answers[0][1] is True


async def test_cb_sub_del_flow(session_factory):
    teacher = await _mk_user(session_factory)
    subject = await _mk_subject(session_factory)
    theme = await _mk_theme(session_factory, subject.id)
    await _grant(session_factory, teacher.id, subject.id)
    sub = await _mk_subtheme(session_factory, theme.id, "Линейные")

    state = await make_fsm()
    m = FakeMessage()
    cb = make_cb(f"tch:sub_del:{sub.id}:0", m)
    await t_h.cb_sub_del(cb, state, db_user=db_user("teacher", teacher.id))
    assert await state.get_state() == DeleteSubthemeStates.confirm
    assert any("Удалить подтему" in text for text, _ in m.edits)

    m2 = FakeMessage("Не то название")
    await t_h.on_sub_del_confirm(m2, state, db_user=db_user("teacher", teacher.id))
    assert await state.get_state() == DeleteSubthemeStates.confirm
    assert any("название" in text.lower() for text, _ in m2.answers)

    m3 = FakeMessage("Линейные")
    await t_h.on_sub_del_confirm(m3, state, db_user=db_user("teacher", teacher.id))
    assert await state.get_state() is None
    async with session_factory() as session:
        assert await session.get(Subtheme, sub.id) is None
    assert any("удалена" in text for text, _ in m3.answers)


async def test_cb_sub_del_no_cancels(session_factory):
    teacher = await _mk_user(session_factory)
    subject = await _mk_subject(session_factory)
    theme = await _mk_theme(session_factory, subject.id)
    await _grant(session_factory, teacher.id, subject.id)
    sub = await _mk_subtheme(session_factory, theme.id)

    state = await make_fsm()
    await t_h.cb_sub_del(make_cb(f"tch:sub_del:{sub.id}:0", FakeMessage()), state, db_user=db_user("teacher", teacher.id))
    await t_h.cb_sub_del_no(make_cb(f"tch:sub_del_no:{sub.id}:0", FakeMessage()), state, db_user=db_user("teacher", teacher.id))
    assert await state.get_state() is None
    async with session_factory() as session:
        assert await session.get(Subtheme, sub.id) is not None


async def test_sub_wizard_nontext_hints(session_factory):
    teacher = await _mk_user(session_factory)
    subject = await _mk_subject(session_factory)
    theme = await _mk_theme(session_factory, subject.id)
    await _grant(session_factory, teacher.id, subject.id)

    m = FakeMessage("")
    await t_h.on_sub_add_name_nontext(m, db_user=db_user("teacher", teacher.id))
    assert any("нужно текстом" in text for text, _ in m.answers)

    m2 = FakeMessage("")
    await t_h.on_sub_rename_name_nontext(m2, db_user=db_user("teacher", teacher.id))
    assert any("нужно текстом" in text for text, _ in m2.answers)

    m3 = FakeMessage("")
    await t_h.on_sub_del_confirm_nontext(m3, db_user=db_user("teacher", teacher.id))
    assert any("текстом" in text for text, _ in m3.answers)


# ---------------------------------------------------------------------------
# Визард задания: шаг «подтема» + сохранение в подтему
# ---------------------------------------------------------------------------
async def test_cb_add_task_asks_subtheme(session_factory):
    teacher = await _mk_user(session_factory)
    subject = await _mk_subject(session_factory)
    theme = await _mk_theme(session_factory, subject.id)
    await _grant(session_factory, teacher.id, subject.id)
    sub = await _mk_subtheme(session_factory, theme.id, "Линейные")

    m = FakeMessage()
    state = await make_fsm()
    await t_h.cb_add_task(make_cb(f"tch:add_task:{theme.id}:0", m), state, db_user=db_user("teacher", teacher.id))
    assert await state.get_state() == AddTaskStates.sub
    buttons = cb_buttons(m.answers[0][1])
    assert any(b[1] == f"tch:at:sub:{sub.id}:0" for b in buttons)
    assert any(b[1] == "tch:at:sub:0:0" for b in buttons)


async def test_cb_pick_subtheme_sets_state(session_factory):
    teacher = await _mk_user(session_factory)
    subject = await _mk_subject(session_factory)
    theme = await _mk_theme(session_factory, subject.id)
    await _grant(session_factory, teacher.id, subject.id)
    sub = await _mk_subtheme(session_factory, theme.id, "Линейные")

    state = await make_fsm()
    m = FakeMessage()
    await t_h.cb_add_task(make_cb(f"tch:add_task:{theme.id}:0", m), state, db_user=db_user("teacher", teacher.id))
    await t_h.cb_pick_subtheme(make_cb(f"tch:at:sub:{sub.id}:0", m), state, db_user=db_user("teacher", teacher.id))
    assert await state.get_state() == AddTaskStates.question
    data = await state.get_data()
    assert data["theme_id"] == theme.id and data["subtheme_id"] == sub.id

    await state.clear()
    await t_h.cb_add_task(make_cb(f"tch:add_task:{theme.id}:0", m), state, db_user=db_user("teacher", teacher.id))
    await t_h.cb_pick_subtheme(make_cb("tch:at:sub:0:0", m), state, db_user=db_user("teacher", teacher.id))
    assert await state.get_state() == AddTaskStates.question
    assert (await state.get_data())["subtheme_id"] is None


async def test_cb_pick_subtheme_stale(session_factory):
    teacher = await _mk_user(session_factory)
    subject = await _mk_subject(session_factory)
    theme = await _mk_theme(session_factory, subject.id)
    await _grant(session_factory, teacher.id, subject.id)
    sub = await _mk_subtheme(session_factory, theme.id)

    state = await make_fsm()
    await state.set_state(AddTaskStates.options)
    cb = make_cb(f"tch:at:sub:{sub.id}:0", FakeMessage())
    await t_h.cb_pick_subtheme(cb, state, db_user=db_user("teacher", teacher.id))
    assert await state.get_state() is None
    assert cb.answers and cb.answers[0][1] is True


async def test_wizard_saves_task_into_subtheme(session_factory):
    teacher = await _mk_user(session_factory)
    subject = await _mk_subject(session_factory)
    theme = await _mk_theme(session_factory, subject.id)
    await _grant(session_factory, teacher.id, subject.id)
    sub1 = await _mk_subtheme(session_factory, theme.id, "Линейные")
    await _mk_subtheme(session_factory, theme.id, "Квадратные")

    state = await make_fsm()
    m = FakeMessage()
    db = db_user("teacher", teacher.id)
    await t_h.cb_add_task(make_cb(f"tch:add_task:{theme.id}:0", m), state, db_user=db)
    await t_h.cb_pick_subtheme(make_cb(f"tch:at:sub:{sub1.id}:0", m), state, db_user=db)
    await t_h.on_task_question(FakeMessage("2 + 2 = ?"), state, db_user=db)
    await t_h.on_task_option(FakeMessage("4"), state, db_user=db)
    await t_h.on_task_option(FakeMessage("5"), state, db_user=db)
    await t_h.cb_opts_done(make_cb("tch:at:opts_done:0", m), state, db_user=db)
    await t_h.cb_pick_correct(make_cb("tch:at:pick:0:0", m), state, db_user=db)
    await t_h.cb_exp_skip(make_cb("tch:at:exp_skip:0", m), state, db_user=db)
    await t_h.cb_task_save(make_cb("tch:at:save:0", m), state, db_user=db)

    assert await state.get_state() is None
    async with session_factory() as session:
        tasks = (await session.scalars(select(Task))).all()
    assert len(tasks) == 1
    assert tasks[0].subtheme_id == sub1.id
    assert tasks[0].question_text == "2 + 2 = ?"


async def test_cb_theme_tasks_groups_by_subtheme(session_factory):
    teacher = await _mk_user(session_factory)
    subject = await _mk_subject(session_factory)
    theme = await _mk_theme(session_factory, subject.id)
    await _grant(session_factory, teacher.id, subject.id)
    sub1 = await _mk_subtheme(session_factory, theme.id, "Первый блок")
    await _mk_task(session_factory, theme.id, subtheme_id=sub1.id, question="В подтеме")
    await _mk_task(session_factory, theme.id, question="Без подтемы")

    m = FakeMessage()
    await t_h.cb_theme_tasks(make_cb(f"tch:tasks:{theme.id}:0", m), db_user=db_user("teacher", teacher.id))
    assert len(m.edits) == 1
    text = m.edits[0][0]
    assert "Первый блок" in text and "Без подтемы" in text
    assert any(b[1] == f"tch:add_task:{theme.id}:0" for b in cb_buttons(m.edits[0][1]))


async def test_cb_theme_mode_toggle(session_factory):
    teacher = await _mk_user(session_factory)
    subject = await _mk_subject(session_factory)
    theme = await _mk_theme(session_factory, subject.id, mode="sequential")
    await _grant(session_factory, teacher.id, subject.id)

    m = FakeMessage()
    cb = make_cb(f"tch:th_mode:{theme.id}:0", m)
    await t_h.cb_theme_mode(cb, db_user=db_user("teacher", teacher.id))
    async with session_factory() as session:
        assert (await session.get(Theme, theme.id)).mode == "random"
    assert len(m.edits) == 1
    assert len(cb.answers) == 1

    m2 = FakeMessage()
    cb2 = make_cb(f"tch:th_mode:{theme.id}:0", m2)
    await t_h.cb_theme_mode(cb2, db_user=db_user("teacher", teacher.id))
    async with session_factory() as session:
        assert (await session.get(Theme, theme.id)).mode == "sequential"


async def test_cb_theme_mode_alien_alert(session_factory):
    teacher = await _mk_user(session_factory)
    alien = await _mk_user(session_factory)
    subject = await _mk_subject(session_factory)
    theme = await _mk_theme(session_factory, subject.id, mode="sequential")
    await _grant(session_factory, teacher.id, subject.id)

    cb = make_cb(f"tch:th_mode:{theme.id}:0", FakeMessage())
    await t_h.cb_theme_mode(cb, db_user=db_user("teacher", alien.id))
    async with session_factory() as session:
        assert (await session.get(Theme, theme.id)).mode == "sequential"
    assert cb.answers and cb.answers[0][1] is True
