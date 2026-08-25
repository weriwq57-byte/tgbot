"""Чтение настроек из .env (и переменных окружения).

Два важных решения (найденные баги):
- путь к .env — АБСОЛЮТНЫЙ (ТЗ раздел 11, ошибка №1);
- settings создаются ЛЕНИВО, при первом обращении, а не при импорте
  модуля: кривое значение в .env (например ADMIN_IDS=abc) даёт понятную
  ошибку на старте в app/main.py, а не сырой pydantic-трейсбек при
  `import app.config`.
"""
from pathlib import Path
from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# Корень проекта (app/config.py → корень)
BASE_DIR = Path(__file__).resolve().parent.parent
ENV_FILE = BASE_DIR / ".env"

# Ошибки при старте (возвращаются через RuntimeError из get_settings/validate)
MSG_NO_TOKEN = "BOT_TOKEN не задан. Заполни .env"
MSG_BAD_ADMIN_IDS = (
    "ADMIN_IDS в .env — должны быть Telegram ID через запятую (только числа)"
)
MSG_BAD_DATABASE_URL = (  # noqa: S105 — не секрет, текст ошибки
    "DATABASE_URL в .env должна начинаться с postgresql+asyncpg://"
)


class Settings(BaseSettings):
    """Настройки бота. Секреты — только из .env / переменных окружения."""

    model_config = SettingsConfigDict(
        env_file=ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Токен бота от @BotFather
    BOT_TOKEN: str = ""

    # Строка подключения к PostgreSQL (asyncpg)
    DATABASE_URL: str = (
        "postgresql+asyncpg://levelup:levelup@localhost:5432/levelup"
    )

    # Telegram ID владельцев (через запятую в .env)
    # Annotated[..., NoDecode]: не пытаться разбирать как JSON —
    # пустое значение в .env не упадёт (парсится валидатором ниже)
    ADMIN_IDS: Annotated[list[int], NoDecode] = Field(default_factory=list)

    # Уровень логирования
    LOG_LEVEL: str = "INFO"

    @field_validator("ADMIN_IDS", mode="before")
    @classmethod
    def _parse_admin_ids(cls, v):
        """Разбор ADMIN_IDS из строки «1,2,3».

        Не числовые значения — явная ошибка конфигурации (не молча None),
        но проверяется лениво (см. MSG_BAD_ADMIN_IDS) — при старте, не при импорте.
        """
        if v is None or v == "":
            return []
        if isinstance(v, str):
            parts = [x.strip() for x in v.split(",") if x.strip()]
            if not parts:
                return []
            for part in parts:
                if not part.isdigit():
                    raise ValueError(f"ADMIN_IDS: «{part}» — не число")
            return [int(x) for x in parts]
        if isinstance(v, (list, tuple)):
            result = []
            for item in v:
                if not isinstance(item, int):
                    raise ValueError(f"ADMIN_IDS: {item!r} — не число")
                result.append(item)
            return result
        raise ValueError(f"ADMIN_IDS: {v!r} — не распознано")

    def validate(self) -> None:
        """Проверка настроек перед стартом. Бросает RuntimeError с понятной ошибкой.

        - убираем случайные пробелы вокруг токена;
        - DATABASE_URL проверяем по формату (иначе криптовые ошибки asyncpg);
        - ADMIN_IDS — «пусто» допустимо (алерты тогда не уходят — см. main.py).
        """
        self.BOT_TOKEN = self.BOT_TOKEN.strip()
        if not self.BOT_TOKEN:
            raise RuntimeError(MSG_NO_TOKEN)

        self.DATABASE_URL = self.DATABASE_URL.strip()
        if not self.DATABASE_URL.startswith("postgresql+asyncpg://"):
            raise RuntimeError(MSG_BAD_DATABASE_URL)


_settings: Settings | None = None


def get_settings() -> Settings:
    """Кешированный доступ к настройкам. Создаются один раз, при первом обращении.

    Ошибку конфигурации (ValidationError из .env) не заворачиваем здесь —
    её ловит main.py и выдаёт понятное сообщение.
    """
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def __getattr__(name: str):
    """Ленивый атрибут модуля (PEP 562): `from app.config import settings`.

    Позволяет не вычислять Settings при импорте — обращение `settings.…`
    из любого модуля выполнится только в момент реального использования.
    """
    if name == "settings":
        return get_settings()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")