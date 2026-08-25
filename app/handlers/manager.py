"""Менеджер (и владелец): ученики, доступ, инвайт-коды (ТЗ, разделы 6–7).

Вся бизнес-логика — в app/services/students.py и app/services/invite.py;
здесь только UI. Все хендлеры (включая текстовые шаги визардов) —
под @require_role("manager", "owner") (ошибка №9). Вход в визарды —
с state.clear(), выход — тоже (ошибка №12).

Карточка и список строк — по дословным шаблонам из ТЗ/промта захода.
"""
import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.database import get_session_factory
from app.keyboards.inline import (
    add_student_continue_kb,
    confirm_deactivate_kb,
    confirm_del_student_kb,
    expiring_kb,
    multiselect_kb,
    student_card_kb,
    students_list_kb,
)
from app.services import invite as invite_svc
from app.services import people as people_svc
from app.services import students as students_svc
from app.states import AddStudentStates, DeleteStudentStates, ExtendAccessStates
from app.utils.dates import parse_date_input, today_minsk
from app.utils.format import esc, format_date, format_datetime
from app.utils.messages import safe_edit
from app.utils.roles import require_role

logger = logging.getLogger(__name__)

router = Router()

MSG_STALE = "Кнопка устарела, начни заново."

TEXT_STUDENTS_EMPTY = "Пока пусто. Добавь первого ученика."
TEXT_EXPIRING_EMPTY = "⏳ Истекающих нет: все доступы действуют ещё минимум неделю."
MSG_STUDENT_NOT_FOUND = "Ученик не найден"
MSG_BAD_DATE = "Не понял дату, пришли ещё раз, например 31.05.2027"
MSG_PAST_DATE = "Дата должна быть не раньше завтрашнего дня (например 31.05.2027)"

# Заголовки групп истекающих (ТЗ, раздел 6)
EXPIRING_GROUP_TITLES = [
    ("expired", "🔴 Истёкшие"),
    ("0", "🟠 Сегодня (последний день)"),
    ("1", "🟡 Завтра"),
    ("3", "🟢 Через 3 дня"),
    ("7", "🔵 Через 7 дней"),
]


def _parse_id(data: str, idx: int) -> int | None:
    """id из callback-данных; None — формат не тот (битая кнопка)."""
    try:
        return int(data.split(":")[idx])
    except (ValueError, IndexError):
        return None


def _student_tail(row: dict) -> str:
    """Хвост строки списка: привязан ✓ | код не активирован ⏳ | доступ истёк ⛔."""
    if not row["linked"]:
        return "код не активирован ⏳"
    if row["expired"]:
        return "доступ истёк ⛔"
    return "привязан ✓"


def students_text(rows: list[dict]) -> str:
    """Список учеников дословно по шаблону:
    {1}. {Имя} — 🔥{стрик} — {предметы} — до {ДД.ММ.ГГГГ} — {статус}.

    Стрик — ПО ПРЕДМЕТАМ (владелец, 13.08): «4/2» — все current через /
    в порядке предметов; без предметов/стриков — «0».
    """
    if not rows:
        return TEXT_STUDENTS_EMPTY
    lines = [f"👨🎓 Ученики ({len(rows)}):", ""]
    for i, row in enumerate(rows, 1):
        subjects = ", ".join(esc(s) for s in row["subject_names"]) or "—"
        streak = "/".join(str(s["current"]) for s in row["subject_streaks"]) or "0"
        lines.append(
            f"{i}. {esc(row['name'])} — 🔥{streak} — {subjects} — "
            f"до {format_date(row['access_until'])} — {_student_tail(row)}"
        )
    return "\n".join(lines)


async def _students_view(role: str = "manager") -> tuple[str, object]:
    async with get_session_factory()() as session:
        rows = await students_svc.list_students(session)
    back_cb = "menu:back:manager:0" if role == "manager" else "menu:back:owner:0"
    return students_text(rows), students_list_kb(rows, back_cb=back_cb)


def card_text(card: dict) -> str:
    """Карточка ученика дословно по шаблону ТЗ (раздел 6)."""
    student, user = card["student"], card["user"]
    linked = user.tg_id is not None
    name = user.tg_full_name or ""

    lines = [f"👨🎓 {esc(name)}"]
    if linked:
        if user.tg_username:
            lines.append(f"📱 @{esc(user.tg_username)} | привязан ✓")
        else:
            lines.append("📱 привязан ✓")
    else:
        lines.append("📱 не привязан ⏳")
    lines.append(f"📅 Создан: {format_datetime(student.created_at)}")
    lines.append(f"⏳ Доступ: до {format_date(student.access_until)}")
    if student.invite_status == "activated":
        lines.append("🔗 привязан ✓")
    else:
        lines.append(f"🔗 код не активирован ⏳ ({esc(student.invite_code)})")
    # Стрики ПО ПРЕДМЕТАМ (владелец, 13.08): каждый предмет своей строкой
    if card["subject_streaks"]:
        lines.append("🔥 Стрики по предметам:")
        for s in card["subject_streaks"]:
            lines.append(
                f"{esc(s['name'])} — 🔥 {s['current']} (рекорд: {s['best']})"
            )
    else:
        lines.append("🔥 Стрики: 0")
    lines.append("")
    lines.append("Предметы:")
    for subject, link in card["subjects"]:
        if link.is_active:
            lines.append(f"✅ {esc(subject.name)} (активен)")
        else:
            lines.append(f"🚫 {esc(subject.name)} (закрыт вручную)")
    if not user.is_active:
        lines.append("")
        lines.append("⛔ Ученик деактивирован")
    return "\n".join(lines)


async def _card_view(student_id: int) -> tuple[str, object] | None:
    """Карточка ученика (текст + клавиатура) или None, если исчез."""
    async with get_session_factory()() as session:
        card = await students_svc.get_student_card(session, student_id)
    if card is None:
        return None
    linked = card["user"].tg_id is not None
    return (
        card_text(card),
        student_card_kb(student_id, card["subjects"], linked, card["user"].is_active),
    )


async def _render_card_to(message: Message, student_id: int) -> bool:
    """Перерисовка карточки (safe_edit текущего сообщения). False — исчез."""
    view = await _card_view(student_id)
    if view is None:
        return False
    text, kb = view
    await safe_edit(message, text, kb)
    return True


async def _send_card(message: Message, student_id: int) -> bool:
    """Отправка карточки новым сообщением (после отдельного answer)."""
    view = await _card_view(student_id)
    if view is None:
        return False
    text, kb = view
    await message.answer(text, reply_markup=kb)
    return True


def _view_role(db_user) -> str:
    """Роль для «← Назад»: владелец возвращается в своё меню, не в меню менеджера."""
    return "manager" if getattr(db_user, "role", "manager") == "manager" else "owner"


# ==========================================================================
# Список учеников
# ==========================================================================
@router.message(Command("students"))
@require_role("manager", "owner")
async def cmd_students(
    message: Message, state: FSMContext = None, db_user=None
) -> None:
    if state is not None:
        await state.clear()  # раздел 0 дефект 1: команда отменяет визард
    text, kb = await _students_view(_view_role(db_user))
    await message.answer(text, reply_markup=kb)


@router.callback_query(F.data == "menu:manager:students:0")
@require_role("manager", "owner")
async def cb_show_students(callback: CallbackQuery, db_user=None) -> None:
    await callback.answer()
    text, kb = await _students_view(_view_role(db_user))
    await safe_edit(callback.message, text, kb)


@router.callback_query(F.data == "mgr:students:0")
@require_role("manager", "owner")
async def cb_back_to_students(callback: CallbackQuery, db_user=None) -> None:
    await callback.answer()
    text, kb = await _students_view(_view_role(db_user))
    await safe_edit(callback.message, text, kb)


# ==========================================================================
# Карточка ученика
# ==========================================================================
@router.callback_query(F.data.startswith("mgr:student:"))
@require_role("manager", "owner")
async def cb_student_card(callback: CallbackQuery) -> None:
    student_id = _parse_id(callback.data, 2)
    if student_id is None:
        await callback.answer(MSG_STALE, show_alert=True)
        return
    async with get_session_factory()() as session:
        card = await students_svc.get_student_card(session, student_id)
    if card is None:
        await callback.answer(MSG_STUDENT_NOT_FOUND, show_alert=True)
        return
    linked = card["user"].tg_id is not None
    await callback.answer()
    await safe_edit(
        callback.message,
        card_text(card),
        student_card_kb(student_id, card["subjects"], linked, card["user"].is_active),
    )


@router.callback_query(F.data.startswith("mgr:subj:"))
@require_role("manager", "owner")
async def cb_student_subject_toggle(callback: CallbackQuery) -> None:
    student_id = _parse_id(callback.data, 2)
    subject_id = _parse_id(callback.data, 3)
    if student_id is None or subject_id is None:
        await callback.answer(MSG_STALE, show_alert=True)
        return
    async with get_session_factory()() as session:
        new_state = await students_svc.toggle_subject_active(
            session, student_id, subject_id
        )
    if new_state is None:
        await callback.answer("Предмет больше не привязан к ученику", show_alert=True)
        return
    if not await _render_card_to(callback.message, student_id):
        await callback.answer(MSG_STUDENT_NOT_FOUND, show_alert=True)
        return
    await callback.answer()


# ==========================================================================
# Визард «Добавить ученика»: имя → предметы → дата
# ==========================================================================
@router.message(Command("add_student"))
@require_role("manager", "owner")
async def cmd_add_student(message: Message, state: FSMContext) -> None:
    await state.clear()  # ошибка №12: вход в визард — с чистого листа
    await state.set_state(AddStudentStates.name)
    await message.answer("👨🎓 Как зовут ученика? (имя и фамилия)")


@router.callback_query(F.data == "menu:manager:add_student:0")
@require_role("manager", "owner")
async def cb_add_student(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.clear()
    await state.set_state(AddStudentStates.name)
    await callback.message.answer("👨🎓 Как зовут ученика? (имя и фамилия)")


@router.message(AddStudentStates.name, ~F.text.startswith("/"))
@require_role("manager", "owner")
async def on_student_name(message: Message, state: FSMContext) -> None:
    name = (message.text or "").strip()
    if not name:
        await message.answer("Имя не может быть пустым. Напиши имя и фамилию ученика:")
        return
    if len(name) > students_svc.STUDENT_NAME_MAX:
        await message.answer(
            f"Слишком длинное имя (максимум {students_svc.STUDENT_NAME_MAX} "
            "символов). Пришли ещё раз:"
        )
        return

    async with get_session_factory()() as session:
        subjects = await people_svc.list_active_subjects(session)
    if not subjects:
        await message.answer("Сначала владелец должен создать предметы. Визард отменён.")
        await state.clear()
        return

    await state.update_data(name=name, selected=[])
    await state.set_state(AddStudentStates.subjects)
    await message.answer(
        "Выбери предметы ученика (можно несколько):",
        reply_markup=multiselect_kb(subjects, set(), "mgr:as", "mgr:as:done:0"),
    )


@router.callback_query(F.data.startswith("mgr:as:"), AddStudentStates.subjects)
@require_role("manager", "owner")
async def cb_student_subjects(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.data == "mgr:as:done:0":
        state_data = await state.get_data()
        selected = set(state_data.get("selected") or [])
        async with get_session_factory()() as session:
            subjects = await people_svc.list_active_subjects(session)
        if not (selected & {s.id for s in subjects}):
            await callback.answer("Выбери хотя бы один предмет!", show_alert=True)
            return
        await state.update_data(selected=sorted(selected & {s.id for s in subjects}))
        await state.set_state(AddStudentStates.date)
        await callback.answer()
        await callback.message.answer(
            "До какого числа доступ? (ДД.ММ.ГГГГ)\nНапример: 31.05.2027"
        )
        return

    subject_id = _parse_id(callback.data, 2)
    if subject_id is None:
        await callback.answer(MSG_STALE, show_alert=True)
        return

    state_data = await state.get_data()
    selected = set(state_data.get("selected") or [])
    if subject_id in selected:
        selected.remove(subject_id)
    else:
        selected.add(subject_id)
    await state.update_data(selected=sorted(selected))

    async with get_session_factory()() as session:
        subjects = await people_svc.list_active_subjects(session)
    await callback.answer()
    await safe_edit(
        callback.message,
        "Выбери предметы ученика (можно несколько):",
        multiselect_kb(subjects, selected, "mgr:as", "mgr:as:done:0"),
    )


@router.message(AddStudentStates.date, ~F.text.startswith("/"))
@require_role("manager", "owner")
async def on_student_date(
    message: Message, state: FSMContext, db_user=None, bot=None
) -> None:
    state_data = await state.get_data()
    name = state_data.get("name", "")
    selected = set(state_data.get("selected") or [])

    access_until = parse_date_input(message.text or "")
    if access_until is None:
        await message.answer(MSG_BAD_DATE)
        return
    if access_until <= today_minsk():
        await message.answer(MSG_PAST_DATE)
        return

    try:
        async with get_session_factory()() as session:
            _, _, code = await students_svc.create_student_record(
                session,
                name,
                selected,
                invited_by_id=db_user.id,
                access_until=access_until,
            )
    except Exception:
        logger.exception("Не удалось создать ученика")
        await message.answer("Что-то пошло не так, попробуй ещё раз.")
        await state.clear()
        return

    bot_username = await invite_svc.get_bot_username(bot)
    link = invite_svc.invite_link(bot_username, code)
    try:
        await message.answer(
            f"✅ Ученик создан!\n\n"
            f"👨🎓 {esc(name)}\n"
            f"📅 Доступ: до {format_date(access_until)}\n"
            f"🔗 Код: <code>{code}</code> (не активирован)\n\n"
            f"📩 Отправь ученику ссылку-приглашение:\n"
            f"<code>{link}</code>\n\n"
            f"Или просто код — ученик введёт его в боте.",
            reply_markup=add_student_continue_kb(),
        )
    except Exception:
        logger.exception("Не удалось отправить сообщение о создании ученика")
    await state.clear()


# ==========================================================================
# Продлить доступ
# ==========================================================================
@router.callback_query(F.data.startswith("mgr:extend:"))
@require_role("manager", "owner")
async def cb_extend_start(callback: CallbackQuery, state: FSMContext) -> None:
    student_id = _parse_id(callback.data, 2)
    if student_id is None:
        await callback.answer(MSG_STALE, show_alert=True)
        return
    async with get_session_factory()() as session:
        card = await students_svc.get_student_card(session, student_id)
    if card is None:
        await callback.answer(MSG_STUDENT_NOT_FOUND, show_alert=True)
        return
    await callback.answer()
    await state.clear()
    await state.update_data(student_id=student_id)
    await state.set_state(ExtendAccessStates.date)
    await callback.message.answer(
        "До какого числа продлить доступ? (ДД.ММ.ГГГГ)\nНапример: 15.06.2027"
    )


@router.message(ExtendAccessStates.date, ~F.text.startswith("/"))
@require_role("manager", "owner")
async def on_extend_date(message: Message, state: FSMContext) -> None:
    state_data = await state.get_data()
    student_id = state_data.get("student_id")

    new_date = parse_date_input(message.text or "")
    if new_date is None:
        await message.answer(MSG_BAD_DATE)
        return
    if new_date <= today_minsk():
        await message.answer(MSG_PAST_DATE)
        return

    async with get_session_factory()() as session:
        ok = await students_svc.extend_access(session, student_id, new_date)
    if not ok:
        await message.answer(MSG_STUDENT_NOT_FOUND)
        await state.clear()
        return

    await message.answer(f"✅ Готово: доступ до {format_date(new_date)}.")
    await state.clear()
    if student_id is not None:
        await _send_card(message, student_id)


# ==========================================================================
# Новый код приглашения
# ==========================================================================
@router.callback_query(F.data.startswith("mgr:newcode:"))
@require_role("manager", "owner")
async def cb_new_code(callback: CallbackQuery, bot=None) -> None:
    student_id = _parse_id(callback.data, 2)
    if student_id is None:
        await callback.answer(MSG_STALE, show_alert=True)
        return
    async with get_session_factory()() as session:
        code = await students_svc.regenerate_invite_code(session, student_id)
    if code is None:
        await callback.answer(
            "Новый код можно выдать только не привязанному ученику",
            show_alert=True,
        )
        return

    bot_username = await invite_svc.get_bot_username(bot)
    link = invite_svc.invite_link(bot_username, code)
    await callback.answer()
    await callback.message.answer(
        f"🔁 Новый код приглашения:\n<code>{code}</code>\n\n"
        f"Ссылка:\n<code>{link}</code>\n\n"
        "Старый код больше не действует."
    )
    await _send_card(callback.message, student_id)


# ==========================================================================
# Деактивация / активация ученика
# ==========================================================================
@router.callback_query(F.data.startswith("mgr:deactivate:"))
@require_role("manager", "owner")
async def cb_deactivate_ask(callback: CallbackQuery) -> None:
    student_id = _parse_id(callback.data, 2)
    if student_id is None:
        await callback.answer(MSG_STALE, show_alert=True)
        return
    async with get_session_factory()() as session:
        card = await students_svc.get_student_card(session, student_id)
    if card is None:
        await callback.answer(MSG_STUDENT_NOT_FOUND, show_alert=True)
        return
    name = card["user"].tg_full_name or ""
    await callback.answer()
    await safe_edit(
        callback.message,
        f"Деактивировать {esc(name)}?\n\n"
        "Ученик потеряет доступ к боту, но данные и история сохранятся. "
        "Вернуть можно в любой момент кнопкой «Активировать».",
        confirm_deactivate_kb(student_id),
    )


@router.callback_query(F.data.startswith("mgr:deact:yes:"))
@require_role("manager", "owner")
async def cb_deact_yes(callback: CallbackQuery) -> None:
    student_id = _parse_id(callback.data, 3)
    if student_id is None:
        await callback.answer(MSG_STALE, show_alert=True)
        return
    async with get_session_factory()() as session:
        ok = await students_svc.set_student_active(session, student_id, False)
    if not ok:
        await callback.answer(MSG_STUDENT_NOT_FOUND, show_alert=True)
        return
    # Перерисовка ДО answer: карточка могла исчезнуть (гонка) — тогда
    # отвечаем алертом, а не молчим после обычного answer
    if not await _render_card_to(callback.message, student_id):
        await callback.answer(MSG_STUDENT_NOT_FOUND, show_alert=True)
        return
    await callback.answer()


@router.callback_query(F.data.startswith("mgr:deact:no:"))
@require_role("manager", "owner")
async def cb_deact_no(callback: CallbackQuery) -> None:
    student_id = _parse_id(callback.data, 3)
    if student_id is None:
        await callback.answer(MSG_STALE, show_alert=True)
        return
    if not await _render_card_to(callback.message, student_id):
        await callback.answer(MSG_STUDENT_NOT_FOUND, show_alert=True)
        return
    await callback.answer()


@router.callback_query(F.data.startswith("mgr:activate:"))
@require_role("manager", "owner")
async def cb_activate(callback: CallbackQuery) -> None:
    student_id = _parse_id(callback.data, 2)
    if student_id is None:
        await callback.answer(MSG_STALE, show_alert=True)
        return
    async with get_session_factory()() as session:
        ok = await students_svc.set_student_active(session, student_id, True)
    if not ok:
        await callback.answer(MSG_STUDENT_NOT_FOUND, show_alert=True)
        return
    if not await _render_card_to(callback.message, student_id):
        await callback.answer(MSG_STUDENT_NOT_FOUND, show_alert=True)
        return
    await callback.answer()


# ==========================================================================
# Полное удаление ученика (UX-пакет) — только текстом «удалить»
# ==========================================================================
TEXT_DEL_STUDENT_ASK = (
    "🗑 Удалить ученика <b>{name}</b> навсегда?\n\n"
    "Удалятся все попытки, прогресс, привязка и код. Безвозвратно.\n\n"
    "Напиши слово «удалить», чтобы подтвердить."
)
TEXT_DEL_STUDENT_NOT_MATCH = (
    "Слово не совпало. Напиши «удалить», чтобы подтвердить удаление."
)
TEXT_DEL_STUDENT_OK = "🗑 Ученик удалён навсегда."


@router.callback_query(F.data.startswith("mgr:del:"))
@require_role("manager", "owner")
async def cb_student_delete(callback: CallbackQuery, state: FSMContext) -> None:
    """«🗑 Удалить навсегда» из карточки → подтверждение словом «удалить»."""
    student_id = _parse_id(callback.data, 2)
    if student_id is None:
        await callback.answer(MSG_STALE, show_alert=True)
        return
    async with get_session_factory()() as session:
        card = await students_svc.get_student_card(session, student_id)
    if card is None:
        await callback.answer(MSG_STUDENT_NOT_FOUND, show_alert=True)
        return
    name = card["user"].tg_full_name or "ученика"
    await state.clear()
    await state.update_data(student_id=student_id)
    await state.set_state(DeleteStudentStates.confirm)
    await callback.answer()
    await safe_edit(
        callback.message,
        TEXT_DEL_STUDENT_ASK.format(name=esc(name)),
        confirm_del_student_kb(student_id),
    )


@router.callback_query(F.data.startswith("mgr:del_no:"))
@require_role("manager", "owner")
async def cb_student_delete_cancel(
    callback: CallbackQuery, state: FSMContext
) -> None:
    """«Отмена» → обратно в карточку ученика."""
    student_id = _parse_id(callback.data, 2)
    if student_id is None:
        await callback.answer(MSG_STALE, show_alert=True)
        return
    await state.clear()
    await callback.answer()
    if not await _render_card_to(callback.message, student_id):
        await callback.answer(MSG_STUDENT_NOT_FOUND, show_alert=True)
        return


@router.message(DeleteStudentStates.confirm, ~F.text.startswith("/"))
@require_role("manager", "owner")
async def on_delete_student_confirm(
    message: Message, state: FSMContext, db_user=None
) -> None:
    """Текст в визарде удаления: «удалить» → удаление + список учеников."""
    state_data = await state.get_data()
    student_id = state_data.get("student_id")
    if student_id is None:
        await message.answer(MSG_STALE)
        await state.clear()
        return
    if (message.text or "").strip() != "удалить":
        await message.answer(TEXT_DEL_STUDENT_NOT_MATCH)
        return

    async with get_session_factory()() as session:
        ok = await students_svc.delete_student(session, student_id)
    await state.clear()
    if not ok:
        await message.answer(MSG_STUDENT_NOT_FOUND)
        return
    await message.answer(TEXT_DEL_STUDENT_OK)
    text, kb = await _students_view(_view_role(db_user))
    await message.answer(text, reply_markup=kb)


@router.message(DeleteStudentStates.confirm, F.photo | F.document)
@require_role("manager", "owner")
async def on_delete_student_confirm_nontext(
    message: Message, db_user=None
) -> None:
    """Медиа в визарде удаления — подсказка, состояние не теряется."""
    await message.answer(TEXT_DEL_STUDENT_NOT_MATCH)


# ==========================================================================
# Истекающие
# ==========================================================================
async def _expiring_view(role: str = "manager") -> tuple[str, object]:
    async with get_session_factory()() as session:
        groups = await students_svc.list_expiring(session)

    back_cb = "menu:back:manager:0" if role == "manager" else "menu:back:owner:0"
    lines: list[str] = []
    items: list[tuple[str, int]] = []
    for key, title in EXPIRING_GROUP_TITLES:
        rows = groups.get(key, [])
        if not rows:
            continue
        lines.append(f"{title} ({len(rows)}):")
        for row in rows:
            date_str = format_date(row["access_until"])
            suffix = f" (просрочка {row['overdue_days']} дн.)" if row["overdue_days"] else ""
            # Текст — с экранированием (HTML), кнопка — с отложенным esc(kb)
            lines.append(f"• {esc(row['name'])} — до {date_str}{suffix}")
            items.append((f"• {row['name']} — до {date_str}{suffix}", row["id"]))
    if not items:
        return TEXT_EXPIRING_EMPTY, expiring_kb([], back_cb=back_cb)
    return "\n".join(lines), expiring_kb(items, back_cb=back_cb)


@router.message(Command("expiring"))
@require_role("manager", "owner")
async def cmd_expiring(
    message: Message, state: FSMContext = None, db_user=None
) -> None:
    if state is not None:
        await state.clear()  # раздел 0 дефект 1: команда отменяет визард
    text, kb = await _expiring_view(_view_role(db_user))
    await message.answer(text, reply_markup=kb)


@router.callback_query(F.data == "menu:manager:expiring:0")
@require_role("manager", "owner")
async def cb_expiring(callback: CallbackQuery, db_user=None) -> None:
    await callback.answer()
    text, kb = await _expiring_view(_view_role(db_user))
    await safe_edit(callback.message, text, kb)
