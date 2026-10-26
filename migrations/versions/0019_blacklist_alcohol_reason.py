"""Motif distinct pour la blacklist alcool.

La blacklist comportementale utilisait déjà ``users.blacklist_reason``. La
blacklist alcool reçoit sa propre colonne : les anciens motifs enregistrés
pour un compte uniquement blacklisté alcool y sont repris.

Revision ID: 0019_blacklist_alcohol_reason
Revises: 0018_article_alcohol_degree
Create Date: 2026-10-26

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0019_blacklist_alcohol_reason"
down_revision: str | Sequence[str] | None = "0018_article_alcohol_degree"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the alcohol blacklist reason and preserve legacy values."""
    op.add_column("users", sa.Column("blacklist_alcohol_reason", sa.String(255), nullable=True))
    op.execute(
        """
        UPDATE users SET blacklist_alcohol_reason = blacklist_reason
        WHERE blacklist_alcohol = TRUE
          AND COALESCE(blacklist, FALSE) = FALSE
          AND blacklist_reason IS NOT NULL
          AND blacklist_reason <> ''
        """
    )


def downgrade() -> None:
    """Drop the alcohol blacklist reason."""
    op.drop_column("users", "blacklist_alcohol_reason")
