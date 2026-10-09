"""Blacklists : enregistrement de l'équipe d'origine.

Chaque blacklist (comportement et alcool) retient désormais l'équipe — le
campus — qui l'a posée : seule cette équipe peut la retirer, l'admin global
restant exempté. Les blacklists existantes, dont l'origine n'est pas connue
(imports legacy, données antérieures), sont rattachées à Brest conformément à
la règle retenue ; le code traite de toute façon un NULL comme « brest ».

Revision ID: 0011_blacklist_origine
Revises: 0010_event_standard_articles
Create Date: 2026-10-09

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '0011_blacklist_origine'
down_revision: Union[str, Sequence[str], None] = '0010_event_standard_articles'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('users', sa.Column('blacklist_by', sa.String(length=10), nullable=True))
    op.add_column(
        'users', sa.Column('blacklist_alcohol_by', sa.String(length=10), nullable=True)
    )
    op.execute("UPDATE users SET blacklist_by = 'brest' WHERE blacklist")
    op.execute(
        "UPDATE users SET blacklist_alcohol_by = 'brest' WHERE blacklist_alcohol"
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('users', 'blacklist_alcohol_by')
    op.drop_column('users', 'blacklist_by')
