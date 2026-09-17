"""Journal d'audit des actions d'administration (P10).

Revision ID: 0004_audit_log
Revises: 0003_trombinoscope
Create Date: 2026-09-17

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '0004_audit_log'
down_revision: Union[str, Sequence[str], None] = '0003_trombinoscope'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'audit_logs',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=True),
        sa.Column('actor', sa.String(length=160), nullable=False),
        sa.Column('campus', sa.String(length=10), nullable=False),
        sa.Column('action', sa.String(length=60), nullable=False),
        sa.Column('target', sa.String(length=160), nullable=False),
        sa.Column('details', sa.Text(), nullable=False),
        sa.Column('ip', sa.String(length=64), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('audit_logs', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_audit_logs_action'), ['action'], unique=False)
        batch_op.create_index(batch_op.f('ix_audit_logs_created_at'), ['created_at'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('audit_logs', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_audit_logs_created_at'))
        batch_op.drop_index(batch_op.f('ix_audit_logs_action'))
    op.drop_table('audit_logs')
