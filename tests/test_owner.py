"""Тесты блока владельца: сервисы людей/предметов и хендлеры.

Хендлеры вызываются напрямую с фейковыми callback/message и FSM-контекстом
(подход как в старых тестах проекта). Сессии — из фикстуры session_factory
(SQLite in-memory), поэтому изменения проверяются запросами в БД.
"""
from types import SimpleNamespace

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from sqlalchemy import select

from app.handlers import owner as owner_h
from app.models import Subject, TeacherSubject, User
from app.services import people as people_svc
from app.states import AddManagerStates, AddSubjectStates, AddTeacherStates
from app.utils.roles import MSG_NO_PERMISSION


# --------------------------------------------------------------------------
# Фейки aiogram-объектов
# --------------------------------------------------------------------------
class FakeMessage:
    """Message: answer пишет в .answers, edit_text — в .edits."""

    def __init__(self, text="", chat_id=42):
        self.text = text
        self.chat = SimpleNamespace(id=chat_id)
        self.answers = []  # [(text, reply_markup)]
        self.edits = []  # [(text, reply_markup)]

    async def answer(self, content="", reply_markup=None, **kwargs):
        self.answers.append((content, reply_markup))

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
    """Плоский список (текст, callback_data) кнопок клавиатуры."""
    if markup is None:
        return []
    return [(b.text, b.callback_data) for row in markup.inline_keyboard for b in row]


async def make_fsm() -> FSMContext:
    storage = MemoryStorage()
    key = StorageKey(chat_id=42, user_id=42, bot_id=42424242, destiny="test")
    return FSMContext(storage=storage, key=key)


async def _make_person(session_factory, *, role: str, tg_id: int) -> User:
    async with session_factory() as session:
        user = User(tg_id=tg_id, tg_username=f"u_{role}", role=role, is_active=True)
        session.add(user)
        await session.commit()
        return user


# --------------------------------------------------------------------------
# Сервисы: преподаватели
# --------------------------------------------------------------------------
async def test_add_teacher_creates_user_and_links(session_factory):
    async with session_factory() as session:
        subj = await people_svc.create_subject(session, "Математика")
        subj2 = await people_svc.create_subject(session, "Физика")
        teacher = await people_svc.add_teacher(session, "ivanov_math", {subj.id, subj2.id})

    assert teacher.role == "teacher"
    assert teacher.is_active is True
    assert teacher.tg_id is None  # привяжется в мидлваре при первом входе

    async with session_factory() as session:
        links = (
            await session.scalars(
                select(TeacherSubject).where(TeacherSubject.teacher_id == teacher.id)
            )
        ).all()
        assert {l.subject_id for l in links} == {subj.id, subj2.id}


async def test_add_teacher_updates_existing_teacher(session_factory):
    """Повторное «добавить преподавателя» = обновление, а не ошибка."""
    async with session_factory() as session:
        await people_svc.add_teacher(session, "ivanov_math", set())
        teacher = await people_svc.add_teacher(session, "ivanov_math", set())
        assert teacher.role == "teacher"
        users = (await session.scalars(select(User))).all()
        assert len(users) == 1  # дублей нет


async def test_add_teacher_case_insensitive(session_factory):
    """@Ivanov и @ivanov — один человек: повторное добавление не создаёт дубль."""
    async with session_factory() as session:
        teacher = await people_svc.add_teacher(session, "Ivanov", set())
        name_in_db = (
            await session.scalar(select(User).where(User.id == teacher.id))
        ).tg_username
        assert name_in_db == "ivanov"  # clean_username нормализует регистр

        await people_svc.add_teacher(session, "IVANOV", set())

        users = (await session.scalars(select(User))).all()
        assert len(users) == 1  # дублей нет


async def test_add_manager_case_insensitive(session_factory):
    async with session_factory() as session:
        await people_svc.add_manager(session, "AnnaMgr")
        await people_svc.add_manager(session, "annamgr")
        users = (await session.scalars(select(User))).all()
        assert len(users) == 1


# --------------------------------------------------------------------------
# Сервисы: добавление по tg_id (без @username)
# --------------------------------------------------------------------------
async def test_add_teacher_by_tg_id_creates_and_links(session_factory):
    async with session_factory() as session:
        subj = await people_svc.create_subject(session, "Математика")
        teacher = await people_svc.add_teacher_by_tg_id(session, 111111111, {subj.id})

    assert teacher.role == "teacher"
    assert teacher.tg_id == 111111111

    async with session_factory() as session:
        links = (
            await session.scalars(
                select(TeacherSubject).where(TeacherSubject.teacher_id == teacher.id)
            )
        ).all()
        assert {l.subject_id for l in links} == {subj.id}


async def test_add_teacher_by_tg_id_adopts_guest(session_factory):
    """Гость с tg_id становится преподавателем (запись переиспользуется)."""
    async with session_factory() as session:
        session.add(User(tg_id=222222222, role="guest", is_active=True))
        await session.commit()

    async with session_factory() as session:
        teacher = await people_svc.add_teacher_by_tg_id(session, 222222222, set())
        users = (await session.scalars(select(User))).all()
        assert len(users) == 1  # дублей нет
        assert teacher.id == users[0].id
        assert teacher.role == "teacher"


async def test_add_teacher_by_tg_id_conflicts_and_swaps(session_factory):
    """Ученик по tg_id — ValueError; активный менеджер — ОБЕ роли (совмещение)."""
    async with session_factory() as session:
        session.add(User(tg_id=333333333, role="student", is_active=True))
        session.add(User(tg_id=444444444, role="manager", is_active=True))
        await session.commit()

    async with session_factory() as session:
        with pytest.raises(ValueError, match="ученик"):
            await people_svc.add_teacher_by_tg_id(session, 333333333, set())
        teacher = await people_svc.add_teacher_by_tg_id(session, 444444444, set())
        assert teacher.role == "manager"
        assert teacher.role2 == "teacher"
        assert teacher.role_set == {"manager", "teacher"}


async def test_add_manager_by_tg_id_adopts_guest(session_factory):
    async with session_factory() as session:
        session.add(User(tg_id=555555555, role="guest", is_active=True))
        await session.commit()

    async with session_factory() as session:
        manager = await people_svc.add_manager_by_tg_id(session, 555555555)
        assert manager.role == "manager"
        assert manager.tg_id == 555555555

    async with session_factory() as session:
        # идемпотентно: повторное добавление того же tg_id — без ошибок
        again = await people_svc.add_manager_by_tg_id(session, 555555555)
        assert again.role == "manager"


async def test_manager_switches_to_teacher(session_factory):
    """Активный менеджер → преподаватель: ОБЕ роли (совмещение).

    «Назначил препода на предмет, потом дал возможность менеджера или
    наоборот — обе роли, пока одну не отняли» (запрос владельца).
    """
    async with session_factory() as session:
        session.add(User(tg_username="anna_mgr", role="manager", is_active=True))
        await session.commit()
        teacher = await people_svc.add_teacher(session, "anna_mgr", set())
        assert teacher.role == "manager"  # первичная роль не перетёрта
        assert teacher.role2 == "teacher"
        assert teacher.role_set == {"manager", "teacher"}
        assert teacher.is_active is True


async def test_teacher_gains_manager_keeps_both(session_factory):
    """Препод, назначенный менеджером: обе роли, связи предметов живы."""
    async with session_factory() as session:
        subject = await people_svc.create_subject(session, "Математика")
        teacher = await people_svc.add_teacher(session, "ivanov_math", {subject.id})
        teacher_id = teacher.id
    async with session_factory() as session:
        manager = await people_svc.add_manager(session, "ivanov_math")
        assert manager.role == "teacher"
        assert manager.role2 == "manager"
        assert manager.role_set == {"teacher", "manager"}
        links = await session.scalars(
            select(TeacherSubject).where(TeacherSubject.teacher_id == teacher_id)
        )
        assert len(list(links)) == 1  # предмет не потерян при совмещении


async def test_add_teacher_conflict_with_active_student(session_factory):
    """Активного ученика нельзя молча переписать в преподавателя (потеря роли)."""
    async with session_factory() as session:
        session.add(User(tg_username="pupil", role="student", is_active=True))
        await session.commit()
        with pytest.raises(ValueError, match="сейчас ученик"):
            await people_svc.add_teacher(session, "pupil", set())


async def test_add_teacher_reactivates_deactivated(session_factory):
    async with session_factory() as session:
        await people_svc.add_teacher(session, "old_t", set())
    async with session_factory() as session:
        old = await session.scalar(select(User).where(User.tg_username == "old_t"))
        old.is_active = False
        await session.commit()
    async with session_factory() as session:
        await people_svc.add_teacher(session, "old_t", set())
        again = await session.scalar(select(User).where(User.tg_username == "old_t"))
        assert again.is_active is True
        assert again.role == "teacher"


# --------------------------------------------------------------------------
# Сервисы: менеджеры, деактивация, предметы
# --------------------------------------------------------------------------
async def test_add_manager(session_factory):
    async with session_factory() as session:
        manager = await people_svc.add_manager(session, "boss_mgr")
        assert manager.role == "manager"
        assert manager.tg_id is None
        # повторное добавление — идемпотентно, без ошибок
        again = await people_svc.add_manager(session, "boss_mgr")
        assert again.role == "manager"


async def test_teacher_switches_to_manager(session_factory):
    """Активный преподаватель → менеджер: ОБЕ роли (совмещение)."""
    async with session_factory() as session:
        await people_svc.add_teacher(session, "ivanov_math", set())
        manager = await people_svc.add_manager(session, "ivanov_math")
        assert manager.role == "teacher"
        assert manager.role2 == "manager"
        assert manager.role_set == {"teacher", "manager"}
        assert manager.is_active is True


async def test_deactivate_person_idempotent(session_factory):
    async with session_factory() as session:
        user = User(tg_username="u1", role="manager")
        session.add(user)
        await session.commit()
        user_id = user.id

    async with session_factory() as session:
        assert await people_svc.deactivate_person(session, user_id) is True
        assert await people_svc.deactivate_person(session, user_id) is False  # повтор
        assert await people_svc.deactivate_person(session, 999999) is False  # нет записи


async def test_list_active_people(session_factory):
    async with session_factory() as session:
        await people_svc.add_teacher(session, "t1", set())
        await people_svc.add_manager(session, "m1")
        await people_svc.add_teacher(session, "t2", set())
    async with session_factory() as session:
        assert len(await people_svc.list_active_people(session, "teacher")) == 2
        assert len(await people_svc.list_active_people(session, "manager")) == 1


async def test_list_active_people_includes_combined(session_factory):
    """Совмещённый (менеджер+препод) виден и в списке преподав, и менеджеров."""
    async with session_factory() as session:
        await people_svc.add_teacher(session, "both_roles", set())
        await people_svc.add_manager(session, "both_roles")
    async with session_factory() as session:
        teachers = await people_svc.list_active_people(session, "teacher")
        managers = await people_svc.list_active_people(session, "manager")
        assert len(teachers) == 1 and teachers[0].tg_username == "both_roles"
        assert len(managers) == 1 and managers[0].tg_username == "both_roles"


async def test_remove_role_strips_keeps_other(session_factory):
    """«Убрать преподавателя» у совмещённого: роль2 снята, менеджер живёт.

    Пока владелец не отнял одну из ролей, человек работает обеими;
    отнял — остаётся вторая (прямое требование владельца).
    """
    async with session_factory() as session:
        uid = (await people_svc.add_teacher(session, "dual", set())).id
        await people_svc.add_manager(session, "dual")
    async with session_factory() as session:
        result = await people_svc.remove_role(session, uid, "teacher")
        assert result == "stripped"
        user = await session.get(User, uid)
        assert user.role == "manager"
        assert user.role2 is None
        assert user.is_active is True  # доступ не закрыт


async def test_remove_role_deactivates_only_role(session_factory):
    """«Убрать менеджера» у человека с ОДНОЙ ролью = деактивация (как раньше)."""
    async with session_factory() as session:
        uid = (await people_svc.add_manager(session, "solo_mgr")).id
    async with session_factory() as session:
        result = await people_svc.remove_role(session, uid, "manager")
        assert result == "deactivated"
        user = await session.get(User, uid)
        assert user.is_active is False


async def test_remove_role_promotes_secondary(session_factory):
    """Убрать ПЕРВИЧНУЮ роль: вторая становится первичной, доступ жив."""
    async with session_factory() as session:
        uid = (await people_svc.add_manager(session, "pri_mgr")).id  # primary manager
        await people_svc.add_teacher(session, "pri_mgr", set())      # role2 teacher
    async with session_factory() as session:
        result = await people_svc.remove_role(session, uid, "manager")
        assert result == "stripped"
        user = await session.get(User, uid)
        assert user.role == "teacher"
        assert user.role2 is None
        assert user.is_active is True


async def test_remove_role_missing_returns_none(session_factory):
    """Роли нет (или человек неактивен) — идемпотентно None."""
    async with session_factory() as session:
        uid = (await people_svc.add_manager(session, "no_role_yet")).id
        assert await people_svc.remove_role(session, uid, "teacher") is None
        assert await people_svc.remove_role(session, 999999, "manager") is None


async def test_create_subject_duplicate_raises(session_factory):
    async with session_factory() as session:
        await people_svc.create_subject(session, "Математика")
        with pytest.raises(ValueError, match="уже существует"):
            await people_svc.create_subject(session, "Математика")
        with pytest.raises(ValueError, match="пустым"):
            await people_svc.create_subject(session, "   ")


async def test_toggle_subject_active(session_factory):
    async with session_factory() as session:
        subject = await people_svc.create_subject(session, "Русский")
        assert (await people_svc.toggle_subject_active(session, subject.id)).is_active is False
        assert (await people_svc.toggle_subject_active(session, subject.id)).is_active is True
        assert await people_svc.toggle_subject_active(session, 777777) is None


# --------------------------------------------------------------------------
# Хендлеры: добавление преподавателя (визард целиком)
# --------------------------------------------------------------------------
async def test_teacher_wizard_full_flow(session_factory):
    async with session_factory() as session:
        await people_svc.create_subject(session, "Математика")
        await people_svc.create_subject(session, "Физика")
    owner = await _make_person(session_factory, role="owner", tg_id=1001)

    msg = FakeMessage()
    state = await make_fsm()
    await owner_h.cb_add_teacher(make_callback("owner:add_teacher:0", msg), state, db_user=owner)
    assert await state.get_state() == AddTeacherStates.username

    # шаг 1: @username
    msg2 = FakeMessage(text="@ivanov_math")
    await owner_h.on_teacher_username(msg2, state, db_user=owner)
    assert await state.get_state() == AddTeacherStates.subjects
    assert any("Выбери предметы" in t for t, _ in msg2.answers)

    # кнопки предметов из последнего ответа
    kb = msg2.answers[-1][1]
    subject_buttons = [c for _, c in cb_buttons(kb) if c != "owner:at:done:0"]
    assert len(subject_buttons) == 2

    # клик по первому предмету — перерисовка мультивыбора
    click_msg = FakeMessage()
    cb_toggle = make_callback(subject_buttons[0], click_msg)
    await owner_h.cb_at_toggle(cb_toggle, state, db_user=owner)
    assert click_msg.edits

    # «Готово»
    cb_done = make_callback("owner:at:done:0", click_msg)
    await owner_h.cb_at_toggle(cb_done, state, db_user=owner)
    assert not click_msg.answers  # финал — edit, а не answer

    async with session_factory() as session:
        teacher = await session.scalar(select(User).where(User.tg_username == "ivanov_math"))
        assert teacher is not None
        assert teacher.role == "teacher"
        assert teacher.tg_id is None
        links = (
            await session.scalars(
                select(TeacherSubject).where(TeacherSubject.teacher_id == teacher.id)
            )
        ).all()
        assert len(links) == 1  # выбран один предмет


async def test_teacher_wizard_done_without_selection_alert(session_factory):
    async with session_factory() as session:
        await people_svc.create_subject(session, "Математика")
    owner = await _make_person(session_factory, role="owner", tg_id=1002)
    state = await make_fsm()

    await owner_h.cb_add_teacher(make_callback("owner:add_teacher:0", FakeMessage()), state, db_user=owner)
    await owner_h.on_teacher_username(FakeMessage(text="@t_alone"), state, db_user=owner)

    msg = FakeMessage()
    cb_done = make_callback("owner:at:done:0", msg)
    await owner_h.cb_at_toggle(cb_done, state, db_user=owner)
    assert cb_done.answers and cb_done.answers[0][1] is True  # алерт
    assert "хотя бы один предмет" in cb_done.answers[0][0]

    async with session_factory() as session:
        assert await session.scalar(select(User).where(User.tg_username == "t_alone")) is None


async def test_teacher_wizard_stale_button(session_factory):
    """Повторный клик «Готово» после завершения визарда — алерт, без дубля."""
    async with session_factory() as session:
        await people_svc.create_subject(session, "Математика")
    owner = await _make_person(session_factory, role="owner", tg_id=1003)
    state = await make_fsm()

    await owner_h.cb_add_teacher(make_callback("owner:add_teacher:0", FakeMessage()), state, db_user=owner)
    msg2 = FakeMessage(text="@ivanov_math")
    await owner_h.on_teacher_username(msg2, state, db_user=owner)
    kb = msg2.answers[-1][1]
    subject_cb = [c for _, c in cb_buttons(kb) if c != "owner:at:done:0"][0]

    msg = FakeMessage()
    await owner_h.cb_at_toggle(make_callback(subject_cb, msg), state, db_user=owner)
    cb_done = make_callback("owner:at:done:0", msg)
    await owner_h.cb_at_toggle(cb_done, state, db_user=owner)  # первый — успех
    # повторный клик по той же кнопке «Готово» (состояние уже очищено)
    msg2b = FakeMessage()
    cb_done2 = make_callback("owner:at:done:0", msg2b)
    await owner_h.cb_at_toggle(cb_done2, state, db_user=owner)

    assert cb_done2.answers and "устарела" in cb_done2.answers[0][0]
    async with session_factory() as session:
        teachers = (
            await session.scalars(select(User).where(User.role == "teacher"))
        ).all()
        assert len(teachers) == 1  # дублей нет


async def test_teacher_username_not_from_at_sign(session_factory):
    owner = await _make_person(session_factory, role="owner", tg_id=1004)
    state = await make_fsm()
    await state.set_state(AddTeacherStates.username)
    msg = FakeMessage(text="ivanov_math")  # без @ и не число — невалидно
    await owner_h.on_teacher_username(msg, state, db_user=owner)
    assert "Не похоже на @username или ID" in msg.answers[0][0]
    assert await state.get_state() == AddTeacherStates.username  # можно повторить


async def test_teacher_wizard_reactivates_deactivated(session_factory):
    """Деактивированного преподавателя можно вернуть через «➕ Добавить»."""
    async with session_factory() as session:
        subj = await people_svc.create_subject(session, "Математика")
        teacher = await people_svc.add_teacher(session, "old_t", set())
        teacher_id = teacher.id
    async with session_factory() as session:
        teacher = await session.get(User, teacher_id)
        teacher.is_active = False
        await session.commit()

    owner = await _make_person(session_factory, role="owner", tg_id=1018)
    state = await make_fsm()
    await owner_h.cb_add_teacher(make_callback("owner:add_teacher:0", FakeMessage()), state, db_user=owner)
    msg2 = FakeMessage(text="@old_t")
    await owner_h.on_teacher_username(msg2, state, db_user=owner)
    assert await state.get_state() == AddTeacherStates.subjects  # не заблокирован

    # завершаем визард: выбрать предмет → «Готово»
    click_msg = FakeMessage()
    await owner_h.cb_at_toggle(
        make_callback(f"owner:at:{subj.id}:t:0", click_msg), state, db_user=owner
    )
    await owner_h.cb_at_toggle(
        make_callback("owner:at:done:0", click_msg), state, db_user=owner
    )

    async with session_factory() as session:
        teacher = await session.get(User, teacher_id)
        assert teacher.role == "teacher"
        assert teacher.is_active is True  # реактивирован
        links = (
            await session.scalars(
                select(TeacherSubject).where(TeacherSubject.teacher_id == teacher_id)
            )
        ).all()
        assert len(links) == 1  # связи перезаписаны


async def test_teacher_wizard_rejects_active_student(session_factory):
    """Активного ученика визард не переписывает в преподавателя."""
    async with session_factory() as session:
        session.add(User(tg_username="pupil", role="student", is_active=True))
        await session.commit()

    owner = await _make_person(session_factory, role="owner", tg_id=1019)
    state = await make_fsm()
    await owner_h.cb_add_teacher(make_callback("owner:add_teacher:0", FakeMessage()), state, db_user=owner)
    msg2 = FakeMessage(text="@pupil")
    await owner_h.on_teacher_username(msg2, state, db_user=owner)
    assert "сейчас ученик" in msg2.answers[0][0]
    assert await state.get_state() is None  # визард закрыт

    async with session_factory() as session:
        pupil = await session.scalar(select(User).where(User.tg_username == "pupil"))
        assert pupil.role == "student"  # роль не тронута


# --------------------------------------------------------------------------
# Хендлеры: менеджер
# --------------------------------------------------------------------------
async def test_manager_wizard(session_factory):
    owner = await _make_person(session_factory, role="owner", tg_id=1005)
    msg = FakeMessage()
    state = await make_fsm()

    await owner_h.cb_add_manager(make_callback("owner:add_manager:0", msg), state, db_user=owner)
    assert await state.get_state() == AddManagerStates.username

    msg2 = FakeMessage(text="@anna_1")
    await owner_h.on_manager_username(msg2, state, db_user=owner)
    assert any("Менеджер" in t for t, _ in msg2.answers)

    async with session_factory() as session:
        manager = await session.scalar(select(User).where(User.tg_username == "anna_1"))
        assert manager.role == "manager"


# --------------------------------------------------------------------------
# Хендлеры: добавление по tg_id (без @username)
# --------------------------------------------------------------------------
async def test_teacher_wizard_by_tg_id(session_factory):
    """Визард препода с числовым tg_id вместо @username."""
    async with session_factory() as session:
        await people_svc.create_subject(session, "Математика")
    owner = await _make_person(session_factory, role="owner", tg_id=1030)

    state = await make_fsm()
    await owner_h.cb_add_teacher(make_callback("owner:add_teacher:0", FakeMessage()), state, db_user=owner)

    msg2 = FakeMessage(text="2000000000")
    await owner_h.on_teacher_username(msg2, state, db_user=owner)
    assert await state.get_state() == AddTeacherStates.subjects

    click_msg = FakeMessage()
    kb = msg2.answers[-1][1]
    subject_cb = [c for _, c in cb_buttons(kb) if c != "owner:at:done:0"][0]
    await owner_h.cb_at_toggle(make_callback(subject_cb, click_msg), state, db_user=owner)
    await owner_h.cb_at_toggle(make_callback("owner:at:done:0", click_msg), state, db_user=owner)

    async with session_factory() as session:
        teacher = await session.scalar(select(User).where(User.tg_id == 2000000000))
        assert teacher is not None
        assert teacher.role == "teacher"
        assert teacher.tg_username is None  # username не нужен


async def test_manager_wizard_by_tg_id(session_factory):
    """Менеджер по tg_id числом — создаётся сразу."""
    owner = await _make_person(session_factory, role="owner", tg_id=1031)
    state = await make_fsm()
    await owner_h.cb_add_manager(make_callback("owner:add_manager:0", FakeMessage()), state, db_user=owner)

    msg2 = FakeMessage(text="3000000000")
    await owner_h.on_manager_username(msg2, state, db_user=owner)
    assert any("Менеджер" in t for t, _ in msg2.answers)

    async with session_factory() as session:
        manager = await session.scalar(select(User).where(User.tg_id == 3000000000))
        assert manager.role == "manager"


async def test_guest_pick_flow_for_teacher(session_factory):
    """Выбор гостя из «заходивших» → визард препода до конца."""
    async with session_factory() as session:
        await people_svc.create_subject(session, "Физика")
        session.add(
            User(tg_id=4000000000, tg_username="nouser", tg_full_name="Без Ника",
                 role="guest", is_active=True)
        )
        await session.commit()
        guest_id = (await session.scalar(select(User).where(User.tg_id == 4000000000))).id

    owner = await _make_person(session_factory, role="owner", tg_id=1032)
    state = await make_fsm()
    await owner_h.cb_add_teacher(make_callback("owner:add_teacher:0", FakeMessage()), state, db_user=owner)

    # список гостей
    msg = FakeMessage()
    await owner_h.cb_guest_pick_list(
        make_callback(f"owner:guestpick:teacher:0", msg), db_user=owner
    )
    assert any(c == f"owner:guestsel:teacher:{guest_id}:0" for _, c in cb_buttons(msg.edits[-1][1]))

    # выбрать гостя
    click_msg = FakeMessage()
    await owner_h.cb_guest_select(
        make_callback(f"owner:guestsel:teacher:{guest_id}:0", click_msg), state, db_user=owner
    )
    assert await state.get_state() == AddTeacherStates.subjects

    # финал
    kb_btns = [c for _, c in cb_buttons(click_msg.answers[-1][1]) if c != "owner:at:done:0"]
    assert kb_btns
    await owner_h.cb_at_toggle(make_callback(kb_btns[0], click_msg), state, db_user=owner)
    await owner_h.cb_at_toggle(make_callback("owner:at:done:0", click_msg), state, db_user=owner)

    async with session_factory() as session:
        teacher = await session.scalar(select(User).where(User.tg_id == 4000000000))
        assert teacher.role == "teacher"
        assert teacher.is_active is True


async def test_guest_pick_flow_for_manager(session_factory):
    """Выбор гостя → менеджер создаётся сразу."""
    async with session_factory() as session:
        session.add(
            User(tg_id=5000000000, tg_username=None, tg_full_name="Нина",
                 role="guest", is_active=True)
        )
        await session.commit()
    guest_id = (await session.scalar(select(User).where(User.tg_id == 5000000000))).id

    owner = await _make_person(session_factory, role="owner", tg_id=1033)
    state = await make_fsm()
    await owner_h.cb_add_manager(make_callback("owner:add_manager:0", FakeMessage()), state, db_user=owner)

    click_msg = FakeMessage()
    await owner_h.cb_guest_select(
        make_callback(f"owner:guestsel:manager:{guest_id}:0", click_msg), state, db_user=owner
    )
    assert any("Менеджер" in t for t, _ in click_msg.edits)

    async with session_factory() as session:
        manager = await session.scalar(select(User).where(User.tg_id == 5000000000))
        assert manager.role == "manager"


async def test_guest_pick_empty_list_alert(session_factory):
    """Нет гостей → алерт, экран не меняется.

    Один ответ-алерт (без обычного answer до него — двойной answer на
    один callback Telegram отклоняет, событие падает в глобальный
    обработчик «Что-то пошло не так»).
    """
    owner = await _make_person(session_factory, role="owner", tg_id=1034)
    msg = FakeMessage()
    cb = make_callback("owner:guestpick:teacher:0", msg)
    await owner_h.cb_guest_pick_list(cb, db_user=owner)
    assert len(cb.answers) == 1
    assert cb.answers[0] == ("Пока никто не заходил в бота без @", True)
    assert not msg.edits


# --------------------------------------------------------------------------
# Хендлеры: убрать преподавателя / менеджера
# --------------------------------------------------------------------------
async def test_remove_teacher_flow(session_factory):
    async with session_factory() as session:
        teacher = await people_svc.add_teacher(session, "ivanov_math", set())
        teacher_id = teacher.id
    owner = await _make_person(session_factory, role="owner", tg_id=1006)
    msg = FakeMessage()

    await owner_h.cb_rt_list(make_callback("owner:rt:list:0", msg), db_user=owner)
    buttons = cb_buttons(msg.edits[-1][1])
    assert ("@ivanov_math", f"owner:rt:pick:{teacher_id}:0") in buttons

    await owner_h.cb_rt_pick(make_callback(f"owner:rt:pick:{teacher_id}:0", msg), db_user=owner)
    assert any(
        c == f"owner:rt:yes:{teacher_id}:0" for _, c in cb_buttons(msg.edits[-1][1])
    )

    await owner_h.cb_rt_yes(make_callback(f"owner:rt:yes:{teacher_id}:0", msg), db_user=owner)
    assert any("убран" in t for t, _ in msg.edits)

    async with session_factory() as session:
        teacher = await session.get(User, teacher_id)
        assert teacher.role == "teacher"
        assert teacher.is_active is False  # «убрать» = деактивация


async def test_remove_teacher_empty_list(session_factory):
    owner = await _make_person(session_factory, role="owner", tg_id=1007)
    msg = FakeMessage()
    await owner_h.cb_rt_list(make_callback("owner:rt:list:0", msg), db_user=owner)
    text, kb = msg.edits[-1]
    assert "Пока никого нет" in text
    assert any(c == "owner:add_teacher:0" for _, c in cb_buttons(kb))  # подменю


async def test_remove_manager_flow(session_factory):
    async with session_factory() as session:
        manager = await people_svc.add_manager(session, "anna_mgr")
        manager_id = manager.id
    owner = await _make_person(session_factory, role="owner", tg_id=1008)
    msg = FakeMessage()

    await owner_h.cb_rm_list(make_callback("owner:rm:list:0", msg), db_user=owner)
    assert any(c == f"owner:rm:pick:{manager_id}:0" for _, c in cb_buttons(msg.edits[-1][1]))
    await owner_h.cb_rm_pick(make_callback(f"owner:rm:pick:{manager_id}:0", msg), db_user=owner)
    await owner_h.cb_rm_yes(make_callback(f"owner:rm:yes:{manager_id}:0", msg), db_user=owner)

    async with session_factory() as session:
        assert (await session.get(User, manager_id)).is_active is False


async def test_remove_teacher_flow_strips_manager(session_factory):
    """«Убрать преподавателя» у совмещённого: только роль2, менеджер живёт.

    Сквозной флоу: список → подтверждение (предупреждение о сохранении
    роли) → подтвердить → сообщение «роль сохранена» + человек активен.
    """
    async with session_factory() as session:
        teacher = await people_svc.add_teacher(session, "dual_roles", set())
        teacher_id = teacher.id
        await people_svc.add_manager(session, "dual_roles")
    owner = await _make_person(session_factory, role="owner", tg_id=1009)
    msg = FakeMessage()

    await owner_h.cb_rt_list(make_callback("owner:rt:list:0", msg), db_user=owner)
    assert any(c == f"owner:rt:pick:{teacher_id}:0" for _, c in cb_buttons(msg.edits[-1][1]))

    await owner_h.cb_rt_pick(make_callback(f"owner:rt:pick:{teacher_id}:0", msg), db_user=owner)
    text = msg.edits[-1][0]
    assert "Вторая роль сохранится" in text  # предупреждение совмещения

    await owner_h.cb_rt_yes(make_callback(f"owner:rt:yes:{teacher_id}:0", msg), db_user=owner)
    assert any("«менеджер» сохранена" in t for t, _ in msg.edits)

    async with session_factory() as session:
        user = await session.get(User, teacher_id)
        assert user.role == "manager"
        assert user.role2 is None
        assert user.is_active is True


# --------------------------------------------------------------------------
# Хендлеры: предметы
# --------------------------------------------------------------------------
async def test_subject_wizard(session_factory):
    owner = await _make_person(session_factory, role="owner", tg_id=1009)
    msg = FakeMessage()
    state = await make_fsm()

    await owner_h.cb_add_subject(make_callback("owner:add_subject:0", msg), state, db_user=owner)
    assert await state.get_state() == AddSubjectStates.name

    msg2 = FakeMessage(text="Биология")
    await owner_h.on_subject_name(msg2, state, db_user=owner)
    assert any("Биология" in t and "создан" in t for t, _ in msg2.answers)

    async with session_factory() as session:
        subj = await session.scalar(select(Subject).where(Subject.name == "Биология"))
        assert subj is not None
        assert subj.is_active is True


async def test_subject_duplicate_stays_in_wizard(session_factory):
    async with session_factory() as session:
        await people_svc.create_subject(session, "Биология")
    owner = await _make_person(session_factory, role="owner", tg_id=1010)
    msg = FakeMessage()
    state = await make_fsm()
    await owner_h.cb_add_subject(make_callback("owner:add_subject:0", msg), state, db_user=owner)

    msg2 = FakeMessage(text="Биология")
    await owner_h.on_subject_name(msg2, state, db_user=owner)
    assert any("уже существует" in t for t, _ in msg2.answers)
    assert await state.get_state() == AddSubjectStates.name  # остались в визарде


async def test_subject_toggle_flow(session_factory):
    async with session_factory() as session:
        subject = await people_svc.create_subject(session, "Химия")
        subject_id = subject.id
    owner = await _make_person(session_factory, role="owner", tg_id=1011)
    msg = FakeMessage()

    await owner_h.cb_subject_toggle_list(make_callback("owner:subj:toggle_list:0", msg), db_user=owner)
    assert any(c == f"owner:subj:toggle:{subject_id}:0" for _, c in cb_buttons(msg.edits[-1][1]))

    await owner_h.cb_subject_toggle(make_callback(f"owner:subj:toggle:{subject_id}:0", msg), db_user=owner)
    async with session_factory() as session:
        assert (await session.get(Subject, subject_id)).is_active is False

    # повторный клик — перерисовка без падений (идемпотентно)
    await owner_h.cb_subject_toggle(make_callback(f"owner:subj:toggle:{subject_id}:0", msg), db_user=owner)
    async with session_factory() as session:
        assert (await session.get(Subject, subject_id)).is_active is True


async def test_subject_toggle_empty(session_factory):
    owner = await _make_person(session_factory, role="owner", tg_id=1012)
    msg = FakeMessage()
    await owner_h.cb_subject_toggle_list(make_callback("owner:subj:toggle_list:0", msg), db_user=owner)
    assert "Предметов пока нет" in msg.edits[-1][0]


async def test_subject_back_returns_to_menu(session_factory):
    owner = await _make_person(session_factory, role="owner", tg_id=1015)
    msg = FakeMessage()
    await owner_h.cb_subject_back(make_callback("owner:subj:back:0", msg), db_user=owner)
    assert "📚 Управление предметами" in msg.edits[-1][0]
    assert any(c == "owner:add_subject:0" for _, c in cb_buttons(msg.edits[-1][1]))


async def test_kick_back_returns_to_categories(session_factory):
    owner = await _make_person(session_factory, role="owner", tg_id=1016)
    msg = FakeMessage()
    await owner_h.cb_kick_back(make_callback("owner:kick:back:0", msg), db_user=owner)
    assert "🔨 Кого деактивировать?" in msg.edits[-1][0]
    assert any(c == "owner:kick:cat:teacher:0" for _, c in cb_buttons(msg.edits[-1][1]))


# --------------------------------------------------------------------------
# Хендлеры: кик
# --------------------------------------------------------------------------
async def test_kick_flow(session_factory):
    async with session_factory() as session:
        user = User(tg_username="bad_user", role="manager")
        session.add(user)
        await session.commit()
        target_id = user.id
    owner = await _make_person(session_factory, role="owner", tg_id=1013)
    msg = FakeMessage()

    await owner_h.cb_kick_category(make_callback("owner:kick:cat:manager:0", msg), db_user=owner)
    assert any(c == f"owner:kick:pick:{target_id}:0" for _, c in cb_buttons(msg.edits[-1][1]))

    await owner_h.cb_kick_pick(make_callback(f"owner:kick:pick:{target_id}:0", msg), db_user=owner)
    assert any(c == f"owner:kick:yes:{target_id}:0" for _, c in cb_buttons(msg.edits[-1][1]))

    await owner_h.cb_kick_yes(make_callback(f"owner:kick:yes:{target_id}:0", msg), db_user=owner)
    async with session_factory() as session:
        assert (await session.get(User, target_id)).is_active is False


async def test_kick_self_forbidden(session_factory):
    owner = await _make_person(session_factory, role="owner", tg_id=1014)
    msg = FakeMessage()
    cb = make_callback(f"owner:kick:pick:{owner.id}:0", msg)
    await owner_h.cb_kick_pick(cb, db_user=owner)
    assert cb.answers and cb.answers[0][0] == "Себя деактивировать нельзя 🙂"

    async with session_factory() as session:
        assert (await session.get(User, owner.id)).is_active is True


async def test_broken_callbacks_do_not_crash(session_factory):
    """Битые колбэки (не-числовой id) — вежливый алерт, без падения."""
    owner = await _make_person(session_factory, role="owner", tg_id=1020)
    cases = [
        ("owner:rt:pick:abc:0", owner_h.cb_rt_pick),
        ("owner:rt:yes:abc:0", owner_h.cb_rt_yes),
        ("owner:rm:pick:xyz:0", owner_h.cb_rm_pick),
        ("owner:kick:pick:abc:0", owner_h.cb_kick_pick),
        ("owner:kick:yes:abc:0", owner_h.cb_kick_yes),
    ]
    for data, handler in cases:
        msg = FakeMessage()
        cb = make_callback(data, msg)
        await handler(cb, db_user=owner)
        assert cb.answers and "устарела" in cb.answers[0][0]
        assert msg.edits == []  # экран не тронут


async def test_kick_empty_category(session_factory):
    owner = await _make_person(session_factory, role="owner", tg_id=1015)
    msg = FakeMessage()
    await owner_h.cb_kick_category(make_callback("owner:kick:cat:student:0", msg), db_user=owner)
    assert "учеников пока нет" in msg.edits[-1][0]


# --------------------------------------------------------------------------
# Права и роли
# --------------------------------------------------------------------------
async def test_owner_menu_requires_owner_role(session_factory):
    guest = await _make_person(session_factory, role="guest", tg_id=1016)
    msg = FakeMessage()
    cb = make_callback("menu:owner:teachers:0", msg)
    # require_role отвечает через data["callback_query"], если event — не
    # настоящий CallbackQuery (см. app/utils/roles.py:_deny)
    await owner_h.cb_owner_teachers_menu(cb, db_user=guest, callback_query=cb)
    assert msg.edits == []  # хендлер не выполнился
    assert cb.answers and cb.answers[0][0] == MSG_NO_PERMISSION
    assert cb.answers[0][1] is True  # алерт


async def test_owner_can_open_submenus(session_factory):
    """Все подменю владельца открываются (списки, визарды, категории)."""
    owner = await _make_person(session_factory, role="owner", tg_id=1017)
    for data in (
        "menu:owner:teachers:0",
        "menu:owner:managers:0",
        "menu:owner:subjects:0",
        "menu:owner:kick:0",
    ):
        msg = FakeMessage()
        await owner_h.cb_owner_teachers_menu(make_callback(data, msg), db_user=owner)
    # не упал ни один


async def test_menu_cancels_owner_wizards(session_factory):
    """Заход 7 (ошибка №12): /menu отменяет визарды владельца
    (AddTeacher/AddManager/AddSubject) — «Визард отменён.» + чистый state."""
    from app.handlers import commands as cmd_h

    owner = await _make_person(session_factory, role="owner", tg_id=1018)
    starters = (
        ("owner:add_teacher:0", owner_h.cb_add_teacher, None),
        ("owner:add_manager:0", owner_h.cb_add_manager, None),
        ("owner:add_subject:0", owner_h.cb_add_subject, None),
    )
    for data, handler, _ in starters:
        state = await make_fsm()
        msg0 = FakeMessage()
        await handler(make_callback(data, msg0), state, db_user=owner)
        assert await state.get_state() is not None, data
        msg = FakeMessage()
        await cmd_h.cmd_menu(msg, db_user=owner, state=state)
        assert msg.answers[0][0] == cmd_h.WIZARD_CANCELED, data
        assert await state.get_state() is None, data

    # вход в визард на ходу (без /menu) тоже чистит предыдущий мусор
    state = await make_fsm()
    await owner_h.cb_add_teacher(make_callback("owner:add_teacher:0", FakeMessage()), state, db_user=owner)
    await state.update_data(username="ivanov_math")
    await owner_h.cb_add_subject(make_callback("owner:add_subject:0", FakeMessage()), state, db_user=owner)
    assert await state.get_state() == AddSubjectStates.name
    assert (await state.get_data()) == {}


# --------------------------------------------------------------------------
# Удалить предмет (список → подтверждение точным названием)
# --------------------------------------------------------------------------
async def test_subject_delete_flow(session_factory):
    """Полный флоу: список → выбор → подтверждение с «❌ Отмена» → удаление.

    Подменю после удаления — НОВЫМ сообщением (message.answer), а не
    safe_edit: message — сообщение пользователя, его нельзя редактировать.
    """
    from app.models import Subject, Theme
    from app.states import DeleteSubjectStates

    owner = await _make_person(session_factory, role="owner", tg_id=1101)
    async with session_factory() as session:
        subject = await people_svc.create_subject(session, "Математика")
        await people_svc.add_teacher(session, "ivanov_math", {subject.id})
        session.add(Theme(subject_id=subject.id, title="Тема 1"))
        await session.commit()

    msg = FakeMessage()
    state = await make_fsm()
    await owner_h.cb_subject_delete_list(make_callback("owner:subj:del_list:0", msg), db_user=owner)
    assert "Какой предмет удалить навсегда?" in msg.edits[-1][0]
    assert ("Математика", f"owner:subj:del:{subject.id}:0") in cb_buttons(msg.edits[-1][1])

    cb = make_callback(f"owner:subj:del:{subject.id}:0", msg)
    await owner_h.cb_subject_delete(cb, state, db_user=owner)
    assert await state.get_state() == DeleteSubjectStates.confirm
    # кнопка «❌ Отмена» на экране подтверждения — обещание в тексте
    assert ("❌ Отмена", "owner:subj:del_no:0") in cb_buttons(msg.edits[-1][1])

    msg2 = FakeMessage(text="математика")  # регистронезависимо
    await owner_h.on_subject_delete_confirm(msg2, state, db_user=owner)
    assert await state.get_state() is None
    assert msg2.answers[0][0] == "Предмет «Математика» удалён."
    # подменю предметов — новым сообщением, НЕ safe_edit чужого текста
    assert msg2.answers[1][0] == owner_h.TEXT_SUBJECTS_MENU
    assert not msg2.edits

    async with session_factory() as session:
        assert (await session.get(Subject, subject.id)) is None
        assert (await session.scalars(select(Theme))).all() == []
        assert (await session.scalars(select(TeacherSubject))).all() == []


async def test_subject_delete_not_match_keeps_wizard(session_factory):
    from app.states import DeleteSubjectStates

    owner = await _make_person(session_factory, role="owner", tg_id=1102)
    async with session_factory() as session:
        subject = await people_svc.create_subject(session, "Математика")

    state = await make_fsm()
    await owner_h.cb_subject_delete(
        make_callback(f"owner:subj:del:{subject.id}:0", FakeMessage()), state, db_user=owner
    )
    msg = FakeMessage(text="физика")
    await owner_h.on_subject_delete_confirm(msg, state, db_user=owner)
    assert msg.answers[0][0] == owner_h.TEXT_DEL_SUBJECT_NOT_MATCH
    assert await state.get_state() == DeleteSubjectStates.confirm  # визард жив

    async with session_factory() as session:
        assert (await session.get(Subject, subject.id)) is not None


async def test_subject_delete_cancel_button(session_factory):
    """«❌ Отмена» на подтверждении → подменю предметов, визард закрыт."""
    owner = await _make_person(session_factory, role="owner", tg_id=1103)
    async with session_factory() as session:
        subject = await people_svc.create_subject(session, "Математика")

    state = await make_fsm()
    msg = FakeMessage()
    await owner_h.cb_subject_delete(
        make_callback(f"owner:subj:del:{subject.id}:0", msg), state, db_user=owner
    )
    await owner_h.cb_subject_delete_cancel(
        make_callback("owner:subj:del_no:0", msg), state, db_user=owner
    )
    assert await state.get_state() is None
    assert msg.edits[-1][0] == owner_h.TEXT_SUBJECTS_MENU

    async with session_factory() as session:
        assert (await session.get(Subject, subject.id)) is not None


async def test_subject_delete_confirm_gone(session_factory):
    """Гонка: предмет удалён до подтверждения → «Предмет больше не существует»."""
    owner = await _make_person(session_factory, role="owner", tg_id=1104)
    async with session_factory() as session:
        subject = await people_svc.create_subject(session, "Математика")

    state = await make_fsm()
    msg = FakeMessage()
    await owner_h.cb_subject_delete(
        make_callback(f"owner:subj:del:{subject.id}:0", msg), state, db_user=owner
    )
    async with session_factory() as session:
        await people_svc.delete_subject(session, subject.id)

    msg2 = FakeMessage(text="Математика")
    await owner_h.on_subject_delete_confirm(msg2, state, db_user=owner)
    assert msg2.answers[0][0] == owner_h.TEXT_DEL_SUBJECT_GONE
    assert await state.get_state() is None


async def test_subject_delete_list_empty(session_factory):
    owner = await _make_person(session_factory, role="owner", tg_id=1105)
    msg = FakeMessage()
    await owner_h.cb_subject_delete_list(make_callback("owner:subj:del_list:0", msg), db_user=owner)
    assert msg.edits[-1][0] == f"{owner_h.TEXT_NO_SUBJECTS}. Создай: ➕ Добавить"
    assert ("➕ Добавить", "owner:add_subject:0") in cb_buttons(msg.edits[-1][1])


async def test_subject_delete_bad_callback(session_factory):
    """Битый колбэк → «Кнопка устарела» (без падения в глобальный обработчик)."""
    owner = await _make_person(session_factory, role="owner", tg_id=1106)
    state = await make_fsm()
    cb = make_callback("owner:subj:del:abc:0", FakeMessage())
    await owner_h.cb_subject_delete(cb, state, db_user=owner)
    assert cb.answers[0] == (owner_h.MSG_STALE, True)
    assert await state.get_state() is None