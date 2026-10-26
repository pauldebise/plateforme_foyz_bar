"""Degré d'alcool dans les noms des articles de tireuse.

Les articles pression s'appellent désormais
« Pinte de Tireuse DROITE (Tarot D'Or - 8.0°) ». La migration renomme les
articles des tireuses actuellement montées, sans changer leurs identifiants :
l'historique de ventes et les statistiques restent rattachés aux mêmes
articles.

Revision ID: 0017_tap_article_degree
Revises: 0016_drop_keg_active
Create Date: 2026-10-26

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '0017_tap_article_degree'
down_revision: Union[str, Sequence[str], None] = '0016_drop_keg_active'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SIZES = {25: "Demi", 50: "Pinte", 33: "Pot"}


def _taps(conn):
    return conn.execute(
        sa.text(
            "SELECT t.number AS tap_number, t.name AS tap_name, "
            "k.id AS keg_id, k.name AS keg_name, k.alcohol_degree "
            "FROM taps t JOIN kegs k ON k.id = t.keg_id"
        )
    ).mappings().all()


def _name(label, tap_name, tap_number, keg_name, degree, *, with_degree):
    tap_label = tap_name or f"tireuse {tap_number}"
    if with_degree and (degree or 0) > 0:
        keg_label = f"{keg_name} - {float(degree):.1f}°"
    else:
        keg_label = keg_name
    return f"{label} de {tap_label} ({keg_label})"[:255]


def _rename(conn, *, with_degree):
    for row in _taps(conn):
        for volume, label in SIZES.items():
            conn.execute(
                sa.text(
                    "UPDATE articles SET name = :name "
                    "WHERE is_tap = TRUE AND tap_number = :tap_number "
                    "AND keg_id = :keg_id AND volume_cl = :volume"
                ),
                {
                    "name": _name(
                        label,
                        row["tap_name"],
                        row["tap_number"],
                        row["keg_name"],
                        row["alcohol_degree"],
                        with_degree=with_degree,
                    ),
                    "tap_number": row["tap_number"],
                    "keg_id": row["keg_id"],
                    "volume": volume,
                },
            )


def upgrade() -> None:
    """Upgrade data."""
    _rename(op.get_bind(), with_degree=True)


def downgrade() -> None:
    """Downgrade data."""
    _rename(op.get_bind(), with_degree=False)
