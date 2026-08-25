# Бот Telegram-школы LevelUp (подготовка к ЦТ/ЦЭ, Беларусь)

Бот на aiogram 3 + SQLAlchemy 2 (asyncio) + PostgreSQL.
Роли: владелец (owner) → менеджер (manager) → преподаватель (teacher) → ученик (student) → гость (guest).
Время — везде Минск (UTC+3, см. `app/utils/dates.py`).

## Возможности (MVP)

- **Владелец**: преподаватели и менеджеры (добавление по @username / tg_id / из гостей,
  смена роли «в один шаг», удаление), предметы (создание, скрытие/показ), кик
  пользователей, ученики.
- **Менеджер**: ученики (создание по инвайт-коду, карточка с прогрессом и стриком,
  продление доступа, де/активация, новый код приглашения), закрытие предмета для
  ученика, список истекающих доступов (7/3/1 день, истёкшие).
- **Преподаватель**: предметы (назначенные владельцем), темы (создание, открыть/закрыть,
  переименовать, удалить по точному названию), задания (визард: вопрос текстом или фото,
  2–4 варианта, правильный ответ, объяснение текстом и/или фото, превью), карточка
  задания (скрыть/показать/удалить).
- **Ученик**: привязка по инвайт-коду (deep link `/start КОД` или текстом), меню со
  стриком, «Мои предметы» (последовательно: осталось N / все решены), решение заданий
  с мгновенной проверкой, мотивационными реакциями (2 пула по 10) и объяснением,
  итог темы + новый рекорд стрика, повтор темы (прогресс сбрасывается, попытки
  остаются — seq защита от устаревших кнопок).

Тексты реакций, блокировок и итогов темы — дословно из ТЗ (разделы 6, 7, 9, 10),
не менять без согласования.

## Команды и меню по ролям

| Роль | Главное меню / команды |
|---|---|
| owner | `/start`, `/menu`, «📋 Мои команды»: преподаватели, менеджеры, предметы, ученики, кикнуть |
| manager | «📋 Мои команды»: ученики `/students`, добавить ученика `/add_student`, истекающие `/expiring` |
| teacher | «📋 Мои команды»: мои предметы `/my_subjects`, добавить тему `/add_theme`, задания `/tasks` |
| student | меню со стриком, «📚 Мои предметы» |
| guest | «🔑 Ввести код приглашения» (или `/start КОД`) |

## Быстрый старт (dev)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env        # заполни BOT_TOKEN и ADMIN_IDS
alembic upgrade head        # создаёт таблицы (PostgreSQL)
python -m app.main          # long-polling
```

`python -m app.main` работает из корня проекта и из любого другого каталога:
путь к `.env` — абсолютный, корень проекта добавляется в `sys.path` (см. `app/main.py`).
Без `BOT_TOKEN` при старте — «BOT_TOKEN не задан. Заполни .env». При недоступной базе —
«Нет соединения с базой данных: …» и выход (ошибки №1 и №4 из ТЗ, раздел 11).

## Окружение (.env)

| Переменная | Описание |
|---|---|
| `BOT_TOKEN` | токен от @BotFather (обязательно) |
| `DATABASE_URL` | `postgresql+asyncpg://user:pass@host:port/dbname` |
| `ADMIN_IDS` | Telegram ID владельцев через запятую (числа) |
| `LOG_LEVEL` | `DEBUG` / `INFO` / `WARNING` / `ERROR` (по умолчанию INFO) |

Свой Telegram ID: напишите @userinfobot. Храните `.env` в секрете, в git не коммитить.

## Миграции (alembic)

```bash
alembic revision --autogenerate -m "описание"   # после изменений app/models.py
alembic upgrade head                            # применить
alembic downgrade -1                            # откатить на один шаг
```

Строка подключения берётся из `.env` (тот же `DATABASE_URL`, что и у бота).

## Тесты

```bash
pytest      # SQLite in-memory, PostgreSQL/боевой токен не нужны
```

~360 тестов: роли и меню, владелец/менеджер/преподаватель (визарды целиком), ученик
(полный флоу привязка → решение → стрики → итог → повтор), доступ и edge-кейсы
(ТЗ раздел 11: дубли кнопок, FSM-мусор, устаревшие кнопки, деактивация, «текущее
досматривает»), реестр колбэков («нет мёртвых кнопок»), маршрутизация через реальный
Dispatcher, понятные ошибки старта.

## Структура проекта

```
app/
  main.py          # точка входа (python -m app.main): настройки → БД → бот → поллинг
  config.py        # настройки из .env (ленивые, путь абсолютный)
  bot.py           # Bot (HTML по умолчанию) + Dispatcher
  database.py      # engine (ленивый), SessionFactory, Base, check_db_connection()
  models.py        # все таблицы БД (включая закладки v2: subthemes, reminder_log, mode)
  states.py        # FSM-состояния визардов (по одному StatesGroup на визард)
  handlers/        # роутеры: student → commands → owner → manager → teacher
  middlewares/     # UserContextMiddleware (роль из БД, блокировка неактивных)
  services/        # бизнес-логика: access, streaks, reactions, invite, students, teacher, student, owner
  keyboards/       # инлайн-клавиатуры + РЕЕСТР колбэков (EXACT/PREFIX/FUTURE)
  utils/           # даты (Минск), esc/format, logging, roles (require_role), errors
alembic/           # миграции (одна первичная — вся схема)
tests/             # pytest (SQLite in-memory)
```

## Логирование и ошибки

- `logs/levelup.log` — ротация 5 МБ × 5 файлов + консоль, уровень из `LOG_LEVEL`.
- Логируются команды (текст, роль, tg_id), создание/деактивация пользователей,
  продления, ошибки.
- Глобальный обработчик ошибок: лог с трейсбеком → пользователю «Что-то пошло не
  так, попробуй ещё раз» → при 10+ ошибках за час уведомление владельцам
  («⚠️ У бота LevelUp N ошибок за последний час. Проверь логи.»).
- HTML-разметка включена; **весь пользовательский текст** идёт через `esc()`.

## Деплой на VPS (systemd)

`/etc/systemd/system/levelup-bot.service`:

```ini
[Unit]
Description=LevelUp Telegram Bot
After=network.target postgresql.service

[Service]
User=levelup
WorkingDirectory=/opt/levelup-bot
EnvironmentFile=/opt/levelup-bot/.env
ExecStart=/opt/levelup-bot/.venv/bin/python -m app.main
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now levelup-bot
journalctl -u levelup-bot -f        # логи
```

### Docker Compose (альтернатива)

`docker-compose.yml`:

```yaml
services:
  postgres:
    image: postgres:16-alpine
    environment:
      POSTGRES_USER: levelup
      POSTGRES_PASSWORD: levelup
      POSTGRES_DB: levelup
    volumes: [pgdata:/var/lib/postgresql/data]
    restart: unless-stopped
  bot:
    build: .
    env_file: .env          # DATABASE_URL указывает на сервис postgres
    depends_on: [postgres]
    restart: unless-stopped
volumes:
  pgdata:
```

При первом запуске выполнить миграции: `alembic upgrade head`.

## Бэкапы (pg_dump)

```bash
pg_dump -h localhost -U levelup -d levelup -Fc -f /backups/levelup_$(date +%F).dump
# восстановление:
pg_restore -h localhost -U levelup -d levelup --clean /backups/levelup_YYYY-MM-DD.dump
```

Рекомендуется cron ежедневно и хранение N дней/недель (ротация).

## Закладки v2/v3 (вне MVP, не реализованы — из ТЗ раздел 12)

Схема БД совместима (таблицы `subthemes`, `reminder_log`, поле `themes.mode` уже есть):

- **v2**: подтемы в интерфейсе; редактирование заданий; режим «открыть все» (рандом);
  cron-напоминания менеджеру (APScheduler, время Минск, дублей нет через `reminder_log`);
  статистика препода/ученика; отчёты родителям; сводка `/all_stats` для владельца;
  бесплатные тесты для гостей; оплата онлайн; экспорт CSV.
- **v3**: веб-админка; рефералки «приведи друга».

## Ключевые технические решения

- Роль пользователя — **только из БД** (`data["db_user"]` в мидлваре), не из callback.
- Callback-данные: `{entity}:{id}:{action}:{seq}` (seq фиксирован `0`, кроме ответов
  ученика — там seq = число попыток, защита от устаревших карточек).
- Реестр колбэков в `app/keyboards/inline.py`: любой callback клавиатуры обязан иметь
  обработчик (проверяется тестом).
- Хендлеры-«перехватчики» широких фильтров для чужих ролей бросают `SkipHandler`
  (иначе диспетчер останавливается на первом совпавшем хендлере даже при return None).
- Доступ проверяется **только при выдаче** (`can_access`), «текущее досматривает»:
  ответ по выданному заданию принимается всегда.