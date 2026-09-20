"""Import d'une base SQLite (poste de développement) dans la base cible.

Les deux schémas sont produits par les mêmes modèles SQLAlchemy : l'import
recopie les colonnes communes, table par table, dans l'ordre des dépendances,
en convertissant les types propres à SQLite (booléens 0/1, dates texte).

L'opération est atomique : `TRUNCATE` puis insertions se font dans une seule
transaction PostgreSQL. En cas d'échec, la cible est restaurée par ROLLBACK
(aucune donnée partiellement écrite). Les séquences (`id`) sont recalculées
après copie pour que les prochains INSERT auto-incrémentés ne collisionnent pas.

PostgreSQL est plus strict que SQLite (INTEGER 32 bits, VARCHAR(n)) : un
contrôle préalable (`preflight`) refuse l'import si la source contient des
valeurs hors bornes, plutôt que d'échouer au milieu. `--clamp-integers` borne
explicitement les entiers à l'intervalle int32 (dangereux pour les montants :
les valeurs bornées sont signalées).

Usage (généralement via deploy/push-local-data.sh, dans le conteneur `web`
où DATABASE_URL pointe vers le service `db`) :

    python -m ops.import_sqlite /data/import.db --yes
"""

import argparse
import os
import sqlite3
import sys
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, Integer, String, create_engine, text

import app.models  # noqa: F401 — enregistre les tables dans db.metadata
from app.extensions import db

BATCH_ROWS = 5000
INT32_MIN = -2147483648
INT32_MAX = 2147483647


def coerce(value, column_type, clamp=False):
    """Adapte une valeur lue dans SQLite au type Python attendu par PostgreSQL."""
    if value is None:
        return None
    if isinstance(column_type, Boolean):
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "t", "yes", "oui", "vrai"}
        return bool(value)
    if isinstance(column_type, DateTime):
        if isinstance(value, datetime):
            return value
        raw = str(value).strip()
        if not raw:
            return None
        try:
            return datetime.fromisoformat(raw)
        except ValueError:
            return datetime.fromisoformat(raw.replace(" ", "T", 1))
    if isinstance(column_type, Integer):
        number = int(value)
        if clamp:
            return max(INT32_MIN, min(INT32_MAX, number))
        return number
    if isinstance(column_type, Float):
        return float(value)
    if isinstance(value, (bytes, memoryview)):
        return bytes(value)
    return value if isinstance(value, str) else str(value)


def table_columns(sqlite_conn, table_name):
    rows = sqlite_conn.execute(f'PRAGMA table_info("{table_name}")').fetchall()
    return {row[1] for row in rows}


def preflight(source, tables):
    """Détecte les valeurs acceptées par SQLite mais refusées par PostgreSQL.

    Retourne (problemes_entiers, problemes_texte), chaque entrée étant un
    dictionnaire {table, colonne, limite, compte, exemples}.
    """
    int_problems, text_problems = [], []
    for table in tables:
        present = table_columns(source, table.name)
        for column in table.columns:
            if column.name not in present:
                continue
            if isinstance(column.type, Integer):
                query = (
                    f'SELECT COUNT(*), MIN("{column.name}"), MAX("{column.name}") '
                    f'FROM "{table.name}" WHERE "{column.name}" IS NOT NULL '
                    f'AND ("{column.name}" < {INT32_MIN} OR "{column.name}" > {INT32_MAX})'
                )
                count, low, high = source.execute(query).fetchone()
                if count:
                    samples = source.execute(
                        f'SELECT "{column.name}" FROM "{table.name}" '
                        f'WHERE "{column.name}" IS NOT NULL '
                        f'AND ("{column.name}" < {INT32_MIN} OR "{column.name}" > {INT32_MAX}) '
                        f"LIMIT 3"
                    ).fetchall()
                    int_problems.append(
                        {
                            "table": table.name,
                            "column": column.name,
                            "count": count,
                            "range": f"[{low} ; {high}]",
                            "samples": [s[0] for s in samples],
                        }
                    )
            elif isinstance(column.type, String) and column.type.length:
                length = column.type.length
                query = (
                    f'SELECT COUNT(*), MAX(LENGTH("{column.name}")) FROM "{table.name}" '
                    f'WHERE LENGTH("{column.name}") > {length}'
                )
                count, worst = source.execute(query).fetchone()
                if count:
                    text_problems.append(
                        {
                            "table": table.name,
                            "column": column.name,
                            "count": count,
                            "limit": length,
                            "worst": worst,
                        }
                    )
    return int_problems, text_problems


def reset_sequences(conn):
    """Recale les séquences sur le MAX(id) après insertion de clés explicites."""
    for table in db.metadata.sorted_tables:
        if "id" not in table.columns:
            continue
        sequence = conn.execute(
            text("SELECT pg_get_serial_sequence(:t, 'id')"), {"t": table.name}
        ).scalar()
        if not sequence:
            continue
        conn.execute(
            text(
                f"SELECT setval('{sequence}', "
                f'GREATEST((SELECT COALESCE(MAX(id), 1) FROM "{table.name}"), 1), '
                f'(SELECT COUNT(*) > 0 FROM "{table.name}"))'
            )
        )


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog="python -m ops.import_sqlite",
        description="Remplace la base cible par le contenu d'une base SQLite.",
    )
    parser.add_argument("sqlite_path", help="fichier SQLite source (instantané)")
    parser.add_argument(
        "--database-url",
        default=None,
        help="URL SQLAlchemy cible (défaut : DATABASE_URL de l'environnement)",
    )
    parser.add_argument(
        "--batch", type=int, default=BATCH_ROWS, help=f"lignes par lot (défaut {BATCH_ROWS})"
    )
    parser.add_argument(
        "--clamp-integers",
        action="store_true",
        help="borne les entiers hors int32 au lieu de refuser (montants inclus : à éviter)",
    )
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="vérifie la source (types/limites) sans modifier la cible",
    )
    parser.add_argument("--yes", action="store_true", help="confirme l'écrasement de la cible")
    args = parser.parse_args(argv)
    if not args.yes and not args.check_only:
        parser.error("ajoutez --yes (l'import écrase la base cible).")
    args.database_url = args.database_url or os.environ.get("DATABASE_URL")
    if not args.database_url and not args.check_only:
        parser.error("DATABASE_URL ou --database-url est requis.")
    return args


def main(argv=None):
    args = parse_args(argv)

    source = sqlite3.connect(f"file:{args.sqlite_path}?mode=ro", uri=True)
    present = {
        row[0] for row in source.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    tables = [t for t in db.metadata.sorted_tables if t.name in present]
    missing = [t.name for t in db.metadata.sorted_tables if t.name not in present]
    if not tables:
        source.close()
        print("Aucune table applicative trouvée dans la source.", file=sys.stderr)
        return 1

    print(f"=== Import SQLite -> cible ({args.sqlite_path}) ===")
    int_problems, text_problems = preflight(source, tables)
    if text_problems:
        print("Refusé : chaînes trop longues pour les VARCHAR cibles :", file=sys.stderr)
        for p in text_problems:
            print(
                f"  {p['table']}.{p['column']}: {p['count']} valeur(s) "
                f"(max {p['worst']}, limite {p['limit']})",
                file=sys.stderr,
            )
        source.close()
        return 1
    if int_problems:
        details = "; ".join(
            f"{p['table']}.{p['column']} ({p['count']}, {p['range']})" for p in int_problems
        )
        if not args.clamp_integers:
            print(
                "Refusé : entiers hors int32 dans la source "
                f"(PostgreSQL INTEGER = ±2 147 483 647) :\n  {details}\n"
                "Corrigez ces valeurs en local, ou relancez avec --clamp-integers "
                "(bornage tracé ci-dessous).",
                file=sys.stderr,
            )
            source.close()
            return 1
        print("ATTENTION : --clamp-integers, valeurs bornées à int32 :", file=sys.stderr)
        for p in int_problems:
            print(
                f"  {p['table']}.{p['column']}: {p['count']} valeur(s) {p['range']} "
                f"-> ex. {p['samples']}",
                file=sys.stderr,
            )

    if args.check_only:
        print("  Source compatible avec la cible : aucun dépassement bloquant.")
        source.close()
        return 0

    engine = create_engine(args.database_url, hide_parameters=True)
    counts = {}
    try:
        with engine.begin() as conn:
            names = ", ".join(f'"{t.name}"' for t in db.metadata.sorted_tables)
            conn.execute(text(f"TRUNCATE {names} RESTART IDENTITY CASCADE"))
            for table in tables:
                common = [c for c in table.columns if c.name in table_columns(source, table.name)]
                columns = ", ".join(f'"{c.name}"' for c in common)
                cursor = source.execute(f'SELECT {columns} FROM "{table.name}"')
                copied = 0
                while True:
                    rows = cursor.fetchmany(args.batch)
                    if not rows:
                        break
                    payload = [
                        {
                            c.name: coerce(value, c.type, clamp=args.clamp_integers)
                            for c, value in zip(common, row, strict=True)
                        }
                        for row in rows
                    ]
                    conn.execute(table.insert(), payload)
                    copied += len(rows)
                counts[table.name] = copied
            reset_sequences(conn)
    except Exception as exc:  # ROLLBACK global puis message lisible
        print(f"ÉCHEC, ROLLBACK (cible inchangée) : {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        source.close()

    for name in sorted(counts):
        print(f"  {name:<20} {counts[name]:>10} lignes")
    if missing:
        print(f"  (tables absentes de la source, vidées : {', '.join(missing)})")
    print(f"  {'TOTAL':<20} {sum(counts.values()):>10} lignes")
    print("  >>> Import terminé : base cible remplacée. <<<")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
