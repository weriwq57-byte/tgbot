"""База данных: engine, фабрика сессий, базовый класс моделей.

Все сессии в боте создаются через get_session_factory() (async).
Тесты подменяют SessionFactory/engine на свои (SQLite в памяти) —
см. tests/conftest.py.

Engine и SessionFactory создаются ЛЕНИВО, при первом использовании:
битая строка DATABASE_URL не роняет импорт модуля, а даёт понятную
ошибку в момент подключения (обрабатывается в app/main.py).
"""
import logging
from typing import Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.config import get_settings

logger = logging.getLogger(__name__)


class Base(DeclarativeBase):
    """Базовый класс всех ORM-моделей."""


engine: Optional[AsyncEngine] = None
SessionFactory: Optional[async_sessionmaker[AsyncSession]] = None


def _ensure_engine() -> AsyncEngine:
    """Создаёт engine (один раз) и фабрику сессий, возвращает engine."""
    global engine, SessionFactory
    if engine is None:
        # pool_pre_ping=True — переживает перезапуск PostgreSQL.
        engine = create_async_engine(
            get_settings().DATABASE_URL,
            pool_pre_ping=True,
            echo=False,
        )
        # expire_on_commit=False — объекты остаются валидными после commit
        # (нужно для данных, которые кладём в data мидлваря).
        SessionFactory = async_sessionmaker(
            engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )
    return engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """Фабрика сессий (создаёт engine при первом вызове)."""
    if SessionFactory is None:
        _ensure_engine()
    assert SessionFactory is not None
    return SessionFactory


async def check_db_connection() -> None:
    """Проверка подключения к БД (SELECT 1). Бросает исключение при недоступности."""
    async with _ensure_engine().connect() as conn:
        await conn.execute(text("SELECT 1"))
