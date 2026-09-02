"""Chargement intermédiaire des exports Paris dans staging_paris_raw (JSONB).

La table de staging est créée DANS la transaction de migration (DDL
transactionnel sous PostgreSQL) et disparaît donc avec un ROLLBACK : aucun
résidu en cas d'échec. La projection vers le schéma cible se fait ensuite en
lecture streaming de la staging (mémoire constante).
"""

from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from . import settings
from .errors import SourceError
from .parsing import files as files_reader
from .parsing.sqlstream import iter_business_rows
from .sources import paris as paris_contract

STAGING_COLUMNS = ("id", "source_file", "source_table", "campus", "record", "loaded_at")

_METADATA = sa.MetaData()
_TABLES = {}


def staging_table(dialect_name):
    """Table de staging (JSONB sous PostgreSQL, JSON ailleurs), mise en cache."""
    if dialect_name not in _TABLES:
        json_type = JSONB if dialect_name == "postgresql" else sa.JSON
        _TABLES[dialect_name] = sa.Table(
            settings.STAGING_TABLE,
            _METADATA,
            sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
            sa.Column("source_file", sa.String(255), nullable=False, server_default=""),
            sa.Column("source_table", sa.String(120), nullable=True),
            sa.Column("campus", sa.String(10), nullable=False),
            sa.Column("record", json_type, nullable=False),
            sa.Column("loaded_at", sa.DateTime, nullable=True),
            extend_existing=True,
        )
    return _TABLES[dialect_name]


_DDL = {
    "postgresql": """
        CREATE TABLE IF NOT EXISTS staging_paris_raw (
            id BIGSERIAL PRIMARY KEY,
            source_file VARCHAR(255) NOT NULL DEFAULT '',
            source_table VARCHAR(120),
            campus VARCHAR(10) NOT NULL,
            record JSONB NOT NULL,
            loaded_at TIMESTAMP
        )
    """,
    "sqlite": """
        CREATE TABLE IF NOT EXISTS staging_paris_raw (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_file VARCHAR(255) NOT NULL DEFAULT '',
            source_table VARCHAR(120),
            campus VARCHAR(10) NOT NULL,
            record JSON NOT NULL,
            loaded_at TIMESTAMP
        )
    """,
}


def create(conn):
    """Crée la staging DANS la transaction courante (DDL transactionnel).

    DDL manuel : metadata.create_all() gérerait sa propre transaction et
    commiterait le DDL, ce qui casserait la garantie de rollback intégral.
    """
    dialect = conn.dialect.name
    if dialect not in _DDL:
        raise SourceError(f"Dialecte de base non supporté pour la staging : {dialect}")
    conn.execute(sa.text(_DDL[dialect]))
    return staging_table(dialect)


def load(conn, source_files, chunk_rows):
    """Charge tous les fichiers Paris dans la staging. Retourne des stats."""
    table = staging_table(conn.dialect.name)
    stats = {"rows": 0, "files": 0}
    for src in source_files:
        if src.campus != "paris":
            continue
        stats["files"] += 1
        batch = []
        if src.kind == "sql_dump":
            stream = _iter_paris_sql(src)
            for source_table, row in stream:
                batch.append((src.path.name, source_table, row))
                if len(batch) >= chunk_rows:
                    _flush(conn, table, batch, stats)
                    batch = []
        else:
            for source_table, row in files_reader.iter_rows(src.path, src.kind):
                batch.append((src.path.name, source_table, row))
                if len(batch) >= chunk_rows:
                    _flush(conn, table, batch, stats)
                    batch = []
        if batch:
            _flush(conn, table, batch, stats)
    return stats


def _iter_paris_sql(src):
    """Itère les lignes d'un dump SQL Paris : toutes les tables NON log."""
    def predicate(table_name):
        from . import logfilter
        return bool(table_name) and not logfilter.is_log_table(table_name)

    for table, _cols, rows in iter_business_rows(
        src.path, keep_predicate=predicate, batch_size=settings.DEFAULT_CHUNK_ROWS
    ):
        for row in rows:
            yield table, row


def _flush(conn, table, batch, stats):
    payload = [
        {
            "source_file": filename[:255],
            "source_table": (source_table or "")[:120] or None,
            "campus": "paris",
            "record": _jsonable(row),
        }
        for filename, source_table, row in batch
    ]
    conn.execute(table.insert(), payload)
    stats["rows"] += len(payload)


def _jsonable(row):
    """Garantit un contenu sérialisable en JSON (dates -> ISO)."""
    out = {}
    for key, value in row.items():
        if isinstance(value, datetime):
            out[str(key)] = value.isoformat(sep=" ")
        else:
            out[str(key)] = value
    return out


def iter_records(conn, batch_size=1000):
    """Re-lit la staging en streaming : itère (source_file, source_table, record)."""
    table = staging_table(conn.dialect.name)
    result = conn.execution_options(stream_results=True).execute(
        sa.select(
            table.c.source_file, table.c.source_table, table.c.record
        ).order_by(table.c.id)
    )
    while True:
        rows = result.fetchmany(batch_size)
        if not rows:
            break
        for source_file, source_table, record in rows:
            yield source_file, source_table, dict(record)


def count(conn):
    table = staging_table(conn.dialect.name)
    return conn.execute(sa.select(sa.func.count()).select_from(table)).scalar_one()


def drop(conn):
    conn.execute(sa.text("DROP TABLE IF EXISTS staging_paris_raw"))
