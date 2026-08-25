"""Ученики и доступ (Заход 3): сервисы students.py и хендлеры manager.py.

Сервисные тесты — на SQLite in-memory через session_factory; хендлеры
вызываются напрямую с фейковыми объектами (как в test_owner.py), путь
которых — `db_user` (manager/owner).
"""
from datetime import date, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.handlers import manager as mgr_h
from app.models import Student, StudentSubject, Subject, User
from app.services import students as students_svc
from app.utils.dates import today_minsk

TODAY = today_minsk()
DATE_2027 = date(2027, 5, 31)


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


def make_callback(data: str, message: FakeMessage):
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


class FakeBot:
    def __init__(self, username="test_bot"):
        self._username = username

    async def get_me(self):
        return type("Me", (), {"username": self._username})()


_MGR_SEQ = {"n": 0}


def manager(user_id=None) -> SimpleNamespace:
    return SimpleNamespace(role="manager", id=user_id, tg_id=user_id)


async def _mk_manager(session_factory) -> User:
    """Реальная запись менеджера в БД (invited_by ссылается на users.id).

    tg_id/username уникальные на каждый вызов: в одном тесте менеджеров
    может быть несколько, а users.tg_id и User-записи — без дублей.
    """
    _MGR_SEQ["n"] += 1
    async with session_factory() as session:
        user = User(
            tg_id=100000001 + _MGR_SEQ["n"],
            tg_username=f"mgr_test{_MGR_SEQ['n']}",
            role="manager",
            is_active=True,
        )
        session.add(user)
        await session.commit()
        return user


async def _actual_manager(session_factory):
    """(state, db_user) с реальной записью менеджера — для визардов."""
    mgr = await _mk_manager(session_factory)
    state = await make_fsm()
    return state, manager(mgr.id)


async def _mk_user_manual(session_factory, role: str, tg_id: int) -> User:
    """Прямая запись пользователя нужной роли (для теневых профилей staff)."""
    async with session_factory() as session:
        user = User(
            tg_id=tg_id,
            tg_username=f"staff_{role}_{tg_id}",
            tg_full_name=f"бот-{role}",
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


async def _mk_student(session_factory, name="Иван Иванов", *, subject_id=None,
                      until=None, link=False, active=True):
    """Создаёт ученика через сервис (стандартный путь). Возвращает (user, student)."""
    async with session_factory() as session:
        mgr = await _mk_manager(session_factory)
        if subject_id is None:
            subject = Subject(name="Математика", is_active=True)
            session.add(subject)
            await session.commit()
            subject_id = subject.id
        until = until or TODAY + timedelta(days=30)
        user, student, _ = await students_svc.create_student_record(
            session, "Иван Иванов", {subject_id}, mgr.id, until
        )
        if link:
            user.tg_id = 123456
            user.tg_username = "pupil_test"
        if not active:
            user.is_active = False
        await session.commit()
        return user, student


# ---------------------------------------------------------------------------
# Сервисы: создание ученика
# ---------------------------------------------------------------------------
async def test_create_student_record(session_factory):
    subj1 = await _mk_subject(session_factory, "Математика")
    subj2 = await _mk_subject(session_factory, "Физика")
    mgr = await _mk_manager(session_factory)
    async with session_factory() as session:
        user, student, code = await students_svc.create_student_record(
            session, "Иван Петров", {subj1.id, subj2.id}, mgr.id, DATE_2027
        )
        assert user.role == "student"
        assert user.tg_id is None
        assert user.tg_full_name == "Иван Петров"
        assert user.is_active is True
        assert student.invite_status == "pending"
        assert student.invited_by == mgr.id
        assert student.access_until == DATE_2027
        assert len(code) == 6
        links = (await session.scalars(select(StudentSubject))).all()
        assert {l.subject_id for l in links} == {subj1.id, subj2.id}


async def test_create_student_codes_unique_and_committed(session_factory):
    subject = await _mk_subject(session_factory)
    mgr = await _mk_manager(session_factory)
    async with session_factory() as session:
        _, student1, code1 = await students_svc.create_student_record(
            session, "Первый", {subject.id}, mgr.id, DATE_2027
        )
        _, student2, code2 = await students_svc.create_student_record(
            session, "Второй", {subject.id}, mgr.id, DATE_2027
        )
    assert code1 != code2
    async with session_factory() as session:
        rows = (await session.scalars(select(Student))).all()
        assert {r.invite_code for r in rows} == {code1, code2}


async def test_create_student_code_unique_in_same_tx(session_factory, monkeypatch):
    """Код проверяется по БД до commit (ошибка №10): занятый код не пройдёт."""
    mgr = await _mk_manager(session_factory)
    uid = await _mk_manager(session_factory)
    async with session_factory() as session:
        session.add(Student(user_id=uid.id, invite_code="TAKEN1", invite_status="pending"))
        await session.commit()
        monkeypatch.setattr("app.services.invite.generate_code", lambda: "TAKEN1")
    subject = await _mk_subject(session_factory)
    async with session_factory() as session:
        with pytest.raises(RuntimeError):
            await students_svc.create_student_record(
                session, "Иван", {subject.id}, mgr.id, DATE_2027
            )


# ---------------------------------------------------------------------------
# Сервисы: список и карточка
# ---------------------------------------------------------------------------
async def test_list_students_sorting(session_factory):
    subject = await _mk_subject(session_factory)
    mgr = await _mk_manager(session_factory)
    today = TODAY
    async with session_factory() as session:
        user, _, _ = await students_svc.create_student_record(
            session, "Пётр", {subject.id}, mgr.id, today - timedelta(days=3)
        )
        user.tg_id = 100
        await students_svc.create_student_record(
            session, "Анна", {subject.id}, mgr.id, today + timedelta(days=30)
        )  # не привязан
        user2, _, _ = await students_svc.create_student_record(
            session, "Борис", {subject.id}, mgr.id, today + timedelta(days=30)
        )
        user2.tg_id = 200
        await session.commit()
    async with session_factory() as session:
        rows = await students_svc.list_students(session)
    # не привязанная Анна → истёкший привязанный Пётр → активный Борис
    assert [r["name"] for r in rows] == ["Анна", "Пётр", "Борис"]
    assert rows[0]["linked"] is False
    assert rows[1]["linked"] is True and rows[1]["expired"] is True
    assert rows[2]["linked"] is True and rows[2]["expired"] is False


async def test_list_students_fields(session_factory):
    mgr = await _mk_manager(session_factory)
    async with session_factory() as session:
        user, student, _ = await students_svc.create_student_record(
            session, "Иван", set(), mgr.id, DATE_2027
        )
        user.streak_current = 5
        await session.commit()
        sid = student.id
    async with session_factory() as session:
        rows = await students_svc.list_students(session)
    assert rows[0]["id"] == sid
    assert rows[0]["streak"] == 5
    assert rows[0]["subject_names"] == []
    assert rows[0]["access_until"] == DATE_2027
    assert rows[0]["linked"] is False


async def test_get_student_card(session_factory):
    subject = await _mk_subject(session_factory)
    mgr = await _mk_manager(session_factory)
    async with session_factory() as session:
        _, student, _ = await students_svc.create_student_record(
            session, "Иван", {subject.id}, mgr.id, DATE_2027
        )
        sid = student.id
    async with session_factory() as session:
        card = await students_svc.get_student_card(session, sid)
    assert card is not None
    assert card["user"].tg_full_name == "Иван"
    assert len(card["subjects"]) == 1
    assert card["subjects"][0][1].is_active is True
    async with session_factory() as session:
        assert await students_svc.get_student_card(session, 99999) is None


# ---------------------------------------------------------------------------
# Сервисы: действия над учеником
# ---------------------------------------------------------------------------
async def test_extend_access(session_factory):
    mgr = await _mk_manager(session_factory)
    async with session_factory() as session:
        _, student, _ = await students_svc.create_student_record(
            session, "Иван", set(), mgr.id, DATE_2027
        )
        sid = student.id
    new_date = date(2028, 9, 1)
    async with session_factory() as session:
        assert await students_svc.extend_access(session, sid, new_date) is True
    async with session_factory() as session:
        assert (await session.get(Student, sid)).access_until == new_date
    async with session_factory() as session:
        assert await students_svc.extend_access(session, 99999, new_date) is False


async def test_toggle_subject_active(session_factory):
    subject = await _mk_subject(session_factory)
    mgr = await _mk_manager(session_factory)
    async with session_factory() as session:
        _, student, _ = await students_svc.create_student_record(
            session, "Иван", {subject.id}, mgr.id, DATE_2027
        )
        sid = student.id
    async with session_factory() as session:
        assert await students_svc.toggle_subject_active(session, sid, subject.id) is False
        assert await students_svc.toggle_subject_active(session, sid, subject.id) is True
        assert await students_svc.toggle_subject_active(session, sid, 77777) is None


async def test_regenerate_invite_code(session_factory):
    mgr = await _mk_manager(session_factory)
    async with session_factory() as session:
        user, student, code = await students_svc.create_student_record(
            session, "Иван", set(), mgr.id, DATE_2027
        )
        sid, uid = student.id, user.id
    async with session_factory() as session:
        new_code = await students_svc.regenerate_invite_code(session, sid)
    assert new_code is not None and new_code != code
    async with session_factory() as session:
        row = await session.get(Student, sid)
        assert row.invite_code == new_code
        assert row.invite_status == "pending"
    # привязанный — отказ
    async with session_factory() as session:
        user = await session.get(User, uid)
        user.tg_id = 123
        await session.commit()
    async with session_factory() as session:
        assert await students_svc.regenerate_invite_code(session, sid) is None
    async with session_factory() as session:
        assert await students_svc.regenerate_invite_code(session, 99999) is None


async def test_set_student_active(session_factory):
    mgr = await _mk_manager(session_factory)
    async with session_factory() as session:
        user, student, _ = await students_svc.create_student_record(
            session, "Иван", set(), mgr.id, DATE_2027
        )
        sid, uid = student.id, user.id
    async with session_factory() as session:
        assert await students_svc.set_student_active(session, sid, False) is True
    async with session_factory() as session:
        assert (await session.get(User, uid)).is_active is False
    async with session_factory() as session:
        assert await students_svc.set_student_active(session, 99999, False) is False


# ---------------------------------------------------------------------------
# Сервисы: истекающие (границы групп)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "days_left,expected",
    [
        (-10, "expired"),
        (-1, "expired"),
        (0, "0"),
        (1, "1"),
        (2, "3"),
        (3, "3"),
        (4, "7"),
        (7, "7"),
        (8, None),
        (30, None),
    ],
)
async def test_list_expiring_groups(session_factory, days_left, expected):
    """Границы: 4–7 → «7», 2–3 → «3», 1 день → «1», сегодня → «0», истёк → «expired»."""
    await _mk_student(session_factory, until=TODAY + timedelta(days=days_left))
    async with session_factory() as session:
        groups = await students_svc.list_expiring(session)
    if expected is None:
        assert sum(len(v) for v in groups.values()) == 0
        return
    assert len(groups[expected]) == 1
    assert sum(len(v) for k, v in groups.items() if k != expected) == 0


async def test_list_expiring_overdue_days(session_factory):
    await _mk_student(session_factory, until=TODAY - timedelta(days=4))
    async with session_factory() as session:
        groups = await students_svc.list_expiring(session)
    row = groups["expired"][0]
    assert row["overdue_days"] == 4
    assert row["name"] == "Иван Иванов"


# ---------------------------------------------------------------------------
# Хендлеры: список и карточка
# ---------------------------------------------------------------------------
async def test_cmd_students_empty(session_factory):
    msg = FakeMessage()
    await mgr_h.cmd_students(msg, db_user=manager())
    text, kb = msg.answers[-1]
    assert text == mgr_h.TEXT_STUDENTS_EMPTY
    cbs = [c for _, c in cb_buttons(kb)]
    assert "menu:manager:add_student:0" in cbs


async def test_cb_show_students_list(session_factory):
    subject = await _mk_subject(session_factory)
    mgr = await _mk_manager(session_factory)
    async with session_factory() as session:
        user, student, _ = await students_svc.create_student_record(
            session, "Иван Иванов", {subject.id}, mgr.id, DATE_2027
        )
        link = await session.get(StudentSubject, (student.id, subject.id))
        link.streak_current = 3
        link.streak_best = 7
        await session.commit()
        sid = student.id
    msg = FakeMessage()
    cb = make_callback("menu:manager:students:0", msg)
    await mgr_h.cb_show_students(cb, db_user=manager())
    text, kb = msg.edits[-1]
    assert "👨🎓 Ученики (1):" in text
    # стрик — ПО ПРЕДМЕТАМ: «3» в списке (владелец, 13.08)
    assert "1. Иван Иванов — 🔥3 — Математика — до 31.05.2027 — код не активирован ⏳" in text
    assert f"mgr:student:{sid}:0" in [c for _, c in cb_buttons(kb)]


async def test_cb_student_card_linked(session_factory):
    mgr = await _mk_manager(session_factory)
    async with session_factory() as session:
        user, student, _ = await students_svc.create_student_record(
            session, "Иван Иванов", set(), mgr.id, TODAY + timedelta(days=10)
        )
        user.tg_id = 424242
        user.tg_username = "ivan_test"
        student.invite_status = "activated"
        await session.commit()
        sid = student.id

    msg = FakeMessage()
    cb = make_callback(f"mgr:student:{sid}:0", msg)
    await mgr_h.cb_student_card(cb, db_user=manager())
    text, kb = msg.edits[-1]
    assert "👨🎓 Иван Иванов" in text
    assert "📱 @ivan_test | привязан ✓" in text
    assert "🔗 привязан ✓" in text
    # предметов нет → стриков по предметам нет (владелец, 13.08)
    assert "🔥 Стрики: 0" in text
    cbs = [c for _, c in cb_buttons(kb)]
    assert f"mgr:extend:{sid}:0" in cbs
    assert f"mgr:deactivate:{sid}:0" in cbs
    assert not any(c.startswith("mgr:newcode:") for c in cbs)  # привязан — без нового кода


async def test_cb_student_card_unlinked_and_deactivated(session_factory):
    subject = await _mk_subject(session_factory)
    mgr = await _mk_manager(session_factory)
    async with session_factory() as session:
        user, student, _ = await students_svc.create_student_record(
            session, "Иван", {subject.id}, mgr.id, TODAY + timedelta(days=10)
        )
        user.is_active = False
        await session.commit()
        sid = student.id
    msg = FakeMessage()
    cb = make_callback(f"mgr:student:{sid}:0", msg)
    await mgr_h.cb_student_card(cb, db_user=manager())
    text, kb = msg.edits[-1]
    assert "📱 не привязан ⏳" in text
    assert "⛔ Ученик деактивирован" in text
    assert "✅ Математика (активен)" in text
    cbs = [c for _, c in cb_buttons(kb)]
    assert f"mgr:newcode:{sid}:0" in cbs
    assert f"mgr:activate:{sid}:0" in cbs
    assert f"mgr:deactivate:{sid}:0" not in cbs


async def test_cb_student_card_stale(session_factory):
    msg = FakeMessage()
    cb = make_callback("mgr:student:99999:0", msg)
    await mgr_h.cb_student_card(cb, db_user=manager())
    assert any("Ученик не найден" in text for text, _ in cb.answers)


async def test_cb_subject_toggle_redraws_card(session_factory):
    subject = await _mk_subject(session_factory)
    mgr = await _mk_manager(session_factory)
    async with session_factory() as session:
        _, student, _ = await students_svc.create_student_record(
            session, "Иван", {subject.id}, mgr.id, TODAY + timedelta(days=10)
        )
        sid = student.id
    msg = FakeMessage()
    cb = make_callback(f"mgr:subj:{sid}:{subject.id}:0", msg)
    await mgr_h.cb_student_subject_toggle(cb, db_user=manager())
    text, kb = msg.edits[-1]
    assert "🚫 Математика (закрыт вручную)" in text
    # повторный клик — снова открыт
    await mgr_h.cb_student_subject_toggle(make_callback(f"mgr:subj:{sid}:{subject.id}:0", msg), db_user=manager())
    assert "✅ Математика (активен)" in msg.edits[-1][0]


async def test_cb_subject_toggle_stale_link(session_factory):
    msg = FakeMessage()
    cb = make_callback("mgr:subj:99999:88888:0", msg)
    await mgr_h.cb_student_subject_toggle(cb, db_user=manager())
    assert any("Предмет больше не привязан" in text for text, _ in cb.answers)


# ---------------------------------------------------------------------------
# Хендлеры: визард «Добавить ученика»
# ---------------------------------------------------------------------------
async def _start_wizard(session_factory):
    """Возвращает (msg, state, db_user) на шаге name."""
    mgr = await _mk_manager(session_factory)
    msg = FakeMessage()
    state = await make_fsm()
    await mgr_h.cb_add_student(
        make_callback("menu:manager:add_student:0", msg), state,
        db_user=manager(mgr.id),
    )
    return msg, state, manager(mgr.id)


async def test_add_student_wizard_full_flow(session_factory):
    subj1 = await _mk_subject(session_factory, "Математика")
    subj2 = await _mk_subject(session_factory, "Физика")
    state, db_user = await _actual_manager(session_factory)

    # шаг 1: имя
    msg2 = FakeMessage(text="Иван Иванов")
    await mgr_h.on_student_name(msg2, state, db_user=db_user)
    assert "Выбери предметы" in msg2.answers[-1][0]

    # шаг 2: toggle предмета + готово
    click = FakeMessage()
    await mgr_h.cb_student_subjects(make_callback(f"mgr:as:{subj1.id}:t:0", click), state, db_user=db_user)
    click2 = FakeMessage()
    await mgr_h.cb_student_subjects(make_callback("mgr:as:done:0", click2), state, db_user=db_user)
    assert "До какого числа" in click2.answers[-1][0]

    # шаг 3: дата → создание + сообщение с кодом и ссылкой
    msg3 = FakeMessage(text="31.05.2027")
    await mgr_h.on_student_date(msg3, state, db_user=db_user, bot=FakeBot("levelup_bot"))
    text, kb = msg3.answers[-1]
    assert "✅ Ученик создан!" in text
    assert "👨🎓 Иван Иванов" in text
    assert "📅 Доступ: до 31.05.2027" in text
    assert "🔗 Код: <code>" in text
    assert "https://t.me/levelup_bot?start=" in text
    assert ("👨🎓 К списку учеников", "mgr:students:0") in cb_buttons(kb)

    async with session_factory() as session:
        user = await session.scalar(select(User).where(User.tg_full_name == "Иван Иванов"))
        assert user.role == "student"
        assert user.tg_id is None
        student = await session.scalar(select(Student).where(Student.user_id == user.id))
        assert student.invite_status == "pending"
        links = (await session.scalars(select(StudentSubject))).all()
        assert {l.subject_id for l in links} == {subj1.id}


async def test_add_student_wizard_bad_dates(session_factory):
    await _mk_subject(session_factory)
    state, db_user = await _actual_manager(session_factory)
    await mgr_h.on_student_name(FakeMessage(text="Иван"), state, db_user=db_user)

    # формат
    msg_bad = FakeMessage(text="не дата")
    await mgr_h.on_student_date(msg_bad, state, db_user=db_user, bot=FakeBot())
    assert not any("✅ Ученик создан!" in t for t, _ in msg_bad.answers)
    # прошлая дата
    past = (TODAY - timedelta(days=1)).strftime("%d.%m.%Y")
    msg_past = FakeMessage(text=past)
    await mgr_h.on_student_date(msg_past, state, db_user=db_user, bot=FakeBot())
    assert any(mgr_h.MSG_PAST_DATE in t for t, _ in msg_past.answers)
    # сегодня тоже нельзя
    msg_today = FakeMessage(text=TODAY.strftime("%d.%m.%Y"))
    await mgr_h.on_student_date(msg_today, state, db_user=db_user, bot=FakeBot())
    assert any(mgr_h.MSG_PAST_DATE in t for t, _ in msg_today.answers)


async def test_add_student_wizard_bad_date_format_message(session_factory):
    """Промт: ошибка формата — «Не понял дату, пришли ещё раз, например 31.05.2027»."""
    await _mk_subject(session_factory)
    state, db_user = await _actual_manager(session_factory)
    await mgr_h.on_student_name(FakeMessage(text="Иван"), state, db_user=db_user)
    msg = FakeMessage(text="31/05/2027 — не то")
    await mgr_h.on_student_date(msg, state, db_user=db_user, bot=FakeBot())
    assert any(mgr_h.MSG_BAD_DATE in t for t, _ in msg.answers)


async def test_add_student_wizard_subjects_empty_alert(session_factory):
    await _mk_subject(session_factory, "Математика")
    state, db_user = await _actual_manager(session_factory)
    await mgr_h.on_student_name(FakeMessage(text="Иван"), state, db_user=db_user)
    click = FakeMessage()
    cb = make_callback("mgr:as:done:0", click)
    await mgr_h.cb_student_subjects(cb, state, db_user=db_user)
    assert any("Выбери хотя бы один предмет!" in text for text, _ in cb.answers)


async def test_add_student_wizard_no_subjects(session_factory):
    state, db_user = await _actual_manager(session_factory)
    msg2 = FakeMessage(text="Иван")
    await mgr_h.on_student_name(msg2, state, db_user=db_user)
    assert any("Сначала владелец должен создать предметы" in t for t, _ in msg2.answers)


async def test_add_student_wizard_too_long_name(session_factory):
    await _mk_subject(session_factory)
    state, db_user = await _actual_manager(session_factory)
    msg2 = FakeMessage(text="И" * 101)
    await mgr_h.on_student_name(msg2, state, db_user=db_user)
    assert any("Слишком длинное имя" in t for t, _ in msg2.answers)


# ---------------------------------------------------------------------------
# Хендлеры: продление, новый код, деактивация
# ---------------------------------------------------------------------------
async def test_extend_flow(session_factory):
    mgr = await _mk_manager(session_factory)
    async with session_factory() as session:
        _, student, _ = await students_svc.create_student_record(
            session, "Иван", set(), mgr.id, DATE_2027
        )
        sid = student.id

    msg = FakeMessage()
    state = await make_fsm()
    cb = make_callback(f"mgr:extend:{sid}:0", msg)
    await mgr_h.cb_extend_start(cb, state, db_user=manager(mgr.id))
    assert "До какого числа продлить" in msg.answers[-1][0]

    msg2 = FakeMessage(text="15.06.2027")
    await mgr_h.on_extend_date(msg2, state, db_user=manager(mgr.id))
    assert any("✅ Готово: доступ до 15.06.2027." in t for t, _ in msg2.answers)
    assert any("👨🎓 Иван" in t for t, _ in msg2.answers)  # карточка отправлена
    async with session_factory() as session:
        assert (await session.get(Student, sid)).access_until == date(2027, 6, 15)


async def test_extend_stale(session_factory):
    msg = FakeMessage()
    cb = make_callback("mgr:extend:99999:0", msg)
    await mgr_h.cb_extend_start(cb, await make_fsm(), db_user=manager())
    assert any("Ученик не найден" in text for text, _ in cb.answers)


async def test_new_code_flow(session_factory):
    mgr = await _mk_manager(session_factory)
    async with session_factory() as session:
        _, student, old_code = await students_svc.create_student_record(
            session, "Иван", set(), mgr.id, DATE_2027
        )
        sid = student.id
    msg = FakeMessage()
    cb = make_callback(f"mgr:newcode:{sid}:0", msg)
    await mgr_h.cb_new_code(cb, bot=FakeBot("levelup_bot"), db_user=manager(mgr.id))
    text = msg.answers[0][0]
    assert "🔁 Новый код приглашения:" in text
    assert "Старый код больше не действует." in text
    assert "https://t.me/levelup_bot?start=" in text
    async with session_factory() as session:
        row = await session.get(Student, sid)
        assert row.invite_code != old_code
        assert row.invite_status == "pending"


async def test_new_code_linked_forbidden(session_factory):
    mgr = await _mk_manager(session_factory)
    async with session_factory() as session:
        user, student, _ = await students_svc.create_student_record(
            session, "Иван", set(), mgr.id, DATE_2027
        )
        user.tg_id = 555
        await session.commit()
        sid = student.id
    msg = FakeMessage()
    cb = make_callback(f"mgr:newcode:{sid}:0", msg)
    await mgr_h.cb_new_code(cb, bot=FakeBot(), db_user=manager(mgr.id))
    assert any("только не привязанному" in text for text, _ in cb.answers)


async def test_deactivate_flow(session_factory):
    mgr = await _mk_manager(session_factory)
    async with session_factory() as session:
        user, student, _ = await students_svc.create_student_record(
            session, "Иван Иванов", set(), mgr.id, DATE_2027
        )
        sid, uid = student.id, user.id

    msg = FakeMessage()
    cb = make_callback(f"mgr:deactivate:{sid}:0", msg)
    await mgr_h.cb_deactivate_ask(cb, db_user=manager(mgr.id))
    text, kb = msg.edits[-1]
    assert "Деактивировать Иван Иванов?" in text
    assert ("🔨 Деактивировать", f"mgr:deact:yes:{sid}:0") in cb_buttons(kb)

    # подтверждение → деактивация + перерисовка карточки
    msg2 = FakeMessage()
    cb2 = make_callback(f"mgr:deact:yes:{sid}:0", msg2)
    await mgr_h.cb_deact_yes(cb2, db_user=manager(mgr.id))
    assert "⛔ Ученик деактивирован" in msg2.edits[-1][0]
    async with session_factory() as session:
        assert (await session.get(User, uid)).is_active is False


async def test_deactivate_cancel(session_factory):
    mgr = await _mk_manager(session_factory)
    async with session_factory() as session:
        user, student, _ = await students_svc.create_student_record(
            session, "Иван", set(), mgr.id, DATE_2027
        )
        sid, uid = student.id, user.id
    msg = FakeMessage()
    cb = make_callback(f"mgr:deact:no:{sid}:0", msg)
    await mgr_h.cb_deact_no(cb, db_user=manager(mgr.id))
    assert msg.edits, "отмена должна перерисовать карточку"
    async with session_factory() as session:
        assert (await session.get(User, uid)).is_active is True


async def test_deactivate_stale(session_factory):
    msg = FakeMessage()
    cb = make_callback("mgr:deact:yes:99999:0", msg)
    await mgr_h.cb_deact_yes(cb, db_user=manager())
    assert any("Ученик не найден" in text for text, _ in cb.answers)


async def test_activate_flow(session_factory):
    mgr = await _mk_manager(session_factory)
    async with session_factory() as session:
        user, student, _ = await students_svc.create_student_record(
            session, "Иван", set(), mgr.id, DATE_2027
        )
        user.is_active = False
        await session.commit()
        sid, uid = student.id, user.id
    msg = FakeMessage()
    cb = make_callback(f"mgr:activate:{sid}:0", msg)
    await mgr_h.cb_activate(cb, db_user=manager(mgr.id))
    async with session_factory() as session:
        assert (await session.get(User, uid)).is_active is True
    assert "⛔" not in msg.edits[-1][0]


# ---------------------------------------------------------------------------
# Хендлеры: истекающие
# ---------------------------------------------------------------------------
async def test_cmd_expiring_groups(session_factory):
    await _mk_student(session_factory, until=TODAY - timedelta(days=2))
    await _mk_student(session_factory, until=TODAY)
    await _mk_student(session_factory, until=TODAY + timedelta(days=1))
    await _mk_student(session_factory, until=TODAY + timedelta(days=3))
    await _mk_student(session_factory, until=TODAY + timedelta(days=7))
    await _mk_student(session_factory, until=TODAY + timedelta(days=20))

    msg = FakeMessage()
    await mgr_h.cmd_expiring(msg, db_user=manager())
    text, kb = msg.answers[-1]
    assert "🔴 Истёкшие (1):" in text
    assert "• Иван Иванов — до" in text and "(просрочка 2 дн.)" in text
    assert "🟠 Сегодня (последний день) (1):" in text
    assert "🟡 Завтра (1):" in text
    assert "🟢 Через 3 дня (1):" in text
    assert "🔵 Через 7 дней (1):" in text
    assert text.count("Иван Иванов") == 5  # пять строк
    # все строки — кнопки на карточки (плюс назад)
    for _, c in cb_buttons(kb):
        assert c.startswith("mgr:student:") or c == "menu:back:manager:0"


async def test_cmd_expiring_empty(session_factory):
    await _mk_student(session_factory, until=TODAY + timedelta(days=30))
    msg = FakeMessage()
    await mgr_h.cmd_expiring(msg, db_user=manager())
    text, kb = msg.answers[-1]
    assert text == mgr_h.TEXT_EXPIRING_EMPTY


async def test_expiring_extend_flow(session_factory):
    """Заход 7: /expiring → карточка ученика → «📅 Продлить доступ»
    → ввод даты → карточка перерисована, навигация целая."""
    subject = await _mk_subject(session_factory)
    mgr = await _mk_manager(session_factory)
    async with session_factory() as session:
        user, student, _ = await students_svc.create_student_record(
            session, "Иван Иванов", {subject.id}, mgr.id,
            TODAY + timedelta(days=2),  # «🟡 Завтра» — группа истекающих
        )
        sid = student.id
    msg_owner = FakeMessage()
    # 1. Истекающие — строка с кнопкой на карточку
    await mgr_h.cmd_expiring(msg_owner, db_user=manager(mgr.id))
    text, kb = msg_owner.answers[-1]
    assert f"mgr:student:{sid}:0" in {d for _, d in cb_buttons(kb)}
    assert any("Иван Иванов" in label for label, _ in cb_buttons(kb))
    # 2. Карточка из списка истекающих (перерисовка сообщения — edits)
    msg_card = FakeMessage()
    cb_card = make_callback(f"mgr:student:{sid}:0", msg_card)
    await mgr_h.cb_student_card(cb_card, db_user=manager(mgr.id))
    assert any("👨🎓 Иван Иванов" in t for t, _ in msg_card.edits)
    card_kb = msg_card.edits[-1][1]
    assert ("📅 Продлить доступ", f"mgr:extend:{sid}:0") in cb_buttons(card_kb)
    # 3. «Продлить доступ» → ввод даты
    state = await make_fsm()
    msg_ext = FakeMessage()
    cb_ext = make_callback(f"mgr:extend:{sid}:0", msg_ext)
    await mgr_h.cb_extend_start(cb_ext, state, db_user=manager(mgr.id))
    assert "До какого числа продлить" in msg_ext.answers[-1][0]
    msg_date = FakeMessage(text="15.06.2027")
    await mgr_h.on_extend_date(msg_date, state, db_user=manager(mgr.id))
    # 4. Подтверждение + перерисованная карточка с новой датой
    assert any("✅ Готово: доступ до 15.06.2027." in t for t, _ in msg_date.answers)
    assert any("⏳ Доступ: до 15.06.2027" in t for t, _ in msg_date.answers)
    assert await state.get_state() is None  # визард закрыт
    async with session_factory() as session:
        row = await session.get(Student, sid)
    assert row.access_until == date(2027, 6, 15)


# ---------------------------------------------------------------------------
# Самопроверка (баги, найденные ревью Захода 3)
# ---------------------------------------------------------------------------
async def test_deact_no_stale_single_answer(session_factory):
    """«Отмена» на исчезнувшей карточке: один ответ-алерт, без двойного
    callback.answer() (второй answer на один callback — 400 от Telegram)."""
    msg = FakeMessage()
    cb = make_callback("mgr:deact:no:99999:0", msg)
    await mgr_h.cb_deact_no(cb, db_user=manager())
    assert len(cb.answers) == 1
    assert cb.answers[0] == (mgr_h.MSG_STUDENT_NOT_FOUND, True)
    assert not msg.edits


async def test_deact_yes_stale_single_answer(session_factory):
    msg = FakeMessage()
    cb = make_callback("mgr:deact:yes:99999:0", msg)
    await mgr_h.cb_deact_yes(cb, db_user=manager())
    assert len(cb.answers) == 1
    assert cb.answers[0] == (mgr_h.MSG_STUDENT_NOT_FOUND, True)


async def test_activate_stale_single_answer(session_factory):
    msg = FakeMessage()
    cb = make_callback("mgr:activate:99999:0", msg)
    await mgr_h.cb_activate(cb, db_user=manager())
    assert len(cb.answers) == 1
    assert cb.answers[0] == (mgr_h.MSG_STUDENT_NOT_FOUND, True)


async def test_students_back_role_aware(session_factory):
    """«← Назад» зависит от роли: менеджер — в своё меню, владелец — в своё."""
    await _mk_student(session_factory)
    msg = FakeMessage()
    await mgr_h.cmd_students(msg, db_user=manager())
    _, kb = msg.answers[-1]
    assert ("← Назад", "menu:back:manager:0") in cb_buttons(kb)

    msg2 = FakeMessage()
    await mgr_h.cmd_students(msg2, db_user=SimpleNamespace(role="owner"))
    _, kb2 = msg2.answers[-1]
    assert ("← Назад", "menu:back:owner:0") in cb_buttons(kb2)
    assert ("← Назад", "menu:back:manager:0") not in cb_buttons(kb2)


async def test_expiring_back_role_aware(session_factory):
    await _mk_student(session_factory, until=TODAY + timedelta(days=5))
    msg = FakeMessage()
    await mgr_h.cmd_expiring(msg, db_user=SimpleNamespace(role="owner"))
    _, kb = msg.answers[-1]
    assert ("← Назад", "menu:back:owner:0") in cb_buttons(kb)
    assert ("← Назад", "menu:back:manager:0") not in cb_buttons(kb)


async def test_expiring_escaped_once(session_factory):
    """Имя с «&»: в тексте и в кнопке экранируется ровно один раз
    (&amp;, а не &amp;amp; — двойное esc ломало подписи кнопок)."""
    subject = await _mk_subject(session_factory)
    mgr = await _mk_manager(session_factory)
    async with session_factory() as session:
        await students_svc.create_student_record(
            session, "Иван & Петя", {subject.id}, mgr.id, TODAY + timedelta(days=2)
        )
    msg = FakeMessage()
    await mgr_h.cmd_expiring(msg, db_user=manager())
    text, kb = msg.answers[-1]
    assert "• Иван &amp; Петя — до" in text
    assert "&amp;amp;" not in text
    labels = [label for label, _ in cb_buttons(kb)]
    assert any("Иван &amp; Петя" in label for label in labels)
    assert not any("&amp;amp;" in label for label in labels)

# ---------------------------------------------------------------------------
# Теневые профили staff (босс/препод решают задания) скрыты из списков менеджера
# ---------------------------------------------------------------------------
async def test_staff_shadow_students_hidden_from_lists(session_factory):
    """Теневой Student владельца/препода не попадает в «Ученики» и
    «Истекающие» — менеджер видит только реальных учеников."""
    from app.models import TeacherSubject

    await _mk_student(
        session_factory, "Иван Иванов", link=True, until=TODAY + timedelta(days=7)
    )
    owner = await _mk_user_manual(session_factory, "owner", 555001)
    teacher = await _mk_user_manual(session_factory, "teacher", 555002)
    async with session_factory() as session:
        session.add(TeacherSubject(teacher_id=teacher.id, subject_id=(await _mk_subject(session_factory)).id))
        session.add(Student(user_id=owner.id, access_until=None, invite_code=None))
        session.add(Student(user_id=teacher.id, access_until=None, invite_code=None))
        await session.commit()

    async with session_factory() as session:
        students = await students_svc.list_students(session)
        expiring = await students_svc.list_expiring(session)
    names = [s["name"] for s in students]
    assert "Иван Иванов" in names
    assert all(s["name"] not in ("бот-владелец", "бот-преподаватель") for s in students)
    exp_names = [
        s["name"] for group in expiring.values() for s in group
    ]
    assert "Иван Иванов" in exp_names
    assert all(n not in ("бот-владелец", "бот-преподаватель") for n in exp_names)
