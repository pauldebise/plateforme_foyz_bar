"""Tests d'exploitation (phase 6) : sauvegardes, rétention, archives, Alembic.

Exécutable sans pytest : python -m tests.test_ops

Couvre :
- T-6.1 : promotion hebdomadaire, rétention 7/4, mode `--prune-only`, liste et
  refus de restauration sans confirmation ;
- T-6.2 : destruction des archives de migration au-delà de la rétention ;
- T-6.4 : `alembic upgrade head` crée le schéma, `downgrade base` le retire,
  application idempotente, révision de tête stable.
"""

import os
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from migration.cleaner import purge_archives  # noqa: E402
from migration.cli import main as migration_main  # noqa: E402
from ops import backup, restore  # noqa: E402
from app.schema import head_revision  # noqa: E402


def _expect(cond, label):
    if not cond:
        raise AssertionError(f"échec : {label}")


def _tmpdir():
    return Path(tempfile.mkdtemp(prefix="foyz_ops_"))


def _write(directory, name, content=b"x"):
    path = Path(directory) / name
    path.write_bytes(content)
    return path


def _stamp(day):
    return (datetime(2026, 9, 1, tzinfo=timezone.utc) + timedelta(days=day)).strftime(
        backup.STAMP_FORMAT
    )


def _quiet(*_args, **_kwargs):
    pass


def test_backup_retention_and_weekly_promotion():
    directory = _tmpdir()
    for day in range(1, 11):
        _write(directory, f"foyz-{_stamp(day)}.dump")
        _write(directory, f"uploads-{_stamp(day)}.tgz")
    _write(directory, "a-lire.txt", b"ignore")

    promoted = backup.promote_weekly(directory, log=_quiet)
    _expect(len(promoted) == 2, f"2 liens hebdomadaires créés ({len(promoted)})")
    _expect((directory / "weekly" / f"foyz-{_stamp(10)}.dump").exists(),
            "dernière copie promue en hebdomadaire")

    removed = backup.prune(directory, daily=7, weekly=4, log=_quiet)
    remaining = sorted(p.name for p in directory.iterdir() if p.is_file())
    _expect(len([n for n in remaining if n.startswith("foyz-")]) == 7,
            "7 dumps quotidiens conservés")
    _expect(len([n for n in remaining if n.startswith("uploads-")]) == 7,
            "7 archives uploads conservées")
    _expect((directory / "a-lire.txt").exists(), "fichier étranger jamais touché")
    _expect(all("weekly" not in str(p) for p in removed), "weekly non purgé")
    _expect(len(list((directory / "weekly").iterdir())) == 2, "2 hebdomadaires conservés")

    backup.prune(directory, daily=1, weekly=1, log=_quiet)
    _expect(len(list((directory / "weekly").iterdir())) == 1, "rétention hebdomadaire appliquée")


def test_backup_cli_prune_only_and_dry_run():
    directory = _tmpdir()
    for day in range(1, 10):
        _write(directory, f"foyz-{_stamp(day)}.dump")
        _write(directory, f"uploads-{_stamp(day)}.tgz")

    rc = backup.main(["--backup-dir", str(directory), "--prune-only",
                      "--retention-daily", "3", "--retention-weekly", "0", "--dry-run"])
    _expect(rc == 0, "mode prune-only en dry-run")
    _expect(len(list(directory.iterdir())) == 18, "dry-run ne supprime rien")

    rc = backup.main(["--backup-dir", str(directory), "--prune-only",
                      "--retention-daily", "3", "--retention-weekly", "0"])
    _expect(rc == 0, "mode prune-only réel")
    _expect(len(list(directory.iterdir())) == 6, "3 paires conservées")


def test_restore_listing_and_confirmation():
    directory = _tmpdir()
    _write(directory, f"foyz-{_stamp(1)}.dump", b"a")
    _write(directory, f"uploads-{_stamp(1)}.tgz", b"a")
    dump = _write(directory, f"foyz-{_stamp(2)}.dump", b"b")
    _write(directory, f"uploads-{_stamp(2)}.tgz", b"b")

    stamps = restore.list_backups(directory, log=_quiet)
    _expect(len(stamps) == 2, "deux sauvegardes listées")

    resolved_dump, resolved_archive = restore.resolve_pair(directory)
    _expect(resolved_dump.name == dump.name, "la plus récente sélectionnée")
    _expect(resolved_archive is not None, "archive uploads associée")

    rc = restore.main(["--backup-dir", str(directory), "--latest",
                       "--pg-container", "faux", "--dry-run"])
    _expect(rc == 0, "dry-run accepté sans --yes")

    rc = restore.main(["--backup-dir", str(directory), "--latest",
                       "--pg-container", "faux"])
    _expect(rc == 1, "restauration refusée sans --yes")


def test_purge_archives_and_cli():
    source = _tmpdir()
    archives = source / "archives"
    old = archives / "20260101-000000"
    recent = archives / "20260916-084833"
    for directory in (old, recent):
        directory.mkdir(parents=True)
        (directory / "brest.sql").write_bytes(b"donnees personnelles")
    stray = archives / "notes-diverses"
    stray.mkdir()
    (stray / "lisez-moi.txt").write_text("à conserver", encoding="utf-8")

    removed = purge_archives(source, 30, now=datetime(2026, 9, 17, tzinfo=timezone.utc),
                             log=_quiet)
    _expect(str(old) in removed, "archive ancienne détruite")
    _expect(not old.exists(), "archive ancienne absente du disque")
    _expect(recent.exists(), "archive récente conservée")
    _expect(stray.exists(), "dossier non horodaté conservé")

    rc = migration_main(["--purge-archives", "30", "--source-dir", str(source),
                         "--database-url", f"sqlite:///{source / 'cible.db'}"])
    _expect(rc == 0, "mode CLI purge-archives")
    _expect(recent.exists(), "archive récente toujours là via CLI")

    old2 = archives / "20200101-000000"
    old2.mkdir()
    purge_archives(source, 30, dry_run=True,
                   now=datetime(2026, 9, 17, tzinfo=timezone.utc), log=_quiet)
    _expect(old2.exists(), "simulation n'efface rien")


def test_alembic_upgrade_downgrade_cycle():
    target = _tmpdir() / "schema.db"
    env = {**os.environ, "DATABASE_URL": f"sqlite:///{target}"}

    def alembic(*args):
        return subprocess.run(
            [sys.executable, "-m", "alembic", *args],
            cwd=ROOT, env=env, capture_output=True, text=True,
        )

    _expect(head_revision() == "0001_baseline", "révision de tête stable")

    result = alembic("upgrade", "head")
    _expect(result.returncode == 0, f"upgrade head ({result.stderr[-200:]})")
    with sqlite3.connect(target) as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        version = conn.execute("SELECT version_num FROM alembic_version").fetchone()
    _expect("users" in tables and "transactions" in tables, "schéma créé par Alembic")
    _expect(version == ("0001_baseline",), "version enregistrée")

    result = alembic("upgrade", "head")
    _expect(result.returncode == 0, "upgrade head idempotent")

    result = alembic("downgrade", "base")
    _expect(result.returncode == 0, f"downgrade base ({result.stderr[-200:]})")
    with sqlite3.connect(target) as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    _expect("users" not in tables, "tables retirées par downgrade")
    _expect("alembic_version" in tables, "suivi conservé")

    result = alembic("upgrade", "head")
    _expect(result.returncode == 0, "ré-application après downgrade")


def main():
    tests = [(name, fn) for name, fn in sorted(globals().items())
             if name.startswith("test_") and callable(fn)]
    failures = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  OK   {name}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"  FAIL {name}: {exc}")
    print(f"\n{len(tests) - failures}/{len(tests)} tests OK")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
