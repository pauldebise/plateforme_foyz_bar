"""Jeton d'idempotence de l'arbitrage de solde.

L'arbitrage ne crée pas de transaction monétaire : son journal d'audit est la
trace durable de l'opération. Une clé unique y est conservée pour qu'un rejeu
du formulaire (double clic, retentative réseau) ne modifie pas deux fois le
solde.

Revision ID: 0020_audit_idempotency_key
Revises: 0019_blacklist_alcohol_reason
Create Date: 2026-11-03

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0020_audit_idempotency_key"
down_revision: str | Sequence[str] | None = "0019_blacklist_alcohol_reason"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the idempotency key and its unique index."""
    op.add_column("audit_logs", sa.Column("idempotency_key", sa.String(64), nullable=True))
    op.create_index(
        op.f("ix_audit_logs_idempotency_key"),
        "audit_logs",
        ["idempotency_key"],
        unique=True,
    )


def downgrade() -> None:
    """Drop the idempotency key and its unique index."""
    op.drop_index(op.f("ix_audit_logs_idempotency_key"), table_name="audit_logs")
    op.drop_column("audit_logs", "idempotency_key")
