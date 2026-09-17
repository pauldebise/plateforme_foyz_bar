"""Retrait du MFA TOTP : suppression des colonnes dédiées.

Revision ID: 0006_drop_mfa_totp
Revises: 0005_mfa_totp
Create Date: 2026-09-17

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '0006_drop_mfa_totp'
down_revision: Union[str, Sequence[str], None] = '0005_mfa_totp'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_column('totp_last_counter')
        batch_op.drop_column('totp_recovery')
        batch_op.drop_column('totp_enabled')
        batch_op.drop_column('totp_secret')


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.add_column(sa.Column('totp_secret', sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column(
            'totp_enabled', sa.Boolean(), nullable=False, server_default=sa.false()
        ))
        batch_op.add_column(sa.Column('totp_recovery', sa.Text(), nullable=True))
        batch_op.add_column(sa.Column('totp_last_counter', sa.Integer(), nullable=True))
