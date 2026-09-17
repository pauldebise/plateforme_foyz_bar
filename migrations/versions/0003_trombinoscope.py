"""Trombinoscopes : photo, role et visibilite des membres d'equipe (T-8.3).

Revision ID: 0003_trombinoscope
Revises: 0002_article_index
Create Date: 2026-09-17

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '0003_trombinoscope'
down_revision: Union[str, Sequence[str], None] = '0002_article_index'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.add_column(sa.Column(
            'trombinoscope_visible', sa.Boolean(), nullable=False, server_default=sa.true()
        ))
        batch_op.add_column(sa.Column('trombinoscope_role', sa.String(length=80), nullable=True))
        batch_op.add_column(sa.Column('photo', sa.String(length=255), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_column('photo')
        batch_op.drop_column('trombinoscope_role')
        batch_op.drop_column('trombinoscope_visible')
