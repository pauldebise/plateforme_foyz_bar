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
