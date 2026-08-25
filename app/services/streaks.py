"""Стрики ученика ПО ПРЕДМЕТУ (ТЗ раздел 9 + доработка владельца 13.08).

Каждый предмет (StudentSubject) ведёт свой стрик: первый ответ дня по
предмету решает (вчера → +1, сегодня → без изменений, пропуск → 1),
день без ответов по предмету сбрасывает его стрик до 1 при следующем
ответе. Рекорд (streak_best) обновляется сразу.

Фраза «И новый рекорд стрика» в итоге темы показывается ТОЛЬКО при
реальном побитии рекорда. Факт побития (best_updated) случается в
check_answer, а итог темы рисуется позже — при выдаче all_done. БД-поля
под это нет, поэтому держим лёгкий кэш «ключ → дата побития» в памяти
процесса (валиден для одного поллинга, что устраивает MVP).
"""
from datetime import date, timedelta

from app.utils.dates import today_minsk

# ключ (student_id, subject_id) → дата побития рекорда по предмету
_record_broken_dates: dict[tuple[int, int], date] = {}


def _remember_record_broken(key: tuple[int, int], day: date) -> None:
    """Фиксирует факт: в день day по предмету key побит рекорд стрика."""
    stale = [k for k, d in _record_broken_dates.items() if d != day]
    for k in stale:
        del _record_broken_dates[k]
    if key is not None:
        _record_broken_dates[key] = day


def record_broken_today(key: tuple[int, int]) -> bool:
    """Побит ли рекорд стрика именно сегодня (по предмету)."""
    return _record_broken_dates.get(key) == today_minsk()


def clear_record_cache() -> None:
    """Сброс кэша (для тестов)."""
    _record_broken_dates.clear()


def register_solved(session, holder, key: tuple[int, int]) -> tuple[int, int, bool]:
    """Регистрирует «сегодня решал по предмету» и обновляет стрик предмета.

    holder — объект с полями streak_current / streak_best / last_solved_date
    (StudentSubject; для Cell-заглушек — просто атрибуты). Коммит делает
    вызывающий — вместе с записью ответа. key — (student_id, subject_id)
    для кэша рекордов. Возвращает (current, best, best_updated).
    """
    today = today_minsk()
    last = holder.last_solved_date

    if last == today:
        current = holder.streak_current or 0
        return current, holder.streak_best or 0, False

    if last is not None and last == today - timedelta(days=1):
        current = (holder.streak_current or 0) + 1
    else:
        current = 1  # первый ответ по предмету или пропуск дней — стрик заново

    holder.streak_current = current
    holder.last_solved_date = today

    best_updated = current > (holder.streak_best or 0)
    if best_updated:
        holder.streak_best = current
        _remember_record_broken(key, today)
    return current, holder.streak_best, best_updated