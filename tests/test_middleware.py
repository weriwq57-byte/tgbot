"""Тесты UserContextMiddleware: создание ролей, привязка по username,
освежение данных, логирование команд, блокировка деактивированных."""
from types import SimpleNamespace

from sqlalchemy import select

from app.middlewares.user import MSG_BLOCKED, UserContextMiddleware
from app.models import User


def make_user(tg_id: int, username=None, full_name=None):
    """Фейковый from_user из апдейта Telegram."""
    return SimpleNamespace(
        id=tg_id, username=username, full_name=full_name, is_bot=False
    )


def make_message(text: str = ""):
    """Фейковое сообщение с записью ответов."""
    answers = []

    async def answer(content="", **kwargs):
        answers.append((content, kwargs))

    return SimpleNamespace(text=text, answer=answer, answers=answers)


async def _run_middleware(session_factory, event, *, data=None):
    """Прогон мидлваря: handler ниже вызывается как handler(event, data)."""
    handled = []
    answers = []

    async def handler(event, data):
        handled.append(data.get("db_user"))
        return "ok"

    data = dict(data or {})
    result = await UserContextMiddleware()(handler, event, data)
    return result, handled, data


# --------------------------------------------------------------------------
# Создание пользователя
# --------------------------------------------------------------------------
async def test_creates_guest_for_new_user(session_factory):
    event = SimpleNamespace(from_user=make_user(777, "newbie", "Newbie"))
    result, handled, data = await _run_middleware(session_factory, event)

    assert result == "ok"
    assert len(handled) == 1
    assert data["db_user"].role == "guest"

    async with session_factory() as session:
        user = await session.scalar(select(User).where(User.tg_id == 777))
        assert user is not None
        assert user.role == "guest"
        assert user.tg_username == "newbie"
        assert user.tg_full_name == "Newbie"


async def test_creates_owner_for_admin_tg_id(session_factory, monkeypatch):
    """tg_id из ADMIN_IDS → роль owner (а не guest)."""
    import app.middlewares.user as user_mw

    monkeypatch.setattr(
        user_mw, "get_settings", lambda: SimpleNamespace(ADMIN_IDS=[555])
    )
    event = SimpleNamespace(from_user=make_user(555, "boss"))
    result, handled, data = await _run_middleware(session_factory, event)

    assert result == "ok"
    assert data["db_user"].role == "owner"

    async with session_factory() as session:
        user = await session.scalar(select(User).where(User.tg_id == 555))
        assert user.role == "owner"


# --------------------------------------------------------------------------
# Привязка «телефонных» преподавателей/менеджеров по @username
# --------------------------------------------------------------------------
async def test_binds_teacher_by_username(session_factory):
    """Препод, добавленный владельцем (tg_id NULL), привязывается."""
    async with session_factory() as session:
        session.add(
            User(
                tg_id=None,
                tg_username="ivanov_math",
                tg_full_name="Иван Иванов",
                role="teacher",
                is_active=True,
            )
        )
        await session.commit()

    event = SimpleNamespace(
        from_user=make_user(424242, "ivanov_math", "Иван Иванов")
    )
    result, _, data = await _run_middleware(session_factory, event)

    assert result == "ok"
    assert data["db_user"].role == "teacher"
    assert data["db_user"].tg_id == 424242

    async with session_factory() as session:
        user = await session.scalar(select(User).where(User.tg_id == 424242))
        assert user is not None
        assert user.role == "teacher"


async def test_binds_manager_by_username(session_factory):
    async with session_factory() as session:
        session.add(
            User(
                tg_id=None,
                tg_username="anna_mgr",
                role="manager",
                is_active=True,
            )
        )
        await session.commit()
    event = SimpleNamespace(from_user=make_user(909, "anna_mgr", "Анна"))
    result, _, data = await _run_middleware(session_factory, event)
    assert result == "ok"
    assert data["db_user"].role == "manager"


async def test_binds_staff_username_case_insensitive(session_factory):
    """Username регистронезависим: владелец добавил @Ivanov (с заглавной),
    человек заходит с username ivanov — привязка всё равно происходит."""
    async with session_factory() as session:
        session.add(
            User(
                tg_id=None,
                tg_username="Ivanov",
                role="teacher",
                is_active=True,
            )
        )
        await session.commit()

    event = SimpleNamespace(from_user=make_user(505, "ivanov", "Иван"))
    result, _, data = await _run_middleware(session_factory, event)

    assert result == "ok"
    assert data["db_user"].role == "teacher"
    assert data["db_user"].tg_id == 505


async def test_binds_staff_username_case_insensitive_reverse(session_factory):
    """Обратный регистр: в БД lowercase, человек заходит с заглавом."""
    async with session_factory() as session:
        session.add(
            User(
                tg_id=None,
                tg_username="petrov_math",
                role="teacher",
                is_active=True,
            )
        )
        await session.commit()

    event = SimpleNamespace(from_user=make_user(606, "Petrov_Math", "Пётр"))
    result, _, data = await _run_middleware(session_factory, event)

    assert result == "ok"
    assert data["db_user"].role == "teacher"


async def test_does_not_bind_guest_username(session_factory):
    """Гость с тем же username не схватывает чужую запись другого tg_id."""
    async with session_factory() as session:
        session.add(
            User(tg_id=111, tg_username="someuser", role="guest", is_active=True)
        )
        await session.commit()

    event = SimpleNamespace(from_user=make_user(222, "someuser"))
    result, _, data = await _run_middleware(session_factory, event)

    assert result == "ok"
    assert data["db_user"].tg_id == 222
    assert data["db_user"].role == "guest"

    async with session_factory() as session:
        users = (await session.scalars(select(User))).all()
        assert len(users) == 2  # старая запись не тронута, новая создана


async def test_does_not_bind_inactive_teacher(session_factory):
    """Деактивированный «телефонный» препод не привязывается (блокируется)."""
    async with session_factory() as session:
        session.add(
            User(
                tg_id=None,
                tg_username="kicked_teacher",
                role="teacher",
                is_active=False,
            )
        )
        await session.commit()

    event = SimpleNamespace(
        from_user=make_user(333, "kicked_teacher", "Уволен")
    )
    result, _, data = await _run_middleware(session_factory, event)

    assert result == "ok"  # создаётся guest (привязка запрещена — is_active=False)
    assert data["db_user"].role == "guest"


async def test_guest_visiting_staff_username_gets_bound(session_factory):
    """Гость, уже заходивший в бота, привязывается к профилю препода/менеджера,
    добавленному владельцем по его @username (guest-запись удаляется — ТЗ §7)."""
    async with session_factory() as session:
        session.add(
            User(tg_id=4242, tg_username="ivanov_math", role="guest", is_active=True)
        )
        session.add(
            User(
                tg_id=None,
                tg_username="ivanov_math",
                tg_full_name="Иван Иванов",
                role="teacher",
                is_active=True,
            )
        )
        await session.commit()

    event = SimpleNamespace(
        from_user=make_user(4242, "ivanov_math", "Иван Иванов")
    )
    result, _, data = await _run_middleware(session_factory, event)

    assert result == "ok"
    assert data["db_user"].role == "teacher"
    assert data["db_user"].tg_id == 4242
    assert data["db_user"].tg_full_name == "Иван Иванов"

    async with session_factory() as session:
        users = (await session.scalars(select(User))).all()
        assert len(users) == 1  # guest-запись удалена, остался только препод


async def test_existing_guest_in_admin_ids_becomes_owner(session_factory, monkeypatch):
    """Владелец из ADMIN_IDS, заходивший раньше как гость, повышается до owner."""
    import app.middlewares.user as user_mw

    monkeypatch.setattr(
        user_mw, "get_settings", lambda: SimpleNamespace(ADMIN_IDS=[515])
    )
    async with session_factory() as session:
        session.add(
            User(tg_id=515, tg_username="boss", role="guest", is_active=True)
        )
        await session.commit()

    event = SimpleNamespace(from_user=make_user(515, "boss", "Босс"))
    result, _, data = await _run_middleware(session_factory, event)

    assert result == "ok"
    assert data["db_user"].role == "owner"

    async with session_factory() as session:
        user = await session.scalar(select(User).where(User.tg_id == 515))
        assert user.role == "owner"


# --------------------------------------------------------------------------
# Освежение данных
# --------------------------------------------------------------------------
async def test_refreshes_username_and_full_name(session_factory):
    async with session_factory() as session:
        session.add(
            User(
                tg_id=888,
                tg_username="old_name",
                tg_full_name="Старое Имя",
                role="guest",
            )
        )
        await session.commit()

    event = SimpleNamespace(
        from_user=make_user(888, "new_name", "Новое Имя")
    )
    result, _, data = await _run_middleware(session_factory, event)
    assert result == "ok"
    assert data["db_user"].tg_username == "new_name"

    async with session_factory() as session:
        user = await session.scalar(select(User).where(User.tg_id == 888))
        assert user.tg_username == "new_name"
        assert user.tg_full_name == "Новое Имя"


async def test_no_refresh_when_nothing_changed(session_factory):
    """Нет изменений → нет лишнего коммита (обычный проход)."""
    async with session_factory() as session:
        session.add(
            User(
                tg_id=7777,
                tg_username="same",
                tg_full_name="Same",
                role="guest",
            )
        )
        await session.commit()
    event = SimpleNamespace(from_user=make_user(7777, "same", "Same"))
    result, _, data = await _run_middleware(session_factory, event)
    assert result == "ok"
    assert data["db_user"].tg_username == "same"


# --------------------------------------------------------------------------
# Блокировка деактивированных (ТЗ 10 + заход «повторная привязка»)
# --------------------------------------------------------------------------
async def test_blocks_inactive_message(session_factory):
    """Обычное сообщение деактивированного блокируется с человеческим
    текстом (новый MSG_BLOCKED подсказывает путь повторной привязки)."""
    async with session_factory() as session:
        session.add(
            User(tg_id=999, tg_username="kicked", role="teacher", is_active=False)
        )
        await session.commit()

    msg = make_message("привет")
    event = SimpleNamespace(
        from_user=make_user(999, "kicked", "Kicked"),
        text="привет",
        answer=msg.answer,
    )
    result, handled, _ = await _run_middleware(session_factory, event)

    assert result is None  # событие НЕ ушло дальше
    assert handled == []
    assert msg.answers[0][0] == MSG_BLOCKED
    assert "новый код приглашения" in MSG_BLOCKED


async def test_start_not_blocked_for_inactive(session_factory):
    """/start деактивированного НЕ блокируется (путь повторной привязки:
    в т.ч. /start КОД — deep link ?start=КОД) — событие идёт в хендлер,
    а заглушку покажет cmd_start (не мидлварь)."""
    async with session_factory() as session:
        session.add(
            User(tg_id=999, tg_username="kicked", role="student", is_active=False)
        )
        await session.commit()

    event = SimpleNamespace(
        from_user=make_user(999, "kicked", "Kicked"),
        text="/start ABC234",
        answer=make_message().answer,
    )
    result, handled, _ = await _run_middleware(session_factory, event)

    assert result == "ok"  # пропущен в хендлер
    assert len(handled) == 1
    assert handled[0].role == "student"


async def test_code_text_not_blocked_for_inactive(session_factory):
    """"Напиши код в чат" (текст MSG_BLOCKED): текст, похожий на код,
    деактивированному НЕ блокируется — идёт в хендлер привязки."""
    async with session_factory() as session:
        session.add(
            User(tg_id=1003, tg_username="kicked", role="student", is_active=False)
        )
        await session.commit()

    event = SimpleNamespace(
        from_user=make_user(1003, "kicked", "Kicked"),
        text="ABC234",
        answer=make_message().answer,
    )
    result, handled, _ = await _run_middleware(session_factory, event)

    assert result == "ok"
    assert len(handled) == 1


async def test_bind_callback_not_blocked_for_inactive(session_factory):
    """Кнопка «Да, это я» (std:bind_yes:) деактивированному НЕ блокируется
    — иначе подтверждение повторной привязки никогда не дошло бы до
    хендлера (колбэк-событие, а не текст)."""
    async with session_factory() as session:
        session.add(
            User(tg_id=1004, tg_username="kicked", role="student", is_active=False)
        )
        await session.commit()

    alerts = []

    async def answer(content="", show_alert=False, **kwargs):
        alerts.append((content, show_alert))

    event = SimpleNamespace(
        from_user=make_user(1004),
        data="std:bind_yes:ABC234:0",
        answer=answer,
    )
    result, handled, _ = await _run_middleware(session_factory, event)

    assert result == "ok"
    assert len(handled) == 1
    assert alerts == []


async def test_blocks_inactive_callback_with_alert(session_factory):
    async with session_factory() as session:
        session.add(User(tg_id=1001, role="student", is_active=False))
        await session.commit()

    alerts = []

    async def answer(content="", show_alert=False, **kwargs):
        alerts.append((content, show_alert))

    callback = SimpleNamespace(data="menu:owner:teachers:0", answer=answer)
    event = SimpleNamespace(from_user=make_user(1001), data="menu:owner:teachers:0")
    event.answer = callback.answer  # duck-typing: колбэк-событие

    result, handled, _ = await _run_middleware(session_factory, event)
    assert result is None
    assert handled == []
    assert alerts[0][0] == MSG_BLOCKED
    assert alerts[0][1] is True  # алерт


async def test_blocks_inactive_student_answer_button(session_factory):
    """Деактивированный ученик жмёт кнопку ответа по СТАРОМУ заданию —
    блокировка в мидлвари, хендлер ответа не вызывается (ТЗ 10)."""
    async with session_factory() as session:
        session.add(User(tg_id=1002, role="student", is_active=False))
        await session.commit()

    alerts = []

    async def answer(content="", show_alert=False, **kwargs):
        alerts.append((content, show_alert))

    event = SimpleNamespace(from_user=make_user(1002), data="task:7:ans:0:0:0")
    event.answer = answer

    result, handled, _ = await _run_middleware(session_factory, event)
    assert result is None
    assert handled == []  # cb_task_answer / check_answer НЕ выполнялись
    assert alerts == [(MSG_BLOCKED, True)]


# --------------------------------------------------------------------------
# Обёртка Update (dp.update) и логирование команд
# --------------------------------------------------------------------------
async def test_unwraps_update(session_factory):
    from datetime import datetime

    from aiogram.types import Chat, Message, Update, User as TgUser

    tg_user = TgUser(id=778, is_bot=False, first_name="New", username="nw")
    msg = Message(
        message_id=1,
        date=datetime(2026, 8, 6, 16, 0, 0),
        chat=Chat(id=778, type="private"),
        from_user=tg_user,
        text="/start",
    )
    event = Update(update_id=1, message=msg)
    result, _, data = await _run_middleware(session_factory, event)
    assert result == "ok"
    assert data["db_user"].role == "guest"
    assert data["db_user"].tg_id == 778


async def test_logs_commands(session_factory, caplog):
    import logging

    caplog.set_level(logging.INFO)
    event = SimpleNamespace(
        from_user=make_user(123, "logger", "Логгер"), text="/menu"
    )
    await _run_middleware(session_factory, event)

    assert any(
        "Команда /menu" in record.getMessage()
        and "role=guest" in record.getMessage()
        and "tg_id=123" in record.getMessage()
        for record in caplog.records
    )


async def test_no_from_user_passes_through(session_factory):
    """Событие без from_user (poll_answer и т.п.) идёт дальше без изменений."""
    event = SimpleNamespace(foo="bar")
    called = []

    async def handler(event, data):
        called.append(data)
        return "done"

    data = {}
    result = await UserContextMiddleware()(handler, event, data)
    assert result == "done"
    assert called == [{}]  # db_user не добавлялся