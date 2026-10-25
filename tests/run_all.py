"""Exécution de toutes les suites de tests.

    python -m tests.run_all                  # SQLite (toutes les suites)
    python -m tests.run_all --postgres URL   # PostgreSQL 16 (caisse, API, auth)
    python -m tests.run_all suite [suite…]   # sous-ensemble (noms de modules)
    python -m tests.run_all --jobs 8         # 8 suites en parallèle (défaut : min(8, CPU))

Chaque suite tourne dans un processus séparé, avec sa propre base. En mode
PostgreSQL, le schéma public est recréé puis migré (alembic upgrade head)
avant chaque suite : la base cible est jetable, jamais une base réelle.

Parallélisme (--jobs N) : les suites tournent par lots de N processus. En mode
PostgreSQL, chaque emplacement utilise sa propre base (<base>_w0, <base>_w1…),
créée au besoin. Les suites sensibles au temps (charge, performance,
concurrence) tournent seules, après les autres, pour ne pas fausser leurs
mesures. `--jobs 1` rétablit l'exécution séquentielle.
"""

import argparse
import os
import queue
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SQLITE_SUITES = [
    "tests.test_security",
    "tests.test_identifiers",
    "tests.test_caisse",
    "tests.test_caisse_evenement",
    "tests.test_recap_evenement",
    "tests.test_gateway",
    "tests.test_api_externe",
    "tests.test_securite_caisse",
    "tests.test_connexion_journaux",
    "tests.test_integrity",
    "tests.test_concurrency",
    "tests.test_hardening",
    "tests.test_ux",
    "tests.test_monitoring",
    "tests.test_audit",
    "tests.test_sante",
    "tests.test_comptes_filtres",
    "tests.test_articles_filtres",
    "tests.test_noms_seuls",
    "tests.test_limites_champs",
    "tests.test_tresorerie",
    "tests.test_recherche_accents",
    "tests.test_catalogue_public",
    "tests.test_passwords",
    "tests.test_pwa",
    "tests.test_i18n",
    "tests.test_charge",
    "tests.test_performance",
    "tests.migration.test_migration",
    "tests.test_ops",
]

# Suites non portables sur PostgreSQL par construction : elles vérifient des
# mécanismes SQLite (schéma hérité, migrations de fichiers sources) ou mesurent
# des volumétries de fichier local.
POSTGRES_SUITES = [
    "tests.test_caisse",
    "tests.test_caisse_evenement",
    "tests.test_recap_evenement",
    "tests.test_gateway",
    "tests.test_api_externe",
    "tests.test_securite_caisse",
    "tests.test_connexion_journaux",
    "tests.test_integrity",
    "tests.test_concurrency",
    "tests.test_hardening",
    "tests.test_ux",
    "tests.test_monitoring",
    "tests.test_audit",
    "tests.test_sante",
    "tests.test_comptes_filtres",
    "tests.test_articles_filtres",
    "tests.test_noms_seuls",
    "tests.test_limites_champs",
    "tests.test_tresorerie",
    "tests.test_recherche_accents",
    "tests.test_catalogue_public",
    "tests.test_passwords",
    "tests.test_pwa",
    "tests.test_i18n",
    "tests.test_charge",
    "tests.test_security",
    "tests.test_identifiers",
]


# Mesures de temps ou de concurrence : exécutées seules.
SERIAL_SUITES = {"tests.test_charge", "tests.test_performance", "tests.test_concurrency"}


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


def _slot_url(url, slot):
    """URL de la base dédiée à un emplacement ; la crée si nécessaire."""
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url

    base = make_url(url)
    target = base.set(database=f"{base.database}_w{slot}")
    admin = create_engine(base, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        exists = conn.execute(
            text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": target.database}
        ).scalar()
        if not exists:
            conn.execute(text(f'CREATE DATABASE "{target.database}"'))
    admin.dispose()
    return target.render_as_string(hide_password=False)


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
    parser.add_argument(
        "--jobs",
        "-j",
        type=int,
        default=min(8, os.cpu_count() or 4),
        help="suites en parallèle (défaut : min(8, CPU) ; 1 = séquentiel)",
    )
    args = parser.parse_args(argv)

    if args.postgres:
        suites = args.suites or POSTGRES_SUITES
        mode = f"PostgreSQL ({args.postgres})"
    else:
        suites = args.suites or SQLITE_SUITES
        mode = "SQLite"
    print(f"Mode {mode} — {len(suites)} suite(s)\n")

    jobs = max(1, args.jobs)
    failures = []
    out_lock = threading.Lock()
    slots = queue.Queue()
    for i in range(jobs):
        slots.put(i)

    def run_suite(module):
        slot = slots.get()
        try:
            return _run_suite(module, slot)
        finally:
            slots.put(slot)

    def _run_suite(module, slot):
        extra = {}
        report = []
        ok = True
        if args.postgres:
            url = args.postgres if jobs == 1 else None
            try:
                url = url or _slot_url(args.postgres, slot)
                _reset_postgres(url)
            except Exception as exc:
                return False, [f"  ERREUR {module}: préparation PostgreSQL impossible ({exc})"]
            migration = _migrate_postgres(url)
            if migration.returncode != 0:
                return False, [
                    f"  ERREUR {module}: alembic upgrade head a échoué",
                    migration.stdout + migration.stderr,
                ]
            extra["FOYZ_TEST_DATABASE_URL"] = url
        res = _run(module, extra)
        report.append(f"  {module:32} {_last_line(res.stdout)}")
        if res.returncode != 0:
            ok = False
            report.append(res.stdout)
            if res.stderr.strip():
                report.append(res.stderr)
        return ok, report

    def record(module, result):
        ok, report = result
        with out_lock:
            print("\n".join(report), flush=True)
        if not ok:
            failures.append(module)

    parallel = [m for m in suites if jobs == 1 or m not in SERIAL_SUITES]
    serial = [m for m in suites if m not in parallel]
    with ThreadPoolExecutor(max_workers=jobs) as pool:
        for module, result in zip(parallel, pool.map(run_suite, parallel), strict=True):
            record(module, result)
    for module in serial:
        record(module, _run_suite(module, 0))

    print()
    if failures:
        print(f"ÉCHEC : {len(failures)} suite(s) en erreur ({', '.join(failures)})")
        return 1
    print(f"OK : {len(suites)} suite(s) vertes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
