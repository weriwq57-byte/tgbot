"""Заход 6 + доработка 13.08: стрики ПО ПРЕДМЕТУ (StudentSubject).

Идемпотентность в рамках дня: первый ответ дня по предмету решает
(вчера → +1, сегодня → без изменений, пропуск → 1); рекорд обновляется
сразу. Каждый предмет — свой стрик (сброс предмета не трогает другие).
"""
from datetime import date, timedelta

from app.models import StudentSubject
from app.services import streaks
from app.utils.dates import today_minsk

TODAY = today_minsk()
KEY = (1, 2)


def _mk_holder(streak_current=0, streak_best=0, last_solved=None) -> StudentSubject:
    return StudentSubject(
        student_id=1,
        subject_id=2,
        streak_current=streak_current,
        streak_best=streak_best,
        last_solved_date=last_solved,
    )


async def _cleanup():
    streaks.clear_record_cache()


async def test_first_solve_sets_one(session, monkeypatch):
    await _cleanup()
    holder = _mk_holder()
    current, best, updated = streaks.register_solved(session, holder, KEY)
    assert current == 1
    assert best == 1
    assert updated is True
    assert holder.last_solved_date == TODAY


async def test_same_day_no_change(session, monkeypatch):
    await _cleanup()
    holder = _mk_holder(streak_current=5, streak_best=7, last_solved=TODAY)
    current, best, updated = streaks.register_solved(session, holder, KEY)
    assert current == 5
    assert best == 7
    assert updated is False
    assert holder.last_solved_date == TODAY


async def test_yesterday_increments(session, monkeypatch):
    await _cleanup()
    holder = _mk_holder(
        streak_current=5, streak_best=7, last_solved=TODAY - timedelta(days=1)
    )
    current, best, updated = streaks.register_solved(session, holder, KEY)
    assert current == 6
    assert best == 7
    assert updated is False


async def test_skip_days_resets_to_one(session, monkeypatch):
    await _cleanup()
    holder = _mk_holder(
        streak_current=5, streak_best=7, last_solved=TODAY - timedelta(days=3)
    )
    current, best, updated = streaks.register_solved(session, holder, KEY)
    assert current == 1
    assert best == 7
    assert updated is False


async def test_record_updated_right_away(session, monkeypatch):
    await _cleanup()
    holder = _mk_holder(
        streak_current=7, streak_best=7, last_solved=TODAY - timedelta(days=1)
    )
    current, best, updated = streaks.register_solved(session, holder, KEY)
    assert current == 8
    assert best == 8
    assert updated is True


async def test_no_last_solve_resets_to_one(session, monkeypatch):
    await _cleanup()
    holder = _mk_holder(streak_current=9, streak_best=9)
    current, best, updated = streaks.register_solved(session, holder, KEY)
    assert current == 1
    assert best == 9
    assert updated is False


async def test_subjects_have_own_streaks(session, monkeypatch):
    """Стрики по разным предметам не влияют друг на друга (владелец 13.08).

    Оба предмета решались вчера (стрики 4 и 2): сегодняшний ответ по
    каждому даёт +1 своему, чужой не трогает.
    """
    await _cleanup()
    math = _mk_holder(
        streak_current=4, streak_best=7, last_solved=TODAY - timedelta(days=1)
    )
    rus = _mk_holder(
        streak_current=2, streak_best=5, last_solved=TODAY - timedelta(days=1)
    )
    current, best, updated = streaks.register_solved(session, math, (1, 2))
    assert (current, best, updated) == (5, 7, False)
    # русский не тронут: стрик тот же, рекорд тот же, дата не менялась
    assert rus.streak_current == 2
    assert rus.streak_best == 5
    current, best, updated = streaks.register_solved(session, rus, (1, 3))
    assert (current, best, updated) == (3, 5, False)
    assert math.streak_current == 5
    assert math.streak_best == 7


async def test_reactions_pools_are_stable():
    """Пулы реакций зафиксированы в ТЗ (раздел 9) — не меняются."""
    from app.services import reactions

    assert len(reactions.POSITIVE_REACTIONS) == 10
    assert len(reactions.MOTIVATIONAL_REACTIONS) == 10
    assert reactions.POSITIVE_REACTIONS[0] == "Вот это точно! Так держать"
    assert reactions.MOTIVATIONAL_REACTIONS[0] == (
        "Бывает. Это не ошибка, это точка роста"
    )


async def test_reaction_never_repeats(session, monkeypatch):
    """Случайная реакция не повторяет последнюю (last_reaction_id)."""
    from app.services import reactions

    choices = []

    def fake_choice(seq):
        choices.append(seq)
        return seq[-1]  # гарантированно последняя позиция — не last_id

    monkeypatch.setattr(reactions.random, "choice", fake_choice)
    _, text = reactions.positive_reaction(last_id=10)
    # id 10 исключён → последний кандидат — позиция 9 списка (инд. 8)
    assert text == reactions.POSITIVE_REACTIONS[8]
    _, text = reactions.motivational_reaction(last_id=9)
    # id 9 (инд. 8) исключён → последний кандидат — позиция 10 (инд. 9)
    assert text == reactions.MOTIVATIONAL_REACTIONS[9]

    # без last_id — любой (кандидатов 10)
    _, text = reactions.positive_reaction(last_id=None)
    assert text in reactions.POSITIVE_REACTIONS