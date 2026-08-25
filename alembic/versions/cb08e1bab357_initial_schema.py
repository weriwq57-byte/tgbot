"""initial schema

Revision ID: cb08e1bab357
Revises:
Create Date: 2026-08-08 16:16:21.606249

Все 11 таблиц из ТЗ раздел 4, включая поля-закладки под v2
(subthemes, themes.mode, reminder_log). Одна первичная миграция.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'cb08e1bab357'
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # users
    op.create_table(
        'users',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('tg_id', sa.BigInteger(), nullable=True),
        sa.Column('tg_username', sa.String(length=64), nullable=True),
        sa.Column('tg_full_name', sa.String(length=128), nullable=True),
        sa.Column('role', sa.String(length=16), nullable=False, server_default='guest'),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.Column('streak_current', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('streak_best', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('last_solved_date', sa.Date(), nullable=True),
        sa.Column('last_reaction_id', sa.Integer(), nullable=True),
        sa.CheckConstraint(
            "role IN ('owner','teacher','manager','student','guest')",
            name='ck_users_role',
        ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('tg_id'),
    )

    # subjects
    op.create_table(
        'subjects',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('name', sa.String(length=128), nullable=False),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.PrimaryKeyConstraint('id'),
    )

    # teacher_subjects
    op.create_table(
        'teacher_subjects',
        sa.Column('teacher_id', sa.Integer(), nullable=False),
        sa.Column('subject_id', sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(['teacher_id'], ['users.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['subject_id'], ['subjects.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('teacher_id', 'subject_id'),
    )

    # themes
    op.create_table(
        'themes',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('subject_id', sa.Integer(), nullable=False),
        sa.Column('title', sa.String(length=200), nullable=False),
        sa.Column('order', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('is_open', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('mode', sa.String(length=16), nullable=False, server_default='sequential'),
        sa.Column('created_by', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.CheckConstraint("mode IN ('sequential','random')", name='ck_themes_mode'),
        sa.ForeignKeyConstraint(['created_by'], ['users.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['subject_id'], ['subjects.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_themes_subject_id', 'themes', ['subject_id'])

    # subthemes — [v2-закладка]
    op.create_table(
        'subthemes',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('theme_id', sa.Integer(), nullable=False),
        sa.Column('title', sa.String(length=200), nullable=False),
        sa.Column('order', sa.Integer(), nullable=False, server_default='0'),
        sa.ForeignKeyConstraint(['theme_id'], ['themes.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_subthemes_theme_id', 'subthemes', ['theme_id'])

    # tasks
    op.create_table(
        'tasks',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('subtheme_id', sa.Integer(), nullable=True),
        sa.Column('theme_id', sa.Integer(), nullable=False),
        sa.Column('question_text', sa.Text(), nullable=True),
        sa.Column('question_photo_id', sa.String(length=256), nullable=True),
        sa.Column('options', sa.JSON(), nullable=False),
        sa.Column('feedback_text', sa.Text(), nullable=True),
        sa.Column('feedback_photo_id', sa.String(length=256), nullable=True),
        sa.Column('order', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('created_by', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.ForeignKeyConstraint(['created_by'], ['users.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['subtheme_id'], ['subthemes.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['theme_id'], ['themes.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_tasks_theme_id', 'tasks', ['theme_id'])
    op.create_index('ix_tasks_subtheme_id', 'tasks', ['subtheme_id'])

    # students
    op.create_table(
        'students',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('access_until', sa.Date(), nullable=True),
        sa.Column('invite_code', sa.String(length=8), nullable=True),
        sa.Column('invite_status', sa.String(length=16), nullable=False, server_default='pending'),
        sa.Column('invited_by', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.CheckConstraint(
            "invite_status IN ('pending','activated')",
            name='ck_students_invite_status',
        ),
        sa.ForeignKeyConstraint(['invited_by'], ['users.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('invite_code'),
        sa.UniqueConstraint('user_id'),
    )

    # student_subjects
    op.create_table(
        'student_subjects',
        sa.Column('student_id', sa.Integer(), nullable=False),
        sa.Column('subject_id', sa.Integer(), nullable=False),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('added_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.ForeignKeyConstraint(['student_id'], ['students.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['subject_id'], ['subjects.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('student_id', 'subject_id'),
    )

    # attempts
    op.create_table(
        'attempts',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('student_id', sa.Integer(), nullable=False),
        sa.Column('task_id', sa.Integer(), nullable=False),
        sa.Column('answer_index', sa.Integer(), nullable=True),
        sa.Column('is_correct', sa.Boolean(), nullable=False),
        sa.Column('answered_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.ForeignKeyConstraint(['student_id'], ['students.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['task_id'], ['tasks.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_attempts_student_id', 'attempts', ['student_id'])
    op.create_index('ix_attempts_answered_at', 'attempts', ['answered_at'])

    # task_progress
    op.create_table(
        'task_progress',
        sa.Column('student_id', sa.Integer(), nullable=False),
        sa.Column('task_id', sa.Integer(), nullable=False),
        sa.Column('status', sa.String(length=8), nullable=False, server_default='done'),
        sa.Column('solved_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.CheckConstraint("status IN ('done','wrong')", name='ck_task_progress_status'),
        sa.ForeignKeyConstraint(['student_id'], ['students.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['task_id'], ['tasks.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('student_id', 'task_id'),
    )

    # reminder_log — [v2-закладка]
    op.create_table(
        'reminder_log',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('student_id', sa.Integer(), nullable=False),
        sa.Column('kind', sa.String(length=24), nullable=False),
        sa.Column('reminded_on', sa.Date(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.ForeignKeyConstraint(['student_id'], ['students.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('student_id', 'kind', 'reminded_on', name='uq_reminder_log_student_kind_day'),
    )
    op.create_index('ix_reminder_log_student_id', 'reminder_log', ['student_id'])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_reminder_log_student_id', table_name='reminder_log')
    op.drop_table('reminder_log')
    op.drop_table('task_progress')
    op.drop_index('ix_attempts_answered_at', table_name='attempts')
    op.drop_index('ix_attempts_student_id', table_name='attempts')
    op.drop_table('attempts')
    op.drop_table('student_subjects')
    op.drop_table('students')
    op.drop_index('ix_tasks_subtheme_id', table_name='tasks')
    op.drop_index('ix_tasks_theme_id', table_name='tasks')
    op.drop_table('tasks')
    op.drop_index('ix_subthemes_theme_id', table_name='subthemes')
    op.drop_table('subthemes')
    op.drop_index('ix_themes_subject_id', table_name='themes')
    op.drop_table('themes')
    op.drop_table('teacher_subjects')
    op.drop_table('subjects')
    op.drop_table('users')
