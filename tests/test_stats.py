"""Статистика ученика ПО ПРЕДМЕТАМ (фича от 12.08–13.08, не в ТЗ).

Дашборд с блоками по активным предметам: стрик по предмету, точность
за 7д/30д/всё с полоской █/▒, «сегодня», тренд 7д против предыдущих 7д,
ошибки в разборе, темы пройдено. Проверяем сервис (build_stats/stats_text)
и хендлеры (/stats, кнопка меню, переключение периода).
"""
from datetime import timedelta
from types import SimpleNamespace

from sqlalchemy import select

from app.models import (
    Attempt,
    Student,
    StudentSubject,
    Subject,
    Task,
    TaskProgress,
    Theme,
    User,
)
from app.services import stats as stats_svc
from app.services import student as student_svc
from app.services import students as students_svc
from app.utils.dates import now_minsk, today_minsk

TODAY = today_minsk()

LETTERS = "АБВГ"


class FakeMessage:
    def __init__(self, text="", chat_id=42, from_user_id=42):
        self.text = text
        self.chat = SimpleNamespace(id=chat_id)
        self.from_user = SimpleNamespace(id=from_user_id)
        self.answers = []
        self.edits = []

    async def answer(self, content="", reply_markup=None, **kwargs):
        self.answers.append((content, reply_markup))

    async def edit_text(self, text, reply_markup=None, **kwargs):
        self.edits.append((text, reply_markup))


def make_cb(data: str, message: FakeMessage, from_user_id=42):
    answers = []

    async def answer(content="", show_alert=False, **kwargs):
        answers.append((content, show_alert))

    return SimpleNamespace(
        data=data,
        message=message,
        answer=answer,
        answers=answers,
        from_user=SimpleNamespace(id=from_user_id),
    )


def cb_buttons(markup):
    if markup is None:
        return []
    return [(b.text, b.callback_data) for row in markup.inline_keyboard for b in row]


def db_user(role: str, user_id: int) -> SimpleNamespace:
    return SimpleNamespace(role=role, id=user_id, tg_id=user_id)


_SEQ = {"n": 0}


async def _mk_user(session_factory, role="student"):
    _SEQ["n"] += 1
    async with session_factory() as session:
        user = User(
            tg_id=500000000 + _SEQ["n"],
            tg_username=f"st_{role}_{_SEQ['n']}",
            role=role,
            is_active=True,
        )
        session.add(user)
        await session.commit()
        return user


async def _mk_subject(session_factory, name="Математика") -> Subject:
    async with session_factory() as session:
        subject = Subject(name=name, is_active=True)
        session.add(subject)
        await session.commit()
        return subject


async def _mk_theme(session_factory, subject_id: int, title="Уравнения") -> Theme:
    async with session_factory() as session:
        theme = Theme(
            subject_id=subject_id, title=title, is_open=True, mode="sequential"
        )
        session.add(theme)
        await session.commit()
        return theme


async def _mk_task(
    session_factory,
    theme_id: int,
    question="2+2?",
    n_options: int = 4,
    correct: int = 0,
    order: int = 0,
) -> Task:
    async with session_factory() as session:
        task = Task(
            theme_id=theme_id,
            question_text=question,
            options=[
                {"t": f"Вариант {i}", "c": i == correct} for i in range(n_options)
            ],
            order=order,
            is_active=True,
        )
        session.add(task)
        await session.commit()
        return task


async def _mk_student(session_factory, subject_id: int):
    u = await _mk_user(session_factory)
    async with session_factory() as session:
        user, student, _ = await students_svc.create_student_record(
            session, "Иван Иванов", {subject_id}, u.id, TODAY + timedelta(days=30)
        )
        await session.commit()
        return user, student


async def _mk_attempts(session_factory, student_id: int, subject_id: int):
    """3 правильных + 1 неправильный ответ за сегодня по предмету."""
    theme_id = (await _mk_theme(session_factory, subject_id)).id
    task_ok = await _mk_task(session_factory, theme_id, correct=0, order=0)
    task_bad = await _mk_task(session_factory, theme_id, correct=1, order=1)
    async with session_factory() as session:
        for task_id in (task_ok.id,) * 3 + (task_bad.id,):
            session.add(
                Attempt(
                    student_id=student_id,
                    task_id=task_id,
                    answer_index=0,
                    is_correct=task_id == task_ok.id,
                )
            )
        session.add(
            TaskProgress(
                student_id=student_id,
                task_id=task_ok.id,
                status="done",
            )
        )
        session.add(
            TaskProgress(
                student_id=student_id,
                task_id=task_bad.id,
                status="wrong",
            )
        )
        # Стрик по предмету: сегодня решал, 2 дня (вчера тоже решал)
        link = await session.scalar(
            select(StudentSubject).where(
                StudentSubject.student_id == student_id,
                StudentSubject.subject_id == subject_id,
            )
        )
        link.streak_current = 2
        link.streak_best = 5
        link.last_solved_date = TODAY
        await session.commit()


# ---------------------------------------------------------------------------
# Сервис: build_stats / stats_text
# ---------------------------------------------------------------------------
async def test_build_stats_no_subjects(session_factory):
    mgr = await _mk_user(session_factory, role="manager")
    async with session_factory() as session:
        _user, student, _ = await students_svc.create_student_record(
            session, "Без предметов", set(), mgr.id, TODAY + timedelta(days=30)
        )
        await session.commit()
        data = await stats_svc.build_stats(session, student.id)
    assert data is not None and data["subject_blocks"] == []
    text = stats_svc.stats_text(data)
    assert "Предметы пока не назначены — напиши менеджеру." in text
    # периода в шапке соответствует запрошенному
    assert "(за 7 дней)" in text


async def test_build_stats_unknown_student(session_factory):
    async with session_factory() as session:
        assert await stats_svc.build_stats(session, 999999) is None


async def test_build_stats_has_progress_and_streak(session_factory):
    subj = await _mk_subject(session_factory)
    user, student = await _mk_student(session_factory, subj.id)
    await _mk_attempts(session_factory, student.id, subj.id)

    async with session_factory() as session:
        data = await stats_svc.build_stats(session, student.id)
    assert data is not None
    blocks = data["subject_blocks"]
    assert len(blocks) == 1
    b = blocks[0]
    assert b["name"] == "Математика"
    # Стрик поднят: сегодня решал, был вчера → 2 (но на поле выше ставили)
    assert b["streak_current"] == 2
    assert b["streak_best"] == 5
    p7 = b["periods"]["7"]
    assert p7["total"] == 4
    assert p7["correct"] == 3
    assert p7["accuracy_text"] == "75% · 3 из 4"
    assert p7["bar"] == "█" * 9 + "▒" * 3  # 75% от 12 = 9
    assert p7["today_correct"] == 3 and p7["today_total"] == 4
    assert b["wrong"] == 1  # одно wrong-задание в разборе
    assert b["themes_done"] == 0  # тема не пройдена: wrong-задание осталось
    assert b["trend"] in ("➖ 0%", "📈 +0%", "📉 0%")

    text = stats_svc.stats_text(data, "7")
    assert "📊 <b>Твоя статистика</b> (за 7 дней)" in text
    assert "<b>Математика</b>" in text
    assert "🔥 Стрик: 2 (рекорд: 5)" in text
    assert "Точность — 75% · 3 из 4" in text
    assert "Сегодня: 3 из 4 ✅" in text
    assert "🔁 Ошибок в разборе: 1 · 🏁 Тем пройдено: 0" in text


async def test_build_stats_no_today_answers(session_factory):
    """Сегодня ничего не решал — «Сегодня: ещё нет решённых» (нейтрально)."""
    subj = await _mk_subject(session_factory)
    _user, student = await _mk_student(session_factory, subj.id)
    theme_id = (await _mk_theme(session_factory, subj.id)).id
    task = await _mk_task(session_factory, theme_id, correct=0, order=0)
    yesterday = today_minsk() - timedelta(days=1)
    async with session_factory() as session:
        session.add(
            Attempt(
                student_id=student.id,
                task_id=task.id,
                answer_index=0,
                is_correct=True,
                answered_at=now_minsk() - timedelta(days=1),
            )
        )
        await session.commit()
        data = await stats_svc.build_stats(session, student.id)
        text = stats_svc.stats_text(data)
    assert "Сегодня: ещё нет решённых" in text
    assert "Точность — 100% · 1 из 1" in text  # вчерашний ответ за 7 дней учтён


async def test_build_stats_themes_done_counted(session_factory):
    """Тема пройдена (все активные задания done) → «🏁 Тем пройдено»."""
    subj = await _mk_subject(session_factory)
    _user, student = await _mk_student(session_factory, subj.id)
    theme_id = (await _mk_theme(session_factory, subj.id)).id
    t1 = await _mk_task(session_factory, theme_id, correct=0, order=0)
    t2 = await _mk_task(session_factory, theme_id, correct=1, order=1)
    async with session_factory() as session:
        for task in (t1, t2):
            session.add(Attempt(student_id=student.id, task_id=task.id,
                                answer_index=0, is_correct=True))
            session.add(TaskProgress(student_id=student.id, task_id=task.id,
                                     status="done"))
        await session.commit()
        data = await stats_svc.build_stats(session, student.id)
        text = stats_svc.stats_text(data)
    assert "🏁 Тем пройдено: 1" in text
    assert "🔁 Ошибок в разборе: 0" in text


# ---------------------------------------------------------------------------
# Клавиатура периодов
# ---------------------------------------------------------------------------
def test_stats_kb_periods():
    from app.keyboards.inline import stats_kb

    kb = stats_kb("7")
    buttons = cb_buttons(kb)
    assert ("✅ 7 дней", "stats:7:0") in buttons
    assert ("30 дней", "stats:30:0") in buttons
    assert ("Всё время", "stats:all:0") in buttons
    kb30 = stats_kb("30")
    assert ("✅ 30 дней", "stats:30:0") in cb_buttons(kb30)


# ---------------------------------------------------------------------------
# Хендлеры: /stats, кнопка меню, переключение периода
# ---------------------------------------------------------------------------
async def test_cmd_stats(session_factory):
    from app.handlers import student as s_h

    subj = await _mk_subject(session_factory)
    user, student = await _mk_student(session_factory, subj.id)
    msg = FakeMessage(text="/stats")
    await s_h.cmd_stats(msg, db_user=db_user("student", user.id))
    assert len(msg.answers) == 1
    text, kb = msg.answers[0]
    assert "<b>Математика</b>" in text  # блок есть
    assert ("✅ 7 дней", "stats:7:0") in cb_buttons(kb)


async def test_cmd_stats_without_profile(session_factory):
    """/stats у пользователя без профиля ученика — отказ."""
    from app.handlers import student as s_h

    u = await _mk_user(session_factory, role="student")
    msg = FakeMessage(text="/stats")
    await s_h.cmd_stats(msg, db_user=db_user("student", u.id))
    assert msg.answers[0][0] == "Предмет больше не доступен — напиши менеджеру."


async def test_cb_menu_student_stats(session_factory):
    """Кнопка «📊 Статистика» в меню ученика ведёт на дашборд."""
    from app.handlers import student as s_h

    subj = await _mk_subject(session_factory)
    user, student = await _mk_student(session_factory, subj.id)
    msg = FakeMessage()
    cb = make_cb("menu:student:stats:0", msg, from_user_id=user.tg_id)
    await s_h.cb_menu_student_stats(cb, db_user=db_user("student", user.id))
    assert len(msg.answers) == 1
    text, kb = msg.answers[0]
    assert text.startswith("📊 <b>Твоя статистика</b>")
    # дашборд — с клавиатурой периодов
    assert ("✅ 7 дней", "stats:7:0") in cb_buttons(kb)


async def test_cb_stats_period_switches(session_factory):
    """Переключение периода — edit того же сообщения."""
    from app.handlers import student as s_h

    subj = await _mk_subject(session_factory)
    user, student = await _mk_student(session_factory, subj.id)
    msg = FakeMessage()
    cb = make_cb("stats:30:0", msg, from_user_id=user.tg_id)
    await s_h.cb_stats_period(cb, db_user=db_user("student", user.id))
    assert len(msg.edits) == 1
    text, kb = msg.edits[0]
    assert "(за 30 дней)" in text
    assert ("✅ 30 дней", "stats:30:0") in cb_buttons(kb)
    assert ("Всё время", "stats:all:0") in cb_buttons(kb)


async def test_cb_stats_period_without_profile(session_factory):
    from app.handlers import student as s_h

    u = await _mk_user(session_factory, role="student")
    msg = FakeMessage()
    cb = make_cb("stats:7:0", msg, from_user_id=u.tg_id)
    await s_h.cb_stats_period(cb, db_user=db_user("student", u.id))
    assert cb.answers == [("Предмет больше не доступен — напиши менеджеру.", True)]
    assert not msg.edits


# ---------------------------------------------------------------------------
# Стрик по предмету участвует в дашборде (связка с streaks)
# ---------------------------------------------------------------------------
async def test_stats_streak_from_subject_link(session_factory):
    """Стрик/рекорд в дашборде — из StudentSubject, не из users."""
    subj = await _mk_subject(session_factory)
    user, student = await _mk_student(session_factory, subj.id)
    await _mk_attempts(session_factory, student.id, subj.id)
    async with session_factory() as session:
        u = await session.get(User, user.id)
        u.streak_current = 999  # «старое» поле на users не должно попасть в блок
        await session.commit()
        data = await stats_svc.build_stats(session, student.id)
    assert data["subject_blocks"][0]["streak_current"] == 2
    assert data["subject_blocks"][0]["streak_best"] == 5