"""Инвайт-коды (ТЗ, раздел 7): алфавит, уникальность, кэш username бота.

Ошибка №10: генерация с проверкой уникальности — в той же транзакции,
что и создание ученика (проверяется в test_students.py через
create_student_record). Здесь — фокус на самой генерации.
"""
import pytest
from sqlalchemy import select

from app.models import Student
from app.services import invite as invite_svc
from app.services import students as students_svc


# --------------------------------------------------------------------------
# generate_code / generate_unique_code
# --------------------------------------------------------------------------
def test_generate_code_format():
    code = invite_svc.generate_code()
    assert len(code) == 6
    assert all(c in invite_svc.CODE_ALPHABET for c in code)
    # Алфавит без похожих: нет 0, O, 1, I
    for bad in "0O1I":
        assert bad not in invite_svc.CODE_ALPHABET


async def test_generate_unique_code_different(session_factory):
    """Два подряд — разные коды."""
    async with session_factory() as session:
        a = await invite_svc.generate_unique_code(session)
        b = await invite_svc.generate_unique_code(session)
    assert a != b
    assert len(a) == len(b) == 6


async def _mk_user(session_factory, username="boss") -> int:
    """Создаёт пользователя (FK для students.user_id) и возвращает его id."""
    from app.models import User

    async with session_factory() as session:
        user = User(tg_username=username, role="guest", is_active=True)
        session.add(user)
        await session.commit()
        return user.id


async def test_generate_unique_code_skips_existing(session_factory, monkeypatch):
    """Занятый код пропускается: генерация продолжает, пока не найдёт свободный."""
    uid = await _mk_user(session_factory)
    counter = {"n": 0}

    def fake_generate():
        counter["n"] += 1
        return "ABC234" if counter["n"] == 1 else "XYZ789"

    async with session_factory() as session:
        session.add(Student(user_id=uid, invite_code="ABC234", invite_status="pending"))
        await session.commit()
        monkeypatch.setattr(invite_svc, "generate_code", fake_generate)
        code = await invite_svc.generate_unique_code(session)
    assert code == "XYZ789"
    assert counter["n"] == 2


async def test_generate_unique_code_collision_retry(session_factory, monkeypatch):
    """Первые 3 «случайных» кода уже заняты — 4-я попытка свободна.

    Моделирует гонку: два параллельных создания не должны дать один код.
    """
    taken = ["ABC111", "ABC222", "ABC333"]

    counter = {"n": 0}

    def fake_generate():
        counter["n"] += 1
        return taken[counter["n"] - 1] if counter["n"] <= len(taken) else "FREE99"

    async with session_factory() as session:
        for i, code in enumerate(taken):
            # students.user_id уникален — по отдельному пользователю на код
            uid = await _mk_user(session_factory, username=f"boss_{i}")
            session.add(Student(user_id=uid, invite_code=code, invite_status="pending"))
        await session.commit()
        monkeypatch.setattr(invite_svc, "generate_code", fake_generate)
        code = await invite_svc.generate_unique_code(session)
    assert code == "FREE99"
    assert counter["n"] == len(taken) + 1


async def test_generate_unique_code_exhausts(session_factory, monkeypatch):
    """Все 100 попыток заняты → RuntimeError (ТЗ, раздел 7)."""
    uid = await _mk_user(session_factory)
    async with session_factory() as session:
        monkeypatch.setattr(invite_svc, "generate_code", lambda: "TAKEN!")
        session.add(Student(user_id=uid, invite_code="TAKEN!", invite_status="pending"))
        await session.commit()
        with pytest.raises(RuntimeError):
            await invite_svc.generate_unique_code(session)


# --------------------------------------------------------------------------
# Username бота (ошибка №11: кэш только успешного результата)
# --------------------------------------------------------------------------
class _FakeBot:
    def __init__(self, username="my_bot", fail=False):
        self._username = username
        self._fail = fail
        self.calls = 0

    async def get_me(self):
        self.calls += 1
        if self._fail:
            raise RuntimeError("get_me failed")
        return type("Me", (), {"username": self._username})()


async def test_get_bot_username_success(monkeypatch):
    monkeypatch.setattr(invite_svc, "_bot_username_cache", None)
    bot = _FakeBot(username="levelup_test")
    assert await invite_svc.get_bot_username(bot) == "levelup_test"
    # второй вызов — из кэша, без повторного get_me
    assert await invite_svc.get_bot_username(bot) == "levelup_test"
    assert bot.calls == 1


async def test_get_bot_username_fallback_not_cached(monkeypatch):
    """Сбой get_me → фолбэк, НО кэш не заполняется (ошибка №11)."""
    monkeypatch.setattr(invite_svc, "_bot_username_cache", None)
    bot_with_error = _FakeBot(fail=True)
    assert await invite_svc.get_bot_username(bot_with_error) == invite_svc.FALLBACK_BOT_USERNAME
    # следующая попытка — снова реальный get_me
    assert await invite_svc.get_bot_username(bot_with_error) == invite_svc.FALLBACK_BOT_USERNAME
    assert bot_with_error.calls == 2


def test_invite_link():
    assert invite_svc.invite_link("my_bot", "ABC123") == "https://t.me/my_bot?start=ABC123"