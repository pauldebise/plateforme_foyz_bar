"""Catalogues par campus : chaque article appartient à un unique campus.

Les deux équipes ne partagent plus le même catalogue (duplications issues
des bases historiques, spécificités locales) : la colonne `campus` porte
l'appartenance et un seul couple de prix (standard/équipe) reste par article.
La bascule des données (déduction du campus, scission des articles standards
partagés, rattachement de l'historique) est partagée avec l'auto-évolution
SQLite : `app.schema.migrate_articles_to_campus`.

Les colonnes sont ajoutées et retirées en DDL natif (pas de `batch_alter_table`)
: la recréation de table qu'implique le mode batch déclencherait les actions
`ON DELETE SET NULL` des clés étrangères pointant vers `articles` et
perdrait les rattachements de l'historique.

Revision ID: 0008_catalogue_campus
Revises: 0007_drop_trombinoscope
Create Date: 2026-09-18

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from app.schema import migrate_articles_to_campus

# revision identifiers, used by Alembic.
revision: str = '0008_catalogue_campus'
down_revision: Union[str, Sequence[str], None] = '0007_drop_trombinoscope'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'articles',
        sa.Column('campus', sa.String(length=10), nullable=False, server_default='brest'),
    )
    op.add_column('articles', sa.Column('price_std', sa.Integer(), nullable=False, server_default='0'))
    op.add_column('articles', sa.Column('price_team', sa.Integer(), nullable=False, server_default='0'))

    migrate_articles_to_campus(op.get_bind())

    op.drop_column('articles', 'price_std_brest')
    op.drop_column('articles', 'price_std_paris')
    op.drop_column('articles', 'price_team_brest')
    op.drop_column('articles', 'price_team_paris')
    op.create_index('ix_articles_campus', 'articles', ['campus'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_articles_campus', table_name='articles')
    op.add_column(
        'articles', sa.Column('price_std_brest', sa.Integer(), nullable=False, server_default='0')
    )
    op.add_column(
        'articles', sa.Column('price_std_paris', sa.Integer(), nullable=False, server_default='0')
    )
    op.add_column(
        'articles', sa.Column('price_team_brest', sa.Integer(), nullable=False, server_default='0')
    )
    op.add_column(
        'articles', sa.Column('price_team_paris', sa.Integer(), nullable=False, server_default='0')
    )

    conn = op.get_bind()
    # Restauration approchée : les prix reviennent sur le campus d'origine ;
    # les copies parisiennes issues de la scission ne sont pas refusionnées.
    conn.execute(sa.text(
        "UPDATE articles SET price_std_brest = price_std, price_team_brest = price_team "
        "WHERE campus = 'brest'"
    ))
    conn.execute(sa.text(
        "UPDATE articles SET price_std_paris = price_std, price_team_paris = price_team "
        "WHERE campus = 'paris'"
    ))

    op.drop_column('articles', 'price_std')
    op.drop_column('articles', 'price_team')
    op.drop_column('articles', 'campus')
