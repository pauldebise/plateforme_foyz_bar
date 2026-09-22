"""Passerelle événement : autoriser ou non les articles standards.

La passerelle encaisse toujours les articles temporaires de l'événement. Ce
réglage, modifiable depuis la gestion de l'événement, autorise en plus le
catalogue standard du campus de l'événement. La colonne est ajoutée en DDL
natif (`batch_alter_table` recréerait la table `events` et déclencherait les
actions `ON DELETE CASCADE` des clés étrangères pointant vers elle).

Revision ID: 0010_event_standard_articles
Revises: 0009_article_prive
Create Date: 2026-09-22

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '0010_event_standard_articles'
down_revision: Union[str, Sequence[str], None] = '0009_article_prive'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'events',
        sa.Column('allow_standard_articles', sa.Boolean(), nullable=False, server_default=sa.true()),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('events', 'allow_standard_articles')
