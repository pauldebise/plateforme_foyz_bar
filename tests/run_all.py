"""Exécution de toutes les suites de tests.

    python -m tests.run_all                  # SQLite (toutes les suites)
    python -m tests.run_all --postgres URL   # PostgreSQL 16 (caisse, API, auth)
    python -m tests.run_all suite [suite…]   # sous-ensemble (noms de modules)

Chaque suite tourne dans un processus séparé, avec sa propre base. En mode
PostgreSQL, le schéma public est recréé puis migré (alembic upgrade head)
avant chaque suite : la base cible est jetable, jamais une base réelle.
"""

import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SQLITE_SUITES = [
    "tests.test_security",
    "tests.test_identifiers",
    "tests.test_caisse",
    "tests.test_gateway",
    "tests.test_integrity",
    "tests.test_concurrency",
    "tests.test_hardening",
    "tests.test_ux",
    "tests.test_monitoring",
    "tests.test_performance",
    "tests.migration.test_migration",
    "tests.test_ops",
]

# Suites non portables sur PostgreSQL par construction : elles vérifient des
# mécanismes SQLite (schéma hérité, migrations de fichiers sources) ou mesurent
# des volumétries de fichier local.
POSTGRES_SUITES = [
    "tests.test_caisse",
    "tests.test_gateway",
    "tests.test_integrity",
    "tests.test_concurrency",
    "tests.test_hardening",
    "tests.test_ux",
    "tests.test_monitoring",
    "tests.test_security",
    "tests.test_identifiers",
]


def _run(module, extra_env):
    env = {**os.environ, **extra_env}
    res = subprocess.run(
        [sys.executable, "-m", module],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    return res


def _last_line(text):
    lines = [ln for ln in text.splitlines() if ln.strip()]
    return lines[-1] if lines else "(aucune sortie)"


def _reset_postgres(url):
    from sqlalchemy import create_engine, text

    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    engine.dispose()


def _migrate_postgres(url):
    return subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=ROOT,
        env={**os.environ, "DATABASE_URL": url},
        capture_output=True,
        text=True,
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description="Suites de tests Foy'z & Bar")
    parser.add_argument("suites", nargs="*", help="modules à exécuter (défaut : tous)")
    parser.add_argument("--postgres", metavar="URL", help="base PostgreSQL de test jetable")
    args = parser.parse_args(argv)

    if args.postgres:
        suites = args.suites or POSTGRES_SUITES
        mode = f"PostgreSQL ({args.postgres})"
    else:
        suites = args.suites or SQLITE_SUITES
        mode = "SQLite"
    print(f"Mode {mode} — {len(suites)} suite(s)\n")

    failures = []
    for module in suites:
        extra = {}
        if args.postgres:
            try:
                _reset_postgres(args.postgres)
            except Exception as exc:
                print(f"  ERREUR {module}: préparation PostgreSQL impossible ({exc})")
                failures.append(module)
                continue
            migration = _migrate_postgres(args.postgres)
            if migration.returncode != 0:
                print(f"  ERREUR {module}: alembic upgrade head a échoué")
                print(migration.stdout + migration.stderr)
                failures.append(module)
                continue
            extra["FOYZ_TEST_DATABASE_URL"] = args.postgres
        res = _run(module, extra)
        print(f"  {module:32} {_last_line(res.stdout)}")
        if res.returncode != 0:
            failures.append(module)
            print(res.stdout)
            if res.stderr.strip():
                print(res.stderr)

    print()
    if failures:
        print(f"ÉCHEC : {len(failures)} suite(s) en erreur ({', '.join(failures)})")
        return 1
    print(f"OK : {len(suites)} suite(s) vertes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
