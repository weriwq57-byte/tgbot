"""Инлайн-клавиатуры и реестр callback-данных.

Формат callback-данных единый: {entity}:{id}:{action}:{seq}.
seq фиксирован (0) — в админ-интерфейсе актуальность проверяется
по состоянию в БД; полноценный seq-механизм придёт с заданиями (Заход 5).

РЕЕСТР КОЛБЭКОВ (защита от ошибки №7 старой версии — «мёртвые кнопки»):
любой callback, созданный клавиатурой, обязан иметь обработчик:
- EXACT_CALLBACKS — точные данные (F.data == ...);
- PREFIX_CALLBACKS — короткие префиксы (F.data.startswith(...));
- FUTURE_CALLBACKS — префиксы будущих заходов (документация, кнопок
  НЕ создают: кнопку добавляем в том же заходе, что и обработчик).

Реестр обновляется вручную при добавлении хендлеров; каждую клавиатуру
проверяет тест tests/test_callbacks.py («нет мёртвых кнопок»).
"""
from typing import Iterable

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.models import Subject, Theme, User
from app.utils.format import esc

# --------------------------------------------------------------------------
# Реестр callback-данных (см. докстринг модуля)
# --------------------------------------------------------------------------
EXACT_CALLBACKS: set[str] = {
    # Главное меню владельца
    "menu:owner:teachers:0",
    "menu:owner:managers:0",
    "menu:owner:subjects:0",
    "menu:owner:kick:0",
    # Главное меню менеджера
    "menu:manager:students:0",
    "menu:manager:add_student:0",
    "menu:manager:expiring:0",
    # Главное меню преподавателя
    "menu:teacher:subjects:0",
    # Возврат в главное меню роли (F.data.regexp — все 5 ролей, роль из БД)
    "menu:back:owner:0",
    "menu:back:manager:0",
    "menu:back:teacher:0",
    "menu:back:student:0",
    "menu:back:guest:0",
    # «📋 Мои команды» (F.data.regexp — все 5 ролей, роль из БД)
    "menu:help:owner:0",
    "menu:help:manager:0",
    "menu:help:teacher:0",
    "menu:help:student:0",
    "menu:help:guest:0",
    # Менеджер: список учеников (из карточки «← Назад»)
    "mgr:students:0",
    # Заход 6: ученик — «📚 Мои предметы» в главном меню, гость — «🔑 Ввести код»
    "menu:student:subjects:0",
    "menu:guest:code:0",
    # Статистика ученика: меню + периоды (stats:{period}:0 — префиксом)
    "menu:student:stats:0",
    # UX-пакет: «← Назад» от тем к выбору предмета
    "std:subjects:0",
    # Добавить преподавателя / менеджера / предмет
    "owner:add_teacher:0",
    "owner:add_manager:0",
    "owner:add_subject:0",
    # Убрать преподавателя / менеджера: списки и отмена
    "owner:rt:list:0",
    "owner:rt:no:0",
    "owner:rm:list:0",
    "owner:rm:no:0",
    # Предметы: скрыть/показать
    "owner:subj:toggle_list:0",
    "owner:subj:back:0",
    # Предметы: удаление (список, подтверждение вводом названия)
    "owner:subj:del_list:0",
    "owner:subj:del_no:0",
    # Кик
    "owner:kick:no:0",
    "owner:kick:back:0",
    # Заход 9: рассылка — «📣 Рассылка» в главном меню владельца, шаг 4
    "menu:owner:broadcast:0",
    "bcast:go",
    "bcast:edit",
    "bcast:cancel",
}

PREFIX_CALLBACKS: set[str] = {
    # Мультивыбор предметов в визарде «Добавить преподавателя»
    "owner:at:",
    # Убрать преподавателя: выбор и подтверждение
    "owner:rt:pick:",
    "owner:rt:yes:",
    # Убрать менеджера: выбор и подтверждение
    "owner:rm:pick:",
    "owner:rm:yes:",
    # Предметы: переключатель активности
    "owner:subj:toggle:",
    # Предметы: выбрать для удаления
    "owner:subj:del:",
    # Кик: категория → выбор → подтверждение
    "owner:kick:cat:",
    "owner:kick:pick:",
    "owner:kick:yes:",
    # Выбор сотрудника без @: из гостей (список → конкретный гость)
    "owner:guestpick:",
    "owner:guestsel:",
    # Менеджер: карточка ученика (mgr:student:{id}:0)
    "mgr:student:",
    # Менеджер: закрыть/открыть предмет ученика (mgr:subj:{student}:{subject}:0)
    "mgr:subj:",
    # Менеджер: продлить доступ (mgr:extend:{id}:0)
    "mgr:extend:",
    # Менеджер: новый код приглашения (mgr:newcode:{id}:0)
    "mgr:newcode:",
    # Менеджер: деактивация ученика — запрос, да/нет
    "mgr:deactivate:",
    "mgr:deact:yes:",
    "mgr:deact:no:",
    # Менеджер: активация ученика (mgr:activate:{id}:0)
    "mgr:activate:",
    # Менеджер: «🗑 Удалить навсегда» — запрос и «Отмена» (mgr:del:{id}:0,
    # mgr:del_no:{id}:0)
    "mgr:del:",
    "mgr:del_no:",
    # Менеджер: мультивыбор предметов в визарде ученика (mgr:as:{sid}:t:0,
    # готово — mgr:as:done:0, обрабатывается тем же хендлером)
    "mgr:as:",
    # Преподаватель (Заход 4):
    # список тем предмета (tch:subj:{subject}:0)
    "tch:subj:",
    # добавить тему (tch:add_theme:{subject}:0)
    "tch:add_theme:",
    # меню темы (tch:theme:{id}:0)
    "tch:theme:",
    # открыть/закрыть тему (tch:th_open:{id}:0)
    "tch:th_open:",
    # список заданий темы (tch:tasks:{id}:0)
    "tch:tasks:",
    # переименовать тему (tch:rename:{id}:0)
    "tch:rename:",
    # удалить тему: запрос и «Отмена» (tch:delete:{id}:0, tch:del_no:{id}:0)
    "tch:delete:",
    "tch:del_no:",
    # «➕ Добавить задание» (tch:add_task:{theme}:0)
    "tch:add_task:",
    # Визард задания (Заход 5): кнопки-«ожидаю ввод» и превью —
    # opt_more/opts_done/pick/exp_skip/exp_more/
    # exp_done/pv/restart/save (tch:at:{action}:0)
    "tch:at:",
    # Карточка задания (tch:task:{id}:0)
    "tch:task:",
    # скрыть/показать задание (tch:t_toggle:{id}:0)
    "tch:t_toggle:",
    # удалить задание: запрос, да/нет (tch:t_del:, tch:t_yes:, tch:t_no:)
    "tch:t_del:",
    "tch:t_yes:",
    "tch:t_no:",
    # «✏️ Редактировать» задание (tch:t_edit:{id}:0)
    "tch:t_edit:",
    # Подтемы (текущий заход): список (tch:subs:{theme}:0), меню подтемы
    # (tch:sub:{id}:0), добавить (tch:sub_add:{theme}:0), переименовать
    # (tch:sub_rename:{id}:0), удалить + отмена (tch:sub_del:{id}:0,
    # tch:sub_del_no:{id}:0), выбор подтемы в визарде задания
    # (tch:at:sub:{id}:0), режим «открыть все» (tch:mode:{theme}:0)
    "tch:subs:",
    "tch:sub:",
    "tch:sub_add:",
    "tch:sub_rename:",
    "tch:sub_del:",
    "tch:sub_del_no:",
    "tch:mode:",
    # Ученик (Заход 6):
    # подтверждение привязки по коду (std:bind_yes:{code}:0, std:bind_no:{code}:0)
    "std:bind_",
    # выбор предмета (std:subj:{id}:0) — уровень 1 навигации (UX-пакет)
    "std:subj:",
    # вход в тему (std:theme:{id}:0)
    "std:theme:",
    # «🎯 Ещё задание» после ответа (std:again:{theme}:0)
    "std:again:",
    # «📚 Вернуться к темам» (std:topics:{theme}:0)
    "std:topics:",
    # «🔁 Повторить тему» (std:retry:{id}:0)
    "std:retry:",
    # «🔁 Повторить ошибки» / «🔁 Следующая ошибка» (std:errors:{theme}:0,
    # std:err_next:{theme}:0)
    "std:errors:",
    "std:err_next:",
    # Ответ ученика (task:{id}:ans:{index}:{seq}, в режиме ошибок — с «:e»)
    "task:",
    # Статистика ученика (stats:{period}:0)
    "stats:",
    # Заход 9: рассылка — шаг 1 (категории, bcast:rcp:*) и шаг 2
    # (предметы, bcast:sub:{id} / bcast:sub:clear)
    "bcast:rcp:",
    "bcast:sub:",
}

# Будущие заходы: префиксы известны, обработчиков пока НЕТ.
# Кнопки с ними НЕ добавляем (правило «кнопку добавляем с обработчиком»),
# набор нужен для документации и контроля в tests/test_callbacks.py.
FUTURE_CALLBACKS: set[str] = set()


# --------------------------------------------------------------------------
# Общие элементы
# --------------------------------------------------------------------------
def back_button(role: str) -> InlineKeyboardButton:
    """Кнопка «← Назад» в главное меню роли."""
    return InlineKeyboardButton(
        text="← Назад", callback_data=f"menu:back:{role}:0"
    )


def confirm_kb(
    yes_cb: str,
    no_cb: str,
    yes_text: str = "Да",
    no_text: str = "Отмена",
) -> InlineKeyboardMarkup:
    """Подтверждение действия: две кнопки."""
    builder = InlineKeyboardBuilder()
    builder.button(text=yes_text, callback_data=yes_cb)
    builder.button(text=no_text, callback_data=no_cb)
    return builder.as_markup()


# --------------------------------------------------------------------------
# Главное меню по ролям
# --------------------------------------------------------------------------
def main_menu_kb(*role_args: str) -> InlineKeyboardMarkup:
    """Главное меню в зависимости от роли (или набора ролей).

    Принимает и отдельные роли (main_menu_kb("owner")), и кортеж
    (main_menu_kb(user_roles(db_user))). Кнопка «📋 Мои команды» есть у
    всех ролей (обработчик menu:help:*). Остальные кнопки добавляются
    строго вместе с обработчиком (правило реестра): «Ученики» (менеджер),
    «Мои предметы» (препод/ученик), «Ввести код» (гость). Может вернуть
    пустую клавиатуру — хендлеры сами решают, отправлять ли её.

    Совмещённый менеджер+преподаватель видит кнопки ОБЕИХ ролей
    (роли хранятся в role/role2, первичная — первая в кортеже).
    """
    roles: set[str] = set()
    for arg in role_args:
        if isinstance(arg, str):
            roles.add(arg)
        else:
            roles.update(arg)
    primary = next(
        (r for arg in role_args for r in (arg if not isinstance(arg, str) else (arg,))),
        "guest",
    )
    builder = InlineKeyboardBuilder()
    if "owner" in roles:
        builder.button(text="👨🏫 Преподаватели", callback_data="menu:owner:teachers:0")
        builder.button(text="👥 Менеджеры", callback_data="menu:owner:managers:0")
        builder.button(text="📚 Предметы", callback_data="menu:owner:subjects:0")
        builder.button(text="👨🎓 Ученики", callback_data="menu:manager:students:0")
        builder.button(text="🔨 Кикнуть", callback_data="menu:owner:kick:0")
        builder.button(text="🎯 Решать задания", callback_data="menu:student:subjects:0")
        builder.button(text="🛠 Редактировать темы", callback_data="menu:teacher:subjects:0")
        builder.button(text="📣 Рассылка", callback_data="menu:owner:broadcast:0")
        builder.adjust(2)
    elif "manager" in roles or "teacher" in roles:
        if "manager" in roles:
            builder.button(text="👨🎓 Ученики", callback_data="menu:manager:students:0")
            builder.button(text="➕ Добавить ученика", callback_data="menu:manager:add_student:0")
            builder.button(text="⏳ Истекающие", callback_data="menu:manager:expiring:0")
            builder.adjust(2)
        if "teacher" in roles:
            builder.button(text="📚 Мои предметы", callback_data="menu:teacher:subjects:0")
            builder.button(text="🎯 Решать задания", callback_data="menu:student:subjects:0")
            builder.adjust(1)
        builder.button(text="📋 Мои команды", callback_data=f"menu:help:{primary}:0")
    elif "student" in roles:
        builder.button(text="🎯 Решать задания", callback_data="menu:student:subjects:0")
        builder.button(text="📊 Статистика", callback_data="menu:student:stats:0")
        builder.adjust(1)
        builder.button(text="📋 Мои команды", callback_data=f"menu:help:{primary}:0")
    else:  # guest
        builder.button(text="🔑 Ввести код", callback_data="menu:guest:code:0")
        builder.adjust(1)
        builder.button(text="📋 Мои команды", callback_data=f"menu:help:{primary}:0")
    return builder.as_markup()


# --------------------------------------------------------------------------
# Подменю владельца
# --------------------------------------------------------------------------
def owner_teachers_menu_kb() -> InlineKeyboardMarkup:
    """Подменю «Преподаватели» для владельца."""
    builder = InlineKeyboardBuilder()
    builder.button(text="➕ Добавить", callback_data="owner:add_teacher:0")
    builder.button(text="➖ Убрать", callback_data="owner:rt:list:0")
    builder.button(text="← Назад", callback_data="menu:back:owner:0")
    builder.adjust(2)
    return builder.as_markup()


def owner_managers_menu_kb() -> InlineKeyboardMarkup:
    """Подменю «Менеджеры» для владельца."""
    builder = InlineKeyboardBuilder()
    builder.button(text="➕ Добавить", callback_data="owner:add_manager:0")
    builder.button(text="➖ Убрать", callback_data="owner:rm:list:0")
    builder.button(text="← Назад", callback_data="menu:back:owner:0")
    builder.adjust(2)
    return builder.as_markup()


def owner_subjects_menu_kb() -> InlineKeyboardMarkup:
    """Подменю «Предметы» для владельца."""
    builder = InlineKeyboardBuilder()
    builder.button(text="➕ Добавить", callback_data="owner:add_subject:0")
    builder.button(text="🚫 Скрыть/Показать", callback_data="owner:subj:toggle_list:0")
    builder.button(text="🗑 Удалить", callback_data="owner:subj:del_list:0")
    builder.button(text="← Назад", callback_data="menu:back:owner:0")
    builder.adjust(1)
    return builder.as_markup()


def kick_categories_kb() -> InlineKeyboardMarkup:
    """Категории для «Кикнуть»: ученик / преподаватель / менеджер."""
    builder = InlineKeyboardBuilder()
    builder.button(text="👨🎓 Ученик", callback_data="owner:kick:cat:student:0")
    builder.button(text="👨🏫 Преподаватель", callback_data="owner:kick:cat:teacher:0")
    builder.button(text="👥 Менеджер", callback_data="owner:kick:cat:manager:0")
    builder.button(text="← Назад", callback_data="menu:back:owner:0")
    builder.adjust(1)
    return builder.as_markup()


# --------------------------------------------------------------------------
# Списки людей и предметов
# --------------------------------------------------------------------------
def person_label(user: User) -> str:
    """Подпись человека для кнопки: @username, иначе имя, иначе ID."""
    if user.tg_username:
        return f"@{user.tg_username}"
    if user.tg_full_name:
        return user.tg_full_name
    return f"ID {user.id}"


def people_list_kb(
    people: Iterable[User],
    pick_prefix: str,
    label_func=None,
    back_cb: str | None = "menu:back:owner:0",
) -> InlineKeyboardMarkup:
    """Список людей кнопками: колбэк {pick_prefix}:{user.id}:0.

    Внизу — «← Назад» (по умолчанию в главное меню владельца).
    """
    builder = InlineKeyboardBuilder()
    for person in people:
        label = label_func(person) if label_func else person_label(person)
        builder.button(
            text=esc(label),
            callback_data=f"{pick_prefix}:{person.id}:0",
        )
    if back_cb is not None:
        builder.button(text="← Назад", callback_data=back_cb)
    builder.adjust(1)
    return builder.as_markup()


def subject_toggle_kb(subjects: Iterable[Subject]) -> InlineKeyboardMarkup:
    """Список предметов с переключателем активности: {id} в колбэке."""
    builder = InlineKeyboardBuilder()
    for subject in subjects:
        builder.button(
            text=esc(subject_label(subject, with_state=True)),
            callback_data=f"owner:subj:toggle:{subject.id}:0",
        )
    builder.button(text="← Назад", callback_data="owner:subj:back:0")
    builder.adjust(1)
    return builder.as_markup()


def subject_delete_kb(subjects: Iterable[Subject]) -> InlineKeyboardMarkup:
    """Список предметов для удаления: {id} в колбэке."""
    builder = InlineKeyboardBuilder()
    for subject in subjects:
        builder.button(
            text=esc(subject.name),
            callback_data=f"owner:subj:del:{subject.id}:0",
        )
    builder.button(text="← Назад", callback_data="owner:subj:back:0")
    builder.adjust(1)
    return builder.as_markup()


def subject_delete_confirm_kb() -> InlineKeyboardMarkup:
    """Подтверждение удаления предмета: «❌ Отмена» (owner:subj:del_no:0).

    Текст TEXT_DEL_SUBJECT_NOT_MATCH обещает отмену кнопкой — она должна
    существовать на экране подтверждения, иначе из визарда только /menu.
    """
    builder = InlineKeyboardBuilder()
    builder.button(text="❌ Отмена", callback_data="owner:subj:del_no:0")
    builder.adjust(1)
    return builder.as_markup()


def subject_label(subject: Subject, with_state: bool = False) -> str:
    """Подпись предмета (с состоянием ✅/🚫 для скрыть/показать)."""
    if with_state:
        return f"{'✅' if subject.is_active else '🚫'} {subject.name}"
    return subject.name


def multiselect_kb(
    subjects: Iterable[Subject],
    selected: set[int],
    toggle_prefix: str,
    done_cb: str,
) -> InlineKeyboardMarkup:
    """Мультивыбор предметов чекбоксами + кнопка «Готово».

    toggle_prefix — префикс колбэка вида owner:at,
    колбэк предмета: {toggle_prefix}:{subject_id}:t:0.
    """
    builder = InlineKeyboardBuilder()
    for subject in subjects:
        checked = "✅ " if subject.id in selected else ""
        builder.button(
            text=f"{checked}{esc(subject.name)}",
            callback_data=f"{toggle_prefix}:{subject.id}:t:0",
        )
    builder.button(text="✅ Готово", callback_data=done_cb)
    builder.adjust(1)
    return builder.as_markup()


def guest_pick_entry_kb(role: str) -> InlineKeyboardMarkup:
    """Кнопка на шаге ввода: «👥 Выбрать из заходивших» (для role)."""
    builder = InlineKeyboardBuilder()
    builder.button(
        text="👥 Выбрать из заходивших",
        callback_data=f"owner:guestpick:{role}:0",
    )
    return builder.as_markup()


def guest_people_list_kb(
    people: Iterable[User], pick_prefix: str
) -> InlineKeyboardMarkup:
    """Список гостей кнопками: имя/username + tg_id, выбор → {prefix}:{id}:0."""
    builder = InlineKeyboardBuilder()
    for person in people:
        label_parts = []
        if person.tg_full_name:
            label_parts.append(person.tg_full_name)
        if person.tg_username:
            label_parts.append(f"@{person.tg_username}")
        if person.tg_id:
            label_parts.append(f"id {person.tg_id}")
        label = " · ".join(label_parts) or f"ID {person.id}"
        builder.button(
            text=esc(label),
            callback_data=f"{pick_prefix}:{person.id}:0",
        )
    builder.button(text="← Назад", callback_data="menu:back:owner:0")
    builder.adjust(1)
    return builder.as_markup()


# --------------------------------------------------------------------------
# Менеджер: ученики
# --------------------------------------------------------------------------
def students_list_kb(
    students: Iterable[dict], back_cb: str = "menu:back:manager:0"
) -> InlineKeyboardMarkup:
    """Список учеников кнопками → карточка + «➕ Добавить ученика».

    back_cb — «← Назад»: для менеджера его меню, для владельца — своё
    (владелец видит те же экраны, но возвращается в главное меню владельца).
    """
    builder = InlineKeyboardBuilder()
    for row in students:
        label = (row.get("name") or "").strip() or f"ID {row['id']}"
        builder.button(
            text=esc(label),
            callback_data=f"mgr:student:{row['id']}:0",
        )
    builder.button(text="➕ Добавить ученика", callback_data="menu:manager:add_student:0")
    builder.button(text="← Назад", callback_data=back_cb)
    builder.adjust(1)
    return builder.as_markup()


def student_card_kb(
    student_id: int,
    subjects: Iterable,
    linked: bool,
    is_active: bool,
) -> InlineKeyboardMarkup:
    """Карточка ученика: переключатели предметов + действия (Заход 3)."""
    builder = InlineKeyboardBuilder()
    for subject, link in subjects:
        state = "✅" if link.is_active else "🚫"
        builder.button(
            text=f"{state} {esc(subject.name)}",
            callback_data=f"mgr:subj:{student_id}:{subject.id}:0",
        )
    builder.button(text="📅 Продлить доступ", callback_data=f"mgr:extend:{student_id}:0")
    if not linked:
        builder.button(
            text="🔁 Новый код приглашения",
            callback_data=f"mgr:newcode:{student_id}:0",
        )
    if is_active:
        builder.button(
            text="🔨 Деактивировать",
            callback_data=f"mgr:deactivate:{student_id}:0",
        )
    else:
        builder.button(
            text="✅ Активировать",
            callback_data=f"mgr:activate:{student_id}:0",
        )
    builder.button(text="🗑 Удалить навсегда", callback_data=f"mgr:del:{student_id}:0")
    builder.button(text="← Назад", callback_data="mgr:students:0")
    builder.adjust(1)
    return builder.as_markup()


def expiring_kb(
    items: Iterable[tuple[str, int]], back_cb: str = "menu:back:manager:0"
) -> InlineKeyboardMarkup:
    """Строки экрана «Истекающие»: подпись → карточка ученика.

    items — (текст строки, student_id). Заголовки групп — текстом,
    не кнопками. Подписи экранируются здесь (в тексте сообщения уже
    экранируются отдельно — не удваивать).
    """
    builder = InlineKeyboardBuilder()
    for label, student_id in items:
        builder.button(
            text=esc(label),
            callback_data=f"mgr:student:{student_id}:0",
        )
    builder.button(text="← Назад", callback_data=back_cb)
    builder.adjust(1)
    return builder.as_markup()


def add_student_continue_kb() -> InlineKeyboardMarkup:
    """Кнопка после создания ученика: «👨🎓 К списку учеников»."""
    builder = InlineKeyboardBuilder()
    builder.button(text="👨🎓 К списку учеников", callback_data="mgr:students:0")
    return builder.as_markup()


def confirm_deactivate_kb(student_id: int) -> InlineKeyboardMarkup:
    """Подтверждение деактивации ученика: да/отмена."""
    builder = InlineKeyboardBuilder()
    builder.button(
        text="🔨 Деактивировать",
        callback_data=f"mgr:deact:yes:{student_id}:0",
    )
    builder.button(text="Отмена", callback_data=f"mgr:deact:no:{student_id}:0")
    return builder.as_markup()


# --------------------------------------------------------------------------
# Преподаватель: предметы и темы (Заход 4)
# --------------------------------------------------------------------------
def theme_status_label(theme) -> str:
    """Статус темы для кнопки: «🔒 закрыта» / «🔓 по порядку» / «🎲 открыть все»."""
    if not theme.is_open:
        return "🔒 закрыта"
    return "🎲 открыть все" if theme.mode == "random" else "🔓 по порядку"


def teacher_subjects_kb(
    subjects: Iterable[dict], back_cb: str = "menu:back:teacher:0"
) -> InlineKeyboardMarkup:
    """«📚 Мои предметы»: предметы и их темы кнопками.

    subjects — [{subject, themes: [(Theme, count)]}]. Кнопки предметов
    → список тем, кнопки тем → меню темы. Внизу «← Назад» (по роли:
    владелец видит тот же экран, но назад — в своё меню).
    """
    builder = InlineKeyboardBuilder()
    for item in subjects:
        subject = item["subject"]
        builder.button(
            text=f"{esc(subject.name)}",
            callback_data=f"tch:subj:{subject.id}:0",
        )
        for theme, _count in item["themes"]:
            builder.button(
                text=f"{theme_status_label(theme)} {esc(theme.title)}",
                callback_data=f"tch:theme:{theme.id}:0",
            )
    builder.button(text="← Назад", callback_data=back_cb)
    builder.adjust(1)
    return builder.as_markup()


def theme_list_kb(
    themes: Iterable[Theme],
    subject_id: int,
    back_cb: str = "menu:teacher:subjects:0",
) -> InlineKeyboardMarkup:
    """Список тем предмета: кнопки тем → меню темы + «➕ Добавить тему»."""
    builder = InlineKeyboardBuilder()
    for theme in themes:
        builder.button(
            text=f"{theme_status_label(theme)} {esc(theme.title)}",
            callback_data=f"tch:theme:{theme.id}:0",
        )
    builder.button(
        text="➕ Добавить тему",
        callback_data=f"tch:add_theme:{subject_id}:0",
    )
    builder.button(text="← Назад", callback_data=back_cb)
    builder.adjust(1)
    return builder.as_markup()


def theme_menu_kb(theme) -> InlineKeyboardMarkup:
    """Меню темы: открыть/закрыть, режим, подтемы, задания, rename/delete, назад."""
    builder = InlineKeyboardBuilder()
    builder.button(
        text="🔒 Закрыть тему" if theme.is_open else "🔓 Открыть тему",
        callback_data=f"tch:th_open:{theme.id}:0",
    )
    builder.button(
        text="🎲 Открыть все (рандом)" if theme.mode == "sequential"
        else "🔓 По порядку",
        callback_data=f"tch:mode:{theme.id}:0",
    )
    builder.button(text="🔖 Подтемы", callback_data=f"tch:subs:{theme.id}:0")
    builder.button(text="📝 Задания", callback_data=f"tch:tasks:{theme.id}:0")
    builder.button(text="✏️ Переименовать", callback_data=f"tch:rename:{theme.id}:0")
    builder.button(text="🗑 Удалить тему", callback_data=f"tch:delete:{theme.id}:0")
    builder.button(text="← Назад", callback_data=f"tch:subj:{theme.subject_id}:0")
    builder.adjust(1)
    return builder.as_markup()


def subthemes_list_kb(
    subthemes: Iterable, theme_id: int
) -> InlineKeyboardMarkup:
    """Список подтем темы: подтема → её меню + «➕ Добавить подтему».

    subthemes — [{subtheme, count}]: название + число заданий.
    """
    builder = InlineKeyboardBuilder()
    for item in subthemes:
        subtheme = item["subtheme"]
        label = f"🔖 {esc(subtheme.title)}"
        count = item.get("count") or 0
        if count:
            label += f" ({count})"
        builder.button(text=label, callback_data=f"tch:sub:{subtheme.id}:0")
    builder.button(
        text="➕ Добавить подтему",
        callback_data=f"tch:sub_add:{theme_id}:0",
    )
    builder.button(text="← В меню темы", callback_data=f"tch:theme:{theme_id}:0")
    builder.adjust(1)
    return builder.as_markup()


def subtheme_menu_kb(subtheme) -> InlineKeyboardMarkup:
    """Меню подтемы: переименовать / удалить / назад."""
    builder = InlineKeyboardBuilder()
    builder.button(
        text="✏️ Переименовать",
        callback_data=f"tch:sub_rename:{subtheme.id}:0",
    )
    builder.button(text="🗑 Удалить подтему", callback_data=f"tch:sub_del:{subtheme.id}:0")
    builder.button(
        text="← К подтемам", callback_data=f"tch:subs:{subtheme.theme_id}:0"
    )
    builder.adjust(1)
    return builder.as_markup()


def subtheme_pick_kb(subthemes: Iterable, theme_id: int) -> InlineKeyboardMarkup:
    """Шаг визарда задания: выбор подтемы (или «на тему напрямую»).

    subthemes — Iterable[Subtheme]; «На тему» — tch:at:sub:0:0
    (subtheme_id=None), «← Отмена» — из визарда в список заданий.
    """
    builder = InlineKeyboardBuilder()
    for subtheme in subthemes:
        builder.button(
            text=f"🔖 {esc(subtheme.title)}",
            callback_data=f"tch:at:sub:{subtheme.id}:0",
        )
    builder.button(
        text="📎 На тему (без подтемы)",
        callback_data="tch:at:sub:0:0",
    )
    builder.button(text="← Отмена", callback_data=f"tch:tasks:{theme_id}:0")
    builder.adjust(1)
    return builder.as_markup()


def confirm_del_subtheme_kb(subtheme_id: int) -> InlineKeyboardMarkup:
    """Кнопка «Отмена» на подтверждении удаления подтемы."""
    builder = InlineKeyboardBuilder()
    builder.button(text="Отмена", callback_data=f"tch:sub_del_no:{subtheme_id}:0")
    return builder.as_markup()


def themes_pick_kb(
    themes: Iterable[Theme], back_cb: str = "menu:back:teacher:0"
) -> InlineKeyboardMarkup:
    """Выбор темы (для /tasks): кнопка темы → список её заданий."""
    builder = InlineKeyboardBuilder()
    for theme in themes:
        builder.button(
            text=f"{theme_status_label(theme)} {esc(theme.title)}",
            callback_data=f"tch:tasks:{theme.id}:0",
        )
    builder.button(text="← Назад", callback_data=back_cb)
    builder.adjust(1)
    return builder.as_markup()


def confirm_del_theme_kb(theme_id: int) -> InlineKeyboardMarkup:
    """Кнопка «Отмена» на подтверждении удаления темы."""
    builder = InlineKeyboardBuilder()
    builder.button(text="Отмена", callback_data=f"tch:del_no:{theme_id}:0")
    return builder.as_markup()


def confirm_del_student_kb(student_id: int) -> InlineKeyboardMarkup:
    """Кнопка «Отмена» на подтверждении удаления ученика (UX-пакет)."""
    builder = InlineKeyboardBuilder()
    builder.button(text="Отмена", callback_data=f"mgr:del_no:{student_id}:0")
    return builder.as_markup()


# --------------------------------------------------------------------------
# Преподаватель: визард «Добавить задание» (Заход 5)
# Буквы вариантов — А, Б, В, Г (как у ученика, ТЗ раздел 9).
# --------------------------------------------------------------------------
_OPTION_LETTERS = "АБВГ"


def options_more_kb() -> InlineKeyboardMarkup:
    """Шаг options: «➕ Ещё вариант» / «✅ Готово»."""
    builder = InlineKeyboardBuilder()
    builder.button(text="➕ Ещё вариант", callback_data="tch:at:opt_more:0")
    builder.button(text="✅ Готово", callback_data="tch:at:opts_done:0")
    builder.adjust(2)
    return builder.as_markup()


def options_done_kb() -> InlineKeyboardMarkup:
    """Шаг options, достигнут максимум 4: только «✅ Готово»."""
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Готово", callback_data="tch:at:opts_done:0")
    return builder.as_markup()


def options_pick_kb(options: list[str]) -> InlineKeyboardMarkup:
    """Шаг correct: варианты кнопками «А. …» → tch:at:pick:{index}:0."""
    builder = InlineKeyboardBuilder()
    for i, text in enumerate(options):
        builder.button(
            text=f"{_OPTION_LETTERS[i]}. {esc(text)}",
            callback_data=f"tch:at:pick:{i}:0",
        )
    builder.adjust(1)
    return builder.as_markup()


def exp_more_kb() -> InlineKeyboardMarkup:
    """После «✅ Объяснение добавлено.»: «➕ Ещё» / «✅ Готово»."""
    builder = InlineKeyboardBuilder()
    builder.button(text="➕ Ещё", callback_data="tch:at:exp_more:0")
    builder.button(text="✅ Готово", callback_data="tch:at:exp_done:0")
    builder.adjust(2)
    return builder.as_markup()


def exp_pass_kb() -> InlineKeyboardMarkup:
    """Шаг exp_input: кнопка «Пропустить» (объяснение необязательно)."""
    builder = InlineKeyboardBuilder()
    builder.button(text="Пропустить", callback_data="tch:at:exp_skip:0")
    return builder.as_markup()


def preview_kb(options: list[str], edit_task_id: int | None = None) -> InlineKeyboardMarkup:
    """Превью задания: варианты кнопками (клик — подсказка) + правки.

    «✏️ Вопрос» / «✏️ Варианты» / «✏️ Объяснение» (tch:at:edit_*:0) —
    вернуться в шаг визарда и перезаписать часть задания; «✅ Сохранить»
    (в режиме редактирования — UPDATE по task_id), «✏️ Заново» — с
    чистого листа. edit_task_id — режим «✏️ Редактировать»: кнопка
    «← К заданию» (tch:at:edit_back:{id}:0) — выход в карточку задания.
    """
    builder = InlineKeyboardBuilder()
    for i, text in enumerate(options):
        builder.button(
            text=f"{_OPTION_LETTERS[i]}. {esc(text)}",
            callback_data=f"tch:at:pv:{i}:0",
        )
    builder.button(text="✏️ Вопрос", callback_data="tch:at:edit_q:0")
    builder.button(text="✏️ Варианты", callback_data="tch:at:edit_o:0")
    builder.button(text="✏️ Объяснение", callback_data="tch:at:edit_e:0")
    builder.button(text="✅ Сохранить", callback_data="tch:at:save:0")
    builder.button(text="✏️ Заново", callback_data="tch:at:restart:0")
    if edit_task_id is not None:
        builder.button(
            text="← К заданию",
            callback_data=f"tch:at:edit_back:{edit_task_id}:0",
        )
    builder.adjust(1)
    return builder.as_markup()


def tasks_menu_kb(theme_id: int) -> InlineKeyboardMarkup:
    """Список заданий темы: «➕ Добавить задание» + «← В меню темы»."""
    builder = InlineKeyboardBuilder()
    builder.button(
        text="➕ Добавить задание",
        callback_data=f"tch:add_task:{theme_id}:0",
    )
    builder.button(
        text="← В меню темы",
        callback_data=f"tch:theme:{theme_id}:0",
    )
    builder.adjust(1)
    return builder.as_markup()


def task_card_kb(task) -> InlineKeyboardMarkup:
    """Карточка задания: скрыть/показать, редактировать, удалить, назад."""
    builder = InlineKeyboardBuilder()
    builder.button(
        text="🚫 Скрыть задание" if task.is_active else "✅ Показать задание",
        callback_data=f"tch:t_toggle:{task.id}:0",
    )
    builder.button(text="✏️ Редактировать", callback_data=f"tch:t_edit:{task.id}:0")
    builder.button(text="🗑 Удалить", callback_data=f"tch:t_del:{task.id}:0")
    builder.button(
        text="← К заданиям",
        callback_data=f"tch:tasks:{task.theme_id}:0",
    )
    builder.adjust(1)
    return builder.as_markup()


def confirm_del_task_kb(task_id: int) -> InlineKeyboardMarkup:
    """Подтверждение удаления задания: «Да, удалить» / «Отмена»."""
    builder = InlineKeyboardBuilder()
    builder.button(text="Да, удалить", callback_data=f"tch:t_yes:{task_id}:0")
    builder.button(text="Отмена", callback_data=f"tch:t_no:{task_id}:0")
    builder.adjust(2)
    return builder.as_markup()


# --------------------------------------------------------------------------
# Ученик (Заход 6): привязка, темы, задания
# --------------------------------------------------------------------------
def bind_confirm_kb(code: str) -> InlineKeyboardMarkup:
    """Подтверждение привязки: «Да, это я» / «Нет, это не я»."""
    builder = InlineKeyboardBuilder()
    builder.button(
        text="Да, это я", callback_data=f"std:bind_yes:{code}:0"
    )
    builder.button(
        text="Нет, это не я", callback_data=f"std:bind_no:{code}:0"
    )
    builder.adjust(1)
    return builder.as_markup()


def guest_code_kb() -> InlineKeyboardMarkup:
    """Текст, не похожий на код: приветствие гостя + «🔑 Ввести код»."""
    builder = InlineKeyboardBuilder()
    builder.button(text="🔑 Ввести код", callback_data="menu:guest:code:0")
    builder.adjust(1)
    return builder.as_markup()


def stats_kb(period: str = "7") -> InlineKeyboardMarkup:
    """Кнопки дашборда статистики: периоды (переключение через edit).

    period: «7» | «30» | «all»; выбранный помечается ✅. «← Назад» —
    в меню ученика. Колбэки: stats:{period}:0.
    """
    builder = InlineKeyboardBuilder()
    labels = {
        "7": ("✅ 7 дней" if period == "7" else "7 дней", "stats:7:0"),
        "30": ("✅ 30 дней" if period == "30" else "30 дней", "stats:30:0"),
        "all": ("✅ Всё" if period == "all" else "Всё время", "stats:all:0"),
    }
    builder.button(text=labels["7"][0], callback_data=labels["7"][1])
    builder.button(text=labels["30"][0], callback_data=labels["30"][1])
    builder.button(text=labels["all"][0], callback_data=labels["all"][1])
    builder.button(text="← Назад", callback_data="menu:back:student:0")
    builder.adjust(3, 1)
    return builder.as_markup()


def student_subjects_kb(
    subjects: Iterable[dict],
    back_cb: str = "menu:back:student:0",
    self_mode: bool = False,
) -> InlineKeyboardMarkup:
    """«📚 Мои предметы» ученика (sequential-режим).

    subjects — [{subject, themes: [{theme, progress, all_done}]}].
    Кнопками — темы (прогресс в подписи): «🔓 {Тема} — осталось {N}»
    (вход в тему) / «✅ {Тема} — все решены» (вход → итог) + отдельная
    «🔁 Повторить «{Тема}»» для пройденных. Внизу «← Назад» (по роли:
    владелец видит тот же экран, но назад — в своё меню).

    self_mode (босс/препод): пустые темы (0 заданий) подписываются
    «— {Тема} — 0 заданий», а не «осталось 0».
    """
    builder = InlineKeyboardBuilder()
    for item in subjects:
        for theme_item in item["themes"]:
            theme = theme_item["theme"]
            progress = theme_item["progress"]
            all_done = theme_item["all_done"]
            if all_done:
                label = f"✅ {esc(theme.title)} — все решены"
            elif self_mode and (progress["total"] or 0) == 0:
                label = f"— {esc(theme.title)} — 0 заданий"
            else:
                label = f"🔓 {esc(theme.title)} — осталось {progress['remaining']}"
            builder.button(text=label, callback_data=f"std:theme:{theme.id}:0")
            if all_done:
                builder.button(
                    text=f"🔁 Повторить «{esc(theme.title)}»",
                    callback_data=f"std:retry:{theme.id}:0",
                )
            if (theme_item.get("wrong_count") or 0) > 0:
                builder.button(
                    text=f"🔁 Повторить ошибки «{esc(theme.title)}»",
                    callback_data=f"std:errors:{theme.id}:0",
                )
    builder.button(text="← Назад", callback_data=back_cb)
    builder.adjust(1)
    return builder.as_markup()


def subjects_pick_kb(
    subjects: Iterable[dict], back_cb: str = "menu:back:student:0"
) -> InlineKeyboardMarkup:
    """Уровень 1 навигации ученика: выбор ПРЕДМЕТА кнопками.

    subjects — [{subject, themes: [...]}] из student_menu (см. выше).
    По клику на предмет (std:subj:{id}:0) — темы этого предмета.
    """
    builder = InlineKeyboardBuilder()
    for item in subjects:
        subject = item["subject"]
        builder.button(
            text=f"{esc(subject.name)}",
            callback_data=f"std:subj:{subject.id}:0",
        )
    builder.button(text="← Назад", callback_data=back_cb)
    builder.adjust(1)
    return builder.as_markup()


def task_kb(
    task,
    seq: int,
    perm: list[int] | None = None,
    errors: bool = False,
    doy: int | None = None,
) -> InlineKeyboardMarkup:
    """Карточка задания ученику: варианты «А. …» → task:{id}:ans:{i}:{seq}(:{doy})(:e).

    seq — число ответов ученика по теме на момент выдачи (защита от
    устаревших карточек: не совпало — «Кнопка устарела»). perm —
    позиции из options_permutation: показываем options[perm[i]], кнопка
    несёт ПОЗИЦИЮ i в карточке (БД-порядок options не меняется).
    doy — день выдачи (issue_day): perm зависит от даты, кнопка чужого
    дня — устаревшая. errors — режим «🔁 Ошибки»: к данным дописывается
    «:e», чтобы после ответа нарисовать кнопку «🔁 Следующая ошибка»,
    а не «🎯 Ещё задание».
    """
    if perm is None:
        perm = list(range(len(task.options or [])))
    builder = InlineKeyboardBuilder()
    suffix = f":{doy}" if doy is not None else ""
    suffix += ":e" if errors else ""
    options = task.options or []
    for i in range(len(options)):
        pos = perm[i] if i < len(perm) else i
        label = str(options[pos].get("t") or "").strip()
        builder.button(
            text=f"{_OPTION_LETTERS[i]}. {esc(label)}",
            callback_data=f"task:{task.id}:ans:{i}:{seq}{suffix}",
        )
    builder.adjust(1)
    return builder.as_markup()


def answer_actions_kb(theme_id: int) -> InlineKeyboardMarkup:
    """После ответа: «⏭ Следующее задание» / «📚 Вернуться к темам»."""
    builder = InlineKeyboardBuilder()
    builder.button(text="⏭ Следующее задание", callback_data=f"std:again:{theme_id}:0")
    builder.button(text="📚 Вернуться к темам", callback_data=f"std:topics:{theme_id}:0")
    builder.adjust(1)
    return builder.as_markup()


def errors_actions_kb(theme_id: int) -> InlineKeyboardMarkup:
    """После ответа в режиме «🔁 Ошибки»: следующая ошибка / к темам."""
    builder = InlineKeyboardBuilder()
    builder.button(text="🔁 Следующая ошибка", callback_data=f"std:err_next:{theme_id}:0")
    builder.button(text="📚 Вернуться к темам", callback_data=f"std:topics:{theme_id}:0")
    builder.adjust(1)
    return builder.as_markup()


def errors_done_kb(theme_id: int) -> InlineKeyboardMarkup:
    """«Ошибки разобраны!»: «📚 Вернуться к темам»."""
    builder = InlineKeyboardBuilder()
    builder.button(text="📚 Вернуться к темам", callback_data=f"std:topics:{theme_id}:0")
    builder.adjust(1)
    return builder.as_markup()


def theme_result_kb(theme_id: int) -> InlineKeyboardMarkup:
    """Итог темы: «🔁 Повторить тему» / «📚 Другие темы»."""
    builder = InlineKeyboardBuilder()
    builder.button(text="🔁 Повторить тему", callback_data=f"std:retry:{theme_id}:0")
    builder.button(text="📚 Другие темы", callback_data=f"std:topics:{theme_id}:0")
    builder.adjust(1)
    return builder.as_markup()


def theme_empty_kb(theme_id: int) -> InlineKeyboardMarkup:
    """Пустая тема (п.0.3 Захода 8): «📚 Другие темы» + «← Назад» — и НИКАКОЙ
    «🔁 Повторить тему» (вечная петля, пока препод не добавит заданий).

    «← Назад» (вход из меню) — на экран «Мои предметы», как и «Другие темы»:
    изучать тут нечего, всё ведёт к списку предметов.
    """
    builder = InlineKeyboardBuilder()
    builder.button(text="📚 Другие темы", callback_data=f"std:topics:{theme_id}:0")
    builder.button(text="← Назад", callback_data=f"std:topics:{theme_id}:0")
    builder.adjust(1)
    return builder.as_markup()


# --------------------------------------------------------------------------
# Рассылка (Заход 9): категории → предметы → подтверждение
# --------------------------------------------------------------------------
def bcast_categories_kb(
    selected: set[str], students_mode: str = "all"
) -> InlineKeyboardMarkup:
    """Шаг 1: тумблеры категорий + «📚 Выбрать предмет» (только при
    выбранных учениках) + «✅ Далее» / «❌ Отмена».

    selected — набор выбранных категорий {"students","teachers","managers"};
    выбранная категория помечается «✅ ». «📚 Выбрать предмет» виден
    только при выбранных учениках (bcast:rcp:subjects).
    """
    builder = InlineKeyboardBuilder()

    def toggle(label: str, key: str) -> str:
        return f"✅ {label}" if key in selected else label

    builder.button(text=toggle("👨🎓 Ученики", "students"), callback_data="bcast:rcp:students")
    builder.button(text=toggle("👨🏫 Преподаватели", "teachers"), callback_data="bcast:rcp:teachers")
    builder.button(text=toggle("👥 Менеджеры", "managers"), callback_data="bcast:rcp:managers")
    if "students" in selected:
        builder.button(text="📚 Выбрать предмет", callback_data="bcast:rcp:subjects")
    builder.button(text="✅ Далее", callback_data="bcast:rcp:next")
    builder.button(text="❌ Отмена", callback_data="bcast:rcp:cancel")
    builder.adjust(1)
    return builder.as_markup()


def bcast_subjects_kb(subjects: Iterable[Subject]) -> InlineKeyboardMarkup:
    """Шаг 2: выбор ОДНОГО предмета (не мульти) + «🌍 Все предметы».

    Кнопка предмета → bcast:sub:{id} (students_mode="subjects" и возврат
    на шаг 1); «🌍 Все предметы» → bcast:sub:clear (students_mode="all").
    """
    builder = InlineKeyboardBuilder()
    for subject in subjects:
        builder.button(
            text=f"{esc(subject.name)}",
            callback_data=f"bcast:sub:{subject.id}",
        )
    builder.button(text="🌍 Все предметы", callback_data="bcast:sub:clear")
    builder.adjust(1)
    return builder.as_markup()


def bcast_confirm_kb() -> InlineKeyboardMarkup:
    """Шаг 4 (предпросмотр): «🚀 Отправить» / «↩ Изменить» / «❌ Отмена»."""
    builder = InlineKeyboardBuilder()
    builder.button(text="🚀 Отправить", callback_data="bcast:go")
    builder.button(text="↩ Изменить", callback_data="bcast:edit")
    builder.button(text="❌ Отмена", callback_data="bcast:cancel")
    builder.adjust(1)
    return builder.as_markup()