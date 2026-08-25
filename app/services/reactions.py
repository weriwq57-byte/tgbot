"""Реакции на ответы ученика (ТЗ раздел 9 — пулы зафиксированы, не менять).

Реакция не должна повторяться два раза подряд: последняя показанная
хранится в users.last_reaction_id; функции исключают её из выбора.
Возвращают (id, текст): id — порядковый номер (1..4) в пуле.
"""
import random

POSITIVE_REACTIONS: list[str] = [
    "Точно в цель! 🎯",
    "Давай дальше! ✅",
    "Всё верно, ты молодец! 🔥",
    "Отлично, продолжай в том же духе! 💯",
]

MOTIVATIONAL_REACTIONS: list[str] = [
    "Не сходится, глянь ещё раз 👀",
    "Вернись к этой теме попозже ⏳",
    "Не получилось, можешь попробовать снова чуть позже 🔁",
    "Не в этот раз, попробуй другую задачу 🔄",
]


def _pick(pool: list[str], last_id: int | None) -> tuple[int, str]:
    """Случайная реакция из пула, не совпадающая с last_id.

    Если у пользователя ещё нет последней реакции (None) — любой вариант.
    Пулы по 10 фраз, поэтому исключение одной не оставляет пустой выбор.
    """
    candidates = [
        (i + 1, text)
        for i, text in enumerate(pool)
        if last_id is None or i + 1 != last_id
    ]
    return random.choice(candidates)


def positive_reaction(last_id: int | None) -> tuple[int, str]:
    """Позитивная реакция на правильный ответ."""
    return _pick(POSITIVE_REACTIONS, last_id)


def motivational_reaction(last_id: int | None) -> tuple[int, str]:
    """Мотивационная реакция на неправильный ответ."""
    return _pick(MOTIVATIONAL_REACTIONS, last_id)