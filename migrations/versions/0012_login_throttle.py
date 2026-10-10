"""Limiteur de connexion partagé : table login_throttle.

Compteur d'échecs par identifiant (empreinte) et par adresse IP (/64 pour
IPv6), partagé entre les workers Gunicorn.

Revision ID: 0012_login_throttle
Revises: 0011_blacklist_origine
Create Date: 2026-10-11

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '0012_login_throttle'
down_revision: Union[str, Sequence[str], None] = '0011_blacklist_origine'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'login_throttle',
        sa.Column('key', sa.String(length=80), nullable=False),
        sa.Column('failures', sa.Integer(), nullable=False),
        sa.Column('last_failure_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('key'),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('login_throttle')
