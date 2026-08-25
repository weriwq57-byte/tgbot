"""Ученик: привязка по коду, меню, предметы, решение заданий (ТЗ, 6–7, 9–10).

Правила:
- Привязка доступна гостю (/start КОД или текст в GuestBindStates.code);
  подтверждение кнопками «Да, это я» / «Нет, это не я».
- Все экраны ученика (темы, карточки, итоги) — НОВЫМИ сообщениями,
  колбэк отвечает тостом; тексты статусов — дословно из ТЗ.
- Доступ проверяется при выдаче (вход в тему, «Ещё задание»,
  «Повторить тему») — «текущее досматривает»: ответ по выданному
  заданию принимается всегда (ТЗ раздел 10).
- Перехваты /start-с-кодом и menu:back:student:0 здесь же; для остальных
  ролей хендлеры бросают SkipHandler — обработку подхватывают следующие
  роутеры (команды, владелец, менеджер, преподаватель).
"""
import logging

from aiogram import F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.bot import commands_reply_kb, sync_commands
from app.database import get_session_factory
from app.handlers.commands import GUEST_GREETING
from app.keyboards.inline import (
    answer_actions_kb,
    bind_confirm_kb,
    errors_actions_kb,
    errors_done_kb,
    guest_code_kb,
    main_menu_kb,
    stats_kb,
    student_subjects_kb,
    subjects_pick_kb,
    task_kb,
    theme_empty_kb,
    theme_result_kb,
)
from app.services import invite as invite_svc
from app.services import stats as stats_svc
from app.services import student as student_svc
from app.states import GuestBindStates
from app.utils.format import esc, format_date
from app.utils.messages import safe_edit
from app.utils.roles import has_any_role, require_role, user_roles
from sqlalchemy import select
from app.models import Student, Task, Theme, User

logger = logging.getLogger(__name__)

router = Router()

# --- Тексты дословно из ТЗ (разделы 6, 7, 9, 10) ---
MSG_BIND_CODE_NOT_FOUND = "Такого кода нет. Проверь у менеджера 🙂"
MSG_BIND_ALREADY_ACTIVATED = (
    "Этот код уже использован. Если это твой код — напиши менеджеру."
)
MSG_BIND_TG_ALREADY_BOUND = (
    "Этот Telegram уже привязан к профилю. Если это не ты — напиши менеджеру."
)
MSG_BIND_NO = "Понял. Если это твой код — свяжись с менеджером, разберёмся 🙂"
# Предупреждение при повторной привязке ДЕАКТИВИРОВАННОГО staff:
# кикнутого препода/менеджера владелец пересоздал учеником, старая
# запись (связи предметов) удалится каскадом.
MSG_REBIND_STAFF_WARNING = (
    "⚠️ Старый аккаунт (преподаватель/менеджер) будет удалён вместе с данными."
)
MSG_EXPIRED = "Доступ истёк. Напиши менеджеру — продлим за минуту"
MSG_THEME_CLOSED = "Тема закрыта, задания пока не выдаём."
MSG_NOT_FOR_YOU = "Предмет больше не доступен — напиши менеджеру."
MSG_THEME_NOT_FOUND = "Тема больше недоступна."
MSG_TASKS_EMPTY = "Заданий пока нет. Препод скоро добавит 👩🏫"
MSG_STALE = "Кнопка устарела — возьми задание заново."
MSG_STALE_MENU = "Кнопка устарела."
MSG_GONE = "Задание больше недоступно."
MSG_NO_SUBJECTS = "Тебе пока не назначены предметы — напиши менеджеру."
MSG_RETRY_STARTED = "🔁 Начинаем тему заново!"
MSG_CODE_HINT = "Отправь код текстом, например ABC123"

MISSING_TEXT = "(фото-вопрос)"


def _parse_parts(data: str, expected_len: int) -> list[str] | None:
    """Разбивает callback-данные на части; None — битая кнопка."""
    parts = data.split(":")
    return parts if len(parts) >= expected_len else None


async def _send_task_card(message: Message, result: dict, errors: bool = False) -> None:
    """Карточка задания (текст/фото). Единая точка рендера (п.0.1 Захода 8):
    общая для выдачи (вход в тему, «Ещё задание», «Повторить тему») и
    режима «🔁 Ошибки» (шапка «🔁 Ошибки · осталось N», варианты
    перемешаны по дню через perm; кнопки ответа — с суффиксом «:e»).
    """
    task = result["task"]
    progress = result["progress"]
    if result.get("random_mode"):
        # «🎲 Открыть все»: случайные задания, прогресс/итог не показываются
        header = "🎲 Случайное задание"
    elif errors:
        header = (
            f"🔁 Ошибки · осталось {result.get('wrong_remaining') or 0}"
        )
    else:
        header = (
            f"Решено {progress['solved']} из {progress['total']} "
            f"· Осталось {progress['remaining']}"
        )
    kb = task_kb(
        task,
        result["seq"],
        perm=result.get("perm"),
        errors=errors,
        doy=result.get("doy"),
    )
    question_text = (task.question_text or "").strip()
    if task.question_photo_id:
        caption = header
        if question_text:
            caption += f"\n\n{esc(question_text)}"
        await message.answer_photo(
            task.question_photo_id, caption=caption, reply_markup=kb
        )
    else:
        await message.answer(
            f"{header}\n\n{esc(question_text or MISSING_TEXT)}", reply_markup=kb
        )


async def _issue_task_and_send(message: Message, user_id: int, theme_id: int) -> None:
    """Выдача задания: карточка или текст статуса (общий для входа/«Ещё»)."""
    async with get_session_factory()() as session:
        result = await student_svc.issue_task(session, user_id, theme_id)

    status = result["status"]
    if status == student_svc.TASK_ISSUE_EXPIRED:
        await message.answer(MSG_EXPIRED)
        return
    if status == student_svc.TASK_ISSUE_THEME_CLOSED:
        await message.answer(MSG_THEME_CLOSED)
        return
    if status == student_svc.TASK_ISSUE_NOT_FOUND:
        await message.answer(MSG_THEME_NOT_FOUND)
        return
    if status == student_svc.TASK_ISSUE_NOT_FOR_YOU:
        await message.answer(MSG_NOT_FOR_YOU)
        return
    if status == student_svc.TASK_ISSUE_EMPTY:
        # Пустая тема: без «🔁 Повторить тему» (п.0.3 Захода 8) — иначе
        # вечная петля, пока препод не добавит заданий.
        await message.answer(MSG_TASKS_EMPTY, reply_markup=theme_empty_kb(theme_id))
        return
    if status == student_svc.TASK_ISSUE_ALL_DONE:
        await _send_theme_result(message, result["summary"], result)
        return
    await _send_task_card(message, result)


async def _send_theme_result(message: Message, summary: dict, result: dict) -> None:
    """Итог темы + строка нового рекорда + кнопки «Повторить/Другие»."""
    lines = [
        "🏁 <b>Тема пройдена!</b>",
        f"✅ {summary['correct']} правильных, ❌ {summary['wrong']} неправильных",
    ]
    # «И новый рекорд…» — только при РЕАЛЬНОМ побитии (не при догоне
    # до старого рекорда): факт фиксируется register_solved в день ответа
    current = result.get("streak_current") or 0
    if result.get("record_broken") and current > 0:
        lines.append(f"И новый рекорд стрика: {current} дней! 🔥")
    await message.answer(
        "\n".join(lines), reply_markup=theme_result_kb(result["theme_id"])
    )


async def _subjects_pick_view(message: Message, db_user) -> None:
    """Уровень 1: выбор ПРЕДМЕТА кнопками (UX-пакет, вместо простыни тем)."""
    async with get_session_factory()() as session:
        data = await student_svc.student_menu(session, db_user.id)
    if data is None or not data["subjects"]:
        await message.answer(MSG_NO_SUBJECTS)
        return
    roles = user_roles(db_user)
    if "teacher" in roles:
        back_cb = "menu:back:teacher:0"
    elif "student" in roles:
        back_cb = "menu:back:student:0"
    else:
        back_cb = "menu:back:owner:0"
    await message.answer(
        "🎯 <b>Решать задания</b>\nВыбери предмет:",
        reply_markup=subjects_pick_kb(data["subjects"], back_cb=back_cb),
    )


async def _themes_view(message: Message, db_user, subject_id: int) -> None:
    """Уровень 2: темы выбранного предмета (вход в тему = выдача задания)."""
    async with get_session_factory()() as session:
        data = await student_svc.student_menu(session, db_user.id)
        if data is None:
            await message.answer(MSG_NO_SUBJECTS)
            return
        item = next(
            (s for s in data["subjects"] if s["subject"].id == subject_id), None
        )
    if item is None:
        await message.answer(MSG_NOT_FOR_YOU)
        return
    subject = item["subject"]
    header = f"<b>{esc(subject.name)}</b>\nВыбери тему:"
    await message.answer(
        header,
        reply_markup=student_subjects_kb(
            [item],
            back_cb="std:subjects:0",
            self_mode=has_any_role(db_user, "owner", "teacher"),
        ),
    )


async def _streak_menu_text(user, streaks) -> str:
    """Шапка меню ученика (ТЗ раздел 6) + стрики ПО ПРЕДМЕТАМ.

    streaks — результат student_svc.subject_streaks: каждой строкой
    «{предмет} — 🔥 {current} (рекорд: {best})» (владелец, 13.08).
    """
    name = (getattr(user, "tg_full_name", None) or "").strip()
    head = f"Привет, {esc(name)}!" if name else ""
    if streaks:
        lines = [
            f"{esc(s['name'])} — 🔥 {s['current']} (рекорд: {s['best']})"
            for s in streaks
        ]
        return f"{head}\n" + "\n".join(lines) + "\n\nМеню ученика:"
    return f"{head}\n\nМеню ученика:"


async def _bind_greeting_text(session, user) -> str:
    """Приветствие после привязки (дословно ТЗ раздел 7, пункт 6)."""
    data = await student_svc.student_menu(session, user.id)
    if data is None:
        subjects = "—"
        until = None
    else:
        subjects = ", ".join(
            esc(item["subject"].name) for item in data["subjects"]
        ) or "—"
        until = data["student"].access_until
    name = esc(user.tg_full_name or user.tg_username or "друг")
    date_str = format_date(until)
    return f"Привет, {name}! 🎉\nТвой доступ: {subjects} — до {date_str}. Погнали? 🔥"


# --------------------------------------------------------------------------
# Привязка по коду (ТЗ раздел 7)
# --------------------------------------------------------------------------
async def _process_bind_code(
    message: Message, code_text: str, state: FSMContext | None
) -> None:
    """Общий флоу для /start КОД и текста кода: проверки → подтверждение.

    Формат кода (онбординг): ровно 6 символов A-Z/0-9 (после
    prepare_code). Текст, НЕ похожий на код («привет», мусор), — не
    «Такого кода нет», а приветствие гостя с подсказкой и кнопкой
    «🔑 Ввести код»: человек не обязан знать формат.
    """
    if state is not None:
        await state.clear()
    code = student_svc.prepare_code(code_text)
    if not invite_svc.looks_like_code(code):
        await message.answer(GUEST_GREETING, reply_markup=guest_code_kb())
        return
    async with get_session_factory()() as session:
        result = await student_svc.bind_by_code(session, message.from_user.id, code_text)

        status = result["status"]
        if status == student_svc.BIND_CODE_NOT_FOUND:
            await message.answer(MSG_BIND_CODE_NOT_FOUND)
            return
        if status == student_svc.BIND_ALREADY_ACTIVATED:
            await message.answer(MSG_BIND_ALREADY_ACTIVATED)
            return
        if status == student_svc.BIND_TG_ALREADY_BOUND:
            await message.answer(MSG_BIND_TG_ALREADY_BOUND)
            return

        student = result["student"]
        user = await session.get(User, student.user_id)
        name = esc((user.tg_full_name or "").strip()) or "ученик"
        confirm_text = f"Подтверди, что ты — {name}?"
        if result.get("occupant_role") in ("teacher", "manager"):
            # деактивированный staff перепривязывается учеником — предупреждаем
            confirm_text += "\n\n" + MSG_REBIND_STAFF_WARNING
        await message.answer(
            confirm_text,
            reply_markup=bind_confirm_kb(student.invite_code),
        )


@router.message(CommandStart(deep_link=True))
async def cmd_start_with_code(
    message: Message, state: FSMContext = None, db_user=None
) -> None:
    """/start КОД — привязка по deep link (только код; голый /start — commands.py)."""
    parts = (message.text or "").split(None, 1)
    code = parts[1].strip() if len(parts) == 2 else ""
    await _process_bind_code(message, code, state)


@router.message(GuestBindStates.code, ~F.text.startswith("/"))
async def on_bind_code_text(
    message: Message, state: FSMContext = None, db_user=None
) -> None:
    """Текст в GuestBindStates.code — вводимый код приглашения."""
    await _process_bind_code(message, message.text or "", state)


@router.message(~F.text.startswith("/"))
async def on_guest_any_text(
    message: Message, state: FSMContext = None, db_user=None
) -> None:
    """Любой текст гостя ВНЕ визарда — трактуем как код (ТЗ 7 п.3).

    Раньше такой текст молча терялся: состояние после панели подтверждения
    очищено, а гость мог написать код прямо текстом. Мусор получит
    приветствие с подсказкой (формат не известен человеку).

    ДЕАКТИВИРОВАННЫЕ (ученик/гость/staff любой роли) тоже попадают сюда:
    мидлварь пропускает для них текст, похожий на код (повторная привязка
    по новому коду — текст блокировки говорит «напиши его в чат»).
    Активные не-гости — SkipHandler (иначе этот хендлер глотает любой
    текст менеджеров/преподавателей/владельца и текстовые шаги их
    визардов — диспетчер останавливается на первом совпавшем хендлере,
    даже вернувшем None).
    """
    if db_user is None:
        raise SkipHandler
    # getattr: тестовые заглушки db_user могут не иметь is_active (активен)
    if db_user.role != "guest" and getattr(db_user, "is_active", True):
        raise SkipHandler
    await _process_bind_code(message, message.text or "", state)


@router.message(GuestBindStates.code)
async def on_bind_non_text(
    message: Message, state: FSMContext = None, db_user=None
) -> None:
    """Медиа в GuestBindStates.code — подсказка, состояние не теряется."""
    await message.answer(MSG_CODE_HINT)


@router.callback_query(F.data.startswith("std:bind_yes:"))
async def cb_bind_yes(callback: CallbackQuery, db_user=None, bot=None) -> None:
    """«Да, это я» — выполняем привязку, приветствие + меню ученика."""
    parts = _parse_parts(callback.data or "", 3)
    if parts is None:
        await callback.answer(MSG_BIND_ALREADY_ACTIVATED, show_alert=True)
        return
    code = parts[2]
    tg_id = callback.from_user.id
    async with get_session_factory()() as session:
        ok = await student_svc.confirm_bind(session, tg_id, code)
        if ok:
            # Перезагрузка: гость удалён, профиль ученика получил tg_id.
            # Поиск по полю tg_id (не путать с первичным ключом id).
            user = await session.scalar(select(User).where(User.tg_id == tg_id))
            greeting = await _bind_greeting_text(session, user)
            streaks = await student_svc.subject_streaks(session, user.id)
            streak_text = await _streak_menu_text(user, streaks)
    if not ok:
        await callback.answer(MSG_BIND_ALREADY_ACTIVATED, show_alert=True)
        return
    await callback.answer()
    # Reply-клавиатура команд прикреплена к приветствию (без лишнего
    # текстового сообщения) — переключатель возле стикеров.
    await callback.message.answer(
        greeting, reply_markup=commands_reply_kb(("student",))
    )
    await callback.message.answer(
        streak_text, reply_markup=main_menu_kb("student")
    )
    # Гость стал учеником — команды нижнего меню по новой роли
    await sync_commands(bot, user)


@router.callback_query(F.data.startswith("std:bind_no:"))
async def cb_bind_no(callback: CallbackQuery, db_user=None) -> None:
    """«Нет, это не я» — остаёмся гостем (ТЗ 7, пункт 7)."""
    await callback.answer()
    await callback.message.answer(MSG_BIND_NO, reply_markup=main_menu_kb("guest"))


@router.callback_query(F.data == "menu:guest:code:0")
async def cb_menu_guest_code(callback: CallbackQuery, db_user=None, state=None) -> None:
    """«🔑 Ввести код» из меню гостя → запрос кода.

    Не-гостю (кнопка осталась после привязки) — тост «Кнопка устарела»,
    без возврата None: дальше хендлеров для этого колбэка нет.
    """
    if db_user is None or db_user.role != "guest":
        await callback.answer(MSG_STALE_MENU, show_alert=True)
        return
    if state is not None:
        await state.set_state(GuestBindStates.code)
    await callback.answer()
    await callback.message.answer(
        "🔑 <b>Ввести код приглашения</b>", reply_markup=main_menu_kb("guest")
    )
    await callback.message.answer(MSG_CODE_HINT)


# --------------------------------------------------------------------------
# Меню ученика, темы, задания
# --------------------------------------------------------------------------
@router.callback_query(F.data == "menu:back:student:0")
async def cb_back_student(callback: CallbackQuery, db_user=None) -> None:
    """«← Назад» — меню по роли из БД.

    Студенту — шапка со стриком; остальным (кнопка осталась после смены
    роли) — своё меню, без перехвата из commands.py (иначе двойной
    обработчик одного колбэка).
    """
    if db_user is None:
        return None
    if db_user.role == "student":
        async with get_session_factory()() as session:
            streaks = await student_svc.subject_streaks(session, db_user.id)
        text = await _streak_menu_text(db_user, streaks)
    else:
        text = "Меню:"
    await callback.answer()
    await callback.message.edit_text(text, reply_markup=main_menu_kb(user_roles(db_user)))


async def _show_stats(message: Message, student_id: int, period: str = "7") -> None:
    """Дашборд статистики (мирровокная точка рендера)."""
    async with get_session_factory()() as session:
        data = await stats_svc.build_stats(session, student_id)
    if data is None:
        await message.answer(MSG_NOT_FOR_YOU)
        return
    await message.answer(
        stats_svc.stats_text(data, period), reply_markup=stats_kb(period)
    )


@router.message(Command("stats"))
@require_role("student")
async def cmd_stats(message: Message, db_user=None) -> None:
    """/stats — статистика по предметам (фича от 13.08, не в ТЗ)."""
    async with get_session_factory()() as session:
        student = await session.scalar(
            select(Student).where(Student.user_id == db_user.id)
        )
    if student is None:
        await message.answer(MSG_NOT_FOR_YOU)
        return
    await _show_stats(message, student.id)


@router.callback_query(F.data == "menu:student:stats:0")
@require_role("student")
async def cb_menu_student_stats(callback: CallbackQuery, db_user=None) -> None:
    """«📊 Статистика» из меню ученика → дашборд (новым сообщением)."""
    await callback.answer()
    async with get_session_factory()() as session:
        student = await session.scalar(
            select(Student).where(Student.user_id == db_user.id)
        )
    if student is None:
        await callback.message.answer(MSG_NOT_FOR_YOU)
        return
    await _show_stats(callback.message, student.id)


@router.callback_query(F.data.regexp(r"^stats:(7|30|all):0$"))
@require_role("student")
async def cb_stats_period(callback: CallbackQuery, db_user=None) -> None:
    """Переключение периода статистики (edit — без новых сообщений)."""
    period = callback.data.split(":")[1]
    async with get_session_factory()() as session:
        student = await session.scalar(
            select(Student).where(Student.user_id == db_user.id)
        )
        data = await stats_svc.build_stats(session, student.id) if student else None
    if data is None:
        await callback.answer(MSG_NOT_FOR_YOU, show_alert=True)
        return
    await callback.answer()
    await safe_edit(
        callback.message,
        stats_svc.stats_text(data, period),
        reply_markup=stats_kb(period),
    )


@router.callback_query(F.data == "menu:student:subjects:0")
@require_role("student", "owner", "teacher")
async def cb_menu_student_subjects(callback: CallbackQuery, db_user=None) -> None:
    """«🎯 Решать задания» из главного меню → выбор предмета."""
    await callback.answer()
    await _subjects_pick_view(callback.message, db_user)


@router.callback_query(F.data == "std:subjects:0")
@require_role("student", "owner", "teacher")
async def cb_student_subjects(callback: CallbackQuery, db_user=None) -> None:
    """«← Назад» от тем → снова выбор предмета (UX-пакет)."""
    await callback.answer()
    await _subjects_pick_view(callback.message, db_user)


@router.callback_query(F.data.startswith("std:subj:"))
@require_role("student", "owner", "teacher")
async def cb_student_subject(callback: CallbackQuery, db_user=None) -> None:
    """Предмет → темы предмета (только открытые — фильтрует student_menu)."""
    parts = _parse_parts(callback.data or "", 3)
    if parts is None:
        await callback.answer(MSG_STALE, show_alert=True)
        return
    await callback.answer()
    try:
        subject_id = int(parts[2])
    except ValueError:
        subject_id = -1
    await _themes_view(callback.message, db_user, subject_id)


@router.callback_query(F.data.startswith("std:theme:"))
@require_role("student", "owner", "teacher")
async def cb_student_theme(callback: CallbackQuery, db_user=None) -> None:
    """Вход в тему — выдача первого нерешённого задания."""
    parts = _parse_parts(callback.data or "", 3)
    if parts is None:
        await callback.answer(MSG_STALE, show_alert=True)
        return
    await callback.answer()
    try:
        theme_id = int(parts[2])
    except ValueError:
        theme_id = -1
    await _issue_task_and_send(callback.message, db_user.id, theme_id)


@router.callback_query(F.data.startswith("std:again:"))
@require_role("student", "owner", "teacher")
async def cb_student_again(callback: CallbackQuery, db_user=None) -> None:
    """«🎯 Ещё задание» — следующее нерешённое (или итог)."""
    parts = _parse_parts(callback.data or "", 3)
    if parts is None:
        await callback.answer(MSG_STALE, show_alert=True)
        return
    await callback.answer()
    try:
        theme_id = int(parts[2])
    except ValueError:
        theme_id = -1
    await _issue_task_and_send(callback.message, db_user.id, theme_id)


@router.callback_query(F.data.startswith("std:topics:"))
@require_role("student", "owner", "teacher")
async def cb_student_topics(callback: CallbackQuery, db_user=None) -> None:
    """«📚 Вернуться к темам» / «📚 Другие темы» — темы ЭТОГО предмета.

    Находит тему по id из колбэка → показывает уровень 2 (темы предмета),
    чтобы ученик оставался в своей теме, а не падал в общий список.
    """
    parts = _parse_parts(callback.data or "", 3)
    if parts is None:
        await callback.answer(MSG_STALE, show_alert=True)
        return
    await callback.answer()
    try:
        theme_id = int(parts[2])
    except ValueError:
        theme_id = -1
    async with get_session_factory()() as session:
        theme = await session.get(Theme, theme_id)
    if theme is None:
        await _subjects_pick_view(callback.message, db_user)
        return
    await _themes_view(callback.message, db_user, theme.subject_id)


@router.callback_query(F.data.startswith("std:retry:"))
@require_role("student", "owner", "teacher")
async def cb_student_retry(callback: CallbackQuery, db_user=None) -> None:
    """«🔁 Повторить тему» — сброс прогресса и новая выдача."""
    parts = _parse_parts(callback.data or "", 3)
    if parts is None:
        await callback.answer(MSG_STALE, show_alert=True)
        return
    await callback.answer()
    try:
        theme_id = int(parts[2])
    except ValueError:
        theme_id = -1
    async with get_session_factory()() as session:
        result = await student_svc.retry_theme(session, db_user.id, theme_id)
    status = result["status"]
    if status == student_svc.TASK_ISSUE_EXPIRED:
        await callback.message.answer(MSG_EXPIRED)
        return
    if status == student_svc.TASK_ISSUE_THEME_CLOSED:
        await callback.message.answer(MSG_THEME_CLOSED)
        return
    if status == student_svc.TASK_ISSUE_NOT_FOUND:
        await callback.message.answer(MSG_THEME_NOT_FOUND)
        return
    if status == student_svc.TASK_ISSUE_NOT_FOR_YOU:
        await callback.message.answer(MSG_NOT_FOR_YOU)
        return
    if status == student_svc.TASK_ISSUE_EMPTY:
        # после повтора в теме не осталось активных заданий — без петли
        await callback.message.answer(
            MSG_TASKS_EMPTY, reply_markup=theme_empty_kb(theme_id)
        )
        return
    await callback.message.answer(MSG_RETRY_STARTED)
    await _send_task_card(callback.message, result)


# --------------------------------------------------------------------------
# Режим «🔁 Ошибки» (повторение неправильных)
# --------------------------------------------------------------------------
MSG_ERRORS_DONE = "Ошибки разобраны! 🎉"


async def _issue_wrong_and_send(message: Message, user_id: int, theme_id: int) -> None:
    """Режим «🔁 Ошибки»: следующее wrong-задание или «Ошибки разобраны!»."""
    async with get_session_factory()() as session:
        result = await student_svc.issue_next_wrong(session, user_id, theme_id)
    status = result["status"]
    if status == student_svc.TASK_ISSUE_EXPIRED:
        await message.answer(MSG_EXPIRED)
        return
    if status == student_svc.TASK_ISSUE_THEME_CLOSED:
        await message.answer(MSG_THEME_CLOSED)
        return
    if status == student_svc.TASK_ISSUE_NOT_FOUND:
        await message.answer(MSG_THEME_NOT_FOUND)
        return
    if status == student_svc.TASK_ISSUE_NOT_FOR_YOU:
        await message.answer(MSG_NOT_FOR_YOU)
        return
    if status == student_svc.ERRORS_DONE:
        await message.answer(
            MSG_ERRORS_DONE, reply_markup=errors_done_kb(result["theme_id"])
        )
        return
    await _send_task_card(message, result, errors=True)


@router.callback_query(F.data.startswith("std:errors:"))
@require_role("student", "owner", "teacher")
async def cb_student_errors(callback: CallbackQuery, db_user=None) -> None:
    """«🔁 Повторить ошибки «Тема»» в «Мои предметы» — разбор ошибок."""
    parts = _parse_parts(callback.data or "", 3)
    if parts is None:
        await callback.answer(MSG_STALE, show_alert=True)
        return
    await callback.answer()
    try:
        theme_id = int(parts[2])
    except ValueError:
        theme_id = -1
    await _issue_wrong_and_send(callback.message, db_user.id, theme_id)


@router.callback_query(F.data.startswith("std:err_next:"))
@require_role("student", "owner", "teacher")
async def cb_student_err_next(callback: CallbackQuery, db_user=None) -> None:
    """«🔁 Следующая ошибка» — следующее wrong-задание или «Ошибки разобраны!»."""
    parts = _parse_parts(callback.data or "", 3)
    if parts is None:
        await callback.answer(MSG_STALE, show_alert=True)
        return
    await callback.answer()
    try:
        theme_id = int(parts[2])
    except ValueError:
        theme_id = -1
    await _issue_wrong_and_send(callback.message, db_user.id, theme_id)


# --------------------------------------------------------------------------
# Ответ на задание (ТЗ раздел 9)
# --------------------------------------------------------------------------
@router.callback_query(F.data.startswith("task:"))
@require_role("student", "owner", "teacher")
async def cb_task_answer(callback: CallbackQuery, db_user=None) -> None:
    """Выбор варианта: реакция, правильный ответ, объяснение, стрик.

    Формат кнопки: task:{id}:ans:{i}:{seq}:{doy}(:e). doy — день выдачи
    (issue_day): кнопка из другого дня — устаревшая (перемешивание
    вариантов зависит от даты). Старые кнопки без doy (5 частей) —
    тоже устаревшие. Режим «🔁 Ошибки» кодируется суффиксом «:e» —
    после ответа рисуется «🔁 Следующая ошибка», а не «🎯 Ещё задание».
    """
    parts = _parse_parts(callback.data or "", 6)
    if parts is None:
        await callback.answer(MSG_STALE, show_alert=True)
        return
    try:
        task_id, answer_index = int(parts[1]), int(parts[3])
        seq = int(parts[4])
        doy = int(parts[5])
    except ValueError:
        await callback.answer(MSG_STALE, show_alert=True)
        return
    errors_mode = len(parts) > 6 and parts[6] == "e"

    async with get_session_factory()() as session:
        result = await student_svc.check_answer(
            session, db_user.id, task_id, answer_index, seq, doy
        )

    status = result["status"]
    if status == student_svc.ANSWER_GONE:
        await callback.answer(MSG_GONE, show_alert=True)
        return
    if status == student_svc.ANSWER_STALE:
        # ТЗ 9 п.5: «Кнопка устарела — возьми задание заново.» + перерисовка
        await callback.answer(MSG_STALE, show_alert=True)
        async with get_session_factory()() as session:
            task = await session.get(Task, task_id)
            theme_id = task.theme_id if task is not None else -1
        if theme_id != -1:
            if errors_mode:
                await _issue_wrong_and_send(callback.message, db_user.id, theme_id)
            else:
                await _issue_task_and_send(callback.message, db_user.id, theme_id)
        return

    line = result["reaction"]
    if not result["is_correct"]:
        line += f"\n\nПравильный ответ: {esc(result['correct_answer'])}"
    await callback.answer()
    kb = (
        errors_actions_kb(result["theme_id"])
        if errors_mode
        else answer_actions_kb(result["theme_id"])
    )
    # Две кнопки — ниже объяснения; объяснения нет — кнопки у реакции.
    # Объяснение всегда ОДНИМ сообщением: фото+текст — фотка с подписью
    # (caption), только фото — голое фото, только текст — сообщение.
    feedback_text = (result.get("feedback_text") or "").strip()
    feedback_photo = result.get("feedback_photo_id")
    if feedback_text and feedback_photo:
        await callback.message.answer(line)
        await callback.message.answer_photo(
            feedback_photo,
            caption=f"💡 <b>Объяснение:</b>\n{esc(feedback_text)}",
            reply_markup=kb,
        )
    elif feedback_text:
        await callback.message.answer(line)
        await callback.message.answer(
            f"💡 <b>Объяснение:</b>\n{esc(feedback_text)}", reply_markup=kb
        )
    elif feedback_photo:
        await callback.message.answer(line)
        await callback.message.answer_photo(feedback_photo, reply_markup=kb)
    else:
        await callback.message.answer(line, reply_markup=kb)