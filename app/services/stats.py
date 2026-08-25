"""Статистика ученика ПО ПРЕДМЕТАМ (фича от 12.08–13.08, не в ТЗ).

Каждый активный предмет ученика — отдельный блок дашборда: стрик
(по предмету), точность за период с полоской █/▒, «сегодня», тренд
(7 дней против предыдущих 7), темы пройдено, ошибки в разборе.

Считается всё сразу за все периоды (7д / 30д / всё) — переключение
кнопками не делает новых запросов (идея из бэклога). Даты по Минску,
счёт только по активным заданиям (Task.is_active).
"""
from datetime import datetime, time, timedelta

from sqlalchemy import Integer, func, select

from app.models import (
    Attempt,
    Student,
    StudentSubject,
    Subject,
    Task,
    TaskProgress,
    Theme,
)
from app.utils.dates import MINSK_TZ, now_minsk, today_minsk

BAR_LEN = 12


def _bar(percent: float) -> str:
    """Полоска точности: █ до 12 символов, остаток ▒ (0% → 0 блоков)."""
    filled = round(percent / 100 * BAR_LEN) if percent > 0 else 0
    return "█" * filled + "▒" * (BAR_LEN - filled)


def _accuracy(correct: int, total: int) -> tuple[float, str]:
    """(процент, строка «NN% · K из M»); без ответов — (0.0, «—»)."""
    if total <= 0:
        return 0.0, "—"
    pct = round(correct / total * 100)
    return pct, f"{pct}% · {correct} из {total}"


async def _subject_attempts(
    session, student_id: int, subject_id: int, since
) -> dict:
    """{total, correct} по Attempts предмета за период (since — datetime)."""
    rows = (
        await session.execute(
            select(
                func.count(),
                func.sum(Attempt.is_correct.cast(Integer)),
            )
            .select_from(Attempt)
            .join(Task, Task.id == Attempt.task_id)
            .join(Theme, Theme.id == Task.theme_id)
            .where(
                Attempt.student_id == student_id,
                Theme.subject_id == subject_id,
                Attempt.answered_at >= since,
            )
        )
    ).one()
    total = rows[0] or 0
    correct = int(rows[1] or 0)
    return {"total": total, "correct": correct}


async def _subject_wrong_remaining(session, student_id: int, subject_id: int) -> int:
    """Ошибки в разборе по предмету: wrong-задания (не закрытые done)."""
    return (
        await session.scalar(
            select(func.count())
            .select_from(TaskProgress)
            .join(Task, Task.id == TaskProgress.task_id)
            .join(Theme, Theme.id == Task.theme_id)
            .where(
                TaskProgress.student_id == student_id,
                Theme.subject_id == subject_id,
                TaskProgress.status == "wrong",
                Task.is_active.is_(True),
            )
        )
    ) or 0


async def _subject_themes_done(session, student_id: int, subject_id: int) -> int:
    """Темы пройдено: темы предмета, где каждое активное задание — done.

    Считаем по задачам: тема пройдена, если solved == total (total>0).
    """
    rows = (
        await session.execute(
            select(Task.theme_id, Task.id)
            .join(Theme, Theme.id == Task.theme_id)
            .where(Theme.subject_id == subject_id, Task.is_active.is_(True))
        )
    ).all()
    task_ids = [t for _, t in rows]
    if not task_ids:
        return 0
    progress = dict(
        (
            await session.execute(
                select(TaskProgress.task_id, TaskProgress.status).where(
                    TaskProgress.student_id == student_id,
                    TaskProgress.task_id.in_(task_ids),
                )
            )
        ).all()
    )
    done_themes = 0
    by_theme: dict[int, list[int]] = {}
    for theme_id, task_id in rows:
        by_theme.setdefault(theme_id, []).append(task_id)
    for theme_id, ids in by_theme.items():
        if all(progress.get(tid) == "done" for tid in ids):
            done_themes += 1
    return done_themes


async def build_stats(session, student_id: int) -> dict | None:
    """Дашборд статистики ученика по предметам.

    Возвращает {student, subject_blocks: [{name, streak_current,
    streak_best, periods: {7: {...}, 30: {...}, all: {...}}, wrong,
    themes_done}]} или None, если ученика нет.

    period-блок: {total, correct, accuracy_text, bar, today_correct,
    today_total, trend_text}.
    """
    student = await session.get(Student, student_id)
    if student is None:
        return None

    links = (
        await session.execute(
            select(Subject, StudentSubject)
            .join(StudentSubject, StudentSubject.subject_id == Subject.id)
            .where(
                StudentSubject.student_id == student.id,
                StudentSubject.is_active.is_(True),
            )
            .order_by(Subject.name.asc(), Subject.id.asc())
        )
    ).all()

    now = now_minsk()
    today = today_minsk()
    blocks = []
    for subject, link in links:
        periods: dict[int, dict] = {}
        for period in (7, 30, None):
            since = (
                (now - timedelta(days=period)).date()
                if period is not None
                else today - timedelta(days=365 * 10)
            )
            since_dt = _to_midnight(since)
            att = await _subject_attempts(session, student.id, subject.id, since_dt)
            today_att = await _subject_attempts(
                session, student.id, subject.id, _to_midnight(today)
            )
            pct, acc_text = _accuracy(att["correct"], att["total"])
            key = str(period) if period is not None else "all"
            periods[key] = {
                "total": att["total"],
                "correct": att["correct"],
                "accuracy_text": acc_text,
                "bar": _bar(pct),
                "today_correct": today_att["correct"],
                "today_total": today_att["total"],
            }
        # Тренд: точность за последние 7 дней против предыдущих 7
        prev7_start = _to_midnight((now - timedelta(days=14)).date())
        cur7_start = _to_midnight((now - timedelta(days=7)).date())
        prev = await _subject_attempts(session, student.id, subject.id, prev7_start)
        cur = await _subject_attempts(session, student.id, subject.id, cur7_start)
        if prev["total"] == 0 and cur["total"] == 0:
            trend = "нет данных"
        else:
            prev_pct = prev["correct"] / prev["total"] if prev["total"] else 0
            cur_pct = cur["correct"] / cur["total"] if cur["total"] else 0
            delta = round(cur_pct * 100 - prev_pct * 100)
            if delta > 0:
                trend = f"📈 +{delta}%"
            elif delta < 0:
                trend = f"📉 {delta}%"
            else:
                trend = "➖ 0%"
        blocks.append(
            {
                "name": subject.name,
                "streak_current": link.streak_current or 0,
                "streak_best": link.streak_best or 0,
                "periods": periods,
                "wrong": await _subject_wrong_remaining(
                    session, student.id, subject.id
                ),
                "themes_done": await _subject_themes_done(
                    session, student.id, subject.id
                ),
                "trend": trend,
            }
        )
    return {"student": student, "subject_blocks": blocks}


def _to_midnight(d: "date") -> "datetime":
    """Полночь по Минску для границы периода (aware)."""
    return datetime.combine(d, time.min, tzinfo=MINSK_TZ)


def stats_text(data: dict, period: str = "7") -> str:
    """Текст дашборда: блоки по предметам, выбранный период в шапке.

    period: «7» | «30» | «all». Ученика без предметов — отдельная строка.
    """
    blocks = data["subject_blocks"]
    period_label = {"7": "за 7 дней", "30": "за 30 дней", "all": "за всё время"}[
        period
    ]
    lines = [f"📊 <b>Твоя статистика</b> ({period_label})", ""]
    if not blocks:
        lines.append("Предметы пока не назначены — напиши менеджеру.")
        return "\n".join(lines)
    for i, b in enumerate(blocks):
        if i:
            lines.append("")
        p = b["periods"][period]
        lines.append(f"<b>{b['name']}</b>")
        lines.append(f"🔥 Стрик: {b['streak_current']} (рекорд: {b['streak_best']})")
        lines.append(f"Точность — {p['accuracy_text']}")
        lines.append(p["bar"])
        lines.append(
            f"Сегодня: {p['today_correct']} из {p['today_total']} ✅"
            if p["today_total"]
            else "Сегодня: ещё нет решённых"
        )
        lines.append(f"Тренд: {b['trend']}")
        lines.append(f"🔁 Ошибок в разборе: {b['wrong']} · 🏁 Тем пройдено: {b['themes_done']}")
    return "\n".join(lines)
