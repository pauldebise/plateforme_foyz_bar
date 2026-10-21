"""Jetons d'API externe : api_tokens et sélections associées.

Un jeton est rattaché à un campus, à des portées (lecture des consommations,
écriture des prix standards) et à une liste d'articles standards, d'articles
d'événement et de fûts (les trois tailles d'une tireuse). Seule l'empreinte
SHA-256 du jeton est stockée.

Revision ID: 0013_api_tokens
Revises: 0012_login_throttle
Create Date: 2026-10-21

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '0013_api_tokens'
down_revision: Union[str, Sequence[str], None] = '0012_login_throttle'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'api_tokens',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('label', sa.String(length=160), nullable=False),
        sa.Column('token_hash', sa.String(length=64), nullable=False),
        sa.Column('token_prefix', sa.String(length=16), nullable=False),
        sa.Column('campus', sa.String(length=10), nullable=False),
        sa.Column(
            'can_read_consumption',
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
        sa.Column(
            'can_write_prices',
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
        sa.Column('history_from', sa.DateTime(), nullable=False),
        sa.Column('history_to', sa.DateTime(), nullable=False),
        sa.Column('expires_at', sa.DateTime(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.Column('revoked_at', sa.DateTime(), nullable=True),
        sa.Column('last_used_at', sa.DateTime(), nullable=True),
        sa.Column('last_request_at', sa.DateTime(), nullable=True),
        sa.Column('created_by_id', sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(['created_by_id'], ['users.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_api_tokens_token_hash', 'api_tokens', ['token_hash'], unique=True)
    op.create_index('ix_api_tokens_campus', 'api_tokens', ['campus'])
    op.create_index('ix_api_tokens_expires_at', 'api_tokens', ['expires_at'])
    op.create_index('ix_api_tokens_revoked_at', 'api_tokens', ['revoked_at'])

    op.create_table(
        'api_token_articles',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('token_id', sa.Integer(), nullable=False),
        sa.Column('article_id', sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(['article_id'], ['articles.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['token_id'], ['api_tokens.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('token_id', 'article_id', name='uq_api_token_article'),
    )
    op.create_index('ix_api_token_articles_token_id', 'api_token_articles', ['token_id'])
    op.create_index('ix_api_token_articles_article_id', 'api_token_articles', ['article_id'])

    op.create_table(
        'api_token_kegs',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('token_id', sa.Integer(), nullable=False),
        sa.Column('keg_id', sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(['keg_id'], ['kegs.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['token_id'], ['api_tokens.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('token_id', 'keg_id', name='uq_api_token_keg'),
    )
    op.create_index('ix_api_token_kegs_token_id', 'api_token_kegs', ['token_id'])
    op.create_index('ix_api_token_kegs_keg_id', 'api_token_kegs', ['keg_id'])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('api_token_kegs')
    op.drop_table('api_token_articles')
    op.drop_table('api_tokens')
