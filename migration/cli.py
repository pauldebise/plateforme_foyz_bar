"""Point d'entrée CLI du module de migration.

    python -m migration --run            # migration + audit + suppression des sources
    python -m migration --dry-run        # cycle complet puis ROLLBACK (sources intactes)
    python -m migration --audit-only     # comparaison soldes sources/cibles sans injection
    python -m migration --purge-archives 30   # destruction des archives anciennes (D14)

Options : --source-dir, --database-url, --chunk-rows, --money-unit,
--keep-archives, --force-import (rejeu d'un lot déjà marqué comme migré).
Codes de sortie : 0 succès, 1 erreur ou écart comptable.
"""

import argparse
import hashlib
import os
import re
import sys
from datetime import datetime, UTC
from pathlib import Path

import sqlalchemy as sa
from dotenv import load_dotenv
from sqlalchemy import create_engine

from . import audit, detect, report, settings, staging
from .cleaner import dispose, purge_archives
from .errors import AccountingError, MigrationError, SourceError
from .etl import Migrator, SourceReader
from .report import line, section

MODES = ("run", "dry-run", "audit-only")

# Marqueur de campagne (R5) : table de la cible refusant de rejouer le même lot.
CAMPAIGN_TABLE = "migration_campaign"

# Redaction des secrets potentiels dans les messages d'erreur (R8) : hash
# werkzeug, bcrypt PHP, empreintes hexadécimales.
_SECRET_RE = re.compile(
    r"(\$2[aby]\$\d{2}\$[^\s'\"]+"
    r"|(?:scrypt|pbkdf2(?:-sha\d+)?|argon2|hex):[^\s'\"]+"
    r"|\b[0-9a-fA-F]{32,64}\b)"
)


def _safe_error(exc):
    """Message d'erreur expurgé de tout hash/mot de passe (R8)."""
    text = f"{type(exc).__name__}: {exc}"
    text = _SECRET_RE.sub("<redacted>", text)
    return text[:600]


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog="python -m migration",
        description="Migration des anciennes bases Brest/Paris vers la plateforme unifiée.",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--run",
        action="store_true",
        help="migration complète : transaction, audit, suppression des sources",
    )
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="cycle complet puis ROLLBACK systématique, fichiers intacts",
    )
    mode.add_argument(
        "--audit-only",
        action="store_true",
        help="compare les soldes sources/cibles sans réinjecter de données",
    )
    mode.add_argument(
        "--purge-archives",
        type=int,
        metavar="JOURS",
        help="détruit les archives de migration plus anciennes que JOURS jours",
    )
    parser.add_argument(
        "--source-dir",
        type=Path,
        default=settings.DEFAULT_SOURCE_DIR,
        help=f"dossier des dumps (défaut : {settings.DEFAULT_SOURCE_DIR.name}/)",
    )
    parser.add_argument(
        "--database-url",
        default=None,
        help="URL SQLAlchemy cible (défaut : DATABASE_URL de l'environnement)",
    )
    parser.add_argument(
        "--chunk-rows",
        type=int,
        default=settings.DEFAULT_CHUNK_ROWS,
        help=f"taille des batchs streaming ({settings.MIN_CHUNK_ROWS}-"
        f"{settings.MAX_CHUNK_ROWS}, défaut {settings.DEFAULT_CHUNK_ROWS})",
    )
    parser.add_argument(
        "--money-unit",
        choices=settings.MONEY_UNITS,
        default=settings.DEFAULT_MONEY_UNIT,
        help="unité des montants sources (défaut : euros -> conversion centimes)",
    )
    parser.add_argument(
        "--keep-archives",
        action="store_true",
        help="avec --run : archive les sources au lieu de les supprimer",
    )
    parser.add_argument(
        "--force-import",
        action="store_true",
        help="avec --run : rejoue un lot déjà marqué comme migré (dangereux)",
    )
    args = parser.parse_args(argv)
    if args.keep_archives and not args.run:
        parser.error("--keep-archives ne s'utilise qu'avec --run.")
    if args.force_import and not args.run:
        parser.error("--force-import ne s'utilise qu'avec --run.")
    if not (settings.MIN_CHUNK_ROWS <= args.chunk_rows <= settings.MAX_CHUNK_ROWS):
        parser.error(
            f"--chunk-rows doit être entre {settings.MIN_CHUNK_ROWS} et {settings.MAX_CHUNK_ROWS}."
        )
    if args.purge_archives is not None:
        if args.purge_archives < 1:
            parser.error("--purge-archives JOURS doit être >= 1.")
        args.mode = "purge-archives"
    else:
        args.mode = "run" if args.run else ("dry-run" if args.dry_run else "audit-only")
    return args


def _mask_url(url):
    return re.sub(r"://([^:/@]+):[^@/]*@", r"://\1:***@", str(url))


def _print_header(args, files, url):
    section(f"MIGRATION FOY'Z — MODE {args.mode.upper()}")
    kv_d = report.kv
    kv_d("Dossier source", str(args.source_dir))
    for src in files:
        kv_d(
            "Fichier détecté", f"{src.path.name} ({src.campus}, {src.kind}, {src.size // 1024} KiB)"
        )
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
    """Crée les tables cibles si besoin (équivalent init-db, hors transaction).

    R21 : cette étape est exécutée AVANT l'ouverture de la transaction de
    données ; ses DDL sont donc hors du périmètre du ROLLBACK. La migration ne
    prétend donc plus que « la base cible est inchangée » mais que « aucune
    donnée métier n'a été écrite » (le schéma peut avoir été initialisé).
    """
    import app.models  # noqa: F401 — enregistre les modèles dans db.metadata
    from app.extensions import db

    db.metadata.create_all(engine)
    _ensure_column(engine, "users", "blacklist_reason", "VARCHAR(255)")
    _ensure_column(engine, "users", "username", "VARCHAR(64)")
    _ensure_column(engine, "users", "nickname", "VARCHAR(255)")
    _ensure_column(engine, "users", "disabled", "BOOLEAN DEFAULT FALSE")
    _ensure_column(engine, "taps", "name", "VARCHAR(255)")
    _ensure_campaign_table(engine)


def _ensure_campaign_table(engine):
    """Table de suivi des campagnes de migration (idempotence, R5)."""
    with engine.connect() as conn:
        conn.execute(
            sa.text(
                f"CREATE TABLE IF NOT EXISTS {CAMPAIGN_TABLE} ("
                "id INTEGER PRIMARY KEY, fingerprint VARCHAR(64) NOT NULL, "
                "file_count INTEGER NOT NULL, files VARCHAR(2000), completed_at TIMESTAMP)"
            )
        )
        conn.commit()


def fingerprint(files):
    """Empreinte stable du lot source : noms + tailles, ordre indifférent."""
    digest = hashlib.sha256()
    for src in sorted(files, key=lambda f: f.path.name):
        digest.update(f"{src.path.name}:{src.size}:{src.campus}:{src.kind}\n".encode())
    return digest.hexdigest()


def _completed_campaign(engine):
    """(fingerprint, completed_at) de la dernière campagne, ou None."""
    with engine.connect() as conn:
        row = conn.execute(
            sa.text(
                f"SELECT fingerprint, completed_at FROM {CAMPAIGN_TABLE} ORDER BY id DESC LIMIT 1"
            )
        ).fetchone()
    return (row[0], row[1]) if row else None


def _record_campaign(conn, files):
    """Enregistre la campagne DANS la transaction de données (commit atomique)."""
    conn.execute(sa.text(f"DELETE FROM {CAMPAIGN_TABLE}"))
    conn.execute(
        sa.text(
            f"INSERT INTO {CAMPAIGN_TABLE} (id, fingerprint, file_count, files, completed_at) "
            "VALUES (1, :fp, :n, :names, :ts)"
        ),
        {
            "fp": fingerprint(files),
            "n": len(files),
            "names": ", ".join(sorted(src.path.name for src in files))[:2000],
            "ts": datetime.now(UTC).replace(tzinfo=None),
        },
    )


def _ensure_column(engine, table, column, ddl_type):
    """Ajoute une colonne manquante sur une base existante (idempotent).

    create_all() ne complète pas les tables déjà présentes : la cible d'une
    bascule peut dater d'avant l'ajout de `users.blacklist_reason`.
    """
    with engine.connect() as conn:
        if engine.dialect.name == "postgresql":
            conn.execute(
                sa.text(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {ddl_type}")
            )
        else:  # sqlite (tests) : introspection puis ALTER
            rows = conn.execute(sa.text(f"PRAGMA table_info({table})")).fetchall()
            if rows and column not in {r[1] for r in rows}:
                conn.execute(sa.text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl_type}"))
        conn.commit()


def _refuse_replay(engine, files, force):
    """Refuse de rejouer un lot déjà migré (R5), sauf --force-import."""
    if force:
        print("  ATTENTION : --force-import — contrôle d'idempotence ignoré.")
        return
    previous = _completed_campaign(engine)
    if previous and previous[0] == fingerprint(files):
        raise MigrationError(
            "Lot déjà migré (campagne terminée le "
            f"{previous[1] or '?'}). Refus de rejouer ces fichiers. "
            "Utilisez --force-import uniquement en connaissance de cause, "
            "ou déposez un nouveau lot."
        )


def _check_consumed(files, reader):
    """Refuse tout nettoyage si un fichier détecté n'a pas été lu (R7)."""
    missing = [src.path.name for src in files if src.path.name not in reader.consumed]
    if missing:
        raise SourceError(
            "Fichiers détectés mais jamais consommés : "
            + ", ".join(missing)
            + " — aucun fichier source ne sera supprimé."
        )


def _run_migration(args, engine, files):
    _ensure_schema(engine)
    _refuse_replay(engine, files, args.force_import)
    with engine.connect() as conn:
        transaction = conn.begin()
        try:
            migrator = Migrator(conn, files, chunk_rows=args.chunk_rows, money_unit=args.money_unit)
            result = migrator.migrate()
            counts = migrator.report_lines()
            _check_consumed(files, migrator.reader)
        except Exception:
            transaction.rollback()
            _drop_staging_after_rollback(conn)
            line()
            print(
                "  >>> ROLLBACK effectué : aucune donnée métier écrite, "
                "fichiers sources conservés (le schéma cible a pu être initialisé)."
            )
            raise
        audit.print_audit(result, extra_counts=counts)
        audit.print_merged(migrator.merged)
        _print_scan_stats(migrator.reader.scan_stats, set(brest_tables()))
        if args.mode == "dry-run":
            transaction.rollback()
            _drop_staging_after_rollback(conn)
            line()
            print("  >>> DRY-RUN : ROLLBACK systématique — données inchangées, fichiers conservés.")
            return 0
        # marqueur de campagne écrit dans la même transaction que les données
        _record_campaign(conn, files)
        transaction.commit()
        # la table de staging n'a plus d'utilité une fois la projection commitée
        staging.drop(conn)
        conn.commit()
        line()
        print("  >>> COMMIT effectué : migration validée (lot marqué comme migré). <<<")
        dispose(
            files,
            keep_archives=args.keep_archives,
            source_dir=args.source_dir,
            consumed=migrator.reader.consumed,
        )
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
    except Exception:
        pass


def _run_audit_only(args, engine, files):
    from .etl import fusion_cutoff, fusion_rule

    _ensure_schema(engine)
    with engine.connect() as conn:
        reader = SourceReader(files, args.money_unit, args.chunk_rows, conn=None)
        source = {"brest": 0, "paris": 0}
        raw = {"brest": 0, "paris": 0}
        occurrences = {"brest": {}, "paris": {}}
        users_by_key = {"brest": {}, "paris": {}}
        unmapped = []
        users_count = 0
        for campus, filename, user, _warning, raw_cents in reader.iter_users():
            raw[campus] += raw_cents
            if user is None:
                if raw_cents:
                    unmapped.append((campus, filename, raw_cents))
                continue
            key = user["key"]
            users_by_key[campus].setdefault(key, []).append(user)
            occurrences[campus].setdefault(key, []).append(
                {"src_id": user["src_id"], "name": user["name"], "balance": user["balance_cents"]}
            )
        # même résolution que l'ETL (fusion_rule) : la somme comparée à la
        # cible est celle que le run projeterait, fusions comprises
        cutoff = fusion_cutoff()
        collisions, merged = [], []
        for campus in ("brest", "paris"):
            for key, users in users_by_key[campus].items():
                occ = occurrences[campus][key]
                if len(users) == 1:
                    source[campus] += users[0]["balance_cents"]
                elif not any(u["src_id"] is not None for u in users):
                    # doublons d'une source sans identifiant : soldes cumulés
                    source[campus] += sum(u["balance_cents"] for u in users)
                else:
                    rule = fusion_rule(users, cutoff)
                    if rule is None:
                        # collision non résolue : seule la 1re occurrence est projetée
                        collisions.append((campus, key, occ))
                        source[campus] += users[0]["balance_cents"]
                    else:
                        source[campus] += sum(u["balance_cents"] for u in users)
                        merged.append(
                            {"campus": campus, "key": key, "rule": rule, "occurrences": occ}
                        )
                users_count += 1
        captured = audit.capture_target(conn)
        result = audit.AuditResult(
            source=source,
            raw_source=raw,
            unmapped=unmapped,
            collisions=collisions,
            initial=audit.TargetTotals(),
            final=captured,
        )
        audit.print_audit(
            result,
            extra_counts=[
                ("Comptes sources exploités", users_count),
            ],
        )
        audit.print_merged(merged)
        _print_scan_stats(reader.scan_stats, set(brest_tables()))
        if result.ok:
            print("  >>> AUDIT-ONLY : sommes sources et cibles réconciliées. <<<")
            return 0
        print("  >>> AUDIT-ONLY : écart détecté (voir rapport ci-dessus). <<<")
        return 1


def _run_purge_archives(args):
    section("PURGE DES ARCHIVES DE MIGRATION")
    report.kv("Dossier source", str(args.source_dir))
    report.kv("Rétention", f"{args.purge_archives} jour(s)")
    purge_archives(args.source_dir, args.purge_archives, log=print)
    return 0


def main(argv=None):
    args = parse_args(argv)
    if args.mode == "purge-archives":
        return _run_purge_archives(args)
    try:
        files = detect.scan(args.source_dir)
        url = _resolve_url(args)
        _print_header(args, files, url)
        # hide_parameters : ne jamais recopier les paramètres liés (hashs,
        # mots de passe) dans les messages d'erreur SQLAlchemy (R8).
        engine = create_engine(url, hide_parameters=True)
        if args.mode == "audit-only":
            return _run_audit_only(args, engine, files)
        return _run_migration(args, engine, files)
    except AccountingError as exc:
        line()
        print(f"ERREUR COMPTABLE : {_safe_error(exc)}", file=sys.stderr)
        return 1
    except (MigrationError, SourceError) as exc:
        line()
        print(f"ERREUR MIGRATION : {_safe_error(exc)}", file=sys.stderr)
        return 1
    except Exception as exc:
        line()
        print(f"ERREUR INATTENDUE : {_safe_error(exc)}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
