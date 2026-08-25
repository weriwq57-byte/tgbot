"""/start, /menu, возврат в меню и «📋 Мои команды».

Главное меню — инлайн-кнопки, не глубже 3 уровней. Guest получает
предложение ввести код приглашения (привязка и deep link ?start=КОД —
Заход 6). menu:back:{role}:0 и menu:help:{role}:0 рисуются по роли
ИЗ БД (db_user), а не из колбэка — устаревшие клавиатуры после смены
роли не обманут.
"""
import logging

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardMarkup

from app.bot import commands_reply_kb, sync_commands
from app.keyboards.inline import back_button, main_menu_kb
from app.middlewares.user import MSG_BLOCKED
from app.utils.format import esc
from app.utils.messages import safe_edit
from app.utils.roles import user_roles

logger = logging.getLogger(__name__)

router = Router()

WIZARD_CANCELED = "Визард отменён."

# Текст для гостя — дословно из ТЗ (раздел 6)
GUEST_GREETING = (
    "Привет! Ты из LevelUp? 👋\n"
    "Введи код приглашения, который прислал менеджер, "
    "чтобы начать заниматься.\n\n"
    "Формат кода — 6 символов, например ABC123"
)

# Человеческие названия ролей для заголовка «Мои команды»
ROLE_LABELS = {
    "owner": "владелец",
    "manager": "менеджер",
    "teacher": "преподаватель",
    "student": "ученик",
    "guest": "гость",
}

# Команды ролей = кнопки ГЛАВНОГО МЕНЮ роли (1 строка = 1 кнопка).
# Правило: «Мои команды» — это ровно то, что видно в меню; любые новые
# кнопки меню добавляются в эту секцию ОДНОВРЕМЕННО с меню (иначе
# рассинхрон: help обещает кнопки, которых нет).
HELP_BY_ROLE: dict[str, list[str]] = {
    "owner": [
        "👨🏫 Преподаватели",
        "👥 Менеджеры",
        "📚 Предметы",
        "👨🎓 Ученики",
        "🔨 Кикнуть",
        "🎯 Решать задания",
        "🛠 Редактировать темы",
        "📣 Рассылка",
    ],
    "manager": [
        "👨🎓 Ученики",
        "➕ Добавить ученика",
        "⏳ Истекающие",
    ],
    "teacher": [
        "📚 Мои предметы",
        "🎯 Решать задания",
    ],
    "student": [
        "🎯 Решать задания",
        "📊 Статистика",
    ],
    "guest": [
        "🔑 Ввести код",
    ],
}

# Все 5 ролей реализованы: «⏳ остальные появятся» больше никому не нужен
_HELP_ROLES_DONE = ("owner", "manager", "teacher", "student", "guest")

# Интро для /help по роли: что за человек и с чего начать. Гость/ученик —
# дословно из промта захода; персонал — краткая инструкция по инструментам.
HELP_INTRO_BY_ROLE: dict[str, str] = {
    "guest": (
        "Как начать: нажми 🔑 Ввести код или просто напиши код "
        "(6 символов, например ABC123) в чат.\n"
        "/start — меню. /help — эта справка."
    ),
    "student": (
        "Здесь ты не просто выбираешь ответы: бот разбирает каждое "
        "задание и объясняет решение.\n"
        "\n"
        "Этот бот - дополнительная отработка тем. Готовиться можно "
        "где угодно: хоть в автобусе по дороге в школу.\n"
        "\n"
        "🧰 Разбор и ошибки\n"
        "💡 После ответа бот показывает, правильно ли, называет верный "
        "вариант и объясняет, почему именно так.\n"
        "🔁 «Повторить ошибки» вернёт задания, где ты споткнулся. "
        "Можешь решать, пока не станет легко.\n"
        "\n"
        "🔥 Занимайся каждый день - стрик растёт, а прогресс видно сразу.\n"
        "\n"
        "📊 Статистика\n"
        "/stats — точность и стрик по каждому предмету.\n"
        "\n"
        "⏳ Доступ\n"
        "Курс открыт на срок, назначенный менеджером. Хочешь продлить — "
        "напиши менеджеру @levelup_sup_bot или оставь заявку на сайте."
    ),
    "manager": (
        "Ты — менеджер школы. Создавай учеников, продлевай доступы, "
        "следи за истекающими.\n"
        "Команды: /students — список, /add_student — добавить, "
        "/expiring — истекающие доступы"
    ),
    "teacher": (
        "Ты — преподаватель. Веди свои предметы: темы и задания.\n"
        "Команды: /my_subjects — мои предметы, /add_theme — новая тема, "
        "/tasks — задания темы"
    ),
    "owner": (
        "Ты — владелец бота. Управляй персоналом, предметами и учениками.\n"
        "/menu — главное меню, /help — эта справка"
    ),
}


def _menu_kb_or_none(db_user) -> InlineKeyboardMarkup | None:
    """Меню роли пользователя; пустую клавиатуру не отправляем."""
    kb = main_menu_kb(user_roles(db_user))
    if not kb.inline_keyboard or not any(row for row in kb.inline_keyboard):
        return None
    return kb


async def _cancel_wizard(message: Message, state: FSMContext | None) -> None:
    """Раздел 0 дефект 1: активный визард отменяется командой.

    «Визард отменён.» — только если визард реально был активен.
    """
    if state is None or await state.get_state() is None:
        return
    await message.answer(WIZARD_CANCELED)
    await state.clear()


@router.message(CommandStart())
async def cmd_start(
    message: Message, db_user=None, state: FSMContext = None, bot=None
) -> None:
    """Приветствие + меню по роли. /start КОД — привязка (Заход 6).

    Деактивированный БЕЗ нового кода (мидлварь пропустил /start ради
    повторной привязки): показываем заглушку с текстом блокировки, а не
    меню роли — он всё равно ничего не может делать, кроме привязки.
    """
    await _cancel_wizard(message, state)
    if not db_user.is_active:
        await message.answer(MSG_BLOCKED)
        return
    if db_user.role == "guest":
        text = GUEST_GREETING
        await message.answer(text, reply_markup=_menu_kb_or_none(db_user))
    else:
        name = db_user.tg_full_name or db_user.tg_username or "друг"
        text = f"Привет, {esc(name)}! 👋 Выбери раздел:"
        # Reply-клавиатура команд прикреплена к приветствию (без лишнего
        # текстового сообщения): переключатель возле стикеров открывает
        # команды «на месте клавиатуры». Инлайн-меню — отдельным сообщением.
        await message.answer(
            text, reply_markup=commands_reply_kb(user_roles(db_user))
        )
        await message.answer("Меню:", reply_markup=_menu_kb_or_none(db_user))
    # Команды нижнего меню Telegram — по роли (чат-скоуп)
    await sync_commands(bot, db_user)


@router.message(Command("menu"))
async def cmd_menu(
    message: Message, db_user=None, state: FSMContext = None, bot=None
) -> None:
    """Главное меню (активный визард — отменяется)."""
    await _cancel_wizard(message, state)
    await message.answer(
        "Меню:", reply_markup=_menu_kb_or_none(db_user)
    )
    # Страховка: меню всегда синхронизирует команды с ролью
    await sync_commands(bot, db_user)


@router.message(Command("help"))
async def cmd_help(message: Message, db_user=None) -> None:
    """/help — справка по роли: интро + «Мои команды» + «← Назад».

    Ученику (владелец, 13.08) блок «Мои команды» НЕ показываем — вся
    полезная информация уже в интро (меню и статистика описаны там).
    """
    roles = user_roles(db_user)
    intro = HELP_INTRO_BY_ROLE.get(roles[0], HELP_INTRO_BY_ROLE["guest"])
    kb = InlineKeyboardMarkup(inline_keyboard=[[back_button(roles[0])]])
    suffix = "" if roles[0] == "student" else f"\n\n{help_text_roles(roles)}"
    await message.answer(f"{intro}{suffix}", reply_markup=kb)


@router.callback_query(
    F.data.regexp(r"^menu:back:(owner|manager|teacher|student|guest):0$")
)
async def cb_back_to_menu(callback: CallbackQuery, db_user=None) -> None:
    """«← Назад» из любого подменю — в главное меню роли.

    Роль берём из БД (db_user), а не из колбэка — на случай устаревших
    клавиатур после смены роли пользователя.
    """
    await callback.answer()
    await safe_edit(callback.message, "Меню:", reply_markup=_menu_kb_or_none(db_user))


def help_text(role: str) -> str:
    """Текст «Мои команды» для роли (для тестов)."""
    items = HELP_BY_ROLE[role]
    lines = [f"📋 <b>Мои команды ({ROLE_LABELS[role]}):</b>", ""]
    lines += [f"• {item}" for item in items]
    return "\n".join(lines)


def help_text_roles(roles: tuple[str, ...]) -> str:
    """Текст «Мои команды» для НАБОРА ролей (совмещённые менеджер+препод).

    Каждая роль печатает свой блок; дубликатов между ролями нет.
    Гостю дополнительно — подсказка про ввод кода (UX-пакет).
    """
    blocks = []
    for role in roles:
        items = HELP_BY_ROLE[role]
        header = f"📋 <b>Мои команды ({ROLE_LABELS[role]}):</b>"
        blocks.append(header + "\n" + "\n".join(f"• {item}" for item in items))
    text = "\n\n".join(blocks)
    if "guest" in roles:
        text += "\n\nВведи код текстом в чат или через кнопку"
    return text


@router.callback_query(
    F.data.regexp(r"^menu:help:(owner|manager|teacher|student|guest):0$")
)
async def cb_help_commands(callback: CallbackQuery, db_user=None) -> None:
    """«📋 Мои команды» — список команд роли.

    Роль берём из БД (db_user), а не из колбэка — аналогично back.
    """
    await callback.answer()
    roles = user_roles(db_user)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[back_button(roles[0])]]
    )
    await safe_edit(callback.message, help_text_roles(roles), reply_markup=kb)