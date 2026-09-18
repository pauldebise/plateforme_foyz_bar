"""Retrait du trombinoscope : suppression des colonnes dédiées.

Revision ID: 0007_drop_trombinoscope
Revises: 0006_drop_mfa_totp
Create Date: 2026-09-18

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '0007_drop_trombinoscope'
down_revision: Union[str, Sequence[str], None] = '0006_drop_mfa_totp'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_column('photo')
        batch_op.drop_column('trombinoscope_role')
        batch_op.drop_column('trombinoscope_visible')


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.add_column(sa.Column(
            'trombinoscope_visible', sa.Boolean(), nullable=False, server_default=sa.true()
        ))
        batch_op.add_column(sa.Column('trombinoscope_role', sa.String(length=80), nullable=True))
        batch_op.add_column(sa.Column('photo', sa.String(length=255), nullable=True))
