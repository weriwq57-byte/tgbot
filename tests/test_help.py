"""«📋 Мои команды» (заход 2.5): кнопка в меню всех ролей + текст по ролям.

Хендлеры вызываются напрямую (как в test_owner.py); роль берётся из
db_user (как в реальном диспетчере после мидлвары), а не из колбэка.
"""
from types import SimpleNamespace

from app.handlers import commands as commands_h
from app.keyboards import inline

ROLES = ("owner", "manager", "teacher", "student", "guest")


class FakeMessage:
    """Message: answer/ edit_text пишут в .answers/.edits."""

    def __init__(self, text="", chat_id=42):
        self.text = text
        self.chat = SimpleNamespace(id=chat_id)
        self.answers = []
        self.edits = []

    async def answer(self, content="", reply_markup=None, **kwargs):
        self.answers.append((content, reply_markup))

    async def edit_text(self, text, reply_markup=None, **kwargs):
        self.edits.append((text, reply_markup))


def make_callback(data, message):
    answers = []

    async def answer(content="", show_alert=False, **kwargs):
        answers.append((content, show_alert))

    return SimpleNamespace(
        data=data, message=message, answer=answer, answers=answers
    )


def cb_buttons(markup):
    if markup is None:
        return []
    return [
        (b.text, b.callback_data)
        for row in markup.inline_keyboard
        for b in row
    ]


# --------------------------------------------------------------------------
# Кнопка в главном меню каждой роли
# --------------------------------------------------------------------------
def test_help_button_in_all_role_menus():
    # У владельца справка доступна через /help — кнопки «Мои команды» нет
    for role in ROLES:
        kb = inline.main_menu_kb(role)
        cbs = [c for _, c in cb_buttons(kb)]
        assert cbs, f"роль {role}: меню пустое"
        if role != "owner":
            assert f"menu:help:{role}:0" in cbs, f"роль {role}: нет кнопки «Мои команды»"


def test_owner_menu_has_solve_button_no_help():
    """У владельца вместо «Мои команды» — «🎯 Решать задания»."""
    kb = inline.main_menu_kb("owner")
    cbs = [c for _, c in cb_buttons(kb)]
    assert "menu:student:subjects:0" in cbs  # «🎯 Решать задания»
    assert "menu:help:owner:0" not in cbs  # справка — только /help
    assert any(t == "🎯 Решать задания" for t, _ in cb_buttons(kb))


# --------------------------------------------------------------------------
# Текст «Мои команды» по ролям
# --------------------------------------------------------------------------
async def test_help_text_owner():
    text = commands_h.help_text("owner")
    assert "📋 <b>Мои команды (владелец):</b>" in text
    for item in commands_h.HELP_BY_ROLE["owner"]:
        assert f"• {item}" in text
    # у владельца все команды реальные — сноски про будущее нет
    assert "появятся в следующих заходах" not in text


async def test_help_text_roles_future_note_gone():
    """Все 5 ролей реализованы — сноски «появятся в следующих заходах»
    больше нет ни у кого (студент/гость тоже со своими кнопками)."""
    for role in ROLES:
        text = commands_h.help_text(role)
        assert f"📋 <b>Мои команды ({commands_h.ROLE_LABELS[role]}):</b>" in text
        for item in commands_h.HELP_BY_ROLE[role]:
            assert f"• {item}" in text
        assert "появятся в следующих заходах" not in text


async def test_help_matches_menu_buttons():
    """«Мои команды» = РОВНО кнопки главного меню роли (ничего лишнего):
    help не обещает команд, которых нет в меню, и покрывает все."""
    for role in ROLES:
        kb = inline.main_menu_kb(role)
        menu_labels = [
            b.text for row in kb.inline_keyboard for b in row
            if b.callback_data != f"menu:help:{role}:0"
        ]
        assert commands_h.HELP_BY_ROLE[role] == menu_labels, (
            f"роль {role}: help рассинхронизирован с меню"
        )


async def test_help_text_manager_commands_are_real():
    """У менеджера команды реализованы (Заход 3) — сноски о будущем нет."""
    text = commands_h.help_text("manager")
    assert "📋 <b>Мои команды (менеджер):</b>" in text
    for item in commands_h.HELP_BY_ROLE["manager"]:
        assert f"• {item}" in text
    assert "появятся в следующих заходах" not in text


async def test_help_text_teacher_commands_are_real():
    """У преподавателя команды реализованы (Заход 4) — сноски о будущем нет."""
    text = commands_h.help_text("teacher")
    assert "📋 <b>Мои команды (преподаватель):</b>" in text
    for item in commands_h.HELP_BY_ROLE["teacher"]:
        assert f"• {item}" in text
    assert "появятся в следующих заходах" not in text


async def test_help_role_from_bd_not_callback():
    """Роль из БД: клик по menu:help:owner:0 менеджером показывает менеджера."""
    msg = FakeMessage()
    cb = make_callback("menu:help:owner:0", msg)
    await commands_h.cb_help_commands(cb, db_user=SimpleNamespace(role="manager"))

    assert msg.edits, "хендлер не отредактировал сообщение"
    text, kb = msg.edits[0]
    assert "📋 <b>Мои команды (менеджер):</b>" in text
    buttons = cb_buttons(kb)
    assert ("← Назад", "menu:back:manager:0") in buttons


async def test_help_each_role_renders():
    for role in ROLES:
        msg = FakeMessage()
        cb = make_callback(f"menu:help:{role}:0", msg)
        await commands_h.cb_help_commands(cb, db_user=SimpleNamespace(role=role))

        text, kb = msg.edits[-1]
        assert f"Мои команды ({commands_h.ROLE_LABELS[role]})" in text
        assert ("← Назад", f"menu:back:{role}:0") in cb_buttons(kb)


def test_combined_menu_has_both_clusters():
    """Совмещённый менеджер+препод видит кнопки ОБОИХ ролей."""
    for roles in (("manager", "teacher"), ("teacher", "manager")):
        kb = inline.main_menu_kb(roles)
        buttons = cb_buttons(kb)
        for cb in (
            "menu:manager:students:0",
            "menu:manager:add_student:0",
            "menu:manager:expiring:0",
            "menu:teacher:subjects:0",
            "menu:student:subjects:0",
        ):
            assert any(c == cb for _, c in buttons), f"{roles}: нет кнопки {cb}"
        assert any(c == f"menu:help:{roles[0]}:0" for _, c in buttons)


def test_combined_help_text_has_both_sections():
    """«Мои команды» совмещённого: оба блока, подпись «← Назад» — первичная."""
    roles = ("teacher", "manager")
    text = commands_h.help_text_roles(roles)
    assert "Мои команды (преподаватель):" in text
    assert "Мои команды (менеджер):" in text
    for item in commands_h.HELP_BY_ROLE["teacher"] + commands_h.HELP_BY_ROLE["manager"]:
        assert f"• {item}" in text


async def test_combined_help_renders_from_bd():
    """Хендлер help рисует оба блока по ролям из БД (role+role2)."""
    msg = FakeMessage()
    cb = make_callback("menu:help:manager:0", msg)
    await commands_h.cb_help_commands(
        cb,
        db_user=SimpleNamespace(role="manager", role2="teacher"),
    )
    text, kb = msg.edits[-1]
    assert "Мои команды (менеджер):" in text
    assert "Мои команды (преподаватель):" in text
    assert ("← Назад", "menu:back:manager:0") in cb_buttons(kb)