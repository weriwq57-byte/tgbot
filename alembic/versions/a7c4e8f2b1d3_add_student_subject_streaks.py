"""add per-subject streak fields to student_subjects

Revision ID: a7c4e8f2b1d3
Revises: 9f3a2c1d5e7b
Create Date: 2026-08-13

Стрики ПО ПРЕДМЕТУ (владелец, 13.08.2026): каждый предмет ученика
ведёт свой стрик (streak_current / streak_best / last_solved_date).
Общие стрик-поля на users остаются (история), но больше не обновляются.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a7c4e8f2b1d3'
down_revision: Union[str, Sequence[str], None] = '9f3a2c1d5e7b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'student_subjects',
        sa.Column('streak_current', sa.Integer(), nullable=False, server_default='0'),
    )
    op.add_column(
        'student_subjects',
        sa.Column('streak_best', sa.Integer(), nullable=False, server_default='0'),
    )
    op.add_column(
        'student_subjects',
        sa.Column('last_solved_date', sa.Date(), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('student_subjects', 'last_solved_date')
    op.drop_column('student_subjects', 'streak_best')
    op.drop_column('student_subjects', 'streak_current')