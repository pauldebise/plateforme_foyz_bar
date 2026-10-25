"""Suppression du champ « fût actif ».

Le champ n'était pas utilisé par l'équipe et pouvait rendre une tireuse
silencieusement inencaissable lorsqu'un fût était assigné avec la case
décochée. La colonne est retirée en DDL natif (`batch_alter_table` recréerait
la table `kegs` et déclencherait les actions `ON DELETE CASCADE` des tarifs et
`ON DELETE SET NULL` des tireuses).

Revision ID: 0016_drop_keg_active
Revises: 0015_drop_keg_remaining
Create Date: 2026-10-25

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '0016_drop_keg_active'
down_revision: Union[str, Sequence[str], None] = '0015_drop_keg_remaining'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Un fût marqué inactif a pu laisser ses articles de tireuse désactivés :
    # la tireuse est toujours montée mais la caisse ne propose plus la bière.
    # On réactive le dernier article de chaque taille dont la tireuse est
    # encore branchée sur son fût ; les articles historiques d'un même fût
    # (régénérations successives) resteraient sinon actifs en double, avec
    # d'anciens prix, dans la caisse. Les tireuses détachées restent hors
    # catalogue.
    op.execute(
        """
        UPDATE articles SET active = TRUE
        WHERE is_tap = TRUE
          AND EXISTS (
            SELECT 1 FROM taps
            WHERE taps.number = articles.tap_number
              AND taps.keg_id = articles.keg_id
          )
          AND articles.id IN (
            SELECT MAX(a.id) FROM articles AS a
            WHERE a.is_tap = TRUE
            GROUP BY a.tap_number, a.volume_cl
          )
        """
    )
    op.drop_column('kegs', 'active')


def downgrade() -> None:
    """Downgrade schema."""
    op.add_column(
        'kegs',
        sa.Column('active', sa.Boolean(), nullable=False, server_default=sa.true()),
    )
