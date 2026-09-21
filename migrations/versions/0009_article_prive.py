"""Articles privés : masquage du catalogue public.

Un article privé garde toutes ses fonctionnalités (vente en caisse,
passerelle, statistiques, tireuses) mais n'apparaît pas dans le catalogue
public des prix. La colonne est ajoutée en DDL natif (`batch_alter_table`
recréerait la table et déclencherait les actions `ON DELETE SET NULL` des clés
étrangères pointant vers `articles`).

Revision ID: 0009_article_prive
Revises: 0008_catalogue_campus
Create Date: 2026-09-21

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '0009_article_prive'
down_revision: Union[str, Sequence[str], None] = '0008_catalogue_campus'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'articles',
        sa.Column('is_private', sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_index('ix_articles_is_private', 'articles', ['is_private'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_articles_is_private', table_name='articles')
    op.drop_column('articles', 'is_private')
