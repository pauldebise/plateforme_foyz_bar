"""Suppression du suivi du volume restant des fûts.

Le volume restant n'est plus utilisé : une tireuse reste associée à son fût
même après plusieurs fûts successifs, et l'équipe n'a plus à mettre à jour un
compteur en soirée. La colonne est retirée en DDL natif (`batch_alter_table`
recréerait la table `kegs` et déclencherait les actions `ON DELETE CASCADE`
des tarifs et `ON DELETE SET NULL` des tireuses).

Revision ID: 0015_drop_keg_remaining
Revises: 0014_api_token_team_prices
Create Date: 2026-10-21

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '0015_drop_keg_remaining'
down_revision: Union[str, Sequence[str], None] = '0014_api_token_team_prices'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.drop_column('kegs', 'remaining_l')


def downgrade() -> None:
    """Downgrade schema."""
    op.add_column(
        'kegs',
        sa.Column('remaining_l', sa.Float(), nullable=False, server_default='0'),
    )
