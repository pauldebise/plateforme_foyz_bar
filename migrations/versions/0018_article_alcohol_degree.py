"""Degré d'alcool facultatif sur les articles.

Le catalogue public affiche une colonne « Degré » pour les articles dont le
degré est renseigné. Les articles de tireuse reprennent automatiquement le
degré de leur fût ; les autres articles restent à NULL tant que l'équipe ne
l'a pas saisi.

Revision ID: 0018_article_alcohol_degree
Revises: 0017_tap_article_degree
Create Date: 2026-10-26

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '0018_article_alcohol_degree'
down_revision: Union[str, Sequence[str], None] = '0017_tap_article_degree'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema and backfill tap articles."""
    op.add_column('articles', sa.Column('alcohol_degree', sa.Float(), nullable=True))
    op.execute(
        """
        UPDATE articles SET alcohol_degree = (
            SELECT CASE WHEN kegs.alcohol_degree > 0 THEN kegs.alcohol_degree END
            FROM kegs WHERE kegs.id = articles.keg_id
        )
        WHERE is_tap = TRUE AND keg_id IS NOT NULL
        """
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('articles', 'alcohol_degree')
