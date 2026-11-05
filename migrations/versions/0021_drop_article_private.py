"""Suppression du masquage manuel des articles.

Le catalogue public ne cache plus que les articles de type Blague. Les anciens
articles marqués privés redeviennent publics : le bar ne conserve pas de
second mécanisme de visibilité. Le type Blague reste vendable en caisse, masqué
du catalogue public et exclu des tendances et des statistiques par défaut.

Revision ID: 0021_drop_article_private
Revises: 0020_audit_idempotency_key
Create Date: 2026-11-05

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '0021_drop_article_private'
down_revision: Union[str, Sequence[str], None] = '0020_audit_idempotency_key'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.drop_index('ix_articles_is_private', table_name='articles')
    op.drop_column('articles', 'is_private')


def downgrade() -> None:
    """Downgrade schema."""
    op.add_column(
        'articles',
        sa.Column('is_private', sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_index('ix_articles_is_private', 'articles', ['is_private'], unique=False)
