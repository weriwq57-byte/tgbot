"""Рассылка сообщений (Заход 9, владелец): категории → предмет → текст/фото → отправка.

Правила (как в остальных визардах проекта):
- все хендлеры под @require_role("owner"); роль всегда из БД;
- сначала перерисовка экрана (safe_edit), потом единственный callback.answer();
- устаревшие кнопки (state не тот или пуст) → «Кнопка устарела»;
- тексты в HTML-сообщениях — через esc(); /menu отменяет визард (commands.py).
"""
import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select

from app.database import get_session_factory
from app.keyboards.inline import (
    bcast_categories_kb,
    bcast_confirm_kb,
    bcast_subjects_kb,
)
from app.models import Subject
from app.services import broadcast as bcast_svc
from app.services import people as people_svc
from app.services.broadcast import (
    STUDENTS_MODE_ALL,
    STUDENTS_MODE_SUBJECTS,
    TEXT_REPORT,
)
from app.states import BroadcastStates
from app.utils.format import esc
from app.utils.messages import safe_edit
from app.utils.roles import require_role

logger = logging.getLogger(__name__)

router = Router()

MSG_STALE = "Кнопка устарела, начни заново."
TEXT_EMPTY_RECIPIENTS = "Выбери получателей"
TEXT_NO_SUBJECTS = "Предметов пока нет"
TEXT_SEND_HINT = "Отправь текст или фото"
TEXT_EMPTY_MSG = "Сообщение пустое"
TEXT_SUBJECT_GONE = "Предмет больше не существует"
TEXT_CANCELED = "Рассылка отменена."

_CATEGORY_KEYS = ("students", "teachers", "managers")
_RCP_ACTIONS = ("students", "teachers", "managers", "subjects", "next", "cancel")


async def _categories_header(session, state: FSMContext) -> str:
    """Заголовок шага 1: текущий выбор категорий («Получатели:» + строки).

    Ученики при students_mode="all" — «— все предметы», при "subjects" —
    «— 📚 {предмет} (выбран)». Показываются только выбранные категории.
    """
    data = await state.get_data()
    recipients = data.get("recipients") or []
    students_mode = data.get("students_mode") or STUDENTS_MODE_ALL
    lines = []
    if "students" in recipients:
        if students_mode == STUDENTS_MODE_SUBJECTS:
            ids = data.get("subject_ids") or []
            names = list(
                await session.scalars(
                    select(Subject.name)
                    .where(Subject.id.in_(ids))
                    .order_by(Subject.name, Subject.id)
                )
            )
            detail = (
                f"📚 {', '.join(names)} (выбран)" if names else "📚 предмет не выбран"
            )
            lines.append(f"👨🎓 Ученики — {detail}")
        else:
            lines.append("👨🎓 Ученики — все предметы")
    if "teachers" in recipients:
        lines.append("👨🏫 Преподаватели")
    if "managers" in recipients:
        lines.append("👥 Менеджеры")
    return "Получатели:\n" + "\n".join(lines) if lines else "Получатели:"


async def _categories_kb_from_state(state: FSMContext):
    """Клавиатура категорий по текущему состоянию визарда."""
    data = await state.get_data()
    return bcast_categories_kb(
        set(data.get("recipients") or []),
        data.get("students_mode") or STUDENTS_MODE_ALL,
    )


async def _redraw_categories(callback: CallbackQuery, state: FSMContext) -> None:
    """Перерисовка шага 1 (клик по тумблеру/возврат с шага 2)."""
    async with get_session_factory()() as session:
        text = await _categories_header(session, state)
    ok = await safe_edit(callback.message, text, await _categories_kb_from_state(state))
    if ok:
        await callback.answer()
    else:
        # перерисовать не вышло — единственный ответ колбэку
        await callback.answer(MSG_STALE, show_alert=True)


async def _start_broadcast(message: Message, state: FSMContext) -> None:
    """Общий старт визарда: /broadcast и кнопка «📣 Рассылка» в меню."""
    await state.clear()
    await state.set_state(BroadcastStates.recipients)
    await state.update_data(
        recipients=[], students_mode=STUDENTS_MODE_ALL, subject_ids=[]
    )
    await message.answer(
        "Получатели:", reply_markup=bcast_categories_kb(set(), STUDENTS_MODE_ALL)
    )


@router.message(Command("broadcast"))
@require_role("owner")
async def cmd_broadcast(message: Message, state: FSMContext) -> None:
    await _start_broadcast(message, state)


@router.callback_query(F.data == "menu:owner:broadcast:0")
@require_role("owner")
async def cb_menu_broadcast(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await _start_broadcast(callback.message, state)


# --------------------------------------------------------------------------
# Шаг 1 — категории получателей
# --------------------------------------------------------------------------
@router.callback_query(F.data.startswith("bcast:rcp:"))
@require_role("owner")
async def cb_bcast_rcp(callback: CallbackQuery, state: FSMContext) -> None:
    action = callback.data.split(":")[2]
    if action not in _RCP_ACTIONS:
        await callback.answer(MSG_STALE, show_alert=True)
        return

    if action == "cancel":
        await state.clear()
        await callback.answer()
        await callback.message.answer(TEXT_CANCELED)
        return

    if await state.get_state() != BroadcastStates.recipients.state:
        # устаревшие кнопки из другого шага или после отмены
        await callback.answer(MSG_STALE, show_alert=True)
        if await state.get_state():
            await state.clear()
        return

    data = await state.get_data()
    recipients = set(data.get("recipients") or [])

    if action == "next":
        if not recipients:
            await callback.answer(TEXT_EMPTY_RECIPIENTS, show_alert=True)
            return
        await state.set_state(BroadcastStates.message_input)
        await callback.answer()
        await callback.message.answer(TEXT_SEND_HINT)
        return

    if action == "subjects":
        # клик «Выбрать предмет» сам добавляет учеников и открывает шаг 2
        recipients.add("students")
        await state.update_data(recipients=sorted(recipients))
        async with get_session_factory()() as session:
            subjects = await people_svc.list_active_subjects(session)
        if not subjects:
            # предметов нет — alert и возврат на шаг 1 (state не менялся)
            await callback.answer(TEXT_NO_SUBJECTS, show_alert=True)
            await _redraw_categories(callback, state)
            return
        await state.set_state(BroadcastStates.subjects)
        await callback.answer()
        await safe_edit(callback.message, "Выбери предмет:", bcast_subjects_kb(subjects))
        return

    # тумблер категории
    if action in recipients:
        recipients.discard(action)
    else:
        recipients.add(action)
    await state.update_data(recipients=sorted(recipients))
    await _redraw_categories(callback, state)


# --------------------------------------------------------------------------
# Шаг 2 — выбор предмета (один)
# --------------------------------------------------------------------------
@router.callback_query(F.data.startswith("bcast:sub:"))
@require_role("owner")
async def cb_bcast_sub(callback: CallbackQuery, state: FSMContext) -> None:
    if await state.get_state() != BroadcastStates.subjects.state:
        await callback.answer(MSG_STALE, show_alert=True)
        if await state.get_state():
            await state.clear()
        return

    action = callback.data.split(":")[2]  # id | clear
    if action == "clear":
        await state.update_data(students_mode=STUDENTS_MODE_ALL, subject_ids=[])
    else:
        try:
            subject_id = int(action)
        except ValueError:
            await callback.answer(MSG_STALE, show_alert=True)
            return
        async with get_session_factory()() as session:
            subject = await session.get(Subject, subject_id)
        if subject is None or not subject.is_active:
            await callback.answer(TEXT_SUBJECT_GONE, show_alert=True)
            return
        await state.update_data(
            students_mode=STUDENTS_MODE_SUBJECTS, subject_ids=[subject_id]
        )
    await state.set_state(BroadcastStates.recipients)
    await _redraw_categories(callback, state)


# --------------------------------------------------------------------------
# Шаг 3 — текст и/или фото
# --------------------------------------------------------------------------
@router.message(BroadcastStates.message_input, F.text)
@require_role("owner")
async def on_bcast_text(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if not text:
        await message.answer(TEXT_SEND_HINT)
        return
    await state.update_data(text=text, photo_file_id=None)
    await _show_preview(message, state)


@router.message(BroadcastStates.message_input, F.photo)
@require_role("owner")
async def on_bcast_photo(message: Message, state: FSMContext) -> None:
    photo = message.photo or []
    if not photo:
        await message.answer(TEXT_SEND_HINT)
        return
    caption = (message.caption or "").strip() or None
    await state.update_data(text=caption, photo_file_id=photo[-1].file_id)
    await _show_preview(message, state)


@router.message(BroadcastStates.message_input, ~F.text, ~F.photo)
@require_role("owner")
async def on_bcast_bad_input(message: Message, state: FSMContext) -> None:
    """Документ/стикер/голосовое и пр. — подсказка, состояние не теряется."""
    await message.answer(TEXT_SEND_HINT)


async def _show_preview(message: Message, state: FSMContext) -> None:
    """Шаг 4: предпросмотр сообщения + «Получателей: N» + кнопки."""
    data = await state.get_data()
    text = data.get("text")
    photo_file_id = data.get("photo_file_id")
    if not text and not photo_file_id:
        await message.answer(TEXT_EMPTY_MSG)
        return  # остаёмся на шаге 3
    await state.set_state(BroadcastStates.confirm)
    async with get_session_factory()() as session:
        recipients = await bcast_svc.collect_recipients(
            session,
            categories=data.get("recipients") or [],
            students_mode=data.get("students_mode") or STUDENTS_MODE_ALL,
            subject_ids=data.get("subject_ids") or [],
        )
    footer = f"\n\nПолучателей: {len(recipients)}"
    if photo_file_id:
        caption = (esc(text) + footer) if text else footer.strip()
        await message.answer_photo(
            photo_file_id, caption=caption, reply_markup=bcast_confirm_kb()
        )
    else:
        await message.answer(esc(text) + footer, reply_markup=bcast_confirm_kb())


# --------------------------------------------------------------------------
# Шаг 4 — предпросмотр: отправить / изменить / отмена
# --------------------------------------------------------------------------
@router.callback_query(F.data == "bcast:go")
@require_role("owner")
async def cb_bcast_go(callback: CallbackQuery, state: FSMContext, bot=None) -> None:
    if await state.get_state() != BroadcastStates.confirm.state:
        await callback.answer(MSG_STALE, show_alert=True)
        if await state.get_state():
            await state.clear()
        return

    data = await state.get_data()
    text = data.get("text") or ""
    photo_file_id = data.get("photo_file_id")
    if not text and not photo_file_id:
        await callback.answer(TEXT_EMPTY_MSG, show_alert=True)
        await state.set_state(BroadcastStates.message_input)
        await callback.message.answer(TEXT_SEND_HINT)
        return

    await callback.answer()
    async with get_session_factory()() as session:
        recipients = await bcast_svc.collect_recipients(
            session,
            categories=data.get("recipients") or [],
            students_mode=data.get("students_mode") or STUDENTS_MODE_ALL,
            subject_ids=data.get("subject_ids") or [],
        )
    report = await bcast_svc.send_broadcast(
        callback.message,
        bot,
        recipients,
        photo=bool(photo_file_id),
        text=text,
        photo_file_id=photo_file_id,
    )
    await callback.message.answer(TEXT_REPORT.format(**report))
    await state.clear()


@router.callback_query(F.data == "bcast:edit")
@require_role("owner")
async def cb_bcast_edit(callback: CallbackQuery, state: FSMContext) -> None:
    if await state.get_state() != BroadcastStates.confirm.state:
        await callback.answer(MSG_STALE, show_alert=True)
        if await state.get_state():
            await state.clear()
        return
    await state.set_state(BroadcastStates.message_input)
    await callback.answer()
    await callback.message.answer(TEXT_SEND_HINT)


@router.callback_query(F.data == "bcast:cancel")
@require_role("owner")
async def cb_bcast_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.answer()
    await callback.message.answer(TEXT_CANCELED)
