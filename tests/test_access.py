"""Заход 6: единая точка проверки доступа can_access (ТЗ раздел 10).

Правило «текущее досматривает» проверено в test_student_flow.py —
здесь только матрица причин отказа.
"""
from datetime import timedelta

from app.models import StudentSubject, Subject, Theme
from app.services import access as access_svc
from app.utils.dates import today_minsk

from tests.test_students import _mk_student, _mk_subject


async def _mk_theme(session_factory, subject_id: int, is_open=True) -> Theme:
    async with session_factory() as session:
        theme = Theme(subject_id=subject_id, title="Тема", is_open=is_open)
        session.add(theme)
        await session.commit()
        return theme


async def test_access_ok(session_factory):
    subj = await _mk_subject(session_factory)
    user, student = await _mk_student(session_factory, subject_id=subj.id, link=True)
    theme = await _mk_theme(session_factory, subj.id)
    async with session_factory() as session:
        ok, reason = await access_svc.can_access(session, student.id, theme.id)
    assert ok is True
    assert reason is None


async def test_access_student_not_found(session_factory):
    subj = await _mk_subject(session_factory)
    user, student = await _mk_student(session_factory, subject_id=subj.id, link=True)
    theme = await _mk_theme(session_factory, subj.id)
    async with session_factory() as session:
        ok, reason = await access_svc.can_access(session, student.id + 999, theme.id)
    assert ok is False
    assert reason == access_svc.REASON_STUDENT_INACTIVE


async def test_access_user_deactivated(session_factory):
    subj = await _mk_subject(session_factory)
    user, student = await _mk_student(
        session_factory, subject_id=subj.id, link=True, active=False
    )
    theme = await _mk_theme(session_factory, subj.id)
    async with session_factory() as session:
        ok, reason = await access_svc.can_access(session, student.id, theme.id)
    assert ok is False
    assert reason == access_svc.REASON_STUDENT_INACTIVE


async def test_access_theme_not_found(session_factory):
    subj = await _mk_subject(session_factory)
    user, student = await _mk_student(session_factory, subject_id=subj.id, link=True)
    async with session_factory() as session:
        ok, reason = await access_svc.can_access(session, student.id, 999999)
    assert ok is False
    assert reason == access_svc.REASON_THEME_NOT_FOUND


async def test_access_subject_hidden(session_factory):
    subj = await _mk_subject(session_factory)
    user, student = await _mk_student(
        session_factory, subject_id=subj.id, link=True, until=None
    )
    theme = await _mk_theme(session_factory, subj.id)
    async with session_factory() as session:
        subject = await session.get(Subject, subj.id)
        subject.is_active = False
        await session.commit()
        ok, reason = await access_svc.can_access(session, student.id, theme.id)
    assert ok is False
    assert reason == access_svc.REASON_SUBJECT_INACTIVE


async def test_access_subject_link_disabled(session_factory):
    subj = await _mk_subject(session_factory)
    user, student = await _mk_student(session_factory, subject_id=subj.id, link=True)
    theme = await _mk_theme(session_factory, subj.id)
    async with session_factory() as session:
        link = await session.get(StudentSubject, (student.id, subj.id))
        link.is_active = False
        await session.commit()
        ok, reason = await access_svc.can_access(session, student.id, theme.id)
    assert ok is False
    assert reason == access_svc.REASON_SUBJECT_INACTIVE


async def test_access_subject_not_assigned(session_factory):
    subj = await _mk_subject(session_factory)
    subj2 = await _mk_subject(session_factory, name="Русский")
    user, student = await _mk_student(session_factory, subject_id=subj.id, link=True)
    theme = await _mk_theme(session_factory, subj2.id)
    async with session_factory() as session:
        ok, reason = await access_svc.can_access(session, student.id, theme.id)
    assert ok is False
    assert reason == access_svc.REASON_SUBJECT_INACTIVE


async def test_access_theme_closed(session_factory):
    subj = await _mk_subject(session_factory)
    user, student = await _mk_student(session_factory, subject_id=subj.id, link=True)
    theme = await _mk_theme(session_factory, subj.id, is_open=False)
    async with session_factory() as session:
        ok, reason = await access_svc.can_access(session, student.id, theme.id)
    assert ok is False
    assert reason == access_svc.REASON_THEME_CLOSED


async def test_access_expired_yesterday(session_factory):
    subj = await _mk_subject(session_factory)
    user, student = await _mk_student(
        session_factory,
        subject_id=subj.id,
        link=True,
        until=today_minsk() - timedelta(days=1),
    )
    theme = await _mk_theme(session_factory, subj.id)
    async with session_factory() as session:
        ok, reason = await access_svc.can_access(session, student.id, theme.id)
    assert ok is False
    assert reason == access_svc.REASON_ACCESS_EXPIRED


async def test_access_today_is_ok(session_factory):
    """День окончания доступа доступен весь день (ТЗ раздел 10)."""
    subj = await _mk_subject(session_factory)
    user, student = await _mk_student(
        session_factory, subject_id=subj.id, link=True, until=today_minsk()
    )
    theme = await _mk_theme(session_factory, subj.id)
    async with session_factory() as session:
        ok, _reason = await access_svc.can_access(session, student.id, theme.id)
    assert ok is True