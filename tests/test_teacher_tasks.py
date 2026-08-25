"""Заход 5: визард «Добавить задание» (ТЗ раздел 8), карточка задания,
раздел 0 (дефекты Заходов 1–4).

Хендлеры вызываются напрямую с фейковыми Message/CallbackQuery (паттерн
test_teacher_themes.py); db_user — SimpleNamespace(role, id), роль всегда
из БД (реальное поведение require_role при прямых вызовах обходится).
"""
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.handlers import commands as cmd_h
from app.handlers import manager as m_h
from app.handlers import teacher as t_h
from app.models import Attempt, Subject, Task, TaskProgress, Theme, User
from app.services import students as students_svc
from app.services import teacher as teacher_svc
from app.states import AddStudentStates, AddTaskStates, AddThemeStates


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
            tg_id=300000000 + _SEQ["n"],
            tg_username=f"t5_{role}_{_SEQ['n']}",
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
    from app.models import TeacherSubject

    async with session_factory() as session:
        session.add(TeacherSubject(teacher_id=teacher_id, subject_id=subject_id))
        await session.commit()


async def _mk_theme(session_factory, subject_id: int, title="Уравнения") -> Theme:
    async with session_factory() as session:
        theme = Theme(subject_id=subject_id, title=title, mode="sequential")
        session.add(theme)
        await session.commit()
        return theme


async def _add_options(state, *options, db_user_id=1):
    """Добавление вариантов текстом (на шаге options)."""
    for text in options:
        msg = FakeMessage(text=text)
        await t_h.on_task_option(msg, state, db_user=db_user("teacher", db_user_id))


async def _to_correct(state, db_user_id=1):
    """options → correct (2+ вариантов уже добавлены): «✅ Готово»."""
    cb = make_cb("tch:at:opts_done:0", FakeMessage())
    await t_h.cb_opts_done(cb, state, db_user=db_user("teacher", db_user_id))
    return cb


async def _to_preview(state, correct_index=0, db_user_id=1):
    """correct → exp_input → «Пропустить» → preview; возвращает cb с message."""
    cb = make_cb(f"tch:at:pick:{correct_index}:0", FakeMessage())
    await t_h.cb_pick_correct(cb, state, db_user=db_user("teacher", db_user_id))
    cb_skip = make_cb("tch:at:exp_skip:0", FakeMessage())
    await t_h.cb_exp_skip(cb_skip, state, db_user=db_user("teacher", db_user_id))
    return cb_skip


# ---------------------------------------------------------------------------
# Полный проход: текст-вопрос → 3 варианта → правильный → текст-объяснение →
# ещё фото → сохранить (критерий готовности 1)
# ---------------------------------------------------------------------------
async def test_task_wizard_full_flow_text(session_factory):
    teacher = await _mk_user(session_factory)
    subject = await _mk_subject(session_factory)
    await _grant(session_factory, teacher.id, subject.id)
    theme = await _mk_theme(session_factory, subject.id, "Уравнения")
    db = db_user("teacher", teacher.id)

    # вход в визард
    state = await make_fsm()
    msg = FakeMessage()
    cb0 = make_cb(f"tch:add_task:{theme.id}:0", msg)
    await t_h.cb_add_task(cb0, state, db_user=db)
    assert msg.answers[-1][0] == t_h.ASK_QUESTION
    assert await state.get_state() == AddTaskStates.question

    # 1. вопрос текстом
    msg = FakeMessage(text="Сколько будет 2+2?")
    await t_h.on_task_question(msg, state, db_user=db)
    assert msg.answers[-1][0] == t_h.ASK_OPTIONS
    assert await state.get_state() == AddTaskStates.options
    buttons = cb_buttons(msg.answers[-1][1])
    assert ("➕ Ещё вариант", "tch:at:opt_more:0") in buttons
    assert ("✅ Готово", "tch:at:opts_done:0") in buttons

    # 2. три варианта
    for i, opt in enumerate(["4", "5x", "Может 10"], 1):
        m = FakeMessage(text=opt)
        await t_h.on_task_option(m, state, db_user=db)
        assert any(t.startswith(f"✅ Вариант {i}") for t, _ in m.answers)
        assert await state.get_state() == AddTaskStates.options

    # 3. «✅ Готово» → correct
    cb = await _to_correct(state, teacher.id)
    assert cb.message.answers[-1][0] == t_h.ASK_CORRECT
    assert await state.get_state() == AddTaskStates.correct
    assert ("А. 4", "tch:at:pick:0:0") in cb_buttons(cb.message.answers[-1][1])
    assert ("Б. 5x", "tch:at:pick:1:0") in cb_buttons(cb.message.answers[-1][1])

    # 4. правильный — Б (индекс 1) → exp_input (UX-пакет: шаг объяснения
    #    принимает и текст, и фото, выбора «текст/фото» больше нет)
    cb = make_cb("tch:at:pick:1:0", FakeMessage())
    await t_h.cb_pick_correct(cb, state, db_user=db)
    assert await state.get_state() == AddTaskStates.exp_input
    assert cb.message.answers[-1][0] == t_h.EXP_CHOICE_MSG
    exp_buttons = cb_buttons(cb.message.answers[-1][1])
    assert ("Пропустить", "tch:at:exp_skip:0") in exp_buttons
    assert not any(c.startswith("tch:at:exp_text") for _, c in exp_buttons)
    assert not any(c.startswith("tch:at:exp_photo") for _, c in exp_buttons)

    # 5. объяснение текстом → «✅ Объяснение добавлено.» + «➕ Ещё»/«✅ Готово»
    m = FakeMessage(text="Разбор по шагам")
    await t_h.on_exp_text(m, state, db_user=db)
    assert any(t == t_h.TEXT_EXP_ADDED for t, _ in m.answers)
    assert await state.get_state() == AddTaskStates.exp_input
    more_buttons = cb_buttons(m.answers[-1][1])
    assert ("➕ Ещё", "tch:at:exp_more:0") in more_buttons
    assert ("✅ Готово", "tch:at:exp_done:0") in more_buttons

    # 6. ещё фото-объяснение: «➕ Ещё» → снова exp_input → фото
    cb = make_cb("tch:at:exp_more:0", FakeMessage())
    await t_h.cb_exp_more(cb, state, db_user=db)
    assert await state.get_state() == AddTaskStates.exp_input
    m = FakeMessage(photo=[SimpleNamespace(file_id="exp_photo_1")])
    await t_h.on_exp_photo(m, state, db_user=db)
    assert any(t == t_h.TEXT_EXP_ADDED for t, _ in m.answers)
    assert await state.get_state() == AddTaskStates.exp_input

    # 7. «✅ Готово» → превью
    cb = make_cb("tch:at:exp_done:0", FakeMessage())
    await t_h.cb_exp_done(cb, state, db_user=db)
    assert await state.get_state() == AddTaskStates.preview
    text, kb = cb.message.answers[-1]
    assert t_h.TEXT_PREVIEW_QUESTION in text
    assert "Сколько будет 2+2?" in text
    assert ("А. 4", "tch:at:pv:0:0") in cb_buttons(kb)
    assert ("✅ Сохранить", "tch:at:save:0") in cb_buttons(kb)
    assert ("✏️ Заново", "tch:at:restart:0") in cb_buttons(kb)

    # клик по варианту в превью — подсказка
    cb = make_cb("tch:at:pv:1:0", FakeMessage())
    await t_h.cb_preview_option(cb, db_user=db)
    assert cb.answers == [(t_h.PREVIEW_OPTION_HINT, False)]

    # 8. «✅ Сохранить» → задание в БД + карточка
    saved = FakeMessage()
    cb = make_cb("tch:at:save:0", saved)
    await t_h.cb_task_save(cb, state, db_user=db)
    assert await state.get_state() is None
    assert saved.edited_markup == [None]  # кнопки превью сняты сразу
    assert any(t.startswith(t_h.TASK_SAVED) for t, _ in saved.answers)
    body, card_kb = saved.answers[-1]
    assert "📝 <b>Задание</b>" in body
    assert "Сколько будет 2+2?" in body
    assert "Вариантов: 3 · Правильный: Б" in body
    assert "Объяснение: есть" in body
    assert "Статус: ✅ видно ученикам" in body
    assert ("🚫 Скрыть задание", "tch:t_toggle:1:0") in cb_buttons(card_kb)
    assert ("🗑 Удалить", "tch:t_del:1:0") in cb_buttons(card_kb)
    assert ("← К заданиям", "tch:tasks:1:0") in cb_buttons(card_kb)

    async with session_factory() as s:
        task = await s.scalar(select(Task).where(Task.theme_id == theme.id))
        assert task is not None
        assert task.question_text == "Сколько будет 2+2?"
        assert task.question_photo_id is None
        assert task.feedback_text == "Разбор по шагам"
        assert task.feedback_photo_id == "exp_photo_1"
        assert task.order == 0
        assert task.created_by == teacher.id
        assert task.is_active is True
        assert [o["t"] for o in task.options] == ["4", "5x", "Может 10"]
        assert sum(1 for o in task.options if o["c"]) == 1
        assert [o["c"] for o in task.options] == [False, True, False]


# ---------------------------------------------------------------------------
# Проход с фото вопроса без caption (критерий 2)
# ---------------------------------------------------------------------------
async def test_task_wizard_photo_question(session_factory):
    teacher = await _mk_user(session_factory)
    subject = await _mk_subject(session_factory)
    await _grant(session_factory, teacher.id, subject.id)
    theme = await _mk_theme(session_factory, subject.id)
    db = db_user("teacher", teacher.id)

    state = await make_fsm()
    cb0 = make_cb(f"tch:add_task:{theme.id}:0", FakeMessage())
    await t_h.cb_add_task(cb0, state, db_user=db)

    # вопрос фото без caption
    m = FakeMessage(photo=[SimpleNamespace(file_id="q_photo_1")])
    await t_h.on_task_question_photo(m, state, db_user=db)
    assert await state.get_state() == AddTaskStates.options

    await _add_options(state, "4", "5x")
    await _to_correct(state, teacher.id)
    cb_skip = await _to_preview(state, 1, teacher.id)
    assert await state.get_state() == AddTaskStates.preview
    # превью фото-вопроса — отправкой фото с подписью
    photo, caption, _kb = cb_skip.message.answers_photo[-1]
    assert photo == "q_photo_1"
    assert "фото-вопрос" in caption

    saved = FakeMessage()
    cb = make_cb("tch:at:save:0", saved)
    await t_h.cb_task_save(cb, state, db_user=db)
    async with session_factory() as s:
        task = await s.scalar(select(Task).where(Task.theme_id == theme.id))
        assert task is not None
        assert task.question_text is None
        assert task.question_photo_id == "q_photo_1"
        assert task.feedback_text is None and task.feedback_photo_id is None
    assert any("фото-вопрос" in t for t, _ in saved.answers)
    assert "Объяснение: нет" in saved.answers[-1][0]


# ---------------------------------------------------------------------------
# Шаг exp_input (UX-пакет): принимает и текст, И фото; документы/мусор —
# подсказка; пустой текст — подсказка; стикеры — подсказка по шагу
# ---------------------------------------------------------------------------
async def test_exp_input_accepts_both_text_and_photo(session_factory):
    """Объяснение: и текст, и фото уходят в одно состояние (оба сохраняются)."""
    teacher = await _mk_user(session_factory)
    subject = await _mk_subject(session_factory)
    await _grant(session_factory, teacher.id, subject.id)
    theme = await _mk_theme(session_factory, subject.id)
    db = db_user("teacher", teacher.id)

    state = await make_fsm()
    cb0 = make_cb(f"tch:add_task:{theme.id}:0", FakeMessage())
    await t_h.cb_add_task(cb0, state, db_user=db)
    m = FakeMessage(text="Вопрос")
    await t_h.on_task_question(m, state, db_user=db)
    await _add_options(state, "4", "5x")
    await _to_correct(state, teacher.id)
    cb_pick = make_cb("tch:at:pick:0:0", FakeMessage())
    await t_h.cb_pick_correct(cb_pick, state, db_user=db)
    assert await state.get_state() == AddTaskStates.exp_input

    # текст — принимается сразу (без выбора «текст/фото»)
    m = FakeMessage(text="объяснение текстом")
    await t_h.on_exp_text(m, state, db_user=db)
    assert any(t == t_h.TEXT_EXP_ADDED for t, _ in m.answers)
    assert (await state.get_data())["feedback_text"] == "объяснение текстом"

    # фото — тоже принимается, состояние то же
    m2 = FakeMessage(photo=[SimpleNamespace(file_id="exp_photo_2")])
    await t_h.on_exp_photo(m2, state, db_user=db)
    assert any(t == t_h.TEXT_EXP_ADDED for t, _ in m2.answers)
    assert (await state.get_data())["feedback_photo_id"] == "exp_photo_2"
    assert await state.get_state() == AddTaskStates.exp_input


async def test_exp_input_document_and_blank_hint(session_factory):
    """Документ и пустой текст в exp_input → подсказка, состояние живёт."""
    teacher = await _mk_user(session_factory)
    subject = await _mk_subject(session_factory)
    await _grant(session_factory, teacher.id, subject.id)
    theme = await _mk_theme(session_factory, subject.id)
    db = db_user("teacher", teacher.id)

    state = await make_fsm()
    cb0 = make_cb(f"tch:add_task:{theme.id}:0", FakeMessage())
    await t_h.cb_add_task(cb0, state, db_user=db)
    m = FakeMessage(text="Вопрос")
    await t_h.on_task_question(m, state, db_user=db)
    await _add_options(state, "4", "5x")
    await _to_correct(state, teacher.id)
    cb_pick = make_cb("tch:at:pick:0:0", FakeMessage())
    await t_h.cb_pick_correct(cb_pick, state, db_user=db)

    m = FakeMessage()
    m.document = SimpleNamespace(file_id="doc1")
    await t_h.on_exp_document(m, db_user=db)
    assert any(t == t_h.TEXT_HINT_EXP_INPUT for t, _ in m.answers)
    assert await state.get_state() == AddTaskStates.exp_input

    m2 = FakeMessage(text="   ")
    await t_h.on_exp_text(m2, state, db_user=db)
    assert any(t == t_h.TEXT_HINT_EXP_INPUT for t, _ in m2.answers)
    assert await state.get_state() == AddTaskStates.exp_input


async def test_sticker_in_wizard_steps_hinted(session_factory):
    """Стикер/голосовое/видео в шагах визарда → подсказка по шагу."""
    teacher = await _mk_user(session_factory)
    subject = await _mk_subject(session_factory)
    await _grant(session_factory, teacher.id, subject.id)
    theme = await _mk_theme(session_factory, subject.id)
    db = db_user("teacher", teacher.id)

    # question: стикер → подсказка вопроса, состояние не меняется
    state = await make_fsm()
    cb0 = make_cb(f"tch:add_task:{theme.id}:0", FakeMessage())
    await t_h.cb_add_task(cb0, state, db_user=db)
    m = FakeMessage()
    m.sticker = SimpleNamespace(file_id="st1")
    await t_h.on_wizard_media(m, state, db_user=db)
    assert any(t == t_h.ASK_QUESTION for t, _ in m.answers)
    assert await state.get_state() == AddTaskStates.question

    # options: голосовое → подсказка вариантов
    m = FakeMessage(text="Вопрос")
    await t_h.on_task_question(m, state, db_user=db)
    m = FakeMessage()
    m.voice = SimpleNamespace(file_id="v1")
    await t_h.on_wizard_media(m, state, db_user=db)
    assert any(t == t_h.ASK_OPTIONS for t, _ in m.answers)
    assert await state.get_state() == AddTaskStates.options

    # exp_input: видео → подсказка объяснения (одна для текста и фото)
    await _add_options(state, "4", "5x")
    await _to_correct(state, teacher.id)
    cb_pick = make_cb("tch:at:pick:0:0", FakeMessage())
    await t_h.cb_pick_correct(cb_pick, state, db_user=db)
    m = FakeMessage()
    m.video = SimpleNamespace(file_id="vid1")
    await t_h.on_wizard_media(m, state, db_user=db)
    assert any(t == t_h.TEXT_HINT_EXP_INPUT for t, _ in m.answers)
    assert await state.get_state() == AddTaskStates.exp_input

    # exp_input: анимация → та же подсказка
    m = FakeMessage()
    m.animation = SimpleNamespace(file_id="a1")
    await t_h.on_wizard_media(m, state, db_user=db)
    assert any(t == t_h.TEXT_HINT_EXP_INPUT for t, _ in m.answers)
    assert await state.get_state() == AddTaskStates.exp_input


# ---------------------------------------------------------------------------
# Кнопка «➕ Ещё вариант» в шаге options — приглашение и продолжение визарда
# ---------------------------------------------------------------------------
async def test_old_opt_more_button_in_options_keeps_wizard(session_factory):
    teacher = await _mk_user(session_factory)
    subject = await _mk_subject(session_factory)
    await _grant(session_factory, teacher.id, subject.id)
    theme = await _mk_theme(session_factory, subject.id)
    db = db_user("teacher", teacher.id)

    state = await make_fsm()
    cb0 = make_cb(f"tch:add_task:{theme.id}:0", FakeMessage())
    await t_h.cb_add_task(cb0, state, db_user=db)
    m = FakeMessage(text="Вопрос")
    await t_h.on_task_question(m, state, db_user=db)
    await _add_options(state, "4", "5x")

    # повторное нажатие «➕ Ещё вариант» в шаге options — приглашение
    msg = FakeMessage()
    cb = make_cb("tch:at:opt_more:0", msg)
    await t_h.cb_opt_more(cb, state, db_user=db)
    assert await state.get_state() == AddTaskStates.options
    assert any("Присылай следующий вариант сообщением." in t for t, _ in msg.answers)

    # следующее сообщение попадает в нужный хендлер и добавляет вариант
    m2 = FakeMessage(text="вариант 3")
    await t_h.on_task_option(m2, state, db_user=db)
    assert len((await state.get_data())["options"]) == 3

    # визард продолжается и доезжает до сохранения
    await _to_correct(state, teacher.id)
    await _to_preview(state, 2, teacher.id)
    saved = FakeMessage()
    cb = make_cb("tch:at:save:0", saved)
    await t_h.cb_task_save(cb, state, db_user=db)
    async with session_factory() as s:
        task = await s.scalar(select(Task).where(Task.theme_id == theme.id))
        assert task is not None
        assert len(task.options) == 3


async def test_opt_more_from_later_step_is_stale(session_factory):
    """Кнопка «➕ Ещё вариант» из пройденного шага НЕ перескакивает визард."""
    teacher = await _mk_user(session_factory)
    subject = await _mk_subject(session_factory)
    await _grant(session_factory, teacher.id, subject.id)
    theme = await _mk_theme(session_factory, subject.id)
    db = db_user("teacher", teacher.id)

    state = await make_fsm()
    cb0 = make_cb(f"tch:add_task:{theme.id}:0", FakeMessage())
    await t_h.cb_add_task(cb0, state, db_user=db)
    m = FakeMessage(text="Вопрос")
    await t_h.on_task_question(m, state, db_user=db)
    await _add_options(state, "4", "5x")
    await _to_correct(state, teacher.id)
    cb_pick = make_cb("tch:at:pick:0:0", FakeMessage())
    await t_h.cb_pick_correct(cb_pick, state, db_user=db)
    assert await state.get_state() == AddTaskStates.exp_input

    # старая кнопка из шага options → «Кнопка устарела», визард погашен
    msg = FakeMessage()
    cb = make_cb("tch:at:opt_more:0", msg)
    await t_h.cb_opt_more(cb, state, db_user=db)
    assert any(t == t_h.MSG_STALE_FULL and alert for t, alert in cb.answers)
    assert await state.get_state() is None


async def test_opt_more_and_done_from_question_is_stale(session_factory):
    """«➕ Ещё вариант»/«✅ Готово» до ввода вопроса → «Кнопка устарела»."""
    teacher = await _mk_user(session_factory)
    subject = await _mk_subject(session_factory)
    await _grant(session_factory, teacher.id, subject.id)
    theme = await _mk_theme(session_factory, subject.id)
    db = db_user("teacher", teacher.id)

    state = await make_fsm()
    cb0 = make_cb(f"tch:add_task:{theme.id}:0", FakeMessage())
    await t_h.cb_add_task(cb0, state, db_user=db)
    assert await state.get_state() == AddTaskStates.question

    for data in ("tch:at:opt_more:0", "tch:at:opts_done:0"):
        state2 = await make_fsm()
        cb1 = make_cb(f"tch:add_task:{theme.id}:0", FakeMessage())
        await t_h.cb_add_task(cb1, state2, db_user=db)
        cb = make_cb(data, FakeMessage())
        handler = t_h.cb_opt_more if data.endswith("opt_more:0") else t_h.cb_opts_done
        await handler(cb, state2, db_user=db)
        assert any(t == t_h.MSG_STALE_FULL and alert for t, alert in cb.answers)
        assert await state2.get_state() is None


async def test_opt_more_with_four_options_warns(session_factory):
    teacher = await _mk_user(session_factory)
    subject = await _mk_subject(session_factory)
    await _grant(session_factory, teacher.id, subject.id)
    theme = await _mk_theme(session_factory, subject.id)
    db = db_user("teacher", teacher.id)

    state = await make_fsm()
    cb0 = make_cb(f"tch:add_task:{theme.id}:0", FakeMessage())
    await t_h.cb_add_task(cb0, state, db_user=db)
    m = FakeMessage(text="Вопрос")
    await t_h.on_task_question(m, state, db_user=db)

    # 4-й вариант — максимум: «достигнут максимум», только «✅ Готово»
    last = None
    for i, opt in enumerate(["а", "б", "в", "г"], 1):
        last = FakeMessage(text=opt)
        await t_h.on_task_option(last, state, db_user=db)
        if i == 4:
            assert any(
                "✅ Вариант 4 добавлен (достигнут максимум — 4)." in t
                for t, _ in last.answers
            )
    buttons = cb_buttons(last.answers[-1][1])
    assert [c for _, c in buttons] == ["tch:at:opts_done:0"]  # без «Ещё вариант»

    # «➕ Ещё вариант» при 4 → предупреждение, вариант не добавился
    msg = FakeMessage()
    cb = make_cb("tch:at:opt_more:0", msg)
    await t_h.cb_opt_more(cb, state, db_user=db)
    assert any(t == t_h.TEXT_OPTION_MAX for t, _ in msg.answers)
    assert len((await state.get_data())["options"]) == 4


# ---------------------------------------------------------------------------
# Защита от двойного клика «Сохранить» (критерий 5, ошибка №8)
# ---------------------------------------------------------------------------
async def test_double_save_creates_one_task(session_factory):
    teacher = await _mk_user(session_factory)
    subject = await _mk_subject(session_factory)
    await _grant(session_factory, teacher.id, subject.id)
    theme = await _mk_theme(session_factory, subject.id)
    db = db_user("teacher", teacher.id)

    state = await make_fsm()
    cb0 = make_cb(f"tch:add_task:{theme.id}:0", FakeMessage())
    await t_h.cb_add_task(cb0, state, db_user=db)
    m = FakeMessage(text="Вопрос")
    await t_h.on_task_question(m, state, db_user=db)
    await _add_options(state, "4", "5x")
    await _to_correct(state, teacher.id)
    await _to_preview(state, 0, teacher.id)

    saved1 = FakeMessage()
    cb1 = make_cb("tch:at:save:0", saved1)
    await t_h.cb_task_save(cb1, state, db_user=db)

    # второй клик (даже если первый ещё «в полёте»/после state.clear) —
    # отвечает «Уже сохраняю…», повторного создания нет
    saved2 = FakeMessage()
    cb2 = make_cb("tch:at:save:0", saved2)
    await t_h.cb_task_save(cb2, state, db_user=db)
    assert any(t == t_h.SAVING_NOW and alert for t, alert in cb2.answers)

    async with session_factory() as s:
        tasks = (await s.scalars(select(Task))).all()
        assert len(tasks) == 1


async def test_save_strips_preview_buttons_immediately(session_factory):
    """Раздел 8.5: клавиатура превью снимается сразу при клике «Сохранить»."""
    teacher = await _mk_user(session_factory)
    subject = await _mk_subject(session_factory)
    await _grant(session_factory, teacher.id, subject.id)
    theme = await _mk_theme(session_factory, subject.id)
    db = db_user("teacher", teacher.id)

    state = await make_fsm()
    cb0 = make_cb(f"tch:add_task:{theme.id}:0", FakeMessage())
    await t_h.cb_add_task(cb0, state, db_user=db)
    m = FakeMessage(text="Вопрос")
    await t_h.on_task_question(m, state, db_user=db)
    await _add_options(state, "4", "5x")
    await _to_correct(state, teacher.id)
    await _to_preview(state, 0, teacher.id)

    saved = FakeMessage()
    cb = make_cb("tch:at:save:0", saved)
    await t_h.cb_task_save(cb, state, db_user=db)
    assert saved.edited_markup == [None]


async def test_save_failure_clears_state(session_factory, monkeypatch):
    """Ошибка БД/сети в create_task → state.clear(), визард не «мёртвый»."""
    teacher = await _mk_user(session_factory)
    subject = await _mk_subject(session_factory)
    await _grant(session_factory, teacher.id, subject.id)
    theme = await _mk_theme(session_factory, subject.id)
    db = db_user("teacher", teacher.id)

    state = await make_fsm()
    cb0 = make_cb(f"tch:add_task:{theme.id}:0", FakeMessage())
    await t_h.cb_add_task(cb0, state, db_user=db)
    m = FakeMessage(text="Вопрос")
    await t_h.on_task_question(m, state, db_user=db)
    await _add_options(state, "4", "5x")
    await _to_correct(state, teacher.id)
    await _to_preview(state, 0, teacher.id)

    async def _boom(*args, **kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(teacher_svc, "create_task", _boom)
    saved = FakeMessage()
    cb = make_cb("tch:at:save:0", saved)
    await t_h.cb_task_save(cb, state, db_user=db)

    assert await state.get_state() is None  # metka saving не висит
    assert any(t == t_h.MSG_SAVE_FAILED for t, _ in saved.answers)
    async with session_factory() as s:
        assert (await s.scalars(select(Task))).all() == []


async def test_save_markup_failure_clears_state(session_factory):
    """Сбой edit_reply_markup (не BadRequest) → state.clear(), не saving-висельник."""
    teacher = await _mk_user(session_factory)
    subject = await _mk_subject(session_factory)
    await _grant(session_factory, teacher.id, subject.id)
    theme = await _mk_theme(session_factory, subject.id)
    db = db_user("teacher", teacher.id)

    state = await make_fsm()
    cb0 = make_cb(f"tch:add_task:{theme.id}:0", FakeMessage())
    await t_h.cb_add_task(cb0, state, db_user=db)
    m = FakeMessage(text="Вопрос")
    await t_h.on_task_question(m, state, db_user=db)
    await _add_options(state, "4", "5x")
    await _to_correct(state, teacher.id)
    await _to_preview(state, 0, teacher.id)

    async def _boom(*args, **kwargs):
        raise RuntimeError("network")

    saved = FakeMessage()
    saved.edit_reply_markup = _boom  # сбой снятия клавиатуры
    cb = make_cb("tch:at:save:0", saved)
    await t_h.cb_task_save(cb, state, db_user=db)

    assert await state.get_state() is None
    assert any(t == t_h.MSG_SAVE_FAILED for t, _ in saved.answers)
    async with session_factory() as s:
        assert (await s.scalars(select(Task))).all() == []


# ---------------------------------------------------------------------------
# Команды отменяют визарды (раздел 0 дефект 1, критерий 6)
# ---------------------------------------------------------------------------
async def test_menu_cancels_task_wizard(session_factory):
    teacher = await _mk_user(session_factory)
    subject = await _mk_subject(session_factory)
    await _grant(session_factory, teacher.id, subject.id)
    theme = await _mk_theme(session_factory, subject.id)
    db = db_user("teacher", teacher.id)
    state = await make_fsm()
    cb0 = make_cb(f"tch:add_task:{theme.id}:0", FakeMessage())
    await t_h.cb_add_task(cb0, state, db_user=db)
    assert await state.get_state() == AddTaskStates.question

    msg = FakeMessage()
    await cmd_h.cmd_menu(msg, db_user=db, state=state)
    assert msg.answers[0][0] == cmd_h.WIZARD_CANCELED
    assert "Меню:" in [a[0] for a in msg.answers]
    assert await state.get_state() is None  # «мусор» в визард больше не попадёт


async def test_menu_cancels_theme_wizard_no_garbage_theme(session_factory):
    teacher = await _mk_user(session_factory)
    subject = await _mk_subject(session_factory)
    await _grant(session_factory, teacher.id, subject.id)
    db = db_user("teacher", teacher.id)

    state = await make_fsm()
    cb0 = make_cb(f"tch:add_theme:{subject.id}:0", FakeMessage())
    await t_h.cb_add_theme(cb0, state, db_user=db)
    assert await state.get_state() == AddThemeStates.name

    msg = FakeMessage()
    await cmd_h.cmd_menu(msg, db_user=db, state=state)
    assert msg.answers[0][0] == cmd_h.WIZARD_CANCELED
    assert await state.get_state() is None
    # текст после отмены не создаёт тему из мусора: визард-хендлер не
    # получит сообщение (state пуст), тем в БД нет
    async with session_factory() as s:
        assert (await s.scalars(select(Theme))).all() == []


async def test_menu_cancels_student_wizard(session_factory):
    """То же для визарда ученика (менеджер): /menu отменяет."""
    state = await make_fsm()
    msg0 = FakeMessage()
    await m_h.cmd_add_student(msg0, state=state, db_user=db_user("manager", 1))
    assert await state.get_state() == AddStudentStates.name

    msg = FakeMessage()
    await cmd_h.cmd_menu(
        msg, db_user=db_user("manager", 1), state=state
    )
    assert msg.answers[0][0] == cmd_h.WIZARD_CANCELED
    assert await state.get_state() is None


async def test_teacher_commands_clear_wizard(session_factory):
    teacher = await _mk_user(session_factory)
    subject = await _mk_subject(session_factory)
    await _grant(session_factory, teacher.id, subject.id)
    theme = await _mk_theme(session_factory, subject.id)
    db = db_user("teacher", teacher.id)

    for command in ("my_subjects", "tasks"):
        state = await make_fsm()
        cb0 = make_cb(f"tch:add_task:{theme.id}:0", FakeMessage())
        await t_h.cb_add_task(cb0, state, db_user=db)
        assert await state.get_state() == AddTaskStates.question
        msg = FakeMessage()
        handler = getattr(t_h, f"cmd_{command}")
        await handler(msg, state=state, db_user=db)
        assert await state.get_state() is None, command

    # /add_theme — старый визард чистится, входит в чистый визард темы
    state = await make_fsm()
    cb0 = make_cb(f"tch:add_task:{theme.id}:0", FakeMessage())
    await t_h.cb_add_task(cb0, state, db_user=db)
    msg_q = FakeMessage(text="2+2?")
    await t_h.on_task_question(msg_q, state, db_user=db)
    await _add_options(state, "4", "5x")
    msg = FakeMessage()
    await t_h.cmd_add_theme(msg, state=state, db_user=db)
    assert await state.get_state() == AddThemeStates.name
    assert (await state.get_data()) == {"subject_id": subject.id}  # без мусора визарда задания


# ---------------------------------------------------------------------------
# Валидация шагов (пустой/длинный вопрос, кнопки опций)
# ---------------------------------------------------------------------------
async def test_task_question_validation(session_factory):
    teacher = await _mk_user(session_factory)
    subject = await _mk_subject(session_factory)
    await _grant(session_factory, teacher.id, subject.id)
    theme = await _mk_theme(session_factory, subject.id)
    db = db_user("teacher", teacher.id)
    state = await make_fsm()
    cb0 = make_cb(f"tch:add_task:{theme.id}:0", FakeMessage())
    await t_h.cb_add_task(cb0, state, db_user=db)

    m = FakeMessage(text="   ")
    await t_h.on_task_question(m, state, db_user=db)
    assert any(t == t_h.TEXT_EMPTY_QUESTION for t, _ in m.answers)
    assert await state.get_state() == AddTaskStates.question

    m = FakeMessage(text="Д" * 1001)
    await t_h.on_task_question(m, state, db_user=db)
    assert any(t == t_h.TEXT_QUESTION_TOO_LONG for t, _ in m.answers)
    assert await state.get_state() == AddTaskStates.question


async def test_opts_done_too_few_alert(session_factory):
    teacher = await _mk_user(session_factory)
    subject = await _mk_subject(session_factory)
    await _grant(session_factory, teacher.id, subject.id)
    theme = await _mk_theme(session_factory, subject.id)
    db = db_user("teacher", teacher.id)
    state = await make_fsm()
    cb0 = make_cb(f"tch:add_task:{theme.id}:0", FakeMessage())
    await t_h.cb_add_task(cb0, state, db_user=db)
    m = FakeMessage(text="Вопрос")
    await t_h.on_task_question(m, state, db_user=db)
    m = FakeMessage(text="один вариант")
    await t_h.on_task_option(m, state, db_user=db)

    cb = make_cb("tch:at:opts_done:0", FakeMessage())
    await t_h.cb_opts_done(cb, state, db_user=db)
    assert any(t == t_h.ALERT_OPTIONS_MIN and alert for t, alert in cb.answers)
    assert await state.get_state() == AddTaskStates.options


async def test_stale_wizard_buttons(session_factory):
    """Устаревшие кнопки визарда на пустом state → «Кнопка устарела» + clear."""
    teacher = await _mk_user(session_factory)
    db = db_user("teacher", teacher.id)
    state = await make_fsm()

    msg = FakeMessage()
    cb = make_cb("tch:at:opt_more:0", msg)
    await t_h.cb_opt_more(cb, state, db_user=db)
    assert any(t == t_h.MSG_STALE_FULL and alert for t, alert in cb.answers)
    assert await state.get_state() is None

    cb = make_cb("tch:at:exp_skip:0", FakeMessage())
    await t_h.cb_exp_skip(cb, state, db_user=db)
    assert any(t == t_h.MSG_STALE_FULL and alert for t, alert in cb.answers)

    msg = FakeMessage()
    cb = make_cb("tch:at:restart:0", msg)
    await t_h.cb_task_restart(cb, state, db_user=db)
    assert any(t == t_h.MSG_STALE_FULL and alert for t, alert in cb.answers)


async def test_restart_keeps_only_theme_id(session_factory):
    """«✏️ Заново»: в question, в state только theme_id (без task_id)."""
    teacher = await _mk_user(session_factory)
    subject = await _mk_subject(session_factory)
    await _grant(session_factory, teacher.id, subject.id)
    theme = await _mk_theme(session_factory, subject.id)
    db = db_user("teacher", teacher.id)

    state = await make_fsm()
    cb0 = make_cb(f"tch:add_task:{theme.id}:0", FakeMessage())
    await t_h.cb_add_task(cb0, state, db_user=db)
    m = FakeMessage(text="Вопрос")
    await t_h.on_task_question(m, state, db_user=db)
    await _add_options(state, "4", "5x")

    msg = FakeMessage()
    cb = make_cb("tch:at:restart:0", msg)
    await t_h.cb_task_restart(cb, state, db_user=db)
    assert await state.get_state() == AddTaskStates.question
    assert (await state.get_data()) == {
        "theme_id": theme.id,
        "subtheme_id": None,  # подтема визарда сохраняется (текущий заход)
    }
    assert any(t == t_h.ASK_QUESTION for t, _ in msg.answers)


# ---------------------------------------------------------------------------
# Карточка задания: показать/скрыть, удалить (критерий 7)
# ---------------------------------------------------------------------------
async def _mk_task(session_factory, theme_id, teacher_id, question="Вопрос", is_active=True):
    async with session_factory() as session:
        return await teacher_svc.create_task(
            session, theme_id, question, None,
            teacher_svc.build_options_json(["А", "Б"], 0),
            None, None, teacher_id,
        )


async def test_task_card_view(session_factory):
    teacher = await _mk_user(session_factory)
    subject = await _mk_subject(session_factory)
    await _grant(session_factory, teacher.id, subject.id)
    theme = await _mk_theme(session_factory, subject.id)
    task = await _mk_task(session_factory, theme.id, teacher.id, "Сколько будет 2+2?")
    db = db_user("teacher", teacher.id)

    msg = FakeMessage()
    cb = make_cb(f"tch:task:{task.id}:0", msg)
    await t_h.cb_task_card(cb, db_user=db)
    body, kb = msg.edits[-1]
    assert "📝 <b>Задание</b>" in body
    assert "Сколько будет 2+2?" in body
    assert "Вариантов: 2 · Правильный: А" in body
    assert "Объяснение: нет" in body
    assert "Статус: ✅ видно ученикам" in body
    assert ("🚫 Скрыть задание", f"tch:t_toggle:{task.id}:0") in cb_buttons(kb)
    assert ("🗑 Удалить", f"tch:t_del:{task.id}:0") in cb_buttons(kb)
    assert ("← К заданиям", f"tch:tasks:{theme.id}:0") in cb_buttons(kb)


async def test_task_toggle_hide_show(session_factory):
    teacher = await _mk_user(session_factory)
    subject = await _mk_subject(session_factory)
    await _grant(session_factory, teacher.id, subject.id)
    theme = await _mk_theme(session_factory, subject.id)
    task = await _mk_task(session_factory, theme.id, teacher.id)
    db = db_user("teacher", teacher.id)

    msg = FakeMessage()
    cb = make_cb(f"tch:t_toggle:{task.id}:0", msg)
    await t_h.cb_task_toggle(cb, db_user=db)
    assert any(
        t == t_h.TEXT_TASK_HIDDEN and not alert for t, alert in cb.answers
    )
    assert len(cb.answers) == 1  # перерисовка → единственный тост
    assert msg.edits
    body = msg.edits[-1][0]
    assert "Статус: 🚫 скрыто" in body
    assert ("✅ Показать задание", f"tch:t_toggle:{task.id}:0") in cb_buttons(msg.edits[-1][1])
    async with session_factory() as s:
        assert (await s.get(Task, task.id)).is_active is False

    msg2 = FakeMessage()
    cb2 = make_cb(f"tch:t_toggle:{task.id}:0", msg2)
    await t_h.cb_task_toggle(cb2, db_user=db)
    assert any(t == t_h.TEXT_TASK_SHOWN for t, _ in cb2.answers)
    async with session_factory() as s:
        assert (await s.get(Task, task.id)).is_active is True


async def test_task_delete_flow_cascades_progress(session_factory):
    teacher = await _mk_user(session_factory)
    manager = await _mk_user(session_factory, role="manager")
    subject = await _mk_subject(session_factory)
    await _grant(session_factory, teacher.id, subject.id)
    theme = await _mk_theme(session_factory, subject.id)
    task = await _mk_task(session_factory, theme.id, teacher.id, "Вопрос")
    db = db_user("teacher", teacher.id)

    # ученик с прогрессом и попыткой по заданию
    async with session_factory() as session:
        _, student, _ = await students_svc.create_student_record(
            session, "Иван", {subject.id}, manager.id, None
        )
        sid = student.id
        session.add(Attempt(student_id=sid, task_id=task.id, is_correct=True))
        session.add(TaskProgress(student_id=sid, task_id=task.id, status="done"))
        await session.commit()

    # подтверждение удаления
    msg = FakeMessage()
    cb = make_cb(f"tch:t_del:{task.id}:0", msg)
    await t_h.cb_task_del(cb, db_user=db)
    body, kb = msg.edits[-1]
    assert "🗑 Удалить задание «Вопрос»?" in body
    assert "Прогресс учеников по этому заданию тоже удалится." in body
    assert ("Да, удалить", f"tch:t_yes:{task.id}:0") in cb_buttons(kb)
    assert ("Отмена", f"tch:t_no:{task.id}:0") in cb_buttons(kb)

    # подтверждение → удалено, прогресс и попытки каскадом чисты
    msg2 = FakeMessage()
    cb2 = make_cb(f"tch:t_yes:{task.id}:0", msg2)
    await t_h.cb_task_del_yes(cb2, db_user=db)
    assert any(t == t_h.TEXT_TASK_DELETED for t, _ in msg2.edits)
    assert msg2.answers  # список заданий темы новым сообщением
    list_body, list_kb = msg2.answers[-1]
    assert "📝 <b>Задания темы «Уравнения»</b>" in list_body
    assert t_h.TEXT_TASKS_EMPTY in list_body
    assert ("➕ Добавить задание", f"tch:add_task:{theme.id}:0") in cb_buttons(list_kb)
    async with session_factory() as s:
        assert await s.get(Task, task.id) is None
        assert (await s.scalars(select(Attempt))).all() == []
        assert (await s.scalars(select(TaskProgress))).all() == []


async def test_task_delete_cancel_returns_card(session_factory):
    teacher = await _mk_user(session_factory)
    subject = await _mk_subject(session_factory)
    await _grant(session_factory, teacher.id, subject.id)
    theme = await _mk_theme(session_factory, subject.id)
    task = await _mk_task(session_factory, theme.id, teacher.id)
    db = db_user("teacher", teacher.id)

    msg = FakeMessage()
    cb = make_cb(f"tch:t_del:{task.id}:0", msg)
    await t_h.cb_task_del(cb, db_user=db)

    msg2 = FakeMessage()
    cb2 = make_cb(f"tch:t_no:{task.id}:0", msg2)
    await t_h.cb_task_del_no(cb2, db_user=db)
    assert "📝 <b>Задание</b>" in msg2.edits[-1][0]  # вернулась карточка
    async with session_factory() as s:
        assert await s.get(Task, task.id) is not None


async def test_task_card_stale_and_foreign(session_factory):
    teacher = await _mk_user(session_factory)
    other = await _mk_user(session_factory)
    subject = await _mk_subject(session_factory)
    await _grant(session_factory, other.id, subject.id)
    theme = await _mk_theme(session_factory, subject.id)
    task = await _mk_task(session_factory, theme.id, other.id)
    db = db_user("teacher", teacher.id)

    msg = FakeMessage()
    cb = make_cb(f"tch:task:{task.id}:0", msg)
    await t_h.cb_task_card(cb, db_user=db)
    assert any(t == t_h.MSG_TASK_NOT_FOUND and alert for t, alert in cb.answers)

    cb = make_cb("tch:task:999999:0", FakeMessage())
    await t_h.cb_task_card(cb, db_user=db)
    assert any(t == t_h.MSG_TASK_NOT_FOUND and alert for t, alert in cb.answers)

    cb = make_cb("tch:t_toggle:999999:0", FakeMessage())
    await t_h.cb_task_toggle(cb, db_user=db)
    assert any(t == t_h.MSG_TASK_NOT_FOUND and alert for t, alert in cb.answers)


# ---------------------------------------------------------------------------
# Раздел 0: прочие дефекты, связанные с визардом/кнопками
# ---------------------------------------------------------------------------
async def test_theme_name_photo_hint_state_kept(session_factory):
    """Раздел 0 дефект 3: фото в шаге «Название темы:» → подсказка."""
    teacher = await _mk_user(session_factory)
    subject = await _mk_subject(session_factory)
    await _grant(session_factory, teacher.id, subject.id)
    db = db_user("teacher", teacher.id)

    state = await make_fsm()
    cb0 = make_cb(f"tch:add_theme:{subject.id}:0", FakeMessage())
    await t_h.cb_add_theme(cb0, state, db_user=db)
    assert await state.get_state() == AddThemeStates.name

    m = FakeMessage(photo=[SimpleNamespace(file_id="p")])
    await t_h.on_add_theme_name_nontext(m, db_user=db)
    assert any(t == t_h.TEXT_HINT_THEME_NAME for t, _ in m.answers)
    assert await state.get_state() == AddThemeStates.name  # не потеряно

    m2 = FakeMessage(text="Уравнения")
    await t_h.on_add_theme_name(m2, state, db_user=db)
    assert any("✅ Тема «Уравнения» создана." in t for t, _ in m2.answers)


async def test_double_enter_wizard_restarts(session_factory):
    """Раздел 0 п.8: повторный вход в визард — с чистого листа (state.clear)."""
    teacher = await _mk_user(session_factory)
    subject = await _mk_subject(session_factory)
    s2 = await _mk_subject(session_factory, "Физика")
    await _grant(session_factory, teacher.id, subject.id)
    await _grant(session_factory, teacher.id, s2.id)
    db = db_user("teacher", teacher.id)

    state = await make_fsm()
    cb0 = make_cb(f"tch:add_theme:{subject.id}:0", FakeMessage())
    await t_h.cb_add_theme(cb0, state, db_user=db)
    cb1 = make_cb(f"tch:add_theme:{s2.id}:0", FakeMessage())
    await t_h.cb_add_theme(cb1, state, db_user=db)
    data = await state.get_data()
    assert data["subject_id"] == s2.id  # мусора от первого входа нет
    assert await state.get_state() == AddThemeStates.name


@pytest.mark.parametrize("command,expected", [
    ("tch:th_open", t_h.MSG_THEME_NOT_FOUND),
    ("tch:theme", t_h.MSG_THEME_NOT_FOUND),
    ("tch:add_theme", t_h.MSG_SUBJECT_NOT_FOUND),
    ("tch:subj", t_h.MSG_SUBJECT_NOT_FOUND),
])
async def test_hidden_subject_old_buttons(session_factory, command, expected):
    """Раздел 0 дефект 2: по скрытому предмету старые кнопки → отказ."""
    teacher = await _mk_user(session_factory)
    subject = await _mk_subject(session_factory)
    await _grant(session_factory, teacher.id, subject.id)
    theme = await _mk_theme(session_factory, subject.id)
    # владелец скрыл предмет
    async with session_factory() as s:
        hidden = await s.get(Subject, subject.id)
        hidden.is_active = False
        await s.commit()
    db = db_user("teacher", teacher.id)

    msg = FakeMessage()
    if command == "tch:th_open":
        cb = make_cb(f"{command}:{theme.id}:0", msg)
        await t_h.cb_theme_toggle_open(cb, db_user=db)
        assert any(t == expected and alert for t, alert in cb.answers)
    elif command == "tch:theme":
        cb = make_cb(f"{command}:{theme.id}:0", msg)
        await t_h.cb_theme_menu(cb, db_user=db)
        assert any(t == expected and alert for t, alert in cb.answers)
    elif command == "tch:add_theme":
        cb = make_cb(f"{command}:{subject.id}:0", msg)
        await t_h.cb_add_theme(cb, state=await make_fsm(), db_user=db)
        # ответ сообщением (не алерт): «Предмет не найден»
        assert any(t == expected for t, _ in msg.answers)
    else:
        cb = make_cb(f"{command}:{subject.id}:0", msg)
        await t_h.cb_theme_list(cb, db_user=db)
        assert any(t == expected and alert for t, alert in cb.answers)
    assert not msg.edits  # ничего не перерисовано


async def test_toggle_open_deleted_theme_race(session_factory, monkeypatch):
    """Раздел 0 дефект 4: тема удалена между проверкой и toggle → alert, без перерисовки."""
    teacher = await _mk_user(session_factory)
    subject = await _mk_subject(session_factory)
    await _grant(session_factory, teacher.id, subject.id)
    theme = await _mk_theme(session_factory, subject.id)
    db = db_user("teacher", teacher.id)

    async def _toggle_none(session, theme_id):
        return None  # тема исчезла между проверкой и переключением

    monkeypatch.setattr(teacher_svc, "toggle_theme_open", _toggle_none)

    msg = FakeMessage()
    cb = make_cb(f"tch:th_open:{theme.id}:0", msg)
    await t_h.cb_theme_toggle_open(cb, db_user=db)
    assert any(t == t_h.MSG_THEME_NOT_FOUND and alert for t, alert in cb.answers)
    assert len(cb.answers) == 1
    assert not msg.edits  # меню удалённой темы не рисуем


async def test_rename_prompt_edits_not_stacks(session_factory):
    """Раздел 0 дефект 5: «Переименовать» перерисовывает сообщение, не копит."""
    teacher = await _mk_user(session_factory)
    subject = await _mk_subject(session_factory)
    await _grant(session_factory, teacher.id, subject.id)
    theme = await _mk_theme(session_factory, subject.id, "Старое")
    db = db_user("teacher", teacher.id)

    msg = FakeMessage()
    state = await make_fsm()
    cb = make_cb(f"tch:rename:{theme.id}:0", msg)
    await t_h.cb_rename_theme(cb, state, db_user=db)
    assert len(msg.edits) == 1 and not msg.answers  # только перерисовка
    assert "Текущее название: «Старое»." in msg.edits[-1][0]

# ---------------------------------------------------------------------------
# «✏️ Редактировать» задание (промт Захода 8.5, фича 4): предзаполненные
# значения, правка через «✏️ Вопрос/Варианты/Объяснение», save = UPDATE
# ---------------------------------------------------------------------------
async def _mk_task_direct(
    session_factory,
    theme_id: int,
    question="2+2?",
    options=None,
    correct: int = 0,
    feedback_text=None,
    is_active: bool = True,
) -> Task:
    if options is None:
        options = ["4", "5", "6"]
    async with session_factory() as session:
        task = Task(
            theme_id=theme_id,
            question_text=question,
            options=[
                {"t": text, "c": i == correct} for i, text in enumerate(options)
            ],
            feedback_text=feedback_text,
            is_active=is_active,
            order=0,
        )
        session.add(task)
        await session.commit()
        return task


async def test_task_edit_full_flow_updates_not_creates(session_factory):
    """«✏️ Редактировать»: превью с данными задания → правки через кнопки
    превью → «✅ Сохранить» = UPDATE (та же запись, is_active/order
    не тронуты, НОВОЕ задание не создаётся)."""
    teacher = await _mk_user(session_factory)
    subject = await _mk_subject(session_factory)
    await _grant(session_factory, teacher.id, subject.id)
    theme = await _mk_theme(session_factory, subject.id, "Уравнения")
    task = await _mk_task_direct(
        session_factory, theme.id, "2+2?", options=["4", "5", "6"],
        correct=1, feedback_text="Сложи", is_active=False,
    )
    db = db_user("teacher", teacher.id)

    # 1. карточка задания → кнопка «✏️ Редактировать»
    msg_card = FakeMessage()
    await t_h.cb_task_card(
        make_cb(f"tch:task:{task.id}:0", msg_card), db_user=db
    )
    assert (
        "✏️ Редактировать",
        f"tch:t_edit:{task.id}:0",
    ) in cb_buttons(msg_card.edits[0][1])

    # 2. вход: state = preview, данные предзаполнены, «← К заданию» на месте
    state = await make_fsm()
    msg = FakeMessage()
    await t_h.cb_task_edit(
        make_cb(f"tch:t_edit:{task.id}:0", msg), state, db_user=db
    )
    assert await state.get_state() == AddTaskStates.preview
    data = await state.get_data()
    assert data["task_id"] == task.id
    assert data["question_text"] == "2+2?"
    assert data["options"] == ["4", "5", "6"]
    assert data["correct_index"] == 1
    assert data["feedback_text"] == "Сложи"
    preview_text, preview_kb = msg.answers[0]
    assert "2+2?" in preview_text
    buttons = cb_buttons(preview_kb)
    assert ("✅ Сохранить", "tch:at:save:0") in buttons
    assert ("✏️ Вопрос", "tch:at:edit_q:0") in buttons
    assert ("✏️ Варианты", "tch:at:edit_o:0") in buttons
    assert ("✏️ Объяснение", "tch:at:edit_e:0") in buttons
    assert ("← К заданию", f"tch:at:edit_back:{task.id}:0") in buttons

    # 3. «✏️ Варианты» → варианты вводятся заново (как в добавлении)
    cb_o = make_cb("tch:at:edit_o:0", FakeMessage())
    await t_h.cb_preview_edit_options(cb_o, state, db_user=db)
    assert await state.get_state() == AddTaskStates.options
    assert (await state.get_data())["options"] == []
    await _add_options(state, "2+2=4", "2+2=5", "2+2=6", db_user_id=teacher.id)
    await _to_correct(state, db_user_id=teacher.id)

    # 4. правильный вариант → «Пропустить» → превью
    await t_h.cb_pick_correct(
        make_cb("tch:at:pick:2:0", FakeMessage()), state, db_user=db
    )
    await t_h.cb_exp_skip(
        make_cb("tch:at:exp_skip:0", FakeMessage()), state, db_user=db
    )
    assert await state.get_state() == AddTaskStates.preview
    data = await state.get_data()
    assert data["options"] == ["2+2=4", "2+2=5", "2+2=6"]
    assert data["correct_index"] == 2

    # 5. «✏️ Вопрос» → новый текст вопроса; «✏️ Объяснение» → Пропустить
    await t_h.cb_preview_edit_question(
        make_cb("tch:at:edit_q:0", FakeMessage()), state, db_user=db
    )
    await t_h.on_task_question(
        FakeMessage(text="3×3?"), state, db_user=db
    )
    await t_h.cb_preview_edit_explanation(
        make_cb("tch:at:edit_e:0", FakeMessage()), state, db_user=db
    )
    await t_h.cb_exp_skip(
        make_cb("tch:at:exp_skip:0", FakeMessage()), state, db_user=db
    )
    assert await state.get_state() == AddTaskStates.preview
    data = await state.get_data()
    assert data["question_text"] == "3×3?"
    assert data["feedback_text"] is None

    # 6. «✅ Сохранить» → UPDATE: та же запись, новые поля, статус/порядок целы
    msg_save = FakeMessage()
    await t_h.cb_task_save(
        make_cb("tch:at:save:0", msg_save), state, db_user=db
    )
    assert await state.get_state() is None
    assert msg_save.answers[0][0].startswith("✅ Задание сохранено!")
    async with session_factory() as session:
        updated = await session.get(Task, task.id)
        assert updated is not None
        assert updated.question_text == "3×3?"
        assert [o["t"] for o in updated.options] == ["2+2=4", "2+2=5", "2+2=6"]
        assert updated.options[2]["c"] is True
        assert updated.feedback_text is None
        assert updated.is_active is False  # статус не сброшен
        assert updated.order == 0          # порядок не сброшен
        tasks = (await session.scalars(select(Task))).all()
        assert len(tasks) == 1             # НЕ создана новая запись


async def test_task_edit_back_returns_to_card_and_cancels(session_factory):
    """«← К заданию» в превью редактирования: визард отменён, карточка."""
    teacher = await _mk_user(session_factory)
    subject = await _mk_subject(session_factory)
    await _grant(session_factory, teacher.id, subject.id)
    theme = await _mk_theme(session_factory, subject.id)
    task = await _mk_task_direct(session_factory, theme.id)
    db = db_user("teacher", teacher.id)

    state = await make_fsm()
    msg = FakeMessage()
    await t_h.cb_task_edit(
        make_cb(f"tch:t_edit:{task.id}:0", msg), state, db_user=db
    )
    msg_back = FakeMessage()
    await t_h.cb_task_edit_back(
        make_cb(f"tch:at:edit_back:{task.id}:0", msg_back), state, db_user=db
    )
    assert await state.get_state() is None
    card_text, card_kb = msg_back.edits[0]
    assert "📝 <b>Задание</b>" in card_text
    assert ("🗑 Удалить", f"tch:t_del:{task.id}:0") in cb_buttons(card_kb)
