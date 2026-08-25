"""Доступ ученика к теме — единая точка проверки (ТЗ раздел 10).

Правило «текущее досматривает»: can_access вызывается ТОЛЬКО при выдаче
задания (вход в тему, «Ещё задание», «Повторить») — НЕ при ответе.
Если доступ истёк или тему закрыли после выдачи — ответ принимается.

Причины (статусы; маппятся в тексты в app/handlers/student.py):
- student_inactive — ученик не найден или деактивирован;
- subject_inactive — предмет скрыт / не назначен / выключен менеджером;
- theme_not_found — темы нет;
- theme_closed — тема закрыта;
- access_expired — access_until < сегодня (по Минску; день окончания
  доступен весь день).
"""
from app.models import Student, StudentSubject, Subject, Theme, User
from app.utils.dates import today_minsk

# Статусы причин (константы для использования в хендлерах/тестах)
REASON_STUDENT_INACTIVE = "student_inactive"
REASON_SUBJECT_INACTIVE = "subject_inactive"
REASON_THEME_NOT_FOUND = "theme_not_found"
REASON_THEME_CLOSED = "theme_closed"
REASON_ACCESS_EXPIRED = "access_expired"


async def can_access(session, student_id: int, theme_id: int) -> tuple[bool, str | None]:
    """Может ли ученик получить задание темы.

    Возвращает (True, None) или (False, статус-причина). Порядок проверок
    по ТЗ раздел 10: ученик → предмет → тема → дата доступа.
    """
    student = await session.get(Student, student_id)
    if student is None:
        return False, REASON_STUDENT_INACTIVE
    user = await session.get(User, student.user_id)
    if user is None or not user.is_active:
        return False, REASON_STUDENT_INACTIVE

    theme = await session.get(Theme, theme_id)
    if theme is None:
        return False, REASON_THEME_NOT_FOUND

    subject = await session.get(Subject, theme.subject_id)
    if subject is None or not subject.is_active:
        return False, REASON_SUBJECT_INACTIVE
    link = await session.get(StudentSubject, (student_id, subject.id))
    if link is None or not link.is_active:
        return False, REASON_SUBJECT_INACTIVE

    if not theme.is_open:
        return False, REASON_THEME_CLOSED

    if student.access_until is None or student.access_until < today_minsk():
        return False, REASON_ACCESS_EXPIRED

    return True, None