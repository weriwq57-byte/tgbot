"""Владелец: преподаватели, менеджеры, предметы, кик.

Все хендлеры под @require_role("owner") (ошибка №9 старой версии — защита
не обходится). Роль всегда из БД (data['db_user']), callback-данные —
только ключи для поиска. «Убрать»/«Кик» = is_active=False, данные и
история сохраняются. После каждого действия — перерисовка текущего
экрана через safe_edit (повторные клики идемпотентны).

«Добавить преподавателя/менеджера» принимает @username, число (tg_id)
или выбор из гостей, уже заходивших в бота (для людей без @).
"""
import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy import func, select

from app.bot import sync_commands
from app.database import get_session_factory
from app.keyboards.inline import (
    confirm_kb,
    guest_pick_entry_kb,
    guest_people_list_kb,
    kick_categories_kb,
    multiselect_kb,
    owner_managers_menu_kb,
    owner_subjects_menu_kb,
    owner_teachers_menu_kb,
    people_list_kb,
    subject_toggle_kb,
    subject_delete_kb,
    subject_delete_confirm_kb,
)
from app.models import Subject, User
from app.services import people as people_svc
from app.states import (
    AddManagerStates,
    AddSubjectStates,
    AddTeacherStates,
    DeleteSubjectStates,
)
from app.utils.format import esc
from app.utils.messages import safe_edit
from app.utils.roles import require_role

logger = logging.getLogger(__name__)

router = Router()

# Категории для «Кикнуть»: (родительный мн.ч., родительный ед.ч.)
KICK_LABELS = {
    "student": ("учеников", "ученика"),
    "teacher": ("преподавателей", "преподавателя"),
    "manager": ("менеджеров", "менеджера"),
}

MSG_STALE = "Кнопка устарела, начни заново."

TEXT_TEACHERS_MENU = "👨🏫 Управление преподавателями:"
TEXT_MANAGERS_MENU = "👥 Управление менеджерами:"
TEXT_SUBJECTS_MENU = "📚 Управление предметами:"
TEXT_KICK_MENU = "🔨 Кого деактивировать?"
TEXT_NOBODY = "Пока никого нет"
TEXT_NO_SUBJECTS = "Предметов пока нет"


def person_full_label(user: User) -> str:
    """Полное описание человека для текстов сообщений (с экранированием)."""
    parts = []
    if user.tg_full_name:
        parts.append(esc(user.tg_full_name))
    if user.tg_username:
        parts.append(f"@{esc(user.tg_username)}")
    if user.tg_id:
        parts.append(f"tg_id: {user.tg_id}")
    return " ".join(parts) or f"ID {user.id}"


async def _get_person(session, user_id: int) -> User | None:
    return await session.get(User, user_id)


# ==========================================================================
# Подменю владельца
# ==========================================================================
@router.callback_query(F.data == "menu:owner:teachers:0")
@require_role("owner")
async def cb_owner_teachers_menu(callback: CallbackQuery) -> None:
    await callback.answer()
    await safe_edit(callback.message, TEXT_TEACHERS_MENU, owner_teachers_menu_kb())


@router.callback_query(F.data == "menu:owner:managers:0")
@require_role("owner")
async def cb_owner_managers_menu(callback: CallbackQuery) -> None:
    await callback.answer()
    await safe_edit(callback.message, TEXT_MANAGERS_MENU, owner_managers_menu_kb())


@router.callback_query(F.data == "menu:owner:subjects:0")
@require_role("owner")
async def cb_owner_subjects_menu(callback: CallbackQuery) -> None:
    await callback.answer()
    await safe_edit(callback.message, TEXT_SUBJECTS_MENU, owner_subjects_menu_kb())


@router.callback_query(F.data == "menu:owner:kick:0")
@require_role("owner")
async def cb_owner_kick_menu(callback: CallbackQuery) -> None:
    await callback.answer()
    await safe_edit(callback.message, TEXT_KICK_MENU, kick_categories_kb())


# ==========================================================================
# Добавить преподавателя (визард: @username → мультивыбор предметов)
# ==========================================================================
@router.callback_query(F.data == "owner:add_teacher:0")
@require_role("owner")
async def cb_add_teacher(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.clear()
    await state.set_state(AddTeacherStates.username)
    await callback.message.answer(
        "Пришли <b>@username</b> преподавателя (например @ivanov_math) "
        "или его <b>ID</b> числом. Если username нет — выбери из заходивших:",
        reply_markup=guest_pick_entry_kb("teacher"),
    )


@router.message(AddTeacherStates.username)
@require_role("owner")
async def on_teacher_username(message: Message, state: FSMContext) -> None:
    """Шаг 1: @username, tg_id числом или выбор гостя (кнопкой)."""
    target = people_svc.parse_target_input(message.text or "")
    if isinstance(target, int):
        await state.update_data(tg_id=target)
        await state.set_state(AddTeacherStates.subjects)
        await _ask_subjects(message, state)
        return

    username = target
    if username is None:
        await message.answer(
            "Не похоже на @username или ID: пришли @username (например "
            "@ivanov_math) или числовой ID. Или нажми «👥 Выбрать из заходивших»:"
        )
        return

    async with get_session_factory()() as session:
        existing = await session.scalar(
            select(User).where(func.lower(User.tg_username) == username.lower())
        )
        if existing is not None:
            if existing.role == "student" and existing.is_active:
                await message.answer(
                    f"Пользователь @{esc(username)} сейчас ученик — его роль "
                    "нельзя заменить на преподавателя. Деактивируй сначала "
                    "(запись появится в менеджерском разделе → следующий заход)."
                )
                await state.clear()
                return
            if existing.role == "owner":
                await message.answer("Владельца нельзя назначить преподавателем.")
                await state.clear()
                return
            # Активный препод/менеджер, деактивированный или гость —
            # сервис add_teacher переключит роль без шага «убрать».

    await state.update_data(username=username)
    await state.set_state(AddTeacherStates.subjects)
    await _ask_subjects(message, state)


@router.message(AddTeacherStates.subjects)
@require_role("owner")
async def on_teacher_subjects_text(message: Message) -> None:
    """Случайный текст в мультивыборе — не теряем молча (подсказка)."""
    await message.answer(
        "Это окно управляется кнопками: отметь предметы и нажми «✅ Готово»."
    )


@router.callback_query(F.data.startswith("owner:guestpick:"))
@require_role("owner")
async def cb_guest_pick_list(callback: CallbackQuery) -> None:
    """Список гостей (заходивших в бота) для привязки без @username."""
    try:
        role = callback.data.split(":")[2]
    except IndexError:
        await callback.answer(MSG_STALE, show_alert=True)
        return
    if role not in ("teacher", "manager"):
        await callback.answer(MSG_STALE, show_alert=True)
        return

    async with get_session_factory()() as session:
        guests = await people_svc.list_guests(session)

    if not guests:
        await callback.answer("Пока никто не заходил в бота без @", show_alert=True)
        return
    await callback.answer()
    await safe_edit(
        callback.message,
        "Кто из них должен стать "
        f"{'преподавателем' if role == 'teacher' else 'менеджером'}?",
        guest_people_list_kb(guests, pick_prefix=f"owner:guestsel:{role}"),
    )


@router.callback_query(F.data.startswith("owner:guestsel:"))
@require_role("owner")
async def cb_guest_select(
    callback: CallbackQuery, state: FSMContext, bot=None
) -> None:
    """Выбран гость: для препода — далее мультивыбор, для менеджера — сразу."""
    try:
        role = callback.data.split(":")[2]
        user_id = int(callback.data.split(":")[3])
    except (ValueError, IndexError):
        await callback.answer(MSG_STALE, show_alert=True)
        return
    if role not in ("teacher", "manager"):
        await callback.answer(MSG_STALE, show_alert=True)
        return

    async with get_session_factory()() as session:
        guest = await session.get(User, user_id)
    if guest is None or guest.role != "guest" or guest.tg_id is None:
        await callback.answer("Этот гость уже привязан", show_alert=True)
        await safe_edit(callback.message, TEXT_TEACHERS_MENU, owner_teachers_menu_kb())
        return

    if role == "teacher":
        await state.update_data(tg_id=guest.tg_id)
        await state.set_state(AddTeacherStates.subjects)
        await _ask_subjects(callback.message, state)
        return

    # Менеджер: создаём сразу (без выбора предметов)
    try:
        async with get_session_factory()() as session:
            manager = await people_svc.add_manager_by_tg_id(session, guest.tg_id)
    except ValueError as exc:
        await callback.answer(str(exc), show_alert=True)
        await state.clear()
        return
    label = person_full_label(manager)
    await callback.answer()
    await safe_edit(
        callback.message,
        f"✅ Менеджер {label} добавлен (tg_id: {guest.tg_id}).",
        owner_managers_menu_kb(),
    )
    await state.clear()
    # Менеджер из гостя — команды нижнего меню по новой роли
    await sync_commands(bot, manager)


async def _ask_subjects(message: Message, state: FSMContext) -> None:
    """Показ мультивыбора предметов для преподавателя."""
    async with get_session_factory()() as session:
        subjects = await people_svc.list_active_subjects(session)
    if not subjects:
        await message.answer(
            "Сначала создай предметы (📚 Предметы → ➕ Добавить). Визард отменён."
        )
        await state.clear()
        return
    # Храним список (не set): FSM-хранилища сериализуют данные в JSON
    await state.update_data(selected=[])
    await message.answer(
        "Выбери предметы преподавателя (можно несколько):",
        reply_markup=multiselect_kb(subjects, set(), "owner:at", "owner:at:done:0"),
    )


@router.callback_query(F.data.startswith("owner:at:"))
@require_role("owner")
async def cb_at_toggle(callback: CallbackQuery, state: FSMContext, bot=None) -> None:
    """Переключение предмета в мультивыборе / «Готово»."""
    if callback.data == "owner:at:done:0":
        await _save_add_teacher(callback, state, bot)
        return

    try:
        subject_id = int(callback.data.split(":")[2])  # owner:at:{id}:t:0
    except (ValueError, IndexError):
        await callback.answer(MSG_STALE, show_alert=True)
        return

    async with get_session_factory()() as session:
        subject = await session.get(Subject, subject_id)
    if subject is None:
        await callback.answer("Предмет больше не существует", show_alert=True)
        return

    state_data = await state.get_data()
    selected: set[int] = set(state_data.get("selected") or [])
    if subject_id in selected:
        selected.remove(subject_id)
    else:
        selected.add(subject_id)
    await state.update_data(selected=sorted(selected))

    await callback.answer()
    async with get_session_factory()() as session:
        subjects = await people_svc.list_active_subjects(session)
    await safe_edit(
        callback.message,
        "Выбери предметы преподавателя (можно несколько):",
        multiselect_kb(subjects, selected, "owner:at", "owner:at:done:0"),
    )


async def _save_add_teacher(
    callback: CallbackQuery, state: FSMContext, bot=None
) -> None:
    """Финал визарда: создание преподавателя и связей с предметами."""
    state_data = await state.get_data()
    username: str = state_data.get("username", "")
    tg_id: int | None = state_data.get("tg_id")
    selected: set[int] = set(state_data.get("selected") or [])

    if not username and tg_id is None:
        await callback.answer(MSG_STALE, show_alert=True)
        await safe_edit(callback.message, TEXT_TEACHERS_MENU, owner_teachers_menu_kb())
        return

    async with get_session_factory()() as session:
        subjects = await people_svc.list_active_subjects(session)
        valid_ids = {s.id for s in subjects}
        chosen = selected & valid_ids
        if not chosen:
            await callback.answer("Выбери хотя бы один предмет!", show_alert=True)
            return

        try:
            if tg_id is not None:
                teacher = await people_svc.add_teacher_by_tg_id(
                    session, tg_id, chosen
                )
            else:
                teacher = await people_svc.add_teacher(session, username, chosen)
        except ValueError as exc:
            await callback.answer(str(exc), show_alert=True)
            await state.clear()
            await safe_edit(
                callback.message, TEXT_TEACHERS_MENU, owner_teachers_menu_kb()
            )
            return

        names = ", ".join(esc(s.name) for s in subjects if s.id in chosen)

    who = f"@{esc(username)}" if username else person_full_label(teacher)
    await callback.answer()
    await safe_edit(
        callback.message,
        f"✅ Преподаватель {who} добавлен.\nПредметы: {names}",
        owner_teachers_menu_kb(),
    )
    await state.clear()
    # Новый препод (или смена роли) — команды нижнего меню по его ролям
    await sync_commands(bot, teacher)
    logger.info(
        "Визард препода завершён: id=%s @%s tg_id=%s",
        teacher.id, username or "-", tg_id or "-",
    )


# ==========================================================================
# Добавить менеджера (визард: @username)
# ==========================================================================
@router.callback_query(F.data == "owner:add_manager:0")
@require_role("owner")
async def cb_add_manager(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.clear()
    await state.set_state(AddManagerStates.username)
    await callback.message.answer(
        "Пришли <b>@username</b> менеджера (например @manager_anna) "
        "или его <b>ID</b> числом. Если username нет — выбери из заходивших:",
        reply_markup=guest_pick_entry_kb("manager"),
    )


@router.message(AddManagerStates.username)
@require_role("owner")
async def on_manager_username(
    message: Message, state: FSMContext, bot=None
) -> None:
    """Единственный шаг визарда менеджера: @username или tg_id числом."""
    target = people_svc.parse_target_input(message.text or "")

    if isinstance(target, int):
        try:
            async with get_session_factory()() as session:
                manager = await people_svc.add_manager_by_tg_id(session, target)
        except ValueError as exc:
            await message.answer(str(exc))
            await state.clear()
            return
        label = person_full_label(manager)
        await message.answer(f"✅ Менеджер {label} добавлен (tg_id: {target}).")
        await state.clear()
        # Новый менеджер (или смена роли) — команды нижнего меню по ролям
        await sync_commands(bot, manager)
        return

    username = target
    if username is None:
        await message.answer(
            "Не похоже на @username или ID: пришли @username (например "
            "@manager_anna) или числовой ID. Или нажми «👥 Выбрать из заходивших»:"
        )
        return

    try:
        async with get_session_factory()() as session:
            await people_svc.add_manager(session, username)
    except ValueError as exc:
        await message.answer(str(exc))
        await state.clear()
        return

    await message.answer(f"✅ Менеджер @{esc(username)} добавлен.")
    await state.clear()


# ==========================================================================
# Убрать преподавателя / менеджера (список → подтверждение → деактивация)
# ==========================================================================
async def _show_remove_people(callback: CallbackQuery, role: str) -> None:
    """Список активных людей роли для удаления (или «Пока никого нет»)."""
    async with get_session_factory()() as session:
        people = await people_svc.list_active_people(session, role)

    if not people:
        await safe_edit(
            callback.message,
            f"{TEXT_NOBODY} ({'преподаватели' if role == 'teacher' else 'менеджеры'})",
            owner_teachers_menu_kb() if role == "teacher" else owner_managers_menu_kb(),
        )
        return

    prefix = "owner:rt:pick" if role == "teacher" else "owner:rm:pick"
    question = (
        "Выбери преподавателя, которого убрать:"
        if role == "teacher"
        else "Выбери менеджера, которого убрать:"
    )
    back_cb = "owner:rt:list:0" if role == "teacher" else "owner:rm:list:0"
    await safe_edit(
        callback.message,
        question,
        people_list_kb(people, prefix, back_cb=back_cb),
    )


async def _confirm_remove(
    callback: CallbackQuery, role: str, menu_text: str
) -> None:
    """Подтверждение удаления человека (проверка роли/активности)."""
    try:
        user_id = int(callback.data.split(":")[3])
    except (ValueError, IndexError):
        await callback.answer(MSG_STALE, show_alert=True)
        return
    async with get_session_factory()() as session:
        user = await _get_person(session, user_id)

    kind = "Преподаватель" if role == "teacher" else "Менеджер"
    if user is None or role not in user.role_set or not user.is_active:
        await callback.answer(f"{kind} уже убран", show_alert=True)
        await safe_edit(callback.message, menu_text,
                        owner_teachers_menu_kb() if role == "teacher" else owner_managers_menu_kb())
        return

    yes_prefix = "owner:rt:yes" if role == "teacher" else "owner:rm:yes"
    keeps_other = len(user.role_set) > 1
    question = (
        f"Убрать {kind.lower()} <b>{person_full_label(user)}</b>?\n\n"
        + (
            "Вторая роль сохранится, доступ открыт, данные не тронутся."
            if keeps_other
            else "Доступ закроется, но данные и история сохранятся."
        )
    )
    await callback.answer()
    await safe_edit(
        callback.message,
        question,
        confirm_kb(f"{yes_prefix}:{user_id}:0", f"owner:rt:no:0" if role == "teacher" else "owner:rm:no:0",
                   "Да, убрать", "Отмена"),
    )


async def _confirm_remove_done(
    callback: CallbackQuery, role: str, menu_text: str
) -> None:
    """Деактивация после подтверждения + перерисовка подменю.

Совмещённые роли: снимается только выбранная (remove_role → «stripped»),
деактивируется лишь человек ровно с одной ролью («deactivated»).
"""
    try:
        user_id = int(callback.data.split(":")[3])
    except (ValueError, IndexError):
        await callback.answer(MSG_STALE, show_alert=True)
        return
    async with get_session_factory()() as session:
        user = await _get_person(session, user_id)
        result = await people_svc.remove_role(session, user_id, role)

    kind = "Преподаватель" if role == "teacher" else "Менеджер"
    if not result:
        await callback.answer(f"{kind} уже убран", show_alert=True)
        await safe_edit(callback.message, menu_text,
                        owner_teachers_menu_kb() if role == "teacher" else owner_managers_menu_kb())
        return

    label = person_full_label(user) if user is not None else f"ID {user_id}"
    await callback.answer()
    if result == "stripped":
        other = "менеджер" if role == "teacher" else "преподаватель"
        message = f"✅ {kind} {label} убран. Роль «{other}» сохранена, доступ открыт."
    else:
        message = f"✅ {kind} {label} убран, доступ закрыт."
    await safe_edit(
        callback.message,
        message,
        owner_teachers_menu_kb() if role == "teacher" else owner_managers_menu_kb(),
    )


@router.callback_query(F.data == "owner:rt:list:0")
@require_role("owner")
async def cb_rt_list(callback: CallbackQuery) -> None:
    await callback.answer()
    await _show_remove_people(callback, "teacher")


@router.callback_query(F.data.startswith("owner:rt:pick:"))
@require_role("owner")
async def cb_rt_pick(callback: CallbackQuery) -> None:
    await _confirm_remove(callback, "teacher", TEXT_TEACHERS_MENU)


@router.callback_query(F.data.startswith("owner:rt:yes:"))
@require_role("owner")
async def cb_rt_yes(callback: CallbackQuery) -> None:
    await _confirm_remove_done(callback, "teacher", TEXT_TEACHERS_MENU)


@router.callback_query(F.data == "owner:rt:no:0")
@require_role("owner")
async def cb_rt_no(callback: CallbackQuery) -> None:
    await callback.answer()
    await safe_edit(callback.message, "Отменено.", owner_teachers_menu_kb())


@router.callback_query(F.data == "owner:rm:list:0")
@require_role("owner")
async def cb_rm_list(callback: CallbackQuery) -> None:
    await callback.answer()
    await _show_remove_people(callback, "manager")


@router.callback_query(F.data.startswith("owner:rm:pick:"))
@require_role("owner")
async def cb_rm_pick(callback: CallbackQuery) -> None:
    await _confirm_remove(callback, "manager", TEXT_MANAGERS_MENU)


@router.callback_query(F.data.startswith("owner:rm:yes:"))
@require_role("owner")
async def cb_rm_yes(callback: CallbackQuery) -> None:
    await _confirm_remove_done(callback, "manager", TEXT_MANAGERS_MENU)


@router.callback_query(F.data == "owner:rm:no:0")
@require_role("owner")
async def cb_rm_no(callback: CallbackQuery) -> None:
    await callback.answer()
    await safe_edit(callback.message, "Отменено.", owner_managers_menu_kb())


# ==========================================================================
# Предметы: добавить / скрыть-показать
# ==========================================================================
@router.callback_query(F.data == "owner:add_subject:0")
@require_role("owner")
async def cb_add_subject(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.clear()
    await state.set_state(AddSubjectStates.name)
    await callback.message.answer(
        "Как назвать предмет? Например: «Математика»"
    )


@router.message(AddSubjectStates.name)
@require_role("owner")
async def on_subject_name(message: Message, state: FSMContext) -> None:
    """Название предмета: создание с проверкой дублей."""
    try:
        async with get_session_factory()() as session:
            subject = await people_svc.create_subject(session, message.text or "")
    except ValueError as exc:
        await message.answer(str(exc))
        return  # остаёмся в визарде: можно ввести другое имя

    await message.answer(f"✅ Предмет «{esc(subject.name)}» создан.")
    await state.clear()


@router.callback_query(F.data == "owner:subj:toggle_list:0")
@require_role("owner")
async def cb_subject_toggle_list(callback: CallbackQuery) -> None:
    await callback.answer()
    await _show_subject_toggle(callback.message)


@router.callback_query(F.data == "owner:subj:back:0")
@require_role("owner")
async def cb_subject_back(callback: CallbackQuery) -> None:
    """← Назад из списка скрытой/показа: снова подменю предметов."""
    await callback.answer()
    await safe_edit(callback.message, TEXT_SUBJECTS_MENU, owner_subjects_menu_kb())


async def _show_subject_toggle(message: Message) -> None:
    """Список предметов с переключателем активности."""
    async with get_session_factory()() as session:
        subjects = await people_svc.list_subjects(session)
    if not subjects:
        await safe_edit(
            message, f"{TEXT_NO_SUBJECTS}. Создай: ➕ Добавить",
            owner_subjects_menu_kb(),
        )
        return
    await safe_edit(
        message,
        "Жми на предмет, чтобы скрыть его от учеников или показать снова:",
        subject_toggle_kb(subjects),
    )


@router.callback_query(F.data.startswith("owner:subj:toggle:"))
@require_role("owner")
async def cb_subject_toggle(callback: CallbackQuery) -> None:
    """Скрыть/показать предмет и перерисовать список (идемпотентно)."""
    try:
        subject_id = int(callback.data.split(":")[3])
    except (ValueError, IndexError):
        await callback.answer(MSG_STALE, show_alert=True)
        return

    async with get_session_factory()() as session:
        subject = await people_svc.toggle_subject_active(session, subject_id)
    if subject is None:
        await callback.answer("Предмет больше не существует", show_alert=True)
        await _show_subject_toggle(callback.message)
        return

    await callback.answer()
    await _show_subject_toggle(callback.message)


# ==========================================================================
# Удалить предмет (список → подтверждение вводом названия)
# ==========================================================================
TEXT_DEL_SUBJECT_ASK = (
    "⚠️ Предмет «{name}» будет удалён НАВСЕГДА вместе с темами, заданиями "
    "и прогрессом учеников. Напиши название предмета текстом, чтобы удалить его:"
)
TEXT_DEL_SUBJECT_NOT_MATCH = (
    "Название не совпало — предмет не удалён. Напиши точное название, "
    "или отправь «❌ Отмена»."
)
TEXT_DEL_SUBJECT_OK = "Предмет «{name}» удалён."
TEXT_DEL_SUBJECT_GONE = "Предмет больше не существует"


@router.callback_query(F.data == "owner:subj:del_list:0")
@require_role("owner")
async def cb_subject_delete_list(callback: CallbackQuery) -> None:
    """Список предметов для удаления."""
    await callback.answer()
    async with get_session_factory()() as session:
        subjects = await people_svc.list_subjects(session)
    if not subjects:
        await safe_edit(
            callback.message,
            f"{TEXT_NO_SUBJECTS}. Создай: ➕ Добавить",
            owner_subjects_menu_kb(),
        )
        return
    await safe_edit(
        callback.message,
        "Какой предмет удалить навсегда?",
        subject_delete_kb(subjects),
    )


@router.callback_query(F.data.startswith("owner:subj:del:"))
@require_role("owner")
async def cb_subject_delete(callback: CallbackQuery, state: FSMContext) -> None:
    """Выбор предмета → подтверждение вводом точного названия."""
    try:
        subject_id = int(callback.data.split(":")[3])
    except (ValueError, IndexError):
        await callback.answer(MSG_STALE, show_alert=True)
        return
    async with get_session_factory()() as session:
        subject = await session.get(Subject, subject_id)
    if subject is None:
        await callback.answer(TEXT_DEL_SUBJECT_GONE, show_alert=True)
        return
    await callback.answer()
    await state.clear()
    await state.update_data(subject_id=subject.id, subject_name=subject.name)
    await state.set_state(DeleteSubjectStates.confirm)
    await safe_edit(
        callback.message,
        TEXT_DEL_SUBJECT_ASK.format(name=esc(subject.name)),
        subject_delete_confirm_kb(),
    )


@router.callback_query(F.data == "owner:subj:del_no:0")
@require_role("owner")
async def cb_subject_delete_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.answer()
    await safe_edit(callback.message, TEXT_SUBJECTS_MENU, owner_subjects_menu_kb())


@router.message(DeleteSubjectStates.confirm, ~F.text.startswith("/"))
@require_role("owner")
async def on_subject_delete_confirm(message: Message, state: FSMContext) -> None:
    state_data = await state.get_data()
    subject_id = state_data.get("subject_id")
    subject_name = state_data.get("subject_name")
    if subject_id is None:
        await message.answer(MSG_STALE)
        await state.clear()
        return
    if (message.text or "").strip().lower() != (subject_name or "").strip().lower():
        await message.answer(TEXT_DEL_SUBJECT_NOT_MATCH)
        return
    async with get_session_factory()() as session:
        deleted = await people_svc.delete_subject(session, subject_id)
    if not deleted:
        # гонка: предмет удалён/исчез до подтверждения
        await message.answer(TEXT_DEL_SUBJECT_GONE)
        await state.clear()
        return
    await message.answer(TEXT_DEL_SUBJECT_OK.format(name=esc(subject_name)))
    await state.clear()
    # НЕ safe_edit: message — сообщение ПОЛЬЗОВАТЕЛЯ, редактировать его
    # Telegram не даст; подменю отправляем новым сообщением (как темы).
    await message.answer(TEXT_SUBJECTS_MENU, reply_markup=owner_subjects_menu_kb())


@router.message(DeleteSubjectStates.confirm, F.photo | F.document)
@require_role("owner")
async def on_subject_delete_bad_input(message: Message) -> None:
    await message.answer(TEXT_DEL_SUBJECT_NOT_MATCH)


# ==========================================================================
# Кикнуть (категория → человек → подтверждение → деактивация)
# ==========================================================================
@router.callback_query(F.data.startswith("owner:kick:cat:"))
@require_role("owner")
async def cb_kick_category(callback: CallbackQuery) -> None:
    """Список активных людей категории."""
    try:
        category = callback.data.split(":")[3]
        plural, singular = KICK_LABELS[category]
    except (KeyError, IndexError):
        await callback.answer("Неизвестная категория", show_alert=True)
        return

    role = category  # 'student' | 'teacher' | 'manager'
    async with get_session_factory()() as session:
        people = await people_svc.list_active_people(session, role)

    await callback.answer()
    if not people:
        await safe_edit(
            callback.message, f"Активных {plural} пока нет.", kick_categories_kb()
        )
        return
    await safe_edit(
        callback.message,
        f"Выбери {singular}, которого деактивировать:",
        people_list_kb(people, "owner:kick:pick", back_cb="owner:kick:back:0"),
    )


@router.callback_query(F.data == "owner:kick:back:0")
@require_role("owner")
async def cb_kick_back(callback: CallbackQuery) -> None:
    """← Назад из списка людей: снова выбор категории."""
    await callback.answer()
    await safe_edit(callback.message, TEXT_KICK_MENU, kick_categories_kb())


@router.callback_query(F.data.startswith("owner:kick:pick:"))
@require_role("owner")
async def cb_kick_pick(callback: CallbackQuery, db_user=None) -> None:
    """Подтверждение деактивации (себя деактивировать нельзя)."""
    try:
        user_id = int(callback.data.split(":")[3])
    except (ValueError, IndexError):
        await callback.answer(MSG_STALE, show_alert=True)
        return
    async with get_session_factory()() as session:
        user = await _get_person(session, user_id)

    if user is None or not user.is_active:
        await callback.answer("Пользователь уже деактивирован", show_alert=True)
        await safe_edit(callback.message, TEXT_KICK_MENU, kick_categories_kb())
        return
    if user.id == db_user.id:
        await callback.answer("Себя деактивировать нельзя 🙂", show_alert=True)
        return

    await callback.answer()
    await safe_edit(
        callback.message,
        f"Деактивировать <b>{person_full_label(user)}</b>?\n\n"
        "Доступ закроется, но данные и история сохранятся. "
        "Повторно активировать можно будет у менеджера.",
        confirm_kb(f"owner:kick:yes:{user_id}:0", "owner:kick:no:0",
                   "Да, деактивировать", "Отмена"),
    )


@router.callback_query(F.data.startswith("owner:kick:yes:"))
@require_role("owner")
async def cb_kick_yes(callback: CallbackQuery, db_user=None) -> None:
    """Деактивация после подтверждения + перерисовка экрана кика."""
    try:
        user_id = int(callback.data.split(":")[3])
    except (ValueError, IndexError):
        await callback.answer(MSG_STALE, show_alert=True)
        return
    async with get_session_factory()() as session:
        user = await _get_person(session, user_id)
        ok = await people_svc.deactivate_person(session, user_id)

    if not ok:
        await callback.answer("Пользователь уже деактивирован", show_alert=True)
        await safe_edit(callback.message, TEXT_KICK_MENU, kick_categories_kb())
        return

    label = person_full_label(user) if user is not None else f"ID {user_id}"
    await callback.answer()
    await safe_edit(
        callback.message,
        f"🔨 {label} деактивирован. История сохранена.",
        kick_categories_kb(),
    )


@router.callback_query(F.data == "owner:kick:no:0")
@require_role("owner")
async def cb_kick_no(callback: CallbackQuery) -> None:
    await callback.answer()
    await safe_edit(callback.message, "Отменено.", kick_categories_kb())