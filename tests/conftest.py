"""Общие фикстуры тестов.

Тесты работают на SQLite в памяти (быстро, без внешнего сервера).
Каскады FK работают благодаря PRAGMA foreign_keys=ON.
"""
import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401 — регистрируем модели в Base.metadata
import app.database
from app.database import Base
from app.models import User
from app.services import streaks


@pytest.fixture
async def db_engine():
    """Async-движок на SQLite в памяти (один общий пул соединений)."""
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,   # in-memory БД живёт, пока жив движок
        connect_args={"check_same_thread": False},
    )

    # PRAGMA foreign_keys — настройка соединения, а не БД. Через event
    # listener на КАЖДОМ новом соединении (внутри транзакции engine.begin()
    # PRAGMA был бы no-op — это хрупко).
    @event.listens_for(engine.sync_engine, "connect")
    def _set_foreign_keys(dbapi_conn, _record):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.fixture
async def session_factory(db_engine):
    """Фабрика сессий для тестов."""
    maker = async_sessionmaker(db_engine, expire_on_commit=False)
    yield maker


@pytest.fixture(autouse=True)
async def _patch_session_factory(monkeypatch, session_factory):
    """Все хендлеры/сервисы ходят в тестовую БД (SQLite in-memory).

    Хендлеры делают `from app.database import SessionFactory`, поэтому
    патчим имя в каждом модуле, где оно импортировано. В следующих
    заходах список модулей пополняется.
    """
    monkeypatch.setattr(app.database, "SessionFactory", session_factory)


@pytest.fixture
async def session(session_factory):
    """Открытая сессия для теста."""
    async with session_factory() as s:
        yield s


@pytest.fixture(autouse=True)
async def _clear_streak_record_cache():
    """Кэш «побил рекорд сегодня» — пер-процессный, чистим до каждого теста."""
    streaks.clear_record_cache()
    yield


@pytest.fixture
async def user_factory(session_factory):
    """Фабрика пользователей: создаёт User в БД и возвращает объект."""
    async def _make(**overrides) -> User:
        data = dict(
            tg_id=None,
            tg_username="test_user",
            tg_full_name="Тестовый Пользователь",
            role="guest",
            is_active=True,
        )
        data.update(overrides)
        async with session_factory() as s:
            user = User(**data)
            s.add(user)
            await s.commit()
            return user
    return _make
