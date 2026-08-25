"""add users.role2 for combined staff roles

Revision ID: 9f3a2c1d5e7b
Revises: cb08e1bab357
Create Date: 2026-08-10

Совмещение ролей teacher ↔ manager (запрос владельца): «Добавить
преподавателя» менеджеру не затирает роль, обе живут до снятия
«Убрать преподавателя/менеджера». role2 хранит вторую роль.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '9f3a2c1d5e7b'
down_revision: Union[str, Sequence[str], None] = 'cb08e1bab357'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('users', sa.Column('role2', sa.String(length=16), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('users', 'role2')