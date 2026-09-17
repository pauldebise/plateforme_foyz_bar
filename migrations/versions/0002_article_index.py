"""Index sur transaction_lines.article_id (T-7.2).

Le classement des articles (`top_article_ids`, appelé à chaque encaissement)
regroupe sur `article_id` : sans index, la base construit un arbre temporaire
à chaque appel.

Revision ID: 0002_article_index
Revises: 0001_baseline
Create Date: 2026-09-17

"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '0002_article_index'
down_revision: Union[str, Sequence[str], None] = '0001_baseline'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('transaction_lines', schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f('ix_transaction_lines_article_id'), ['article_id'], unique=False
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('transaction_lines', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_transaction_lines_article_id'))
