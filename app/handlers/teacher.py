"""Преподаватель: предметы, темы, визард «Добавить задание» (ТЗ разделы 6, 8).

Все хендлеры — под @require_role("teacher", "owner") (ошибка №9).
Права на КОНКРЕТНУЮ тему/предмет/задание проверяются по БД через сервисы
(teacher.py): препод работает только со своими предметами, владелец —
с любыми (кроме скрытых — устаревшие кнопки по скрытому предмету
не работают, раздел 0 дефект 2). Вход в визарды — с state.clear(),
выход — тоже (ошибка №12). Перерисовка экранов — через safe_edit;
текстовые шаги визардов — с ~F.text.startswith("/") (команды не теряются,
раздел 8.6); неожиданный тип сообщения (фото/документ вместо текста) —
подсказка, состояние не меняется (раздел 0 дефект 3).

Визард задания (ТЗ раздел 8): question → options → correct → exp_input →
preview (UX-пакет: шаг объяснения без выбора «текст/фото»). После каждого
приёма данных состояние ОБЯЗАНО смениться; кнопки-«ожидаю ввод» явно
ставят состояние (ошибка №6);
«Сохранить» защищён от двойного клика (ошибка №8).
"""
import logging

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select

from app.database import get_session_factory
from app.keyboards.inline import (
    confirm_del_subtheme_kb,
    confirm_del_task_kb,
    confirm_del_theme_kb,
    exp_more_kb,
    exp_pass_kb,
    options_done_kb,
    options_more_kb,
    options_pick_kb,
    preview_kb,
    subtheme_menu_kb,
    subtheme_pick_kb,
    subthemes_list_kb,
    task_card_kb,
    teacher_subjects_kb,
    tasks_menu_kb,
    theme_list_kb,
    theme_menu_kb,
    themes_pick_kb,
)
from app.models import Subject, Subtheme, Theme
from app.services import teacher as teacher_svc
from app.states import (
    AddSubthemeStates,
    AddTaskStates,
    AddThemeStates,
    DeleteSubthemeStates,
    DeleteThemeStates,
    RenameSubthemeStates,
    RenameThemeStates,
)
from app.utils.format import esc
from app.utils.messages import safe_edit
from app.utils.roles import require_role

logger = logging.getLogger(__name__)

router = Router()

MSG_STALE = "Кнопка устарела"
MSG_STALE_FULL = "Кнопка устарела, начни заново."
MSG_THEME_NOT_FOUND = "Тема не найдена"
MSG_SUBJECT_NOT_FOUND = "Предмет не найден"
MSG_TASK_NOT_FOUND = "Задание не найдено"

TEXT_NO_SUBJECTS_TEACHER = "Предметы ещё не назначены, обратись к владельцу"
TEXT_NO_SUBJECTS_OWNER = "Пока нет предметов — создай первый"
TEXT_THEMES_EMPTY = "Тем пока нет. Добавь первую кнопкой ниже."
TEXT_TASKS_EMPTY = "Заданий пока нет. Добавь первое кнопкой ниже."
TEXT_ADD_THEME_OK = "✅ Тема «{name}» создана."
TEXT_RENAME_PROMPT = "Текущее название: «{name}».\nПришли новое название:"
TEXT_RENAME_OK = "✅ Тема переименована в «{name}»."
TEXT_DEL_THEME_ASK = (
    "🗑 Удалить тему <b>«{name}»</b> вместе со всеми заданиями?\n\n"
    "Напиши название темы текстом, чтобы удалить её с заданиями."
)
TEXT_DEL_THEME_NOT_MATCH = (
    "Название не совпало — тема не удалена. Если хочешь удалить, "
    "напиши название точно как в теме, или нажми «Отмена»."
)
TEXT_DEL_THEME_OK = "🗑 Тема «{name}» удалена вместе с заданиями."
TEXT_THEME_OPEN = "✅ Тема открыта — ученики её видят."
TEXT_THEME_CLOSED = "🔒 Тема закрыта."
ASK_THEME_TITLE = "Название темы:"

# Режим «🎲 Открыть все» (текущий заход)
MODE_RANDOM_ON = "🎲 Режим «открыть все»: задания выдаются в случайном порядке."
MODE_SEQUENTIAL_ON = "🔓 Режим «по порядку»: задания идут по порядку."

# Подтемы (текущий заход)
TEXT_SUBS_EMPTY = "Подтем пока нет. Добавь первую кнопкой ниже."
ASK_SUB_TITLE = "Название подтемы:"
TEXT_SUB_ADDED = "✅ Подтема «{name}» создана."
TEXT_SUB_RENAME_PROMPT = "Текущее название: «{name}».\nПришли новое название:"
TEXT_SUB_RENAME_OK = "✅ Подтема переименована в «{name}»."
TEXT_SUB_DEL_ASK = (
    "🗑 Удалить подтему <b>«{name}»</b> вместе с её заданиями "
    "(всего {count})?\n\n"
    "Напиши название подтемы текстом, чтобы удалить её с заданиями."
)
TEXT_SUB_DEL_NOT_MATCH = (
    "Название не совпало — подтема не удалена. Если хочешь удалить, "
    "напиши название точно как в подтеме, или нажми «Отмена»."
)
TEXT_SUB_DEL_OK = "🗑 Подтема «{name}» удалена вместе с заданиями."
MSG_SUB_NOT_FOUND = "Подтема не найдена"
TEXT_HINT_SUB_NAME = "Название подтемы нужно текстом. Пришли текст:"
TEXT_HINT_SUB_RENAME = "Новое название нужно текстом. Пришли текст:"
TEXT_HINT_SUB_DELETE = "Пришли название подтемы текстом, чтобы удалить её:"
ASK_SUB_PICK = "В какую подтему добавить задание?"
TEXT_SUB_HINT_PICK = "Выбери подтему кнопками ниже."

# Раздел 0 дефект 3: подсказки на неожиданный тип сообщения в текстовых шагах
TEXT_HINT_THEME_NAME = "Название темы нужно текстом. Пришли текст:"
TEXT_HINT_RENAME = "Новое название нужно текстом. Пришли текст:"
TEXT_HINT_DELETE = "Пришли название темы текстом, чтобы удалить её:"

# Тексты визарда «Добавить задание» (ТЗ раздел 8)
ASK_QUESTION = "Отправь вопрос текстом или фото с текстом:"
ASK_OPTIONS = "Присылай варианты по одному сообщением (2–4):"
TEXT_OPTION_ADDED = "✅ Вариант {n} добавлен"
TEXT_OPTION_ADDED_MAX = "✅ Вариант {n} добавлен (достигнут максимум — 4)."
TEXT_OPTION_MAX = "Уже 4 варианта — это максимум. Нажми «✅ Готово», если достаточно."
ALERT_OPTIONS_MIN = "Нужно минимум 2 варианта"
ASK_CORRECT = "Какой вариант правильный?"
EXP_CHOICE_MSG = "💡 Скинь объяснение — можно текстом или фото. Или пропусти"
TEXT_EXP_ADDED = "✅ Объяснение добавлено."
TEXT_EXP_MORE = "Добавь ещё текст или фото:"
TEXT_HINT_EXP_INPUT = "Скинь объяснение текстом или фото (или нажми «Пропустить»):"
TEXT_EMPTY_QUESTION = "Вопрос не может быть пустым — отправь текст или фото."
TEXT_QUESTION_TOO_LONG = (
    f"Слишком длинный вопрос (максимум {teacher_svc.QUESTION_TEXT_MAX} символов)."
)
TEXT_OPTION_TOO_LONG = (
    f"Слишком длинный вариант (максимум {teacher_svc.OPTION_TEXT_MAX} символов)."
)
TEXT_EMPTY_OPTION = "Вариант не может быть пустым — пришли текст."
TASK_SAVED = "✅ Задание сохранено!"
SAVING_NOW = "Уже сохраняю…"
MSG_SAVE_FAILED = "Не удалось сохранить задание. Попробуй ещё раз."
PREVIEW_OPTION_HINT = "Это превью — правильный ответ не показывается"
TEXT_PREVIEW_QUESTION = "📝 <b>Вопрос:</b>"
TEXT_PREVIEW_OPTIONS = "🔘 <b>Варианты:</b>"

# Карточка задания (ТЗ раздел 8.5 / промт захода 5, раздел 3)
TEXT_TASK_HIDDEN = "🚫 Задание скрыто — ученикам не выдаётся."
TEXT_TASK_SHOWN = "✅ Задание снова видно ученикам."
TEXT_DEL_TASK_ASK = (
    "🗑 Удалить задание «{q}»?\n\n"
    "Прогресс учеников по этому заданию тоже удалится."
)
TEXT_TASK_DELETED = "🗑 Задание удалено."

# Максимум символов вопроса в строке списка заданий
_QUESTION_PREVIEW = 50

_OPTION_LETTERS = "АБВГ"


def _parse_id(data: str, idx: int) -> int | None:
    try:
        return int(data.split(":")[idx])
    except (ValueError, IndexError):
        return None


def _back_cb(db_user) -> str:
    """«← Назад» по роли: владелец возвращается в своё меню."""
    role = getattr(db_user, "role", "teacher")
    return f"menu:back:{role}:0"


def _theme_line(theme) -> str:
    status = "🔒 закрыта"
    if theme.is_open:
        status = "🎲 открыть все" if theme.mode == "random" else "🔓 по порядку"
    return f"{status} {esc(theme.title)}"


def _question_preview(task) -> str:
    """Вопрос строкой списка: текст (обрезанный) или «фото-вопрос»."""
    if task.question_text:
        text = task.question_text.strip().replace("\n", " ")
        if len(text) > _QUESTION_PREVIEW:
            text = text[:_QUESTION_PREVIEW].rstrip() + "…"
        return text
    if task.question_photo_id:
        return "фото-вопрос"
    return "без вопроса"


def _question_for_card(task) -> str:
    """Вопрос для карточки задания и подтверждения удаления."""
    if task.question_text:
        return " ".join(task.question_text.strip().split())
    if task.question_photo_id:
        return "фото-вопрос"
    return "без вопроса"


async def _wizard_data(state: FSMContext) -> dict | None:
    """Данные визарда задания. None — визард неактивен (устар. кнопка).

    Устаревшую кнопку гасим с очисткой state (ошибка №12 — без «призрачных»
    визардов и task_id из других флоу).
    """
    data = await state.get_data()
    if data.get("theme_id") is None:
        await state.clear()
        return None
    return data


# ==========================================================================
# «📚 Мои предметы»
# ==========================================================================
async def _subjects_view(db_user) -> tuple[str, object]:
    async with get_session_factory()() as session:
        data = await teacher_svc.list_teacher_subjects(session, db_user.id)
    if not data:
        text = (
            TEXT_NO_SUBJECTS_OWNER
            if db_user.role == "owner"
            else TEXT_NO_SUBJECTS_TEACHER
        )
        return text, teacher_subjects_kb([], back_cb=_back_cb(db_user))
    lines = ["📚 Мои предметы", ""]
    for item in data:
        lines.append(f"{esc(item['subject'].name)}")
        for theme, _count in item["themes"]:
            lines.append(_theme_line(theme))
    kb = teacher_subjects_kb(data, back_cb=_back_cb(db_user))
    return "\n".join(lines), kb


@router.message(Command("my_subjects"))
@require_role("teacher", "owner")
async def cmd_my_subjects(
    message: Message, state: FSMContext = None, db_user=None
) -> None:
    if state is not None:
        await state.clear()  # раздел 0 дефект 1: команда отменяет визард
    text, kb = await _subjects_view(db_user)
    await message.answer(text, reply_markup=kb)


@router.callback_query(F.data == "menu:teacher:subjects:0")
@require_role("teacher", "owner")
async def cb_my_subjects(callback: CallbackQuery, db_user=None) -> None:
    await callback.answer()
    text, kb = await _subjects_view(db_user)
    await safe_edit(callback.message, text, kb)


# ==========================================================================
# Список тем предмета
# ==========================================================================
async def _theme_list_view(subject_id: int, db_user) -> tuple[str, object] | None:
    async with get_session_factory()() as session:
        subject = await session.get(Subject, subject_id)
        if subject is None:
            return None
        if not await teacher_svc.can_manage_subject(session, db_user.id, subject_id):
            # включает и скрытый предмет (раздел 0 дефект 2)
            return None
        themes = (
            await session.scalars(
                select(Theme)
                .where(Theme.subject_id == subject_id)
                .order_by(Theme.order, Theme.id)
            )
        ).all()
    lines = [f"{esc(subject.name)}", ""]
    if not themes:
        lines.append(TEXT_THEMES_EMPTY)
    else:
        for theme in themes:
            lines.append(_theme_line(theme))
    kb = theme_list_kb(themes, subject_id, back_cb=_back_cb(db_user))
    return "\n".join(lines), kb


@router.callback_query(F.data.startswith("tch:subj:"))
@require_role("teacher", "owner")
async def cb_theme_list(callback: CallbackQuery, db_user=None) -> None:
    subject_id = _parse_id(callback.data, 2)
    if subject_id is None:
        await callback.answer(MSG_STALE_FULL, show_alert=True)
        return
    view = await _theme_list_view(subject_id, db_user)
    if view is None:
        await callback.answer(MSG_SUBJECT_NOT_FOUND, show_alert=True)
        return
    await callback.answer()
    await safe_edit(callback.message, *view)


# ==========================================================================
# Меню темы
# ==========================================================================
async def _theme_menu_view(theme) -> tuple[str, object]:
    async with get_session_factory()() as session:
        count = await teacher_svc.count_theme_tasks(session, theme.id)
    text = f"📚 {esc(theme.title)}\n\n📝 Заданий: {count}"
    return text, theme_menu_kb(theme)


@router.callback_query(F.data.startswith("tch:theme:"))
@require_role("teacher", "owner")
async def cb_theme_menu(callback: CallbackQuery, db_user=None) -> None:
    theme_id = _parse_id(callback.data, 2)
    if theme_id is None:
        await callback.answer(MSG_STALE_FULL, show_alert=True)
        return
    async with get_session_factory()() as session:
        theme = await teacher_svc.get_theme_for_teacher(session, db_user.id, theme_id)
    if theme is None:
        await callback.answer(MSG_THEME_NOT_FOUND, show_alert=True)
        return
    text, kb = await _theme_menu_view(theme)
    await callback.answer()
    await safe_edit(callback.message, text, kb)


@router.callback_query(F.data.startswith("tch:th_open:"))
@require_role("teacher", "owner")
async def cb_theme_toggle_open(callback: CallbackQuery, db_user=None) -> None:
    theme_id = _parse_id(callback.data, 2)
    if theme_id is None:
        await callback.answer(MSG_STALE_FULL, show_alert=True)
        return
    async with get_session_factory()() as session:
        theme = await teacher_svc.get_theme_for_teacher(session, db_user.id, theme_id)
        if theme is None:
            await callback.answer(MSG_THEME_NOT_FOUND, show_alert=True)
            return
        is_open = await teacher_svc.toggle_theme_open(session, theme_id)
    if is_open is None:
        # гонка: тема удалена между проверкой и toggle (раздел 0 дефект 4)
        await callback.answer(MSG_THEME_NOT_FOUND, show_alert=True)
        return
    text, kb = await _theme_menu_view(theme)
    if not await safe_edit(callback.message, text, kb):
        await callback.answer(MSG_THEME_NOT_FOUND, show_alert=True)
        return
    await callback.answer(TEXT_THEME_OPEN if is_open else TEXT_THEME_CLOSED)


# ==========================================================================
# Добавить тему (визард: название)
# ==========================================================================
async def _start_add_theme(
    message: Message, state: FSMContext, subject_id: int, db_user
) -> None:
    """Вход в визард: проверка доступа к предмету, затем «Название темы:»."""
    async with get_session_factory()() as session:
        subject = await session.get(Subject, subject_id)
        if subject is None or not subject.is_active:
            await message.answer(MSG_SUBJECT_NOT_FOUND)
            return
        if not await teacher_svc.can_manage_subject(session, db_user.id, subject_id):
            await message.answer("У тебя нет доступа к этому предмету.")
            return
    await state.clear()  # ошибка №12: вход в визард — с чистого листа
    await state.update_data(subject_id=subject_id)
    await state.set_state(AddThemeStates.name)
    await message.answer(ASK_THEME_TITLE)


@router.callback_query(F.data.startswith("tch:add_theme:"))
@require_role("teacher", "owner")
async def cb_add_theme(
    callback: CallbackQuery, state: FSMContext, db_user=None
) -> None:
    subject_id = _parse_id(callback.data, 2)
    if subject_id is None:
        await callback.answer(MSG_STALE_FULL, show_alert=True)
        return
    await callback.answer()
    await _start_add_theme(callback.message, state, subject_id, db_user)


@router.message(Command("add_theme"))
@require_role("teacher", "owner")
async def cmd_add_theme(
    message: Message, state: FSMContext = None, db_user=None
) -> None:
    """/add_theme: один предмет → сразу название; несколько — выбрать."""
    if state is not None:
        await state.clear()  # раздел 0 дефект 1: команда отменяет визард
    async with get_session_factory()() as session:
        data = await teacher_svc.list_teacher_subjects(session, db_user.id)
    if not data:
        text = (
            TEXT_NO_SUBJECTS_OWNER
            if db_user.role == "owner"
            else TEXT_NO_SUBJECTS_TEACHER
        )
        await message.answer(text)
        return
    if len(data) == 1:
        await _start_add_theme(message, state, data[0]["subject"].id, db_user)
        return
    builder = InlineKeyboardBuilder()
    for item in data:
        builder.button(
            text=f"{esc(item['subject'].name)}",
            callback_data=f"tch:add_theme:{item['subject'].id}:0",
        )
    builder.button(text="← Назад", callback_data=_back_cb(db_user))
    builder.adjust(1)
    await message.answer(
        "В какой предмет добавить тему?", reply_markup=builder.as_markup()
    )


@router.message(AddThemeStates.name, ~F.text.startswith("/"))
@require_role("teacher", "owner")
async def on_add_theme_name(
    message: Message, state: FSMContext, db_user=None
) -> None:
    state_data = await state.get_data()
    subject_id = state_data.get("subject_id")
    if subject_id is None:
        await message.answer(MSG_STALE_FULL)
        await state.clear()
        return
    try:
        async with get_session_factory()() as session:
            theme = await teacher_svc.add_theme(
                session, db_user.id, subject_id, message.text or ""
            )
    except ValueError as exc:
        await message.answer(str(exc))
        return
    await message.answer(TEXT_ADD_THEME_OK.format(name=esc(theme.title)))
    await state.clear()  # ошибка №12: выход из визарда
    text, kb = await _theme_menu_view(theme)
    await message.answer(text, reply_markup=kb)


@router.message(AddThemeStates.name, F.photo | F.document)
@require_role("teacher", "owner")
async def on_add_theme_name_nontext(message: Message, db_user=None) -> None:
    """Раздел 0 дефект 3: фото/документ в текстовом шаге → подсказка."""
    await message.answer(TEXT_HINT_THEME_NAME)


# ==========================================================================
# Переименовать тему
# ==========================================================================
@router.callback_query(F.data.startswith("tch:rename:"))
@require_role("teacher", "owner")
async def cb_rename_theme(
    callback: CallbackQuery, state: FSMContext, db_user=None
) -> None:
    theme_id = _parse_id(callback.data, 2)
    if theme_id is None:
        await callback.answer(MSG_STALE_FULL, show_alert=True)
        return
    async with get_session_factory()() as session:
        theme = await teacher_svc.get_theme_for_teacher(session, db_user.id, theme_id)
    if theme is None:
        await callback.answer(MSG_THEME_NOT_FOUND, show_alert=True)
        return
    await callback.answer()
    await state.clear()
    await state.update_data(theme_id=theme.id)
    await state.set_state(RenameThemeStates.name)
    # раздел 0 дефект 5: перерисовка, а не новое сообщение (не копим)
    await safe_edit(
        callback.message, TEXT_RENAME_PROMPT.format(name=esc(theme.title)), None
    )


@router.message(RenameThemeStates.name, ~F.text.startswith("/"))
@require_role("teacher", "owner")
async def on_rename_theme_name(
    message: Message, state: FSMContext, db_user=None
) -> None:
    state_data = await state.get_data()
    theme_id = state_data.get("theme_id")
    if theme_id is None:
        await message.answer(MSG_STALE_FULL)
        await state.clear()
        return
    try:
        async with get_session_factory()() as session:
            ok = await teacher_svc.rename_theme(session, theme_id, message.text or "")
            theme = await teacher_svc.get_theme_for_teacher(
                session, db_user.id, theme_id
            )
    except ValueError as exc:
        await message.answer(str(exc))
        return
    if not ok or theme is None:
        await message.answer(MSG_THEME_NOT_FOUND)
        await state.clear()
        return
    await message.answer(TEXT_RENAME_OK.format(name=esc(theme.title)))
    await state.clear()
    text, kb = await _theme_menu_view(theme)
    await message.answer(text, reply_markup=kb)


@router.message(RenameThemeStates.name, F.photo | F.document)
@require_role("teacher", "owner")
async def on_rename_theme_name_nontext(message: Message, db_user=None) -> None:
    """Раздел 0 дефект 3."""
    await message.answer(TEXT_HINT_RENAME)


# ==========================================================================
# Удалить тему (подтверждение точным названием)
# ==========================================================================
@router.callback_query(F.data.startswith("tch:delete:"))
@require_role("teacher", "owner")
async def cb_delete_theme(
    callback: CallbackQuery, state: FSMContext, db_user=None
) -> None:
    theme_id = _parse_id(callback.data, 2)
    if theme_id is None:
        await callback.answer(MSG_STALE_FULL, show_alert=True)
        return
    async with get_session_factory()() as session:
        theme = await teacher_svc.get_theme_for_teacher(session, db_user.id, theme_id)
    if theme is None:
        await callback.answer(MSG_THEME_NOT_FOUND, show_alert=True)
        return
    await callback.answer()
    await state.clear()
    await state.update_data(theme_id=theme.id)
    await state.set_state(DeleteThemeStates.confirm)
    await safe_edit(
        callback.message,
        TEXT_DEL_THEME_ASK.format(name=esc(theme.title)),
        confirm_del_theme_kb(theme.id),
    )


@router.callback_query(F.data.startswith("tch:del_no:"))
@require_role("teacher", "owner")
async def cb_delete_cancel(
    callback: CallbackQuery, state: FSMContext, db_user=None
) -> None:
    theme_id = _parse_id(callback.data, 2)
    if theme_id is None:
        await callback.answer(MSG_STALE_FULL, show_alert=True)
        return
    await state.clear()
    await callback.answer()
    async with get_session_factory()() as session:
        theme = await teacher_svc.get_theme_for_teacher(session, db_user.id, theme_id)
    if theme is None:
        await safe_edit(callback.message, MSG_THEME_NOT_FOUND, None)
        return
    text, kb = await _theme_menu_view(theme)
    await safe_edit(callback.message, text, kb)


@router.message(DeleteThemeStates.confirm, ~F.text.startswith("/"))
@require_role("teacher", "owner")
async def on_delete_confirm(
    message: Message, state: FSMContext, db_user=None
) -> None:
    state_data = await state.get_data()
    theme_id = state_data.get("theme_id")
    if theme_id is None:
        await message.answer(MSG_STALE_FULL)
        await state.clear()
        return
    async with get_session_factory()() as session:
        theme = await teacher_svc.get_theme_for_teacher(session, db_user.id, theme_id)
    if theme is None:
        await message.answer(MSG_THEME_NOT_FOUND)
        await state.clear()
        return
    if (message.text or "").strip() != theme.title:
        await message.answer(TEXT_DEL_THEME_NOT_MATCH)
        return

    async with get_session_factory()() as session:
        await teacher_svc.delete_theme(session, theme.id)
    await message.answer(TEXT_DEL_THEME_OK.format(name=esc(theme.title)))
    await state.clear()
    text, kb = await _subjects_view(db_user)
    await message.answer(text, reply_markup=kb)


@router.message(DeleteThemeStates.confirm, F.photo | F.document)
@require_role("teacher", "owner")
async def on_delete_confirm_nontext(message: Message, db_user=None) -> None:
    """Раздел 0 дефект 3."""
    await message.answer(TEXT_HINT_DELETE)


# ==========================================================================
# Режим темы: «🔓 По порядку» ↔ «🎲 Открыть все (рандом)» (текущий заход)
# ==========================================================================
@router.callback_query(F.data.startswith("tch:mode:"))
@require_role("teacher", "owner")
async def cb_theme_mode(callback: CallbackQuery, db_user=None) -> None:
    theme_id = _parse_id(callback.data, 2)
    if theme_id is None:
        await callback.answer(MSG_STALE_FULL, show_alert=True)
        return
    async with get_session_factory()() as session:
        theme = await teacher_svc.get_theme_for_teacher(session, db_user.id, theme_id)
        if theme is None:
            await callback.answer(MSG_THEME_NOT_FOUND, show_alert=True)
            return
        mode = await teacher_svc.toggle_theme_mode(session, theme_id)
    if mode is None:
        # гонка: тема удалена между проверкой и toggle
        await callback.answer(MSG_THEME_NOT_FOUND, show_alert=True)
        return
    text, kb = await _theme_menu_view(theme)
    if not await safe_edit(callback.message, text, kb):
        await callback.answer(MSG_THEME_NOT_FOUND, show_alert=True)
        return
    await callback.answer(
        MODE_RANDOM_ON if mode == "random" else MODE_SEQUENTIAL_ON
    )


# ==========================================================================
# Подтемы (текущий заход): список, меню, добавить/переименовать/удалить
# ==========================================================================
async def _subthemes_view(theme_id: int, db_user) -> tuple[str, object] | None:
    """Экран «🔖 Подтемы темы». None — темы нет или нет доступа."""
    async with get_session_factory()() as session:
        theme = await teacher_svc.get_theme_for_teacher(session, db_user.id, theme_id)
        if theme is None:
            return None
        subthemes = await teacher_svc.list_theme_subthemes(session, theme_id)

    lines = [f"🔖 <b>Подтемы темы «{esc(theme.title)}»</b>", ""]
    if subthemes:
        for item in subthemes:
            count = item["count"]
            label = esc(item["subtheme"].title)
            lines.append(f"🔖 {label}{f' — {count} зад.' if count else ''}")
    else:
        lines.append(TEXT_SUBS_EMPTY)
    return "\n".join(lines), subthemes_list_kb(
        subthemes, theme_id
    )


@router.callback_query(F.data.startswith("tch:subs:"))
@require_role("teacher", "owner")
async def cb_subthemes(callback: CallbackQuery, db_user=None) -> None:
    theme_id = _parse_id(callback.data, 2)
    if theme_id is None:
        await callback.answer(MSG_STALE_FULL, show_alert=True)
        return
    view = await _subthemes_view(theme_id, db_user)
    if view is None:
        await callback.answer(MSG_THEME_NOT_FOUND, show_alert=True)
        return
    await callback.answer()
    await safe_edit(callback.message, *view)


async def _subtheme_menu_view(subtheme) -> tuple[str, object]:
    async with get_session_factory()() as session:
        count = await teacher_svc.count_subtheme_tasks(session, subtheme.id)
    text = f"🔖 {esc(subtheme.title)}\n\n📝 Заданий: {count}"
    return text, subtheme_menu_kb(subtheme)


@router.callback_query(F.data.startswith("tch:sub:"))
@require_role("teacher", "owner")
async def cb_subtheme_menu(callback: CallbackQuery, db_user=None) -> None:
    subtheme_id = _parse_id(callback.data, 2)
    if subtheme_id is None:
        await callback.answer(MSG_STALE_FULL, show_alert=True)
        return
    async with get_session_factory()() as session:
        subtheme = await teacher_svc.get_subtheme_for_teacher(
            session, db_user.id, subtheme_id
        )
    if subtheme is None:
        await callback.answer(MSG_SUB_NOT_FOUND, show_alert=True)
        return
    text, kb = await _subtheme_menu_view(subtheme)
    await callback.answer()
    await safe_edit(callback.message, text, kb)


@router.callback_query(F.data.startswith("tch:sub_add:"))
@require_role("teacher", "owner")
async def cb_sub_add(callback: CallbackQuery, state: FSMContext, db_user=None) -> None:
    theme_id = _parse_id(callback.data, 2)
    if theme_id is None:
        await callback.answer(MSG_STALE_FULL, show_alert=True)
        return
    async with get_session_factory()() as session:
        theme = await teacher_svc.get_theme_for_teacher(session, db_user.id, theme_id)
    if theme is None:
        await callback.answer(MSG_THEME_NOT_FOUND, show_alert=True)
        return
    await state.clear()  # ошибка №12: вход в визард — с чистого листа
    await state.update_data(theme_id=theme.id)
    await state.set_state(AddSubthemeStates.name)
    await callback.answer()
    await callback.message.answer(ASK_SUB_TITLE)


@router.message(AddSubthemeStates.name, ~F.text.startswith("/"))
@require_role("teacher", "owner")
async def on_sub_add_name(
    message: Message, state: FSMContext, db_user=None
) -> None:
    state_data = await state.get_data()
    theme_id = state_data.get("theme_id")
    if theme_id is None:
        await message.answer(MSG_STALE_FULL)
        await state.clear()
        return
    try:
        async with get_session_factory()() as session:
            subtheme = await teacher_svc.add_subtheme(
                session, db_user.id, theme_id, message.text or ""
            )
    except ValueError as exc:
        await message.answer(str(exc))
        return
    await message.answer(TEXT_SUB_ADDED.format(name=esc(subtheme.title)))
    await state.clear()  # ошибка №12: выход из визарда
    view = await _subthemes_view(theme_id, db_user)
    if view is not None:
        text, kb = view
        await message.answer(text, reply_markup=kb)


@router.message(AddSubthemeStates.name, F.photo | F.document)
@require_role("teacher", "owner")
async def on_sub_add_name_nontext(message: Message, db_user=None) -> None:
    """Фото/документ в текстовом шаге → подсказка (раздел 0 дефект 3)."""
    await message.answer(TEXT_HINT_SUB_NAME)


@router.callback_query(F.data.startswith("tch:sub_rename:"))
@require_role("teacher", "owner")
async def cb_sub_rename(
    callback: CallbackQuery, state: FSMContext, db_user=None
) -> None:
    subtheme_id = _parse_id(callback.data, 2)
    if subtheme_id is None:
        await callback.answer(MSG_STALE_FULL, show_alert=True)
        return
    async with get_session_factory()() as session:
        subtheme = await teacher_svc.get_subtheme_for_teacher(
            session, db_user.id, subtheme_id
        )
    if subtheme is None:
        await callback.answer(MSG_SUB_NOT_FOUND, show_alert=True)
        return
    await callback.answer()
    await state.clear()
    await state.update_data(subtheme_id=subtheme.id)
    await state.set_state(RenameSubthemeStates.name)
    await safe_edit(
        callback.message,
        TEXT_SUB_RENAME_PROMPT.format(name=esc(subtheme.title)),
        None,
    )


@router.message(RenameSubthemeStates.name, ~F.text.startswith("/"))
@require_role("teacher", "owner")
async def on_sub_rename_name(
    message: Message, state: FSMContext, db_user=None
) -> None:
    state_data = await state.get_data()
    subtheme_id = state_data.get("subtheme_id")
    if subtheme_id is None:
        await message.answer(MSG_STALE_FULL)
        await state.clear()
        return
    try:
        async with get_session_factory()() as session:
            ok = await teacher_svc.rename_subtheme(
                session, subtheme_id, message.text or ""
            )
            subtheme = await teacher_svc.get_subtheme_for_teacher(
                session, db_user.id, subtheme_id
            )
    except ValueError as exc:
        await message.answer(str(exc))
        return
    if not ok or subtheme is None:
        await message.answer(MSG_SUB_NOT_FOUND)
        await state.clear()
        return
    await message.answer(TEXT_SUB_RENAME_OK.format(name=esc(subtheme.title)))
    await state.clear()
    text, kb = await _subtheme_menu_view(subtheme)
    await message.answer(text, reply_markup=kb)


@router.message(RenameSubthemeStates.name, F.photo | F.document)
@require_role("teacher", "owner")
async def on_sub_rename_name_nontext(message: Message, db_user=None) -> None:
    await message.answer(TEXT_HINT_SUB_RENAME)


@router.callback_query(F.data.startswith("tch:sub_del:"))
@require_role("teacher", "owner")
async def cb_sub_del(
    callback: CallbackQuery, state: FSMContext, db_user=None
) -> None:
    subtheme_id = _parse_id(callback.data, 2)
    if subtheme_id is None:
        await callback.answer(MSG_STALE_FULL, show_alert=True)
        return
    async with get_session_factory()() as session:
        subtheme = await teacher_svc.get_subtheme_for_teacher(
            session, db_user.id, subtheme_id
        )
    if subtheme is None:
        await callback.answer(MSG_SUB_NOT_FOUND, show_alert=True)
        return
    async with get_session_factory()() as session:
        count = await teacher_svc.count_subtheme_tasks(session, subtheme.id)
    await callback.answer()
    await state.clear()
    await state.update_data(subtheme_id=subtheme.id)
    await state.set_state(DeleteSubthemeStates.confirm)
    await safe_edit(
        callback.message,
        TEXT_SUB_DEL_ASK.format(name=esc(subtheme.title), count=count),
        confirm_del_subtheme_kb(subtheme.id),
    )


@router.callback_query(F.data.startswith("tch:sub_del_no:"))
@require_role("teacher", "owner")
async def cb_sub_del_no(
    callback: CallbackQuery, state: FSMContext, db_user=None
) -> None:
    subtheme_id = _parse_id(callback.data, 2)
    if subtheme_id is None:
        await callback.answer(MSG_STALE_FULL, show_alert=True)
        return
    await state.clear()
    await callback.answer()
    async with get_session_factory()() as session:
        subtheme = await teacher_svc.get_subtheme_for_teacher(
            session, db_user.id, subtheme_id
        )
    if subtheme is None:
        await safe_edit(callback.message, MSG_SUB_NOT_FOUND, None)
        return
    text, kb = await _subtheme_menu_view(subtheme)
    await safe_edit(callback.message, text, kb)


@router.message(DeleteSubthemeStates.confirm, ~F.text.startswith("/"))
@require_role("teacher", "owner")
async def on_sub_del_confirm(
    message: Message, state: FSMContext, db_user=None
) -> None:
    state_data = await state.get_data()
    subtheme_id = state_data.get("subtheme_id")
    if subtheme_id is None:
        await message.answer(MSG_STALE_FULL)
        await state.clear()
        return
    async with get_session_factory()() as session:
        subtheme = await teacher_svc.get_subtheme_for_teacher(
            session, db_user.id, subtheme_id
        )
    if subtheme is None:
        await message.answer(MSG_SUB_NOT_FOUND)
        await state.clear()
        return
    if (message.text or "").strip() != subtheme.title:
        await message.answer(TEXT_SUB_DEL_NOT_MATCH)
        return

    theme_id = subtheme.theme_id
    async with get_session_factory()() as session:
        await teacher_svc.delete_subtheme(session, subtheme.id)
    await message.answer(TEXT_SUB_DEL_OK.format(name=esc(subtheme.title)))
    await state.clear()
    view = await _subthemes_view(theme_id, db_user)
    if view is not None:
        text, kb = view
        await message.answer(text, reply_markup=kb)


@router.message(DeleteSubthemeStates.confirm, F.photo | F.document)
@require_role("teacher", "owner")
async def on_sub_del_confirm_nontext(message: Message, db_user=None) -> None:
    await message.answer(TEXT_HINT_SUB_DELETE)


# ==========================================================================
# Список заданий темы (строки текстом; кнопки — «➕ Добавить задание» и назад)
# ==========================================================================
async def _theme_tasks_view(theme_id: int, db_user) -> tuple[str, object] | None:
    """Экран «📝 Задания темы». None — темы нет или нет доступа.

    Группировка по подтемам (текущий заход): подтемы в порядке следования
    (заголовок «🔖 …»), задания без подтемы — в конце; нумерация сквозная.
    """
    async with get_session_factory()() as session:
        theme = await teacher_svc.get_theme_for_teacher(session, db_user.id, theme_id)
        if theme is None:
            return None
        tasks = await teacher_svc.list_theme_tasks(session, theme_id)
        subthemes = await teacher_svc.list_theme_subthemes(session, theme_id)

    lines = [f"📝 <b>Задания темы «{esc(theme.title)}»</b>", ""]
    if tasks:
        n = 1
        by_subtheme = {}
        for row in tasks:
            sub = row["subtheme"]
            by_subtheme.setdefault(sub.id if sub is not None else None, []).append(row)
        for item in subthemes:
            rows = by_subtheme.get(item["subtheme"].id, [])
            if not rows:
                continue
            lines.append(f"🔖 {esc(item['subtheme'].title)}")
            for row in rows:
                mark = "✅" if row["task"].is_active else "🚫"
                lines.append(f"{n}. {mark} {esc(_question_preview(row['task']))}")
                n += 1
        for row in by_subtheme.get(None, []):
            mark = "✅" if row["task"].is_active else "🚫"
            lines.append(f"{n}. {mark} {esc(_question_preview(row['task']))}")
            n += 1
    else:
        lines.append(TEXT_TASKS_EMPTY)
    return "\n".join(lines), tasks_menu_kb(theme_id)


@router.message(Command("tasks"))
@require_role("teacher", "owner")
async def cmd_tasks(message: Message, state: FSMContext = None, db_user=None) -> None:
    """/tasks — выбор темы → список заданий."""
    if state is not None:
        await state.clear()  # раздел 0 дефект 1: команда отменяет визард
    async with get_session_factory()() as session:
        data = await teacher_svc.list_teacher_subjects(session, db_user.id)
        themes = [theme for item in data for theme, _count in item["themes"]]
    if not themes:
        await message.answer(
            "Тем пока нет. Добавь первую в «📚 Мои предметы».",
            reply_markup=InlineKeyboardBuilder()
            .button(text="← Назад", callback_data=_back_cb(db_user))
            .as_markup(),
        )
        return
    await message.answer(
        "📝 Выбери тему:",
        reply_markup=themes_pick_kb(themes, back_cb=_back_cb(db_user)),
    )


@router.callback_query(F.data.startswith("tch:tasks:"))
@require_role("teacher", "owner")
async def cb_theme_tasks(callback: CallbackQuery, db_user=None) -> None:
    theme_id = _parse_id(callback.data, 2)
    if theme_id is None:
        await callback.answer(MSG_STALE_FULL, show_alert=True)
        return
    view = await _theme_tasks_view(theme_id, db_user)
    if view is None:
        await callback.answer(MSG_THEME_NOT_FOUND, show_alert=True)
        return
    await callback.answer()
    await safe_edit(callback.message, *view)


# ==========================================================================
# Визард «Добавить задание»: question → options → correct → exp_input →
# preview (UX-пакет: объяснение без выбора «текст/фото»)
# ==========================================================================
@router.callback_query(F.data.startswith("tch:add_task:"))
@require_role("teacher", "owner")
async def cb_add_task(
    callback: CallbackQuery, state: FSMContext, db_user=None
) -> None:
    """"tch:add_task:{theme_id}:0" — вход в визард с чистого листа.

    Если у темы есть подтемы — сначала шаг выбора подтемы (tch:at:sub:),
    иначе сразу вопрос.
    """
    theme_id = _parse_id(callback.data, 2)
    if theme_id is None:
        await callback.answer(MSG_STALE_FULL, show_alert=True)
        return
    async with get_session_factory()() as session:
        theme = await teacher_svc.get_theme_for_teacher(session, db_user.id, theme_id)
        if theme is None:
            await callback.answer(MSG_THEME_NOT_FOUND, show_alert=True)
            return
        subthemes = await teacher_svc.list_theme_subthemes(session, theme.id)
    await state.clear()  # ошибка №12: вход в визард — с чистого листа
    await state.update_data(theme_id=theme.id, subtheme_id=None)
    if subthemes:
        await state.set_state(AddTaskStates.sub)  # обязательный переход
        await callback.answer()
        await callback.message.answer(
            ASK_SUB_PICK,
            reply_markup=subtheme_pick_kb(
                [item["subtheme"] for item in subthemes], theme.id
            ),
        )
        return
    await state.set_state(AddTaskStates.question)  # обязательный переход
    await callback.answer()
    await callback.message.answer(ASK_QUESTION)


@router.callback_query(F.data.startswith("tch:at:sub:"))
@require_role("teacher", "owner")
async def cb_pick_subtheme(
    callback: CallbackQuery, state: FSMContext, db_user=None
) -> None:
    """Выбор подтемы в визарде задания. «:0» — на тему напрямую.

    Кнопка работает только в шаге sub: нажатая из другого шага —
    «Кнопка устарела» + state.clear() (как opt_more/opts_done).
    """
    if await state.get_state() != AddTaskStates.sub:
        await state.clear()
        await callback.answer(MSG_STALE_FULL, show_alert=True)
        return
    subtheme_id = _parse_id(callback.data, 3)
    if subtheme_id is None or subtheme_id < 0:
        await callback.answer(MSG_STALE_FULL, show_alert=True)
        return
    data = await state.get_data()
    if data.get("theme_id") is None:
        await callback.answer(MSG_STALE_FULL, show_alert=True)
        await state.clear()
        return
    # 0 — «На тему (без подтемы)» → subtheme_id=None
    await state.update_data(subtheme_id=subtheme_id or None)
    await state.set_state(AddTaskStates.question)  # обязательный переход
    await callback.answer()
    await callback.message.answer(ASK_QUESTION)


@router.message(AddTaskStates.sub, ~F.text.startswith("/"))
@require_role("teacher", "owner")
async def on_task_sub_hint(message: Message, db_user=None) -> None:
    """Текст/медиа в шаге sub — подсказка, состояние не теряется."""
    await message.answer(TEXT_SUB_HINT_PICK)


async def _confirm_wizard_state(
    callback: CallbackQuery, state: FSMContext
) -> bool:
    """Кнопка визарда при отсутствующих данных → «Кнопка устарела». False = стоп."""
    if await _wizard_data(state) is None:
        await callback.answer(MSG_STALE_FULL, show_alert=True)
        return False
    return True


# --- question (ВАЖНО: photo/document ПЕРВЫМИ — иначе фото с подписью
# ловится text-хендлером как текст-вопрос, а сама фотка теряется) ---
@router.message(AddTaskStates.question, F.photo)
@require_role("teacher", "owner")
async def on_task_question_photo(
    message: Message, state: FSMContext, db_user=None
) -> None:
    photo_id = message.photo[-1].file_id
    caption = (message.caption or "").strip()
    if len(caption) > teacher_svc.QUESTION_TEXT_MAX:
        await message.answer(TEXT_QUESTION_TOO_LONG)
        return
    await state.update_data(
        question_text=caption or None, question_photo_id=photo_id
    )
    await state.set_state(AddTaskStates.options)  # обязательный переход
    await message.answer(ASK_OPTIONS, reply_markup=options_more_kb())


@router.message(AddTaskStates.question, F.document)
@require_role("teacher", "owner")
async def on_task_question_document(message: Message, db_user=None) -> None:
    """Файл вместо вопроса — подсказка, состояние не меняется."""
    await message.answer(ASK_QUESTION)


@router.message(AddTaskStates.question, ~F.text.startswith("/"))
@require_role("teacher", "owner")
async def on_task_question(
    message: Message, state: FSMContext, db_user=None
) -> None:
    text = (message.text or "").strip()
    if not text:
        await message.answer(TEXT_EMPTY_QUESTION)
        return
    if len(text) > teacher_svc.QUESTION_TEXT_MAX:
        await message.answer(TEXT_QUESTION_TOO_LONG)
        return
    await state.update_data(question_text=text, question_photo_id=None)
    await state.set_state(AddTaskStates.options)  # обязательный переход
    await message.answer(ASK_OPTIONS, reply_markup=options_more_kb())


# --- options -------------------------------------------------------------
@router.message(AddTaskStates.options, ~F.text.startswith("/"))
@require_role("teacher", "owner")
async def on_task_option(message: Message, state: FSMContext, db_user=None) -> None:
    data = await _wizard_data(state)
    if data is None:
        await message.answer(MSG_STALE_FULL)
        return
    text = (message.text or "").strip()
    if not text:
        await message.answer(TEXT_EMPTY_OPTION)
        return
    if len(text) > teacher_svc.OPTION_TEXT_MAX:
        await message.answer(TEXT_OPTION_TOO_LONG)
        return
    options = list(data.get("options") or [])
    options.append(text)
    await state.update_data(options=options)
    n = len(options)
    if n >= teacher_svc.OPTIONS_MAX:
        await message.answer(
            TEXT_OPTION_ADDED_MAX.format(n=n), reply_markup=options_done_kb()
        )
    else:
        await message.answer(
            TEXT_OPTION_ADDED.format(n=n), reply_markup=options_more_kb()
        )


@router.message(AddTaskStates.options, F.photo | F.document)
@require_role("teacher", "owner")
async def on_task_option_nontext(message: Message, db_user=None) -> None:
    """Вариант — только текстом; состояние не меняется (объяснение ТЗ 8.2)."""
    await message.answer(ASK_OPTIONS)


@router.callback_query(F.data == "tch:at:opt_more:0")
@require_role("teacher", "owner")
async def cb_opt_more(callback: CallbackQuery, state: FSMContext, db_user=None) -> None:
    """«➕ Ещё вариант»: работает только в шаге options.

    Кнопка из пройденного шага (вопрос уже введён, визард ушёл вперёд) —
    «Кнопка устарела» + state.clear(): визард НЕ перескакивает вперёд.
    """
    if await state.get_state() != AddTaskStates.options:
        await state.clear()
        await callback.answer(MSG_STALE_FULL, show_alert=True)
        return
    data = await state.get_data()
    if len(data.get("options") or []) >= teacher_svc.OPTIONS_MAX:
        await callback.answer()
        await callback.message.answer(TEXT_OPTION_MAX)
        return
    await state.set_state(AddTaskStates.options)
    await callback.answer()
    await callback.message.answer("Присылай следующий вариант сообщением.")


@router.callback_query(F.data == "tch:at:opts_done:0")
@require_role("teacher", "owner")
async def cb_opts_done(callback: CallbackQuery, state: FSMContext, db_user=None) -> None:
    """«✅ Готово»: только в шаге options; минимум 2 варианта (ТЗ 8.2)."""
    if await state.get_state() != AddTaskStates.options:
        await state.clear()
        await callback.answer(MSG_STALE_FULL, show_alert=True)
        return
    data = await state.get_data()
    options = data.get("options") or []
    if len(options) < teacher_svc.OPTIONS_MIN:
        await callback.answer(ALERT_OPTIONS_MIN, show_alert=True)
        return
    await state.set_state(AddTaskStates.correct)  # обязательный переход
    await callback.answer()
    await callback.message.answer(ASK_CORRECT, reply_markup=options_pick_kb(options))


# --- correct --------------------------------------------------------------
@router.callback_query(F.data.startswith("tch:at:pick:"))
@require_role("teacher", "owner")
async def cb_pick_correct(callback: CallbackQuery, state: FSMContext, db_user=None) -> None:
    index = _parse_id(callback.data, 3)
    options = (await state.get_data()).get("options") or []
    if index is None or not (0 <= index < len(options)):
        await callback.answer(MSG_STALE, show_alert=True)  # «Кнопка устарела»
        return
    await state.update_data(correct_index=index)
    await state.set_state(AddTaskStates.exp_input)  # обязательный переход
    await callback.answer()
    await callback.message.answer(EXP_CHOICE_MSG, reply_markup=exp_pass_kb())


# --- exp_input (UX-пакет: без выбора «текст/фото», принимаем ОБА типа) ---
@router.callback_query(F.data == "tch:at:exp_skip:0")
@require_role("teacher", "owner")
async def cb_exp_skip(
    callback: CallbackQuery, state: FSMContext, db_user=None
) -> None:
    if not await _confirm_wizard_state(callback, state):
        return
    await state.set_state(AddTaskStates.preview)  # обязательный переход
    await callback.answer()
    await _render_preview(callback.message, await state.get_data())


@router.callback_query(F.data == "tch:at:exp_more:0")
@require_role("teacher", "owner")
async def cb_exp_more(
    callback: CallbackQuery, state: FSMContext, db_user=None
) -> None:
    """«➕ Ещё» после «✅ Объяснение добавлено.» — снова приём объяснения."""
    if not await _confirm_wizard_state(callback, state):
        return
    await state.set_state(AddTaskStates.exp_input)
    await callback.answer()
    await callback.message.answer(TEXT_EXP_MORE, reply_markup=exp_pass_kb())


@router.callback_query(F.data == "tch:at:exp_done:0")
@require_role("teacher", "owner")
async def cb_exp_done(
    callback: CallbackQuery, state: FSMContext, db_user=None
) -> None:
    if not await _confirm_wizard_state(callback, state):
        return
    await state.set_state(AddTaskStates.preview)  # обязательный переход
    await callback.answer()
    await _render_preview(callback.message, await state.get_data())


# ВАЖНО: photo/document ПЕРВЫМИ — иначе фото с подписью ловится text-хендлером
# как текст объяснения, а сама фотка теряется (правило вопроса).
@router.message(AddTaskStates.exp_input, F.photo)
@require_role("teacher", "owner")
async def on_exp_photo(message: Message, state: FSMContext, db_user=None) -> None:
    data = await _wizard_data(state)
    if data is None:
        await message.answer(MSG_STALE_FULL)
        return
    await state.update_data(feedback_photo_id=message.photo[-1].file_id)
    await message.answer(TEXT_EXP_ADDED, reply_markup=exp_more_kb())


@router.message(AddTaskStates.exp_input, F.document)
@require_role("teacher", "owner")
async def on_exp_document(message: Message, db_user=None) -> None:
    """Файл вместо объяснения — подсказка, состояние не теряется."""
    await message.answer(TEXT_HINT_EXP_INPUT, reply_markup=exp_pass_kb())


@router.message(AddTaskStates.exp_input, ~F.text.startswith("/"))
@require_role("teacher", "owner")
async def on_exp_text(message: Message, state: FSMContext, db_user=None) -> None:
    data = await _wizard_data(state)
    if data is None:
        await message.answer(MSG_STALE_FULL)
        return
    text = (message.text or "").strip()
    if not text:
        await message.answer(TEXT_HINT_EXP_INPUT, reply_markup=exp_pass_kb())
        return
    await state.update_data(feedback_text=text)
    await message.answer(TEXT_EXP_ADDED, reply_markup=exp_more_kb())


@router.message(
    StateFilter(
        AddTaskStates.question,
        AddTaskStates.options,
        AddTaskStates.exp_input,
    ),
    F.sticker | F.voice | F.video | F.audio | F.animation | F.video_note,
)
@require_role("teacher", "owner")
async def on_wizard_media(message: Message, state: FSMContext, db_user=None) -> None:
    """Стикер/голосовое/видео в шаге визарда → подсказка по шагу.

    Перехват разрешённых в шаге типов (F.photo|F.document) не покрывает
    остальной медиа-мусор — без этого хендлера он молча терялся.
    """
    hint = {
        AddTaskStates.question: ASK_QUESTION,
        AddTaskStates.options: ASK_OPTIONS,
        AddTaskStates.exp_input: TEXT_HINT_EXP_INPUT,
    }.get(await state.get_state(), ASK_QUESTION)
    await message.answer(hint, reply_markup=exp_pass_kb())


# --- preview ----------------------------------------------------------------
async def _render_preview(message: Message, data: dict) -> None:
    """Превью задания как у ученика: фото (если есть) + вопрос + варианты."""
    options = data.get("options") or []
    text = "\n".join(
        [
            TEXT_PREVIEW_QUESTION,
            esc(data.get("question_text") or "фото-вопрос"),
            "",
            TEXT_PREVIEW_OPTIONS,
        ]
    )
    kb = preview_kb(options, edit_task_id=data.get("task_id"))
    if data.get("question_photo_id"):
        await message.answer_photo(
            photo=data["question_photo_id"], caption=text, reply_markup=kb
        )
    else:
        await message.answer(text, reply_markup=kb)


@router.callback_query(F.data.startswith("tch:at:pv:"))
@require_role("teacher", "owner")
async def cb_preview_option(callback: CallbackQuery, db_user=None) -> None:
    """Клик по варианту в превью (ТЗ 8.5)."""
    await callback.answer(PREVIEW_OPTION_HINT)


@router.callback_query(F.data == "tch:at:restart:0")
@require_role("teacher", "owner")
async def cb_task_restart(
    callback: CallbackQuery, state: FSMContext, db_user=None
) -> None:
    """«✏️ Заново»: в question; подтема и режим редактирования сохраняются."""
    data = await state.get_data()
    theme_id = data.get("theme_id")
    task_id = data.get("task_id")
    subtheme_id = data.get("subtheme_id")
    await state.clear()
    if theme_id is None:
        await callback.answer(MSG_STALE_FULL, show_alert=True)
        return
    await state.update_data(theme_id=theme_id, subtheme_id=subtheme_id)
    if task_id is not None:
        await state.update_data(task_id=task_id)
    await state.set_state(AddTaskStates.question)
    await callback.answer()
    await callback.message.answer(ASK_QUESTION)


@router.callback_query(F.data == "tch:at:save:0")
@require_role("teacher", "owner")
async def cb_task_save(callback: CallbackQuery, state: FSMContext, db_user=None) -> None:
    """«✅ Сохранить»: защита от двойного клика (ошибка №8, ТЗ 8.5).

    Сначала валидация state и темы — только потом saving/снятие клавиатуры.
    Любая ошибка (сеть/БД/Telegram) — state.clear() + сообщение: визард
    не остаётся «мёртвым» навсегда с меткой saving.
    """
    data = await state.get_data()
    if await state.get_state() != AddTaskStates.preview or data.get("saving"):
        await callback.answer(SAVING_NOW, show_alert=True)
        return
    task_id = data.get("task_id")
    theme_id = data.get("theme_id")
    options = data.get("options")
    correct_index = data.get("correct_index")
    if theme_id is None or options is None or correct_index is None:
        await state.clear()
        await callback.answer(MSG_STALE_FULL, show_alert=True)
        return
    try:
        options_json = teacher_svc.build_options_json(options, correct_index)
    except ValueError as exc:
        await state.clear()
        await callback.answer(str(exc), show_alert=True)
        return
    async with get_session_factory()() as session:
        theme = await teacher_svc.get_theme_for_teacher(session, db_user.id, theme_id)
        if theme is None:
            await state.clear()
            await callback.answer(MSG_THEME_NOT_FOUND, show_alert=True)
            return
    await state.update_data(saving=True)
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        pass  # клавиатуры уже нет — нормально
    except Exception:
        await state.clear()
        logger.exception("Не удалось снять клавиатуру превью")
        await callback.message.answer(MSG_SAVE_FAILED)
        return
    await callback.answer()
    try:
        async with get_session_factory()() as session:
            if task_id is not None:
                # «✏️ Редактировать»: UPDATE по существующему заданию (order/
                # is_active/прогресс учеников не трогаются).
                task = await teacher_svc.update_task(
                    session,
                    task_id,
                    data.get("question_text"),
                    data.get("question_photo_id"),
                    options_json,
                    data.get("feedback_text"),
                    data.get("feedback_photo_id"),
                    db_user.id,
                )
                if task is None:
                    await state.clear()
                    await callback.answer(MSG_TASK_NOT_FOUND, show_alert=True)
                    return
            else:
                task = await teacher_svc.create_task(
                    session,
                    theme_id,
                    data.get("question_text"),
                    data.get("question_photo_id"),
                    options_json,
                    data.get("feedback_text"),
                    data.get("feedback_photo_id"),
                    db_user.id,
                    subtheme_id=data.get("subtheme_id"),
                )
    except Exception:
        await state.clear()
        logger.exception("Ошибка при сохранении задания")
        await callback.message.answer(MSG_SAVE_FAILED)
        return
    await state.clear()  # ошибка №12: выход из визарда
    text, kb = await _task_card_view(task)
    await callback.message.answer(TASK_SAVED + "\n\n" + text, reply_markup=kb)


# ==========================================================================
# Карточка задания (промт захода 5, раздел 3)
# ==========================================================================
async def _task_card_view(task) -> tuple[str, object]:
    options = task.options or []
    letters = _OPTION_LETTERS
    correct_index = next(
        (i for i, o in enumerate(options) if o.get("c")), None
    )
    correct_label = letters[correct_index] if correct_index is not None else "—"
    has_feedback = bool(task.feedback_text or task.feedback_photo_id)
    lines = [
        "📝 <b>Задание</b>",
        esc(_question_for_card(task)),
        f"Вариантов: {len(options)} · Правильный: {correct_label}",
        f"Объяснение: {'есть' if has_feedback else 'нет'}",
        f"Статус: {'✅ видно ученикам' if task.is_active else '🚫 скрыто'}",
    ]
    return "\n".join(lines), task_card_kb(task)


async def _render_task_card(callback: CallbackQuery, task) -> None:
    """Карточка задания (общая для «показать карточку» и выхода из
    визарда редактирования). Фото-вопрос нельзя показать через edit —
    отправляем новым сообщением."""
    text, kb = await _task_card_view(task)
    if task.question_photo_id:
        await callback.message.answer_photo(
            task.question_photo_id, caption=text, reply_markup=kb
        )
    else:
        await safe_edit(callback.message, text, kb)
    if task.feedback_photo_id:
        await callback.message.answer_photo(
            task.feedback_photo_id, caption="💡 <b>Объяснение (фото)</b>"
        )


async def _task_or_alert(
    callback: CallbackQuery, db_user, task_id: int
) -> object | None:
    """Задание с проверкой прав; None → уже отвечено алертом."""
    async with get_session_factory()() as session:
        task = await teacher_svc.get_task_for_teacher(session, db_user.id, task_id)
    if task is None:
        await callback.answer(MSG_TASK_NOT_FOUND, show_alert=True)
        return None
    return task


@router.callback_query(F.data.startswith("tch:task:"))
@require_role("teacher", "owner")
async def cb_task_card(callback: CallbackQuery, db_user=None) -> None:
    task_id = _parse_id(callback.data, 2)
    if task_id is None:
        await callback.answer(MSG_STALE_FULL, show_alert=True)
        return
    task = await _task_or_alert(callback, db_user, task_id)
    if task is None:
        return
    await callback.answer()
    await _render_task_card(callback, task)


@router.callback_query(F.data.startswith("tch:t_edit:"))
@require_role("teacher", "owner")
async def cb_task_edit(callback: CallbackQuery, state: FSMContext, db_user=None) -> None:
    """«✏️ Редактировать» в карточке: визард с предзаполненными значениями.

    state заполняется текущими данными задания, FSM — сразу на preview
    (вопрос/варианты/правильный/объяснение правятся кнопками превью
    «✏️ Вопрос» / «✏️ Варианты» / «✏️ Объяснение»). save с task_id в
    state делает UPDATE, а не INSERT.
    """
    task_id = _parse_id(callback.data, 2)
    if task_id is None:
        await callback.answer(MSG_STALE_FULL, show_alert=True)
        return
    task = await _task_or_alert(callback, db_user, task_id)
    if task is None:
        return
    options = []
    for opt in task.options or []:
        options.append(str(opt.get("t") or "").strip())
    correct_index = next(
        (i for i, o in enumerate(task.options or []) if o.get("c")), None
    )
    await state.clear()  # ошибка №12: вход в визард — с чистого листа
    await state.update_data(
        theme_id=task.theme_id,
        task_id=task.id,
        question_text=task.question_text,
        question_photo_id=task.question_photo_id,
        options=options,
        correct_index=correct_index,
        feedback_text=task.feedback_text,
        feedback_photo_id=task.feedback_photo_id,
    )
    await state.set_state(AddTaskStates.preview)  # обязательный переход
    await callback.answer()
    await _render_preview(callback.message, await state.get_data())


@router.callback_query(F.data.startswith("tch:at:edit_back:"))
@require_role("teacher", "owner")
async def cb_task_edit_back(callback: CallbackQuery, state: FSMContext, db_user=None) -> None:
    """«← К заданию» в превью режима редактирования: отмена визарда и карточка."""
    task_id = _parse_id(callback.data, 3)
    await state.clear()
    if task_id is None:
        await callback.answer(MSG_STALE_FULL, show_alert=True)
        return
    task = await _task_or_alert(callback, db_user, task_id)
    if task is None:
        return
    await callback.answer()
    await _render_task_card(callback, task)


@router.callback_query(F.data == "tch:at:edit_q:0")
@require_role("teacher", "owner")
async def cb_preview_edit_question(callback: CallbackQuery, state: FSMContext, db_user=None) -> None:
    """«✏️ Вопрос» в превью: шаг вопроса (данные остаются в state)."""
    if not await _confirm_wizard_state(callback, state):
        return
    await state.set_state(AddTaskStates.question)  # обязательный переход
    await callback.answer()
    await callback.message.answer(ASK_QUESTION)


@router.callback_query(F.data == "tch:at:edit_o:0")
@require_role("teacher", "owner")
async def cb_preview_edit_options(callback: CallbackQuery, state: FSMContext, db_user=None) -> None:
    """«✏️ Варианты» в превью: варианты вводятся заново (как в добавлении)."""
    if not await _confirm_wizard_state(callback, state):
        return
    await state.update_data(options=[], correct_index=None)
    await state.set_state(AddTaskStates.options)  # обязательный переход
    await callback.answer()
    await callback.message.answer(ASK_OPTIONS, reply_markup=options_more_kb())


@router.callback_query(F.data == "tch:at:edit_e:0")
@require_role("teacher", "owner")
async def cb_preview_edit_explanation(callback: CallbackQuery, state: FSMContext, db_user=None) -> None:
    """«✏️ Объяснение» в превью: текст/фото заново, Пропустить — убрать."""
    if not await _confirm_wizard_state(callback, state):
        return
    await state.update_data(feedback_text=None, feedback_photo_id=None)
    await state.set_state(AddTaskStates.exp_input)  # обязательный переход
    await callback.answer()
    await callback.message.answer(EXP_CHOICE_MSG, reply_markup=exp_pass_kb())


@router.callback_query(F.data.startswith("tch:t_toggle:"))
@require_role("teacher", "owner")
async def cb_task_toggle(callback: CallbackQuery, db_user=None) -> None:
    task_id = _parse_id(callback.data, 2)
    if task_id is None:
        await callback.answer(MSG_STALE_FULL, show_alert=True)
        return
    async with get_session_factory()() as session:
        task = await teacher_svc.get_task_for_teacher(session, db_user.id, task_id)
        if task is None:
            await callback.answer(MSG_TASK_NOT_FOUND, show_alert=True)
            return
        is_active = await teacher_svc.toggle_task_active(session, task_id)
    if is_active is None:
        # гонка: задание удалено между проверкой и toggle
        await callback.answer(MSG_TASK_NOT_FOUND, show_alert=True)
        return
    text, kb = await _task_card_view(task)
    if not await safe_edit(callback.message, text, kb):
        await callback.answer(MSG_TASK_NOT_FOUND, show_alert=True)
        return
    await callback.answer(TEXT_TASK_SHOWN if is_active else TEXT_TASK_HIDDEN)


@router.callback_query(F.data.startswith("tch:t_del:"))
@require_role("teacher", "owner")
async def cb_task_del(callback: CallbackQuery, db_user=None) -> None:
    task_id = _parse_id(callback.data, 2)
    if task_id is None:
        await callback.answer(MSG_STALE_FULL, show_alert=True)
        return
    task = await _task_or_alert(callback, db_user, task_id)
    if task is None:
        return
    await callback.answer()
    await safe_edit(
        callback.message,
        TEXT_DEL_TASK_ASK.format(q=esc(_question_for_card(task))),
        confirm_del_task_kb(task.id),
    )


@router.callback_query(F.data.startswith("tch:t_yes:"))
@require_role("teacher", "owner")
async def cb_task_del_yes(callback: CallbackQuery, db_user=None) -> None:
    task_id = _parse_id(callback.data, 2)
    if task_id is None:
        await callback.answer(MSG_STALE_FULL, show_alert=True)
        return
    async with get_session_factory()() as session:
        task = await teacher_svc.get_task_for_teacher(session, db_user.id, task_id)
        if task is None:
            await callback.answer(MSG_TASK_NOT_FOUND, show_alert=True)
            return
        theme_id = task.theme_id
        await teacher_svc.delete_task(session, task_id)
    await callback.answer()
    await safe_edit(callback.message, TEXT_TASK_DELETED, None)
    view = await _theme_tasks_view(theme_id, db_user)
    if view is not None:
        text, kb = view
        await callback.message.answer(text, reply_markup=kb)


@router.callback_query(F.data.startswith("tch:t_no:"))
@require_role("teacher", "owner")
async def cb_task_del_no(callback: CallbackQuery, db_user=None) -> None:
    task_id = _parse_id(callback.data, 2)
    if task_id is None:
        await callback.answer(MSG_STALE_FULL, show_alert=True)
        return
    task = await _task_or_alert(callback, db_user, task_id)
    if task is None:
        return
    await callback.answer()
    text, kb = await _task_card_view(task)
    await safe_edit(callback.message, text, kb)