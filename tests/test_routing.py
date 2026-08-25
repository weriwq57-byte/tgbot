"""Регрессия маршрутизации через реальный диспетчер (аудит 10.08.2026).

Прямые вызовы хендлеров не видят конфликтов роутеров: aiogram останавливает
обработку апдейта на ПЕРВОМ совпавшем хендлере — даже вернувшем None.
Найденный баг: хендлер гостя on_guest_any_text совпадает с любым текстом
и для не-гостей возвращал None, поэтому «съедал» текстовые шаги визардов
менеджера/преподавателя/владельца (в проде визарды «Добавить ученика» и
т.п. застревали на первом шаге).

Тесты гоняют события через dp.feed_update с роутерами в порядке app/main.py
(student → commands → owner → manager → teacher) и проверяют:
- визард менеджера доезжает до шага subjects (текст не проглочен);
- текст гостя с кодом по-прежнему обрабатывается (регрессия фикса);
- «← Назад» роли обрабатывается ровно одним хендлером (student/commands).
"""
from datetime import datetime

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.fsm.storage.base import StorageKey
from aiogram.methods import (
    AnswerCallbackQuery,
    EditMessageText,
    SendMessage,
    SendPhoto,
    SetChatMenuButton,
    SetMyCommands,
)
from aiogram.types import CallbackQuery, Chat, Message, Update, User

from app.handlers import commands, manager, owner, student, teacher
from app.middlewares.user import UserContextMiddleware
from app.models import Subject, User as DBUser
from app.states import AddStudentStates

# Формат токена валиден (конструктор Bot не ходит в сеть)
FAKE_TOKEN = "1234567890:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw"


class StubSession(AiohttpSession):
    """Сессия без сети: исходящие методы записываются в списки.

    aiogram 3.x сливает Bot.send_message и т.п. в единый вызов
    session(bot, method) — перехватываем именно его, HTTP не происходит.
    Хендлеры не используют возврат answer/edit, поэтому None безопасен.
    """

    def __init__(self):
        self.sent: list[str] = []
        self.edited: list[str] = []
        self.answered: list[str] = []
        self.commands: list[tuple[str, str]] = []
        self.command_scopes: list = []
        self.menu_buttons: list = []
        self.markups: list = []

    async def __call__(self, bot, method, timeout=None):
        if isinstance(method, (SendMessage, SendPhoto)):
            self.sent.append(method.text or method.caption or "")
            self.markups.append(method.reply_markup)
        elif isinstance(method, EditMessageText):
            self.edited.append(method.text or "")
        elif isinstance(method, AnswerCallbackQuery):
            self.answered.append(method.text or "")
        elif isinstance(method, SetMyCommands):
            self.commands.extend((c.command, c.description) for c in method.commands)
            self.command_scopes.append(method.scope)
        elif isinstance(method, SetChatMenuButton):
            self.menu_buttons.append((method.chat_id, method.menu_button.type))
        return None


def _markup_buttons(markup) -> list[str]:
    """Подписи кнопок инлайн-клавиатуры (для проверки «живых» кнопок)."""
    if markup is None:
        return []
    return [b.text for row in markup.inline_keyboard for b in row]


def _make_bot() -> Bot:
    return Bot(
        token=FAKE_TOKEN,
        session=StubSession(),
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )


def _message(chat_id, text: str | None) -> Message:
    user = User(id=chat_id, is_bot=False, first_name="Тест")
    chat = Chat(id=chat_id, type="private")
    return Message(
        message_id=1, date=datetime.now(), chat=chat, from_user=user, text=text or ""
    )


def _text_update(tg_id: int, text: str, mid: int = 10) -> Update:
    user = User(id=tg_id, is_bot=False, first_name="Тест")
    chat = Chat(id=tg_id, type="private")
    msg = Message(
        message_id=mid, date=datetime.now(), chat=chat, from_user=user, text=text
    )
    return Update(update_id=mid, message=msg)


def _callback_update(tg_id: int, data: str, mid: int = 20) -> Update:
    user = User(id=tg_id, is_bot=False, first_name="Тест")
    chat = Chat(id=tg_id, type="private")
    msg = Message(message_id=mid, date=datetime.now(), chat=chat, from_user=user)
    cb = CallbackQuery(
        id=f"cb:{mid}", from_user=user, chat_instance="test", data=data, message=msg
    )
    return Update(update_id=mid, callback_query=cb)


_DP = None


def _build_dp() -> Dispatcher:
    """Диспетчер как в app/main.py (порядок роутеров критичен).

    Роутер может быть прикреплён только к одному родителю — синглтон
    на весь тестовый модуль. FSM-ключи у тестов разные (tg_id), общий
    MemoryStorage не конфликтует.
    """
    global _DP
    if _DP is None:
        dp = Dispatcher()
        dp.update.middleware(UserContextMiddleware())
        dp.include_router(student.router)
        dp.include_router(commands.router)
        dp.include_router(owner.router)
        dp.include_router(manager.router)
        dp.include_router(teacher.router)
        _DP = dp
    return _DP


def _state_key(bot: Bot, tg_id: int) -> StorageKey:
    return StorageKey(bot_id=bot.id, chat_id=tg_id, user_id=tg_id)


async def _mk_user(session_factory, tg_id: int, role: str) -> None:
    async with session_factory() as s:
        s.add(DBUser(tg_id=tg_id, role=role, is_active=True, tg_full_name="Тест"))
        await s.commit()


async def test_manager_wizard_text_not_swallowed_by_guest_handler(session_factory):
    """Визард «Добавить ученика»: текст «Имя» доходит до менеджерского шага.

    До фикса on_guest_any_text возвращал None и событие «съедалось»:
    состояние оставалось AddStudentStates.name навсегда.
    """
    await _mk_user(session_factory, 555, "manager")
    async with session_factory() as s:
        s.add(Subject(name="Математика", is_active=True))
        await s.commit()

    bot = _make_bot()
    dp = _build_dp()
    key = _state_key(bot, 555)

    # вход в визард кнопкой меню → шаг name
    await dp.feed_update(bot, _callback_update(555, "menu:manager:add_student:0"))
    assert await dp.storage.get_state(key) == AddStudentStates.name.state
    assert any("Как зовут ученика" in t for t in bot.session.sent)

    # текст имени НЕ должен проглатываться хендлером гостя
    await dp.feed_update(bot, _text_update(555, "Иван Иванов", mid=11))
    assert await dp.storage.get_state(key) == AddStudentStates.subjects.state
    assert any("Выбери предметы" in t for t in bot.session.sent)


async def test_guest_text_with_code_still_handled(session_factory):
    """Регрессия: текст гостя с кодом по-прежнему обрабатывается (ТЗ 7 п.3)."""
    await _mk_user(session_factory, 777, "guest")

    bot = _make_bot()
    dp = _build_dp()

    await dp.feed_update(bot, _text_update(777, "ABCDEF", mid=30))
    assert any("Такого кода нет" in t for t in bot.session.sent)
    assert not any("Выбери предметы" in t for t in bot.session.sent)


async def test_back_student_handled_once_by_student_router(session_factory):
    """menu:back:student:0 — ровно один ответ (student router, не commands)."""
    await _mk_user(session_factory, 888, "student")

    bot = _make_bot()
    dp = _build_dp()

    await dp.feed_update(bot, _callback_update(888, "menu:back:student:0", mid=40))
    assert len(bot.session.answered) == 1
    assert any("Меню ученика" in t for t in bot.session.edited)


async def test_back_manager_goes_to_commands_router(session_factory):
    """menu:back:manager:0 — обрабатывается commands.py (student не трогает)."""
    await _mk_user(session_factory, 666, "manager")

    bot = _make_bot()
    dp = _build_dp()

    await dp.feed_update(bot, _callback_update(666, "menu:back:manager:0", mid=50))
    assert len(bot.session.answered) == 1
    assert any(t.startswith("Меню:") for t in bot.session.edited)

async def test_task_answer_seq1_reaches_handler(session_factory):
    """Критичный фикс: кнопка ответа task:{id}:ans:{idx}:{seq}:{doy}
    (фикс perm: + день выдачи) ДОХОДИТ до хендлера через реальный роутер.

    Фильтр F.data.startswith("task:") совпадает с реальными кнопками
    (в т.ч. после первого ответа seq=1) — ответ «грузится». Прямые вызовы
    хендлеров этот класс багов не видят.
    """
    from datetime import timedelta

    from sqlalchemy import select

    from app.models import (
        Attempt,
        Student,
        StudentSubject,
        Task as DBTask,
        Theme,
    )
    from app.utils.dates import today_minsk
    from app.services.student import issue_day

    tg_id = 999
    await _mk_user(session_factory, tg_id, "student")
    async with session_factory() as s:
        subj = Subject(name="Математика", is_active=True)
        s.add(subj)
        await s.commit()
        user = await s.scalar(select(DBUser).where(DBUser.tg_id == tg_id))
        st = Student(
            user_id=user.id,
            access_until=today_minsk() + timedelta(days=30),
            invite_code="RRRT1",
        )
        s.add(st)
        await s.flush()
        s.add(StudentSubject(student_id=st.id, subject_id=subj.id))
        theme = Theme(subject_id=subj.id, title="Уравнения", is_open=True, mode="sequential")
        s.add(theme)
        await s.flush()
        t1 = DBTask(
            theme_id=theme.id, question_text="2+2?",
            options=[{"t": "А", "c": True}, {"t": "Б", "c": False}],
            is_active=True,
        )
        t2 = DBTask(
            theme_id=theme.id, question_text="3+3?",
            options=[{"t": "А", "c": True}, {"t": "Б", "c": False}],
            is_active=True,
        )
        s.add_all([t1, t2])
        await s.flush()
        # одна попытка по теме → следующая выдача будет с seq=1
        s.add(Attempt(student_id=st.id, task_id=t1.id, is_correct=False))
        await s.commit()
        task2_id, theme_id = t2.id, theme.id

    bot = _make_bot()
    dp = _build_dp()

    # вход в тему → карточка с seq=1: task:{task2}:ans:0:1:{doy}
    await dp.feed_update(bot, _callback_update(tg_id, f"std:theme:{theme_id}:0"))
    assert any(
        t.startswith("Решено 0 из 2") for t in bot.session.sent
    ), bot.session.sent

    # ответ по кнопке seq=1 через РЕАЛЬНЫЙ роутер (фильтры F.data)
    bot.session.sent.clear()
    bot.session.answered.clear()
    await dp.feed_update(
        bot, _callback_update(tg_id, f"task:{task2_id}:ans:0:1:{issue_day()}")
    )
    assert bot.session.answered, "ответная кнопка не дошла до хендлера"
    assert not any("Кнопка устарела" in t for t in bot.session.answered)
    assert len(bot.session.sent) >= 1, "реакция не отправлена"


async def test_default_commands_registered_in_telegram_menu(session_factory):
    """Default scope (все чаты) — только /start + /help; ролевые — в чат-скоупе."""
    from app.bot import DEFAULT_COMMANDS, set_bot_commands

    assert [c.command for c in DEFAULT_COMMANDS] == ["start", "help"]

    bot = _make_bot()
    await set_bot_commands(bot)
    assert [c for c, _ in bot.session.commands] == ["start", "help"]
    assert bot.session.command_scopes[-1] is None  # default scope


async def test_build_commands_by_role():
    """build_commands: гость/ученик — /start+/help; менеджер/препод — свои
    инструменты; владелец — все; совмещённые роли — объединение без дублей."""
    from app.bot import build_commands

    def keys(roles):
        return [c.command for c in build_commands(roles)]

    assert keys(("guest",)) == ["start", "help"]
    assert keys(("student",)) == ["start", "help", "stats"]
    assert keys(("manager",)) == [
        "start", "help", "students", "add_student", "expiring",
    ]
    assert keys(("teacher",)) == [
        "start", "help", "my_subjects", "add_theme", "tasks",
    ]
    assert keys(("owner",)) == [
        "start", "help", "menu", "broadcast", "students", "add_student",
        "expiring", "my_subjects", "add_theme", "tasks",
    ]
    union = keys(("teacher", "manager"))
    assert union == [
        "start", "help", "my_subjects", "add_theme", "tasks",
        "students", "add_student", "expiring",
    ]
    assert len(set(union)) == len(union), "команды не должны дублироваться"
    assert "menu" not in union  # менеджеру/преподу /menu не обещаем


async def test_commands_reply_kb_by_role():
    """commands_reply_kb: reply-клавиатура кнопок команд «на месте
    клавиатуры» — те же команды роли, что в нижнем меню."""
    from app.bot import commands_reply_kb

    def buttons(roles):
        kb = commands_reply_kb(roles)
        return [b.text for row in kb.keyboard for b in row]

    assert buttons(("student",)) == ["/start", "/help", "/stats"]
    assert buttons(("manager",)) == [
        "/start", "/help", "/students", "/add_student", "/expiring",
    ]
    assert buttons(("owner",))[2] == "/menu"
    # совмещённый препод+менеджер — объединение без дублей
    kb = commands_reply_kb(("teacher", "manager"))
    flat = [b.text for row in kb.keyboard for b in row]
    assert len(flat) == len(set(flat))
    assert "/my_subjects" in flat and "/students" in flat
    assert kb.resize_keyboard is True


async def test_sync_commands_chat_scope(session_factory):
    """sync_commands: setMyCommands со scope=chat конкретного пользователя;
    кнопка «Меню» (MenuButtonCommands) ставится для того же чата;
    tg_id=None и bot=None — безопасный пропуск (ни одного вызова)."""
    from types import SimpleNamespace

    from app.bot import sync_commands

    bot = _make_bot()
    await sync_commands(bot, SimpleNamespace(tg_id=4242, role="manager"))
    assert bot.session.command_scopes[-1].chat_id == 4242
    assert bot.session.menu_buttons[-1] == (4242, "commands")
    assert {"students", "add_student", "expiring"} <= set(
        c for c, _ in bot.session.commands
    )

    bot.session.commands.clear()
    bot.session.menu_buttons.clear()
    await sync_commands(
        bot, SimpleNamespace(tg_id=4242, role="teacher", role2="manager")
    )
    cmds = {c for c, _ in bot.session.commands}
    assert {"students", "my_subjects", "add_theme", "expiring"} <= cmds
    assert "menu" not in cmds
    assert bot.session.menu_buttons[-1] == (4242, "commands")

    bot.session.command_scopes.clear()
    bot.session.commands.clear()
    bot.session.menu_buttons.clear()
    await sync_commands(bot, SimpleNamespace(tg_id=None, role="manager"))
    assert (
        bot.session.command_scopes == []
        and bot.session.commands == []
        and bot.session.menu_buttons == []
    )

    await sync_commands(None, SimpleNamespace(tg_id=1, role="manager"))


async def test_start_syncs_role_commands(session_factory):
    """/start менеджера выставляет ЕГО команды в чат-скоуп (точка вызова)."""
    await _mk_user(session_factory, 555701, "manager")

    bot = _make_bot()
    dp = _build_dp()

    await dp.feed_update(bot, _text_update(555701, "/start", mid=70))
    cmds = {c for c, _ in bot.session.commands}
    assert {"start", "help", "students", "add_student", "expiring"} <= cmds
    assert "my_subjects" not in cmds
    assert bot.session.command_scopes and bot.session.command_scopes[-1].chat_id == 555701
    assert bot.session.menu_buttons[-1] == (555701, "commands")
    # reply-клавиатура команд прикреплена к сообщению /start
    from aiogram.types import ReplyKeyboardMarkup

    reply = [m for m in bot.session.markups if isinstance(m, ReplyKeyboardMarkup)]
    assert reply, "reply-клавиатура команд не прикреплена"
    kb = reply[-1]
    texts = [b.text for row in kb.keyboard for b in row]
    assert texts == ["/start", "/help", "/students", "/add_student", "/expiring"]


@pytest.mark.parametrize(
    "role, intro, header",
    [
        ("guest", "Как начать: нажми 🔑 Ввести код", "Мои команды (гость)"),
        ("student", "Здесь ты не просто выбираешь ответы", "Мои команды (ученик)"),
        ("manager", "Ты — менеджер школы", "Мои команды (менеджер)"),
        ("teacher", "Ты — преподаватель", "Мои команды (преподаватель)"),
        ("owner", "Ты — владелец бота", "Мои команды (владелец)"),
    ],
)
async def test_help_command_by_role(session_factory, role, intro, header):
    """/help по ролям: интро роли + «Мои команды» (через реальный роутер)."""
    await _mk_user(session_factory, 555702, role)

    bot = _make_bot()
    dp = _build_dp()

    await dp.feed_update(bot, _text_update(555702, "/help", mid=71))
    sent = bot.session.sent
    assert any(intro in t for t in sent), sent
    if role == "student":
        # Владелец (13.08): ученику блок «📋 Мои команды» НЕ показываем —
        # полезная информация уже в интро /help
        assert not any("Мои команды" in t for t in sent), sent
        assert any("/stats" in t for t in sent), sent
    else:
        assert any(header in t for t in sent), sent


async def test_guest_greeting_instead_of_refusal(session_factory):
    """Онбординг: «привет» от гостя — приветствие с подсказкой и кнопкой
    «🔑 Ввести код», а НЕ «Такого кода нет» (промт захода, проблема 2)."""
    await _mk_user(session_factory, 555703, "guest")

    bot = _make_bot()
    dp = _build_dp()

    await dp.feed_update(bot, _text_update(555703, "привет", mid=72))
    sent = bot.session.sent
    assert any("Привет! Ты из LevelUp?" in t for t in sent), sent
    assert any("🔑 Ввести код" in btn for m in bot.session.markups for btn in _markup_buttons(m)), sent
    assert not any("Такого кода нет" in t for t in sent)


async def test_guest_code_like_but_unknown(session_factory):
    """ZZZZZZ — 6 символов валидного алфавита, но не найден:
    «Такого кода нет. Проверь у менеджера 🙂» (как и раньше)."""
    await _mk_user(session_factory, 555704, "guest")

    bot = _make_bot()
    dp = _build_dp()

    await dp.feed_update(bot, _text_update(555704, "ZZZZZZ", mid=73))
    assert any("Такого кода нет" in t for t in bot.session.sent)


async def test_guest_real_code_asks_confirmation(session_factory):
    """ABC123 — код есть в БД: «Подтверди, что ты — {имя}?» (не мусор)."""
    from datetime import timedelta

    from sqlalchemy import select

    from app.models import Subject
    from app.models import User as DBUser
    from app.services import students as students_svc
    from app.utils.dates import today_minsk

    tg_id = 555705
    async with session_factory() as s:
        subj = Subject(name="Математика", is_active=True)
        s.add(subj)
        await s.commit()
        s.add(DBUser(tg_id=555799, role="manager", is_active=True))
        await s.commit()
        mgr = await s.scalar(select(DBUser).where(DBUser.tg_id == 555799))
        _, student, _ = await students_svc.create_student_record(
            s, "Иван", {subj.id}, mgr.id, today_minsk() + timedelta(days=30)
        )
        student.invite_code = "ABC123"
        await s.commit()
    await _mk_user(session_factory, tg_id, "guest")

    bot = _make_bot()
    dp = _build_dp()

    await dp.feed_update(bot, _text_update(tg_id, "ABC123", mid=74))
    sent = bot.session.sent
    assert any("Подтверди, что ты —" in t for t in sent), sent
    assert not any("Такого кода нет" in t for t in sent)


# ---------------------------------------------------------------------------
# Повторная привязка деактивированного (заход «новый код приглашения»)
# ---------------------------------------------------------------------------
async def test_kicked_student_text_code_rebinds(session_factory):
    """Кикнутого ударили, выдали НОВЫЙ код: текст кода доходит через мидлварь
    (looks_like_code), подтверждение кнопкой std:bind_yes проходит, старая
    запись удалена каскадом, новый ученик привязан к тому же tg_id."""
    from datetime import timedelta

    from sqlalchemy import select

    from app.models import Attempt, Student, Task, Theme
    from app.services import students as students_svc
    from app.utils.dates import today_minsk

    tg_id = 555706
    async with session_factory() as s:
        subj = Subject(name="Математика", is_active=True)
        s.add(subj)
        await s.commit()
        s.add(DBUser(tg_id=555799, role="manager", is_active=True))
        await s.commit()
        mgr = await s.scalar(select(DBUser).where(DBUser.tg_id == 555799))

        # старый ученик: привязан, кикнут, есть прогресс
        old_user, old_st, _ = await students_svc.create_student_record(
            s, "Старый", {subj.id}, mgr.id, today_minsk() + timedelta(days=5)
        )
        await s.commit()
        o_u = await s.get(DBUser, old_user.id)
        o_u.tg_id = tg_id
        o_u.is_active = False
        theme = Theme(subject_id=subj.id, title="Уравнения", is_open=True, mode="sequential")
        s.add(theme)
        await s.flush()
        t1 = Task(
            theme_id=theme.id, question_text="2+2?",
            options=[{"t": "А", "c": True}, {"t": "Б", "c": False}],
            is_active=True,
        )
        s.add(t1)
        await s.flush()
        s.add(Attempt(student_id=old_st.id, task_id=t1.id, is_correct=False))
        old_ids = (old_user.id, old_st.id)
        await s.commit()

        # НОВЫЙ код приглашения на нового ученика
        new_user, new_st, _ = await students_svc.create_student_record(
            s, "Новый", {subj.id}, mgr.id, today_minsk() + timedelta(days=30)
        )
        new_st.invite_code = "ABC456"
        await s.commit()

    bot = _make_bot()
    dp = _build_dp()

    # 1) текст нового кода → панель подтверждения
    await dp.feed_update(bot, _text_update(tg_id, "ABC456", mid=75))
    sent = bot.session.sent
    assert any("Подтверди, что ты — Новый?" in t for t in sent), sent

    # 2) «Да, это я» → привязка (кнопка доходит до хендлера — мидлварь
    #    пропускает std:bind_yes: даже для деактивированного)
    bot.session.sent.clear()
    await dp.feed_update(bot, _callback_update(tg_id, "std:bind_yes:ABC456:0", mid=76))
    sent = bot.session.sent
    assert any("Привет, Новый!" in t for t in sent), sent

    # 3) старая запись удалена каскадом, новый ученик привязан
    async with session_factory() as s:
        assert await s.get(DBUser, old_ids[0]) is None
        assert await s.get(Student, old_ids[1]) is None
        fresh = await s.scalar(select(DBUser).where(DBUser.tg_id == tg_id))
        assert fresh is not None
        assert fresh.role == "student"
        assert fresh.is_active is True
        fresh_st = await s.scalar(
            select(Student).where(Student.invite_code == "ABC456")
        )
        assert fresh_st.invite_status == "activated"


async def test_kicked_student_start_shows_stub_not_menu(session_factory):
    """/start деактивированного без нового кода: мидлварь пропускает,
    cmd_start рисует ЗАГЛУШКУ с текстом блокировки, а не меню роли."""
    from sqlalchemy import select

    from app.middlewares.user import MSG_BLOCKED

    await _mk_user(session_factory, 555707, "student")
    async with session_factory() as s:
        u = await s.scalar(select(DBUser).where(DBUser.tg_id == 555707))
        u.is_active = False
        await s.commit()

    bot = _make_bot()
    dp = _build_dp()

    await dp.feed_update(bot, _text_update(555707, "/start", mid=77))
    sent = bot.session.sent
    assert len(sent) == 1
    assert sent[0] == MSG_BLOCKED
    assert not any("Выбери раздел" in t for t in sent)
    assert not any("Стрик" in t for t in sent)


async def test_kicked_student_plain_text_blocked(session_factory):
    """Обычное сообщение (НЕ код) деактивированного — блокируется мидлварью
    с текстом MSG_BLOCKED (код-текст и /start — единственные пути)."""
    from sqlalchemy import select

    from app.middlewares.user import MSG_BLOCKED

    await _mk_user(session_factory, 555708, "student")
    async with session_factory() as s:
        u = await s.scalar(select(DBUser).where(DBUser.tg_id == 555708))
        u.is_active = False
        await s.commit()

    bot = _make_bot()
    dp = _build_dp()

    await dp.feed_update(bot, _text_update(555708, "привет, я тут", mid=78))
    sent = bot.session.sent
    assert len(sent) == 1
    assert sent[0] == MSG_BLOCKED


async def test_kicked_teacher_rebinds_with_warning(session_factory):
    """Дополнение: кикнутого ПРЕПОДА владелец пересоздал учеником —
    текст нового кода доходит через мидлварь и on_guest_any_text,
    подтверждение содержит предупреждение, std:bind_yes привязывает
    ученика и удаляет старую запись staff каскадом (TeacherSubject)."""
    from datetime import timedelta

    from sqlalchemy import select

    from app.models import Student, TeacherSubject, Theme
    from app.services import students as students_svc
    from app.utils.dates import today_minsk

    tg_id = 555709
    async with session_factory() as s:
        subj = Subject(name="Математика", is_active=True)
        s.add(subj)
        await s.commit()
        s.add(DBUser(tg_id=555799, role="manager", is_active=True))
        await s.commit()
        mgr = await s.scalar(select(DBUser).where(DBUser.tg_id == 555799))

        # старый препод: привязан, кикнут, есть связь предмета
        s.add(
            DBUser(
                tg_id=tg_id, tg_username="ex_teacher", role="teacher",
                is_active=False, tg_full_name="Бывший",
            )
        )
        await s.commit()
        ex = await s.scalar(select(DBUser).where(DBUser.tg_id == tg_id))
        s.add(TeacherSubject(teacher_id=ex.id, subject_id=subj.id))
        ex_id = ex.id
        await s.commit()

        # НОВЫЙ код приглашения на нового ученика
        new_user, new_st, _ = await students_svc.create_student_record(
            s, "Новичок", {subj.id}, mgr.id, today_minsk() + timedelta(days=30)
        )
        new_st.invite_code = "ABC789"
        await s.commit()

    bot = _make_bot()
    dp = _build_dp()

    # 1) текст нового кода → подтверждение С предупреждением
    await dp.feed_update(bot, _text_update(tg_id, "ABC789", mid=79))
    sent = bot.session.sent
    assert any("Подтверди, что ты — Новичок?" in t for t in sent), sent
    assert any("будет удалён вместе с данными" in t for t in sent), sent

    # 2) «Да, это я» → привязка ученика
    bot.session.sent.clear()
    await dp.feed_update(bot, _callback_update(tg_id, "std:bind_yes:ABC789:0", mid=80))
    sent = bot.session.sent
    assert any("Привет, Новичок!" in t for t in sent), sent

    # 3) старая запись staff удалена каскадом (TeacherSubject — CASCADE)
    async with session_factory() as s:
        assert await s.get(DBUser, ex_id) is None
        assert await s.get(TeacherSubject, (ex_id, subj.id)) is None
        fresh = await s.scalar(select(DBUser).where(DBUser.tg_id == tg_id))
        assert fresh is not None
        assert fresh.role == "student"
        assert fresh.is_active is True
        fresh_st = await s.scalar(
            select(Student).where(Student.invite_code == "ABC789")
        )
        assert fresh_st.invite_status == "activated"


async def test_errors_callback_routed_via_dispatcher(session_factory):
    """«🔁 Повторить ошибки» (std:errors:{theme}:0) доходит до ученического
    роутера через реальный диспетчер: приходит карточка «🔁 Ошибки · …»
    (суффикс «:e» у кнопок ответа — внутри callback-данных карточки)."""
    from datetime import timedelta

    from sqlalchemy import select

    from app.models import Task, TaskProgress, Theme
    from app.services import students as students_svc
    from app.utils.dates import today_minsk

    tg_id = 888807
    async with session_factory() as s:
        subj = Subject(name="Математика", is_active=True)
        s.add(subj)
        await s.commit()
        subj_id = subj.id
        theme = Theme(subject_id=subj_id, title="Уравнения", is_open=True, mode="sequential")
        s.add(theme)
        await s.commit()
        theme_id = theme.id
        task = Task(
            theme_id=theme_id, question_text="2+2?",
            options=[{"t": "А", "c": True}, {"t": "Б", "c": False}],
            is_active=True, order=0,
        )
        s.add(task)
        await s.commit()
        task_id = task.id
    async with session_factory() as s:
        s.add(DBUser(tg_id=888809, role="manager", is_active=True))
        await s.commit()
        mgr = await s.scalar(
            select(DBUser).where(DBUser.tg_id == 888809)
        )
        user, student, _ = await students_svc.create_student_record(
            s, "Иван", {subj_id}, mgr.id, today_minsk() + timedelta(days=30)
        )
        s.add(TaskProgress(student_id=student.id, task_id=task_id, status="wrong"))
        await s.commit()
        user.tg_id = tg_id  # наш tg_id (в create_student_record был другой)
        user.tg_full_name = "Иван"
        await s.commit()

    bot = _make_bot()
    dp = _build_dp()
    await dp.feed_update(bot, _callback_update(tg_id, f"std:errors:{theme_id}:0"))
    sent = bot.session.sent
    assert any(t.startswith("🔁 Ошибки · осталось 1") for t in sent)


async def test_task_question_photo_with_caption_goes_to_photo_handler(session_factory):
    """Баг-фикс: фото с подписью в шаге вопроса НЕ уходит в text-хендлер
    (раньше caption становился вопросом, а фотка терялась) — photo-хендлер
    первый по порядку: сохраняется question_photo_id + подпись."""
    from aiogram.types import PhotoSize

    from app.states import AddTaskStates

    tg_id = 888811
    async with session_factory() as s:
        s.add(DBUser(tg_id=tg_id, role="teacher", is_active=True, tg_full_name="П"))
        await s.commit()

    bot = _make_bot()
    dp = _build_dp()
    state_key = _state_key(bot, tg_id)
    await dp.storage.set_state(state_key, AddTaskStates.question)

    user = User(id=tg_id, is_bot=False, first_name="П")
    chat = Chat(id=tg_id, type="private")
    msg = Message(
        message_id=33, date=datetime.now(), chat=chat, from_user=user,
        photo=[PhotoSize(file_id="PHOTO_123", file_unique_id="u1", width=10, height=10)],
        caption="Сколько будет 2+2?",
    )
    await dp.feed_update(bot, Update(update_id=33, message=msg))

    data = await dp.storage.get_data(state_key)
    assert data["question_photo_id"] == "PHOTO_123"   # фото сохранено
    assert data["question_text"] == "Сколько будет 2+2?"  # подпись — текстом
    # визард перешёл к вариантам (не «Вопрос не может быть пустым»)
    assert await dp.storage.get_state(state_key) == AddTaskStates.options
    sent = bot.session.sent
    assert any(t == teacher.ASK_OPTIONS for t in sent)
