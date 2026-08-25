"""FSM-состояния визардов.

StatesGroup добавляются в своих заходах (менеджер — Заход 3,
темы/задания — Заходы 4–5, привязка ученика — Заход 6).
Файл — точка сборки всех визардов проекта.
"""
from aiogram.fsm.state import State, StatesGroup


class AddTeacherStates(StatesGroup):
    """Визард «Добавить преподавателя»: @username → выбор предметов."""

    username = State()
    subjects = State()


class AddManagerStates(StatesGroup):
    """Визард «Добавить менеджера»: @username."""

    username = State()


class AddSubjectStates(StatesGroup):
    """Визард «Добавить предмет»: название."""

    name = State()


class AddStudentStates(StatesGroup):
    """Визард «Добавить ученика»: имя → предметы → дата доступа."""

    name = State()
    subjects = State()
    date = State()


class ExtendAccessStates(StatesGroup):
    """Продление доступа ученика: дата."""

    date = State()


class DeleteSubjectStates(StatesGroup):
    """Удаление предмета (владелец): подтверждение вводом точного названия."""

    confirm = State()


class AddThemeStates(StatesGroup):
    """Визард «Добавить тему»: название."""

    name = State()


class AddSubthemeStates(StatesGroup):
    """Визард «Добавить подтему»: название (текущий заход)."""

    name = State()


class RenameSubthemeStates(StatesGroup):
    """Переименование подтемы: новое название."""

    name = State()


class DeleteSubthemeStates(StatesGroup):
    """Удаление подтемы: подтверждение вводом точного названия."""

    confirm = State()


class RenameThemeStates(StatesGroup):
    """Переименование темы: новое название."""

    name = State()


class DeleteThemeStates(StatesGroup):
    """Удаление темы: подтверждение вводом точного названия."""

    confirm = State()


class AddTaskStates(StatesGroup):
    """Визард «Добавить задание»: подтема → вопрос → варианты → правильный →
    объяснение → превью.

    Переходы строго по ТЗ раздел 8; после каждого приёма данных состояние
    ОБЯЗАТЕЛЬНО смениться (критичная ошибка старой версии — «замерзание»).
    Данные визарда живут в state: theme_id, subtheme_id (None — на тему
    напрямую), question_text/photo, options (list[str]), correct_index,
    feedback_text/photo, saving.

    Шаг sub появляется только если у темы есть подтемы (текущий заход).
    Объяснение (UX-пакет): один шаг exp_input вместо выбора «текст/фото»
    — можно слать и текст, и фото (оба сохраняются), «Пропустить»/«✅ Готово»
    уводят в preview.
    """

    sub = State()
    question = State()
    options = State()
    correct = State()
    exp_input = State()
    preview = State()


class GuestBindStates(StatesGroup):
    """Привязка по коду приглашения (Заход 6, ТЗ раздел 7).

    Гость вводит код текстом (или /start КОД) → подтверждение кнопками →
    привязка. Состояние не хранит данных — только ожидание кода.
    """

    code = State()


class DeleteStudentStates(StatesGroup):
    """Полное удаление ученика (UX-пакет): подтверждение словом «удалить»."""

    confirm = State()


class BroadcastStates(StatesGroup):
    """Рассылка сообщений (Заход 9, владелец).

    recipients — категории {"students","teachers","managers"} + режим
    предметов; subjects — выбор ОДНОГО предмета (или «все»);
    message_input — текст и/или фото; confirm — предпросмотр + отправка.
    Данные визарда в state: recipients (list), students_mode
    ("all"|"subjects"), subject_ids (list), text, photo_file_id.
    """

    recipients = State()
    subjects = State()
    message_input = State()
    confirm = State()