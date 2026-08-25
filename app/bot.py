"""Создание бота и диспетчера + команды нижнего меню Telegram.

HTML-разметка включена по умолчанию; любой пользовательский текст
экранируется через utils.format.esc.

Команды нижней кнопки «Меню» — ПО РОЛИ (BotCommandScopeChat):
- default scope (для всех чатов, включая ещё не зашедших) — /start + /help;
- гость и ученик — только /start + /help;
- менеджер — + /students /add_student /expiring;
- преподаватель — + /my_subjects /add_theme /tasks;
- владелец — все команды (в т.ч. /menu).
Совмещённые роли (teacher+manager, role2) — объединение без дублей.
"""
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import (
    BotCommand,
    BotCommandScopeChat,
    KeyboardButton,
    MenuButtonCommands,
    ReplyKeyboardMarkup,
)

from app.config import get_settings
from app.middlewares.user import UserContextMiddleware
from app.utils.roles import user_roles

logger = logging.getLogger(__name__)

# Порядок команд = порядок в нижнем меню (owner видит все)
COMMAND_DEFS: dict[str, BotCommand] = {
    "start": BotCommand(command="start", description="Запуск и привязка по коду"),
    "help": BotCommand(command="help", description="Справка и команды"),
    "menu": BotCommand(command="menu", description="Главное меню"),
    "broadcast": BotCommand(command="broadcast", description="Рассылка сообщений"),
    "students": BotCommand(command="students", description="Ученики"),
    "add_student": BotCommand(command="add_student", description="Добавить ученика"),
    "expiring": BotCommand(command="expiring", description="Истекающие доступы"),
    "my_subjects": BotCommand(command="my_subjects", description="Мои предметы"),
    "add_theme": BotCommand(command="add_theme", description="Добавить тему"),
    "tasks": BotCommand(command="tasks", description="Задания темы"),
    "stats": BotCommand(command="stats", description="Статистика"),
}

_ALL_KEYS = tuple(COMMAND_DEFS.keys())
_BASE_KEYS = ("start", "help")

ROLE_COMMAND_KEYS: dict[str, tuple[str, ...]] = {
    # broadcast — ТОЛЬКО владельцу (между menu и students в нижнем меню)
    "owner": _BASE_KEYS + ("menu", "broadcast")
    + ("students", "add_student", "expiring", "my_subjects", "add_theme", "tasks"),
    "manager": _BASE_KEYS + ("students", "add_student", "expiring"),
    "teacher": _BASE_KEYS + ("my_subjects", "add_theme", "tasks"),
    "student": _BASE_KEYS + ("stats",),
    "guest": _BASE_KEYS,
}

# Команды default-скоупа (для новых чатов; чаты ролей получают свои)
DEFAULT_COMMANDS: list[BotCommand] = [COMMAND_DEFS["start"], COMMAND_DEFS["help"]]


def build_commands(roles) -> list[BotCommand]:
    """Команды для набора ролей: объединение без дублей.

    Порядок — как в COMMAND_DEFS (start, help, menu, ...); первичная роль
    первой, но дублей не бывает (совмещённый teacher+manager = 8 команд).
    """
    keys: list[str] = []
    for role in roles:
        for key in ROLE_COMMAND_KEYS.get(role, _BASE_KEYS):
            if key not in keys:
                keys.append(key)
    return [COMMAND_DEFS[k] for k in keys]


def commands_reply_kb(roles) -> ReplyKeyboardMarkup:
    """Reply-клавиатура команд роли: кнопки «на месте клавиатуры».

    Telegram показывает её как клавиши поверх обычной клавиатуры (не
    панель поверх строки ввода); переключатель клавиатуры появляется
    рядом с кнопкой стикеров. Кнопка — текст команды («/students» и т.п.),
    по нажатию отправляется команда.
    """
    commands = build_commands(roles)
    keyboard = [
        [KeyboardButton(text=f"/{c.command}")]
        for c in commands
    ]
    return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)


async def set_bot_commands(bot: Bot) -> None:
    """Регистрирует команды default-скоупа: /start + /help (для всех чатов).

    Ролевые команды выставляются на лету через sync_commands (чат-скоуп).
    """
    await bot.set_my_commands(commands=DEFAULT_COMMANDS)


async def sync_commands(bot: Bot | None, db_user) -> None:
    """Команды нижнего меню для КОНКРЕТНОГО чата (scope = chat).

    Вызывается в точках смены роли/первого контакта: /start, /menu,
    подтверждение привязки ученика, создание препода/менеджера владельцем.
    tg_id=None (пользователь ещё не заходил) — безопасный пропуск;
    сбой Telegram не роняет хендлер (лог).
    """
    if bot is None:
        return
    tg_id = getattr(db_user, "tg_id", None)
    if not tg_id:
        return
    try:
        await bot.set_my_commands(
            commands=build_commands(user_roles(db_user)),
            scope=BotCommandScopeChat(chat_id=tg_id),
        )
        # Кнопка «Меню» рядом с полем ввода (где сообщения/стикеры) →
        # по нажатию открывается список команд роли вместо клавиатуры
        await bot.set_chat_menu_button(
            chat_id=tg_id,
            menu_button=MenuButtonCommands(),
        )
    except Exception:
        logger.exception("Не удалось обновить команды пользователя (tg_id=%s)", tg_id)


def create_bot() -> Bot:
    """Создаёт Bot с HTML по умолчанию. Вызывается после settings.validate()."""
    return Bot(
        token=get_settings().BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )


dp = Dispatcher()
# Контекст пользователя для каждого апдейта (роль из БД, блокировка
# деактивированных) — см. app/middlewares/user.py
dp.update.middleware(UserContextMiddleware())
