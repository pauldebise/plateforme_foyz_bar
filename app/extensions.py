import sqlite3

from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import event
from sqlalchemy.engine import Engine

db = SQLAlchemy()


@event.listens_for(Engine, "connect")
def _enable_sqlite_foreign_keys(dbapi_connection, connection_record):
    """SQLite n'applique pas les clés étrangères par défaut (PRAGMA
    foreign_keys=0). Sans cette activation, les ON DELETE SET NULL/CASCADE des
    modèles sont ignorés : la suppression d'un compte laisse des contributions
    orphelines et l'historique tombe en erreur (divergence avec PostgreSQL)."""
    if isinstance(dbapi_connection, sqlite3.Connection):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()
