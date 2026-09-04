"""Point d'entrée CLI du module de migration.

    python -m migration --run            # migration + audit + suppression des sources
    python -m migration --dry-run        # cycle complet puis ROLLBACK (sources intactes)
    python -m migration --audit-only     # comparaison soldes sources/cibles sans injection

Options : --source-dir, --database-url, --chunk-rows, --money-unit, --keep-archives.
Codes de sortie : 0 succès, 1 erreur ou écart comptable.
"""

import argparse
import os
import re
import sys
from pathlib import Path

import sqlalchemy as sa
from dotenv import load_dotenv
from sqlalchemy import create_engine

from . import audit, detect, report, settings, staging
from .cleaner import dispose
from .errors import AccountingError, MigrationError, SourceError
from .etl import Migrator, SourceReader
from .report import line, section

MODES = ("run", "dry-run", "audit-only")


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog="python -m migration",
        description="Migration des anciennes bases Brest/Paris vers la plateforme unifiée.",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--run", action="store_true",
                      help="migration complète : transaction, audit, suppression des sources")
    mode.add_argument("--dry-run", action="store_true",
                      help="cycle complet puis ROLLBACK systématique, fichiers intacts")
    mode.add_argument("--audit-only", action="store_true",
                      help="compare les soldes sources/cibles sans réinjecter de données")
    parser.add_argument("--source-dir", type=Path, default=settings.DEFAULT_SOURCE_DIR,
                        help=f"dossier des dumps (défaut : {settings.DEFAULT_SOURCE_DIR.name}/)")
    parser.add_argument("--database-url", default=None,
                        help="URL SQLAlchemy cible (défaut : DATABASE_URL de l'environnement)")
    parser.add_argument("--chunk-rows", type=int, default=settings.DEFAULT_CHUNK_ROWS,
                        help=f"taille des batchs streaming ({settings.MIN_CHUNK_ROWS}-"
                             f"{settings.MAX_CHUNK_ROWS}, défaut {settings.DEFAULT_CHUNK_ROWS})")
    parser.add_argument("--money-unit", choices=settings.MONEY_UNITS,
                        default=settings.DEFAULT_MONEY_UNIT,
                        help="unité des montants sources (défaut : euros -> conversion centimes)")
    parser.add_argument("--keep-archives", action="store_true",
                        help="avec --run : archive les sources au lieu de les supprimer")
    args = parser.parse_args(argv)
    if args.keep_archives and not args.run:
        parser.error("--keep-archives ne s'utilise qu'avec --run.")
    if not (settings.MIN_CHUNK_ROWS <= args.chunk_rows <= settings.MAX_CHUNK_ROWS):
        parser.error(f"--chunk-rows doit être entre {settings.MIN_CHUNK_ROWS} "
                     f"et {settings.MAX_CHUNK_ROWS}.")
    args.mode = "run" if args.run else ("dry-run" if args.dry_run else "audit-only")
    return args


def _mask_url(url):
    return re.sub(r"://([^:/@]+):[^@/]*@", r"://\1:***@", str(url))


def _print_header(args, files, url):
    section(f"MIGRATION FOY'Z — MODE {args.mode.upper()}")
    kv_d = report.kv
    kv_d("Dossier source", str(args.source_dir))
    for src in files:
        kv_d("Fichier détecté", f"{src.path.name} ({src.campus}, {src.kind}, "
                                f"{src.size // 1024} KiB)")
    kv_d("Base cible", _mask_url(url))
    kv_d("Batch (lignes)", str(args.chunk_rows))
    kv_d("Unité montants sources", args.money_unit)


def _print_scan_stats(scan_stats, migrated_tables):
    ignored_log, unknown = [], []
    seen = {}
    for scanner in scan_stats.values():
        for table in scanner.tables_seen:
            if table.startswith("__"):
                continue
            seen.setdefault(table, [0, 0])
            seen[table][0] += scanner.skipped_statements.get(table, 0)
            seen[table][1] += scanner.skipped_rows_approx.get(table, 0)
    for table, (_stmts, approx_rows) in sorted(seen.items()):
        if table in migrated_tables:
            continue
        kind = logfilter_classify(table)
        label = f"{table} (~{approx_rows} lignes)" if approx_rows else table
        (ignored_log if kind == "log" else unknown).append(label)
    if ignored_log:
        report.kv("Tables logs exclues", ", ".join(ignored_log))
    if unknown:
        report.kv("Tables inconnues NON migrées", ", ".join(unknown))


def logfilter_classify(table):
    from . import logfilter
    return logfilter.classify(table)


def _resolve_url(args):
    load_dotenv()
    url = args.database_url or os.environ.get("DATABASE_URL")
    if not url:
        from app.config import Config
        url = Config.SQLALCHEMY_DATABASE_URI
    return url


def _ensure_schema(engine):
    """Crée les tables cibles si besoin (équivalent init-db, hors transaction)."""
    import app.models  # noqa: F401 — enregistre les modèles dans db.metadata
    from app.extensions import db
    db.metadata.create_all(engine)
    _ensure_column(engine, "users", "blacklist_reason", "VARCHAR(255)")
    _ensure_column(engine, "taps", "name", "VARCHAR(255)")


def _ensure_column(engine, table, column, ddl_type):
    """Ajoute une colonne manquante sur une base existante (idempotent).

    create_all() ne complète pas les tables déjà présentes : la cible d'une
    bascule peut dater d'avant l'ajout de `users.blacklist_reason`.
    """
    with engine.connect() as conn:
        if engine.dialect.name == "postgresql":
            conn.execute(sa.text(
                f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {ddl_type}"
            ))
        else:  # sqlite (tests) : introspection puis ALTER
            rows = conn.execute(sa.text(f"PRAGMA table_info({table})")).fetchall()
            if rows and column not in {r[1] for r in rows}:
                conn.execute(sa.text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl_type}"))
        conn.commit()


def _run_migration(args, engine, files):
    _ensure_schema(engine)
    with engine.connect() as conn:
        transaction = conn.begin()
        try:
            migrator = Migrator(conn, files, chunk_rows=args.chunk_rows,
                                money_unit=args.money_unit)
            result = migrator.migrate()
            counts = migrator.report_lines()
        except Exception:
            transaction.rollback()
            _drop_staging_after_rollback(conn)
            line()
            print("  >>> ROLLBACK effectué : base cible inchangée, fichiers sources conservés.")
            raise
        audit.print_audit(result, extra_counts=counts)
        _print_scan_stats(migrator.reader.scan_stats, set(brest_tables()))
        if args.mode == "dry-run":
            transaction.rollback()
            _drop_staging_after_rollback(conn)
            line()
            print("  >>> DRY-RUN : ROLLBACK systématique — base inchangée, fichiers conservés.")
            return 0
        transaction.commit()
        # la table de staging n'a plus d'utilité une fois la projection commitée
        staging.drop(conn)
        conn.commit()
        line()
        print("  >>> COMMIT effectué : migration validée. <<<")
        dispose(files, keep_archives=args.keep_archives, source_dir=args.source_dir)
        return 0


def brest_tables():
    from .sources.brest import TABLE_MAP
    return TABLE_MAP.keys()


def _drop_staging_after_rollback(conn):
    """Purge résiduelle de la staging après rollback.

    Sous PostgreSQL le DDL est transactionnel : la table disparaît déjà avec le
    rollback et ce DROP est un no-op (IF EXISTS). Sous SQLite, le driver pysqlite
    committe implicitement le DDL : cette purge évite tout résidu en dev/test.
    Le DROP est exécuté puis commité explicitement (sinon SQLAlchemy 2.0
    l'annulerait à la fermeture de la connexion).
    """
    try:
        staging.drop(conn)
        conn.commit()
    except Exception:  # noqa: BLE001 — nettoyage best-effort
        pass


def _run_audit_only(args, engine, files):
    _ensure_schema(engine)
    with engine.connect() as conn:
        reader = SourceReader(files, args.money_unit, args.chunk_rows, conn=None)
        source = {"brest": 0, "paris": 0}
        users_count = 0
        for campus, _filename, user, _warning in reader.iter_users():
            if user is not None:
                source[campus] += user["balance_cents"]
                users_count += 1
        captured = audit.capture_target(conn)
        result = audit.AuditResult(
            source=source,
            initial=audit.TargetTotals(),
            final=captured,
        )
        audit.print_audit(result, extra_counts=[
            ("Comptes sources exploités", users_count),
        ])
        _print_scan_stats(reader.scan_stats, set(brest_tables()))
        if result.ok:
            print("  >>> AUDIT-ONLY : sommes sources et cibles réconciliées. <<<")
            return 0
        print("  >>> AUDIT-ONLY : écart détecté (voir rapport ci-dessus). <<<")
        return 1


def main(argv=None):
    args = parse_args(argv)
    try:
        files = detect.scan(args.source_dir)
        url = _resolve_url(args)
        _print_header(args, files, url)
        engine = create_engine(url)
        if args.mode == "audit-only":
            return _run_audit_only(args, engine, files)
        return _run_migration(args, engine, files)
    except AccountingError as exc:
        line()
        print(f"ERREUR COMPTABLE : {exc}", file=sys.stderr)
        return 1
    except (MigrationError, SourceError) as exc:
        line()
        print(f"ERREUR MIGRATION : {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 — barrière finale de la bascule nocturne
        line()
        print(f"ERREUR INATTENDUE : {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
