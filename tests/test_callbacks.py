"""Реестр callback-данных: «нет мёртвых кнопок» (ошибка №7 старой версии).

Тест генерирует клавиатуры из КАЖДОЙ функции app.keyboards.inline и
проверяет, что каждый callback-данного имеет обработчик:
- точное совпадение в EXACT_CALLBACKS (F.data == ...);
- либо startswith одного из PREFIX_CALLBACKS (F.data.startswith).

Правило проекта: кнопку добавляем в том же заходе, что и обработчик,
поэтому префиксы FUTURE_CALLBACKS (будущие заходы) не должны встречаться
в клавиатурах — иначе тест падает и напоминает, что обработчика нет.

Если добавишь новую функцию клавиатуры в inline.py и забудешь указать
её в KB_CASES — отдельный тест test_all_keyboards_covered тоже упадёт.
"""
import inspect

from aiogram.types import InlineKeyboardMarkup

import app.keyboards.inline as inline
from app.models import StudentSubject, Subject, Subtheme, Task, Theme, User

# --------------------------------------------------------------------------
# Образцы данных для генерации клавиатур
# --------------------------------------------------------------------------
PEOPLE = [
    User(id=1, tg_username="ivanov_math"),
    User(id=2, tg_username=None, tg_full_name="Иван Иванов"),
]
SUBJECTS = [
    Subject(id=1, name="Математика"),
    Subject(id=2, name="Физика"),
]
# Образцы для клавиатур менеджера (Заход 3)
STUDENT_ROWS = [
    {"id": 1, "name": "Иван Иванов", "streak": 3, "subject_names": ["Математика"],
     "access_until": None, "linked": False, "expired": False, "active": True},
    {"id": 2, "name": "Пётр Петров", "streak": 0, "subject_names": [],
     "access_until": None, "linked": True, "expired": True, "active": True},
]
STUDENT_SUBJECT_LINKS = [
    (Subject(id=1, name="Математика"), StudentSubject(student_id=1, subject_id=1, is_active=True)),
    (Subject(id=2, name="Физика"), StudentSubject(student_id=1, subject_id=2, is_active=False)),
]
# Образцы для клавиатур преподавателя (Заход 4)
THEMES = [
    Theme(id=1, subject_id=1, title="Уравнения", is_open=True, mode="sequential"),
    Theme(id=2, subject_id=1, title="Теория чисел", is_open=False, mode="sequential"),
    Theme(id=3, subject_id=1, title="Рандом", is_open=True, mode="random"),
]
# Образцы для клавиатур подтем (текущий заход)
SUBTHEMES = [
    Subtheme(id=10, theme_id=1, title="Линейные"),
    Subtheme(id=11, theme_id=1, title="Квадратные"),
]
SUBTHEMES_WITH_COUNT = [
    {"subtheme": SUBTHEMES[0], "count": 3},
    {"subtheme": SUBTHEMES[1], "count": 0},
]
SUBJECT_WITH_THEMES = {
    "subject": Subject(id=1, name="Математика"),
    "themes": [(THEMES[0], 3), (THEMES[1], 0)],
}
# Образцы для клавиатур заданий (Заход 5)
TASK_OPTIONS = ["4", "5x", "x+1"]
TASK_ACTIVE = Task(
    id=1, theme_id=1, question_text="Сколько будет 2+2?",
    options=[{"t": "4", "c": True}, {"t": "5x", "c": False}], is_active=True,
)
TASK_HIDDEN = Task(
    id=2, theme_id=1, question_photo_id="photo1", options=[], is_active=False,
)
# Образцы для клавиатур ученика (Заход 6)
STUDENT_THEME_ITEMS = [
    {
        "subject": SUBJECTS[0],
        "themes": [
            {"theme": THEMES[0], "progress": {"solved": 1, "total": 3, "remaining": 2},
             "all_done": False, "wrong_count": 2},
            {"theme": THEMES[1], "progress": {"solved": 2, "total": 2, "remaining": 0},
             "all_done": True, "wrong_count": 0},
        ],
    },
]

# Вызовы: имя функции → список кортежей позиционных аргументов.
# Каждая комбинация генерирует клавиатуру и прогоняется через проверку.
KB_CASES: dict[str, list[tuple]] = {
    "main_menu_kb": [
        ("owner",), ("manager",), ("teacher",), ("student",), ("guest",),
        ("manager", "teacher"),  # совмещённые роли: обе секции в меню
        ("teacher", "manager"),
    ],
    "owner_teachers_menu_kb": [()],
    "owner_managers_menu_kb": [()],
    "owner_subjects_menu_kb": [()],
    "kick_categories_kb": [()],
    "people_list_kb": [
        (PEOPLE, "owner:rt:pick", None, "owner:rt:list:0"),
        (PEOPLE, "owner:rm:pick", None, "owner:rm:list:0"),
        (PEOPLE, "owner:kick:pick", None, "owner:kick:back:0"),
        (PEOPLE, "owner:rt:pick"),  # по умолчанию ← Назад в главное меню
    ],
    "subject_toggle_kb": [(SUBJECTS,)],
    "subject_delete_kb": [(SUBJECTS,), ([],)],
    "subject_delete_confirm_kb": [()],
    "subject_delete_kb": [(SUBJECTS,)],
    "multiselect_kb": [
        (SUBJECTS, {1}, "owner:at", "owner:at:done:0"),
        (SUBJECTS, {1, 2}, "mgr:as", "mgr:as:done:0"),
    ],
    "confirm_kb": [
        ("owner:rt:yes:1:0", "owner:rt:no:0", "Да, убрать", "Отмена"),
        ("owner:rm:yes:2:0", "owner:rm:no:0", "Да, убрать", "Отмена"),
        ("owner:kick:yes:3:0", "owner:kick:no:0", "Да, деактивировать", "Отмена"),
    ],
    "guest_pick_entry_kb": [("teacher",), ("manager",)],
    "guest_people_list_kb": [
        (PEOPLE, "owner:guestsel:teacher"),
        (PEOPLE, "owner:guestsel:manager"),
    ],
    "students_list_kb": [(STUDENT_ROWS,)],
    "student_card_kb": [
        (1, STUDENT_SUBJECT_LINKS, True, True),
        (1, STUDENT_SUBJECT_LINKS, False, False),
    ],
    "expiring_kb": [
        ([("• Иван Иванов — до 31.05.2027", 1),
          ("• Пётр Петров — до 01.01.2026 (просрочка 7 дн.)", 2)],),
        ([],),  # пустой экран «Истекающих нет»
    ],
    "add_student_continue_kb": [()],
    "confirm_deactivate_kb": [(7,)],
    "teacher_subjects_kb": [
        ([SUBJECT_WITH_THEMES],),
        ([], "menu:back:owner:0"),
    ],
    "theme_list_kb": [
        (THEMES, 1),
        ([], 1, "menu:back:owner:0"),
    ],
    "theme_menu_kb": [(THEMES[0],), (THEMES[1],), (THEMES[2],)],
    "subthemes_list_kb": [(SUBTHEMES_WITH_COUNT, 1), ([], 1)],
    "subtheme_menu_kb": [(SUBTHEMES[0],)],
    "subtheme_pick_kb": [(SUBTHEMES, 1), ([], 2)],
    "confirm_del_subtheme_kb": [(5,)],
    "themes_pick_kb": [
        (THEMES, "menu:back:teacher:0"),
        ([],),
    ],
    "confirm_del_theme_kb": [(5,)],
    "options_more_kb": [()],
    "options_done_kb": [()],
    "options_pick_kb": [(TASK_OPTIONS,)],
    "exp_more_kb": [()],
    "exp_pass_kb": [()],
    "preview_kb": [(TASK_OPTIONS,)],
    "tasks_menu_kb": [(1,)],
    "task_card_kb": [(TASK_ACTIVE,), (TASK_HIDDEN,)],
    "confirm_del_task_kb": [(7,)],
    "bind_confirm_kb": [("ABC123",)],
    "confirm_del_student_kb": [(7,)],
    "guest_code_kb": [()],
    "subjects_pick_kb": [
        (STUDENT_THEME_ITEMS,),
        ([], "menu:back:owner:0"),
    ],
    "student_subjects_kb": [
        (STUDENT_THEME_ITEMS,),
        (STUDENT_THEME_ITEMS, "menu:back:owner:0"),
        ([],),
    ],
    "task_kb": [
        (TASK_ACTIVE, 2),
        (TASK_HIDDEN, 0),
        (TASK_ACTIVE, 2, [1, 0], True),
        (TASK_ACTIVE, 2, [1, 0], False, 738000),  # фикс perm: день выдачи
    ],
    "answer_actions_kb": [(3,)],
    "errors_actions_kb": [(3,)],
    "errors_done_kb": [(4,)],
    "theme_result_kb": [(4,)],
    # Заход 8, п.0.3: пустая тема — «Другие темы» + «← Назад», без «Повторить»
    "theme_empty_kb": [(5,)],
    # Заход 9: рассылка
    "bcast_categories_kb": [
        (set(), "all"),
        ({"students"}, "all"),
        ({"students", "teachers", "managers"}, "subjects"),
    ],
    "bcast_subjects_kb": [(SUBJECTS,), ([],)],
    "bcast_confirm_kb": [()],
    "stats_kb": [("7",), ("30",), ("all",)],
}

# Служебные функции модуля (не клавиатуры) — их в KB_CASES не ждём
_NOT_KEYBOARDS = {"back_button", "person_label", "subject_label"}


def _keyboard_functions() -> dict[str, callable]:
    """Имена функций модуля, возвращающих InlineKeyboardMarkup."""
    result = {}
    for name, obj in inspect.getmembers(inline, inspect.isfunction):
        if name in _NOT_KEYBOARDS:
            continue
        try:
            ann = inspect.signature(obj).return_annotation
        except (ValueError, TypeError):
            continue
        if ann is InlineKeyboardMarkup:
            result[name] = obj
    return result


def _iter_callback_data(markup):
    """Плоский список callback_data кнопок (None — не колбэки)."""
    for row in markup.inline_keyboard:
        for button in row:
            if button.callback_data is not None:
                yield button.callback_data


# --------------------------------------------------------------------------
# Покрытие: каждая клавиатура-функция описана в KB_CASES
# --------------------------------------------------------------------------
def test_all_keyboards_covered():
    keyboards = _keyboard_functions()
    covered = set(KB_CASES)
    assert covered == set(keyboards), (
        f"В KB_CASES лишние {sorted(covered - set(keyboards))}; "
        f"не покрыты тестом: {sorted(set(keyboards) - covered)}"
    )


# --------------------------------------------------------------------------
# Нет мёртвых кнопок: каждый колбэк имеет обработчик
# --------------------------------------------------------------------------
def _has_handler(cb: str) -> bool:
    if cb in inline.EXACT_CALLBACKS:
        return True
    if any(cb.startswith(p) for p in inline.PREFIX_CALLBACKS):
        return True
    return False


def test_no_dead_buttons():
    dead = []
    for name, cases in KB_CASES.items():
        fn = getattr(inline, name)
        for args in cases:
            markup = fn(*args)
            for cb in _iter_callback_data(markup):
                if not _has_handler(cb):
                    dead.append(f"{name}{args} → колбэк {cb!r} без обработчика")
    assert not dead, "\n".join(sorted(set(dead)))


def test_future_callbacks_not_used():
    """Будущие префиксы не должны появляться в клавиатурах."""
    used = []
    for name, cases in KB_CASES.items():
        fn = getattr(inline, name)
        for args in cases:
            for cb in _iter_callback_data(fn(*args)):
                if any(cb.startswith(p) for p in inline.FUTURE_CALLBACKS):
                    used.append(f"{name} → {cb!r} (будущий заход: обработчика ещё нет)")
    assert not used, "\n".join(used)


def test_registry_clean():
    """Реестр аккуратен: точные данные не дублируются в префиксах."""
    for exact in inline.EXACT_CALLBACKS:
        assert not any(exact.startswith(p) for p in inline.PREFIX_CALLBACKS), exact
    assert not (set(inline.EXACT_CALLBACKS) & set(inline.PREFIX_CALLBACKS))
    assert not (
        set(inline.EXACT_CALLBACKS) & set(inline.FUTURE_CALLBACKS)
    )


def test_subject_toggle_has_back_button():
    """Список «Скрыть/Показать» не застревающий: есть «← Назад» (регрессия
    на дубликат функции, который перетирал версию с кнопкой)."""
    kb = inline.subject_toggle_kb(SUBJECTS)
    back = [c for c in _iter_callback_data(kb) if c == "owner:subj:back:0"]
    assert back, "В списке предметов нет кнопки «← Назад»"