"""Тесты рассылки (Заход 9): сервисы сбора получателей/отправки и визард.

Хендлеры вызываются напрямую с фейковыми callback/message и FSM-контекстом
(подход как в test_owner.py). Получатели и владелец — реальные записи БД.
"""
from types import SimpleNamespace

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from app.handlers import broadcast as b_h
from app.handlers import commands as cmd_h
from app.models import Student, StudentSubject, Subject, User
from app.services import broadcast as bcast_svc
from app.services import people as people_svc
from app.states import BroadcastStates

MSG_STALE = "Кнопка устарела, начни заново."
TEXT_EMPTY_RECIPIENTS = "Выбери получателей"
TEXT_NO_SUBJECTS = "Предметов пока нет"
TEXT_SEND_HINT = "Отправь текст или фото"
TEXT_SUBJECT_GONE = "Предмет больше не существует"
TEXT_CANCELED = "Рассылка отменена."


class FakeMessage:
    """Message: answer/answer_photo пишут в списки, edit_text — в .edits."""

    def __init__(self, text="", chat_id=42):
        self.text = text
        self.caption = None
        self.chat = SimpleNamespace(id=chat_id)
        self.answers = []
        self.photos = []
        self.edits = []

    async def answer(self, content="", reply_markup=None, **kwargs):
        self.answers.append((content, reply_markup))

    async def answer_photo(self, photo, caption=None, reply_markup=None, **kwargs):
        self.photos.append((photo, caption, reply_markup))

    async def edit_text(self, text, reply_markup=None, **kwargs):
        self.edits.append((text, reply_markup))


def make_callback(data: str, message: FakeMessage):
    answers = []

    async def answer(content="", show_alert=False, **kwargs):
        answers.append((content, show_alert))

    return SimpleNamespace(
        data=data, message=message, answer=answer, answers=answers
    )


def cb_buttons(markup):
    if markup is None:
        return []
    return [(b.text, b.callback_data) for row in markup.inline_keyboard for b in row]


async def make_fsm() -> FSMContext:
    storage = MemoryStorage()
    key = StorageKey(chat_id=42, user_id=42, bot_id=42424242, destiny="test")
    return FSMContext(storage=storage, key=key)


class StubBot:
    """Бот для отправки: записывает вызовы, для fail_tg_ids бросает."""

    def __init__(self, fail_tg_ids=()):
        self.sent = []
        self.fail_tg_ids = set(fail_tg_ids)

    async def send_message(self, chat_id, text, **kwargs):
        if chat_id in self.fail_tg_ids:
            raise RuntimeError("boom")
        self.sent.append(("msg", chat_id, text))

    async def send_photo(self, chat_id, photo, caption=None, **kwargs):
        if chat_id in self.fail_tg_ids:
            raise RuntimeError("boom")
        self.sent.append(("photo", chat_id, caption))


async def _mk_user(session_factory, *, role, tg_id, is_active=True, role2=None) -> User:
    async with session_factory() as session:
        user = User(
            tg_id=tg_id,
            tg_username=f"u_{role}_{tg_id}",
            role=role,
            role2=role2,
            is_active=is_active,
        )
        session.add(user)
        await session.commit()
        return user


async def _mk_student(
    session_factory, *, tg_id, subject_ids=(), subject_active=True, is_active=True
) -> User:
    user = await _mk_user(
        session_factory, role="student", tg_id=tg_id, is_active=is_active
    )
    async with session_factory() as session:
        student = Student(user_id=user.id)
        session.add(student)
        await session.flush()
        for sid in subject_ids:
            session.add(
                StudentSubject(
                    student_id=student.id, subject_id=sid, is_active=subject_active
                )
            )
        await session.commit()
    return user


async def _mk_owner(session_factory) -> User:
    return await _mk_user(session_factory, role="owner", tg_id=999)


async def _start(session_factory):
    """Возвращает (owner, msg, cb-фабрику, state) с запущенным визардом."""
    owner = await _mk_owner(session_factory)
    state = await make_fsm()
    msg = FakeMessage()
    await b_h.cmd_broadcast(msg, state, db_user=owner)
    return owner, msg, state


# --------------------------------------------------------------------------
# Сервис: сбор получателей
# --------------------------------------------------------------------------
async def test_collect_students_all(session_factory):
    async with session_factory() as session:
        subj = await people_svc.create_subject(session, "Математика")
    await _mk_student(session_factory, tg_id=1, subject_ids=[subj.id])
    await _mk_student(session_factory, tg_id=2, subject_ids=[subj.id], is_active=False)
    await _mk_user(session_factory, role="teacher", tg_id=3)
    await _mk_user(session_factory, role="manager", tg_id=4)

    async with session_factory() as session:
        users = await bcast_svc.collect_recipients(session, ["students"])
    assert {u.tg_id for u in users} == {1}  # неактивный и staff не попадают


async def test_collect_students_by_subject(session_factory):
    async with session_factory() as session:
        math = await people_svc.create_subject(session, "Математика")
        phys = await people_svc.create_subject(session, "Физика")
        hidden = await people_svc.create_subject(session, "Скрытый")
        hidden.is_active = False
        await session.commit()
    await _mk_student(session_factory, tg_id=1, subject_ids=[math.id])
    await _mk_student(session_factory, tg_id=2, subject_ids=[phys.id])
    # запись по математике, но предмет скрыт — не попадает
    await _mk_student(session_factory, tg_id=3, subject_ids=[hidden.id])

    async with session_factory() as session:
        users = await bcast_svc.collect_recipients(
            session, ["students"], "subjects", [math.id]
        )
    assert {u.tg_id for u in users} == {1}

    # неактивная запись по выбранному предмету — не попадает
    await _mk_student(session_factory, tg_id=4, subject_ids=[math.id], subject_active=False)
    async with session_factory() as session:
        users = await bcast_svc.collect_recipients(
            session, ["students"], "subjects", [math.id]
        )
    assert {u.tg_id for u in users} == {1}


async def test_collect_staff_including_role2(session_factory):
    """Преподаватели/менеджеры: по role И role2; совмещённый — один раз."""
    await _mk_user(session_factory, role="teacher", tg_id=1)
    await _mk_user(session_factory, role="manager", tg_id=2)
    await _mk_user(session_factory, role="teacher", tg_id=3, role2="manager")
    await _mk_user(session_factory, role="teacher", tg_id=4, is_active=False)
    await _mk_student(session_factory, tg_id=5)

    async with session_factory() as session:
        teachers = await bcast_svc.collect_recipients(session, ["teachers"])
        managers = await bcast_svc.collect_recipients(session, ["managers"])
        both = await bcast_svc.collect_recipients(
            session, ["teachers", "managers"]
        )
    assert {u.tg_id for u in teachers} == {1, 3}
    assert {u.tg_id for u in managers} == {2, 3}
    assert {u.tg_id for u in both} == {1, 2, 3}  # без дублей


async def test_collect_empty_categories(session_factory):
    await _mk_student(session_factory, tg_id=1)
    async with session_factory() as session:
        assert await bcast_svc.collect_recipients(session, []) == []


# --------------------------------------------------------------------------
# Сервис: отправка
# --------------------------------------------------------------------------
async def test_send_broadcast_ok_failed_skipped(session_factory):
    users = [
        SimpleNamespace(tg_id=100, is_active=True),
        SimpleNamespace(tg_id=101, is_active=True),
        SimpleNamespace(tg_id=102, is_active=True),  # бот кинет исключение
        SimpleNamespace(tg_id=None, is_active=True),  # без tg_id — пропуск
        SimpleNamespace(tg_id=103, is_active=False),  # неактивный — пропуск
    ]
    bot = StubBot(fail_tg_ids={102})
    report = await bcast_svc.send_broadcast(
        FakeMessage(), bot, users, photo=False, text="Всем привет!"
    )
    assert report == {"ok": 2, "failed": 1, "skipped": 2}
    assert len(bot.sent) == 2


async def test_send_broadcast_photo(session_factory):
    users = [SimpleNamespace(tg_id=100, is_active=True)]
    bot = StubBot()
    report = await bcast_svc.send_broadcast(
        FakeMessage(), bot, users, photo=True, text="Смотрите!", photo_file_id="f1"
    )
    assert report == {"ok": 1, "failed": 0, "skipped": 0}
    assert bot.sent[0] == ("photo", 100, "Смотрите!")


# --------------------------------------------------------------------------
# Визард: старт и шаг 1 (категории)
# --------------------------------------------------------------------------
async def test_cmd_broadcast_starts_wizard(session_factory):
    owner, msg, state = await _start(session_factory)
    assert msg.answers[0][0] == "Получатели:"
    buttons = cb_buttons(msg.answers[0][1])
    assert ("👨🎓 Ученики", "bcast:rcp:students") in buttons
    assert ("📚 Выбрать предмет", "bcast:rcp:subjects") not in buttons  # ещё не выбраны
    assert await state.get_state() == BroadcastStates.recipients.state


async def test_rcp_toggle_redraws(session_factory):
    owner, msg, state = await _start(session_factory)
    cb = make_callback("bcast:rcp:students", msg)
    await b_h.cb_bcast_rcp(cb, state, db_user=owner)

    assert msg.edits  # перерисован шаг 1
    edited = msg.edits[0][0]
    assert "👨🎓 Ученики — все предметы" in edited
    buttons = cb_buttons(msg.edits[0][1])
    assert ("✅ 👨🎓 Ученики", "bcast:rcp:students") in buttons
    assert ("📚 Выбрать предмет", "bcast:rcp:subjects") in buttons  # появился
    # единственный ответ колбэку — пустой (перерисовка удалась)
    assert cb.answers == [("", False)]


async def test_rcp_next_without_selection_alert(session_factory):
    owner, msg, state = await _start(session_factory)
    cb = make_callback("bcast:rcp:next", msg)
    await b_h.cb_bcast_rcp(cb, state, db_user=owner)
    assert cb.answers[0] == (TEXT_EMPTY_RECIPIENTS, True)
    assert await state.get_state() == BroadcastStates.recipients.state


async def test_rcp_next_goes_to_message_input(session_factory):
    owner, msg, state = await _start(session_factory)
    await b_h.cb_bcast_rcp(
        make_callback("bcast:rcp:managers", msg), state, db_user=owner
    )
    cb = make_callback("bcast:rcp:next", msg)
    await b_h.cb_bcast_rcp(cb, state, db_user=owner)
    assert msg.answers[-1][0] == TEXT_SEND_HINT
    assert await state.get_state() == BroadcastStates.message_input.state


async def test_rcp_subjects_no_subjects_alert(session_factory):
    owner, msg, state = await _start(session_factory)
    cb = make_callback("bcast:rcp:subjects", msg)
    await b_h.cb_bcast_rcp(cb, state, db_user=owner)
    # alert «предметов нет» и возврат на шаг 1 (state не ушёл в subjects)
    assert cb.answers[0] == (TEXT_NO_SUBJECTS, True)
    assert await state.get_state() == BroadcastStates.recipients.state


async def test_rcp_subjects_flow_and_clear(session_factory):
    async with session_factory() as session:
        math = await people_svc.create_subject(session, "Математика")
    owner, msg, state = await _start(session_factory)
    await b_h.cb_bcast_rcp(
        make_callback("bcast:rcp:subjects", msg), state, db_user=owner
    )
    assert await state.get_state() == BroadcastStates.subjects.state
    assert msg.edits[-1][0] == "Выбери предмет:"
    assert ("Математика", f"bcast:sub:{math.id}") in cb_buttons(msg.edits[-1][1])

    # выбор предмета → возврат на шаг 1 с пометкой «(выбран)»
    cb = make_callback(f"bcast:sub:{math.id}", msg)
    await b_h.cb_bcast_sub(cb, state, db_user=owner)
    assert await state.get_state() == BroadcastStates.recipients.state
    assert "👨🎓 Ученики — 📚 Математика (выбран)" in msg.edits[-1][0]
    assert cb.answers == [("", False)]

    # «🌍 Все предметы» на шаге 2 → режим «все», возврат на шаг 1
    await b_h.cb_bcast_rcp(
        make_callback("bcast:rcp:subjects", msg), state, db_user=owner
    )
    await b_h.cb_bcast_sub(make_callback("bcast:sub:clear", msg), state, db_user=owner)
    assert await state.get_state() == BroadcastStates.recipients.state
    assert "👨🎓 Ученики — все предметы" in msg.edits[-1][0]


async def test_subject_pick_gone(session_factory):
    async with session_factory() as session:
        await people_svc.create_subject(session, "Математика")
    owner, msg, state = await _start(session_factory)
    await b_h.cb_bcast_rcp(
        make_callback("bcast:rcp:subjects", msg), state, db_user=owner
    )
    cb = make_callback("bcast:sub:777", msg)
    await b_h.cb_bcast_sub(cb, state, db_user=owner)
    assert cb.answers[0] == (TEXT_SUBJECT_GONE, True)
    assert await state.get_state() == BroadcastStates.subjects.state


async def test_rcp_stale_when_not_on_step1(session_factory):
    owner, msg, state = await _start(session_factory)
    await b_h.cb_bcast_rcp(
        make_callback("bcast:rcp:managers", msg), state, db_user=owner
    )
    await b_h.cb_bcast_rcp(
        make_callback("bcast:rcp:next", msg), state, db_user=owner
    )
    cb = make_callback("bcast:rcp:students", msg)  # старая кнопка шага 1
    await b_h.cb_bcast_rcp(cb, state, db_user=owner)
    assert cb.answers[0] == (MSG_STALE, True)


# --------------------------------------------------------------------------
# Визард: шаг 3 (текст/фото) и шаг 4 (предпросмотр)
# --------------------------------------------------------------------------
async def test_on_bcast_text_preview(session_factory):
    owner, msg, state = await _start(session_factory)
    await b_h.cb_bcast_rcp(
        make_callback("bcast:rcp:students", msg), state, db_user=owner
    )
    await _mk_student(session_factory, tg_id=1)
    await b_h.cb_bcast_rcp(
        make_callback("bcast:rcp:next", msg), state, db_user=owner
    )

    msg2 = FakeMessage(text="Всем привет!")
    await b_h.on_bcast_text(msg2, state, db_user=owner)
    assert await state.get_state() == BroadcastStates.confirm.state
    text, markup = msg2.answers[-1]
    assert text == "Всем привет!\n\nПолучателей: 1"
    assert ("🚀 Отправить", "bcast:go") in cb_buttons(markup)


async def test_on_bcast_photo_preview(session_factory):
    owner, msg, state = await _start(session_factory)
    await b_h.cb_bcast_rcp(
        make_callback("bcast:rcp:teachers", msg), state, db_user=owner
    )
    await b_h.cb_bcast_rcp(
        make_callback("bcast:rcp:next", msg), state, db_user=owner
    )

    msg2 = FakeMessage()
    msg2.photo = [SimpleNamespace(file_id="small"), SimpleNamespace(file_id="big")]
    msg2.caption = "Фото дня"
    await b_h.on_bcast_photo(msg2, state, db_user=owner)
    assert await state.get_state() == BroadcastStates.confirm.state
    assert msg2.photos[-1][0] == "big"
    assert "Получателей: 0" in msg2.photos[-1][1]


async def test_on_bcast_bad_input_hint(session_factory):
    owner, msg, state = await _start(session_factory)
    await b_h.cb_bcast_rcp(
        make_callback("bcast:rcp:managers", msg), state, db_user=owner
    )
    await b_h.cb_bcast_rcp(
        make_callback("bcast:rcp:next", msg), state, db_user=owner
    )
    msg2 = FakeMessage(text=None)
    msg2.document = SimpleNamespace()
    await b_h.on_bcast_bad_input(msg2, state, db_user=owner)
    assert msg2.answers[0][0] == TEXT_SEND_HINT
    assert await state.get_state() == BroadcastStates.message_input.state


async def test_bcast_go_sends_and_reports(session_factory):
    owner, msg, state = await _start(session_factory)
    await _mk_student(session_factory, tg_id=1)
    await _mk_user(session_factory, role="teacher", tg_id=2)
    await _mk_user(session_factory, role="teacher", tg_id=None)  # без tg_id
    await b_h.cb_bcast_rcp(
        make_callback("bcast:rcp:students", msg), state, db_user=owner
    )
    await b_h.cb_bcast_rcp(
        make_callback("bcast:rcp:teachers", msg), state, db_user=owner
    )
    await b_h.cb_bcast_rcp(
        make_callback("bcast:rcp:next", msg), state, db_user=owner
    )
    msg2 = FakeMessage(text="Анонс!")
    await b_h.on_bcast_text(msg2, state, db_user=owner)

    bot = StubBot(fail_tg_ids={2})
    cb = make_callback("bcast:go", msg)
    await b_h.cb_bcast_go(cb, state, db_user=owner, bot=bot)
    assert len(bot.sent) == 1  # студент дошёл, препод упал
    assert msg.answers[-1][0] == (
        "✅ Рассылка отправлена.\n"
        "Отправлено: 1\n"
        "Не удалось: 1\n"
        "Пропущено: 1"
    )
    assert await state.get_state() is None


async def test_bcast_go_stale(session_factory):
    owner, msg, state = await _start(session_factory)
    cb = make_callback("bcast:go", msg)
    await b_h.cb_bcast_go(cb, state, db_user=owner, bot=StubBot())
    assert cb.answers[0] == (MSG_STALE, True)


async def test_bcast_edit_returns_to_input(session_factory):
    owner, msg, state = await _start(session_factory)
    await b_h.cb_bcast_rcp(
        make_callback("bcast:rcp:managers", msg), state, db_user=owner
    )
    await b_h.cb_bcast_rcp(
        make_callback("bcast:rcp:next", msg), state, db_user=owner
    )
    await b_h.on_bcast_text(FakeMessage(text="Текст"), state, db_user=owner)

    cb = make_callback("bcast:edit", msg)
    await b_h.cb_bcast_edit(cb, state, db_user=owner)
    assert await state.get_state() == BroadcastStates.message_input.state
    assert msg.answers[-1][0] == TEXT_SEND_HINT


async def test_bcast_cancel_clears(session_factory):
    owner, msg, state = await _start(session_factory)
    cb = make_callback("bcast:cancel", msg)
    await b_h.cb_bcast_cancel(cb, state, db_user=owner)
    assert msg.answers[-1][0] == TEXT_CANCELED
    assert await state.get_state() is None


async def test_menu_cancels_broadcast_wizard(session_factory):
    owner, msg, state = await _start(session_factory)
    await cmd_h.cmd_menu(msg, db_user=owner, state=state)
    # answers[0] — «Получатели:» от старта визарда, answers[1] — отмена
    assert msg.answers[1][0] == cmd_h.WIZARD_CANCELED
    assert await state.get_state() is None
