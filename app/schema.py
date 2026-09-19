"""Application du schéma : Alembic en production, create_all en développement.

- PostgreSQL (production) : le schéma est versionné par Alembic et le démarrage
  de l'application n'exécute AUCUN DDL ; `flask upgrade-db` (ou `init-db`)
  applique les révisions (`migrations/versions/`).
- SQLite (développement, tests, petites installations) : `create_all()` +
  `ensure_schema_upgrades()` restent utilisés pour ne pas imposer Alembic aux
  bases locales jetables.

Une base existante créée par `create_all()` est adoptée par
`alembic stamp 0001_baseline` (voir README §6.1).
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ALEMBIC_INI = ROOT / "alembic.ini"


def alembic_config():
    from alembic.config import Config

    cfg = Config(str(ALEMBIC_INI))
    # Chemin absolu : la commande doit fonctionner quel que soit le cwd.
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    return cfg


def upgrade_to_head():
    """Applique toutes les révisions en attente (idempotent)."""
    from alembic import command

    command.upgrade(alembic_config(), "head")


def current_revision():
    """Révision appliquée, ou None si la base n'est pas encore versionnée."""
    from sqlalchemy import inspect, text

    from app.extensions import db

    if "alembic_version" not in inspect(db.engine).get_table_names():
        return None
    with db.engine.connect() as conn:
        row = conn.execute(text("SELECT version_num FROM alembic_version")).first()
    return row[0] if row else None


def head_revision():
    from alembic.script import ScriptDirectory

    return ScriptDirectory.from_config(alembic_config()).get_current_head()


def is_sqlite():
    from app.extensions import db

    return db.engine.dialect.name == "sqlite"


def tables_present():
    """Vrai si le schéma applicatif est déjà en place (table `users`).

    Permet au démarrage de ne pas échouer sur une base PostgreSQL vierge :
    dans ce cas c'est `flask init-db` / `upgrade-db` qui crée le schéma.
    """
    from sqlalchemy import inspect

    from app.extensions import db

    return "users" in inspect(db.engine).get_table_names()


def migrate_articles_to_campus(conn):
    """Bascule du catalogue vers des articles par campus (révision 0008).

    Exécutée par la révision Alembic (production) et par l'auto-évolution
    SQLite (développement) : la connexion fournie porte la transaction,
    aucun commit n'est fait ici. Les colonnes `campus`/`price_std`/`price_team`
    doivent déjà exister et les colonnes historiques `price_*_{brest,paris}`
    être encore présentes.

    Règles :
    - un article tireuse suit le campus de sa tireuse, un article d'événement
      celui de son événement ;
    - un article standard présent sur les DEUX campus (prix non nuls des deux
      côtés) est scindé : l'original reste brestois, une copie parisienne est
      créée et les lignes de vente parisiennes lui sont rattachées ;
    - un article standard sans prix brestois mais avec des prix parisiens
      bascule côté parisien.
    """
    from sqlalchemy import text

    # Campus déduit du porteur (EXISTS : une sous-requête sans correspondance
    # ne doit pas écrire NULL dans une colonne NOT NULL).
    conn.execute(
        text(
            "UPDATE articles SET campus = (SELECT events.campus FROM events "
            "WHERE events.id = articles.event_id) "
            "WHERE articles.event_id IS NOT NULL "
            "AND EXISTS (SELECT 1 FROM events WHERE events.id = articles.event_id)"
        )
    )
    conn.execute(
        text(
            "UPDATE articles SET campus = (SELECT taps.campus FROM taps "
            "WHERE taps.number = articles.tap_number) "
            "WHERE articles.is_tap AND articles.tap_number IS NOT NULL "
            "AND EXISTS (SELECT 1 FROM taps WHERE taps.number = articles.tap_number)"
        )
    )

    # Prix du campus de l'article tant que les colonnes historiques existent.
    for campus in ("brest", "paris"):
        conn.execute(
            text(
                f"UPDATE articles SET price_std = price_std_{campus}, "
                f"price_team = price_team_{campus} WHERE campus = '{campus}'"
            )
        )

    # Articles standards partagés : copie parisienne, l'original restant
    # brestois. Les articles tireuse/événement sont exclus : leur campus vient
    # de la tireuse ou de l'événement, leurs prix historiques de l'autre campus
    # sont sans effet. Les lignes de vente parisiennes sont rattachées à la
    # copie (les stats par campus regroupent sur article_id) ; les libellés et
    # montants étant copiés dans la ligne, l'historique reste lisible.
    shared = conn.execute(
        text(
            "SELECT id FROM articles WHERE campus = 'brest' AND event_id IS NULL "
            "AND NOT is_tap AND (price_std_brest != 0 OR price_team_brest != 0) "
            "AND (price_std_paris != 0 OR price_team_paris != 0)"
        )
    ).fetchall()
    for (article_id,) in shared:
        row = (
            conn.execute(
                text(
                    "SELECT name, article_type, volume_cl, price_std_paris AS price_std, "
                    "price_team_paris AS price_team, is_alcohol, is_tap, tap_number, keg_id, "
                    "event_id, active, created_at FROM articles WHERE id = :i"
                ),
                {"i": article_id},
            )
            .mappings()
            .one()
        )
        copy_id = conn.execute(
            text(
                "INSERT INTO articles (name, article_type, volume_cl, campus, price_std, "
                "price_team, is_alcohol, is_tap, tap_number, keg_id, event_id, active, "
                "created_at, price_std_brest, price_team_brest, price_std_paris, "
                "price_team_paris) VALUES (:name, :article_type, :volume_cl, 'paris', "
                ":price_std, :price_team, :is_alcohol, :is_tap, :tap_number, :keg_id, "
                ":event_id, :active, :created_at, 0, 0, :price_std, :price_team) RETURNING id"
            ),
            dict(row),
        ).scalar_one()
        conn.execute(
            text(
                "UPDATE transaction_lines SET article_id = :copy "
                "WHERE article_id = :orig AND transaction_id IN ("
                "SELECT id FROM transactions WHERE campus = 'paris')"
            ),
            {"copy": copy_id, "orig": article_id},
        )

    # Articles standards sans prix brestois mais présents côté parisien (idem
    # pour un article tireuse orphelin, sans tireuse correspondante).
    conn.execute(
        text(
            "UPDATE articles SET campus = 'paris', price_std = price_std_paris, "
            "price_team = price_team_paris WHERE event_id IS NULL "
            "AND NOT (is_tap AND tap_number IS NOT NULL "
            "AND EXISTS (SELECT 1 FROM taps WHERE taps.number = articles.tap_number)) "
            "AND campus = 'brest' AND price_std_brest = 0 AND price_team_brest = 0 "
            "AND (price_std_paris != 0 OR price_team_paris != 0)"
        )
    )
