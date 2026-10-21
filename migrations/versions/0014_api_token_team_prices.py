"""Droits distincts sur les prix équipe des jetons d'API.

Les jetons existants conservent leurs droits actuels : les deux colonnes sont
ajoutées à faux. L'administrateur peut ensuite les activer jeton par jeton,
sans régénérer le jeton.

Revision ID: 0014_api_token_team_prices
Revises: 0013_api_tokens
Create Date: 2026-10-21

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '0014_api_token_team_prices'
down_revision: Union[str, Sequence[str], None] = '0013_api_tokens'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'api_tokens',
        sa.Column(
            'can_write_team_prices',
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        'api_tokens',
        sa.Column(
            'can_read_team_prices',
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('api_tokens', 'can_read_team_prices')
    op.drop_column('api_tokens', 'can_write_team_prices')
