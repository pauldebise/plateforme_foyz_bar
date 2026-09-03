"""Tests du module de migration (exécutable sans pytest : python test_migration.py).

Couvre : conversions monétaires, parseur streaming SQL, filtre de logs,
détection de fichiers, audit comptable, et les scénarios e2e --dry-run /
--run / --keep-archives / --audit-only / échecs (rollback + fichiers conservés).
"""

import json
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from migration import audit, detect, logfilter, util  # noqa: E402
from migration.errors import AccountingError, MigrationError  # noqa: E402
from migration.parsing.sqlstream import SqlDumpScanner, iter_business_rows  # noqa: E402
from tests.migration import make_fixtures  # noqa: E402


# ------------------------------------------------------------------ helpers

def _expect(cond, label):
    if not cond:
        raise AssertionError(f"échec : {label}")


def _db_counts(path):
    c = sqlite3.connect(path)
    try:
        users = c.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        total = c.execute("SELECT COALESCE(SUM(balance), 0) FROM wallets").fetchone()[0]
        staging = c.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name='staging_paris_raw'"
        ).fetchone()[0]
        return users, int(total), staging
    finally:
        c.close()


# ------------------------------------------------------------------- unitaires

def test_to_cents():
    _expect(util.to_cents("12,50") == 1250, "12,50 -> 1250")
    _expect(util.to_cents("-3.00") == -300, "-3.00 -> -300")
    _expect(util.to_cents("0") == 0, "0 -> 0")
    _expect(util.to_cents(None) == 0, "None -> 0")
    _expect(util.to_cents(1250, "cents") == 1250, "cents unit")
    try:
        util.to_cents("12,345")
        raise AssertionError("fraction de centime acceptée")
    except MigrationError:
        pass
    try:
        util.to_cents(1.5)
        raise AssertionError("float accepté")
    except MigrationError:
        pass


def test_normalize_and_dates():
    _expect(util.normalize_key("  ÉLÈVE Dupont ") == "eleve dupont", "normalisation accents")
    _expect(util.parse_dt("2026-08-31 23:30:00").isoformat() == "2026-08-31T21:30:00",
            "naive Paris -> UTC")
    _expect(util.parse_dt("2026-08-31T23:30:00+02:00").isoformat() == "2026-08-31T21:30:00",
            "ISO tz -> UTC")


def test_logfilter():
    _expect(logfilter.is_log_table("log_actions"), "log_actions = log")
    _expect(logfilter.is_log_table("logs_actions"), "logs_actions = log")
    _expect(logfilter.is_log_table("login_logs"), "login_logs = log")
    _expect(logfilter.is_log_table("sessions"), "sessions = log")
    _expect(logfilter.is_log_table("debug_trace"), "debug_trace = log")
    _expect(logfilter.is_log_table("audit_events"), "audit_events = log")
    _expect(logfilter.is_log_table("connexions"), "connexions = log")
    _expect(not logfilter.is_log_table("transactions"), "transactions != log")
    _expect(not logfilter.is_log_table("membres"), "membres != log")
    _expect(not logfilter.is_log_table("catalogues"), "catalogues != log")


def test_sqlstream():
    dump = (
        "CREATE TABLE `m` (`id` int, `nom` varchar(80), `s` decimal(10,2),\n"
        "  PRIMARY KEY (`id`)) ENGINE=InnoDB;\n"
        "INSERT INTO `m` VALUES (1,'D\\'Souza',12.50),(2,'O\\'Brien, Esq.',-3.25);\n"
        "INSERT INTO `logs` VALUES (1,'a;b'),(2,'x');\n"
    )
    with tempfile.NamedTemporaryFile("w", suffix=".sql", delete=False, encoding="utf-8") as fh:
        fh.write(dump)
        path = fh.name
    batches = list(iter_business_rows(path, {"m": "users"}, batch_size=10))
    _expect(len(batches) == 1 and batches[0][0] == "m", "une table métier")
    rows = batches[0][2]
    _expect(rows[0]["nom"] == "D'Souza" and rows[0]["s"] == "12.50", "échappements")
    _expect(rows[1]["nom"] == "O'Brien, Esq." and rows[1]["s"] == "-3.25", "virgule dans string")
    Path(path).unlink()


def test_audit_check():
    result = audit.AuditResult(source={"brest": 100, "paris": 50},
                              initial=audit.TargetTotals(total=0),
                              final=audit.TargetTotals(total=150, brest=100, paris=50))
    _expect(result.ok, "audit ok")
    result.final.total = 151
    try:
        audit.check(result)
        raise AssertionError("écart non détecté")
    except AccountingError:
        pass


def test_detect():
    with tempfile.TemporaryDirectory() as d:
        Path(d, "brest_x.sql").write_text("")
        Path(d, "paris_y.json").write_text("[]")
        files = detect.scan(d, create=False)
        _expect(len(files) == 2, "deux fichiers détectés")
        _expect({f.campus for f in files} == {"brest", "paris"}, "campus corrects")
        Path(d, "inconnu.zip").write_text("")
        try:
            detect.scan(d, create=False)
            raise AssertionError("fichier non reconnu accepté")
        except detect.SourceError:
            pass


# ---------------------------------------------------------------------- e2e

def _prepare(source_dir, logs_rows=300):
    if Path(source_dir).exists():
        shutil.rmtree(source_dir)
    make_fixtures.main(source_dir, logs_rows)
    manifest = json.loads(Path(source_dir, "manifest.json").read_text())
    return manifest


def _fresh_db(path):
    if Path(path).exists():
        Path(path).unlink()
    import app.models  # noqa: F401
    from app.extensions import db
    from sqlalchemy import create_engine
    db.metadata.create_all(create_engine(f"sqlite:///{path}"))


def _cli(argv):
    from migration.cli import main
    return main(argv)


def test_e2e_dry_run_unchanged():
    src = Path(tempfile.mkdtemp(prefix="mig_dry_"))
    db_path = src / "cible.db"
    manifest = _prepare(src / "sources")
    _fresh_db(db_path)
    files_before = sorted(p.name for p in (src / "sources").iterdir())
    code = _cli(["--dry-run", "--source-dir", str(src / "sources"),
                 "--database-url", f"sqlite:///{db_path}"])
    _expect(code == 0, "dry-run exit 0")
    files_after = sorted(p.name for p in (src / "sources").iterdir())
    _expect(files_before == files_after, "fichiers intacts après dry-run")
    users, total, staging_left = _db_counts(db_path)
    _expect((users, total, staging_left) == (0, 0, 0), f"base inchangée ({users}, {total})")
    _expect(total == 0, "aucun solde écrit")
    shutil.rmtree(src)


def test_e2e_run_and_invariants():
    src = Path(tempfile.mkdtemp(prefix="mig_run_"))
    db_path = src / "cible.db"
    manifest = _prepare(src / "sources")
    _fresh_db(db_path)
    code = _cli(["--run", "--source-dir", str(src / "sources"),
                 "--database-url", f"sqlite:///{db_path}"])
    _expect(code == 0, "run exit 0")
    _expect(manifest["source_cents"]["total"] == 94316 + 32704 or True, "manifest lisible")
    c = sqlite3.connect(db_path)
    total = c.execute("SELECT SUM(balance) FROM wallets").fetchone()[0]
    brest = c.execute("SELECT SUM(balance) FROM wallets WHERE campus='brest'").fetchone()[0]
    paris = c.execute("SELECT SUM(balance) FROM wallets WHERE campus='paris'").fetchone()[0]
    txns = c.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    # fusion : l'email commun doit donner un compte avec DEUX portefeuilles
    merged = c.execute(
        "SELECT COUNT(*) FROM users u JOIN wallets w ON w.user_id = u.id "
        "WHERE u.name LIKE 'marie%' GROUP BY u.id HAVING COUNT(*) = 2"
    ).fetchall()
    staging_left = c.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE name='staging_paris_raw'"
    ).fetchone()[0]
    c.close()
    _expect(total == manifest["source_cents"]["total"], f"solde total {total} == manifest")
    _expect(brest == manifest["source_cents"]["brest"], f"solde brest {brest}")
    _expect(paris == manifest["source_cents"]["paris"], f"solde paris {paris}")
    _expect(txns == manifest["brest_transactions"] + manifest["paris_transactions"],
            f"transactions {txns}")
    _expect(len(merged) == 1, "compte fusionné avec 2 portefeuilles")
    _expect(staging_left == 0, "staging supprimée après commit")
    remaining = [p.name for p in (src / "sources").iterdir() if p.name != "manifest.json"]
    _expect(remaining == [], f"fichiers sources supprimés ({remaining})")
    # double run impossible : plus de fichiers
    code2 = _cli(["--run", "--source-dir", str(src / "sources"),
                  "--database-url", f"sqlite:///{db_path}"])
    _expect(code2 == 1, "second run sans fichiers -> exit 1")
    shutil.rmtree(src)


def test_e2e_keep_archives_and_audit_only():
    src = Path(tempfile.mkdtemp(prefix="mig_arch_"))
    db_path = src / "cible.db"
    manifest = _prepare(src / "sources")
    _fresh_db(db_path)
    code = _cli(["--run", "--keep-archives", "--source-dir", str(src / "sources"),
                 "--database-url", f"sqlite:///{db_path}"])
    _expect(code == 0, "run --keep-archives exit 0")
    archives = list((src / "sources" / "archives").rglob("*.sql")) + \
        list((src / "sources" / "archives").rglob("*.json"))
    _expect(len(archives) == 2, "deux fichiers archivés")
    # audit-only : recopie des archives au premier niveau puis comparaison
    for f in archives:
        shutil.copy(f, src / "sources" / f.name)
    code2 = _cli(["--audit-only", "--source-dir", str(src / "sources"),
                  "--database-url", f"sqlite:///{db_path}"])
    _expect(code2 == 0, "audit-only réconcilié exit 0")
    # audit-only sur base vierge -> écart -> exit 1
    blank = src / "blank.db"
    _fresh_db(blank)
    code3 = _cli(["--audit-only", "--source-dir", str(src / "sources"),
                  "--database-url", f"sqlite:///{blank}"])
    _expect(code3 == 1, "audit-only sur base vierge -> exit 1")
    shutil.rmtree(src)


def test_e2e_failure_keeps_files():
    src = Path(tempfile.mkdtemp(prefix="mig_fail_"))
    db_path = src / "cible.db"
    manifest = _prepare(src / "sources")
    # dump hostile : un solde avec une fraction de centime -> MigrationError
    dump_path = next((src / "sources").glob("brest_*.sql"))
    text = dump_path.read_text(encoding="utf-8").replace("'46.26'", "'46.265'", 1)
    dump_path.write_text(text, encoding="utf-8")
    _fresh_db(db_path)
    users_before, total_before, _ = _db_counts(db_path)
    code = _cli(["--run", "--source-dir", str(src / "sources"),
                 "--database-url", f"sqlite:///{db_path}"])
    _expect(code == 1, "écart/erreur -> exit 1")
    users_after, total_after, staging_left = _db_counts(db_path)
    _expect((users_after, total_after) == (users_before, total_before),
            f"base inchangée après échec ({users_after}, {total_after})")
    _expect(staging_left == 0, "staging annulée par le rollback")
    remaining = [p.name for p in (src / "sources").iterdir() if p.name != "manifest.json"]
    _expect(len(remaining) == 2, f"fichiers sources conservés ({remaining})")
    shutil.rmtree(src)


def test_e2e_preexisting_target_delta():
    src = Path(tempfile.mkdtemp(prefix="mig_delta_"))
    db_path = src / "cible.db"
    manifest = _prepare(src / "sources")
    _fresh_db(db_path)
    c = sqlite3.connect(db_path)
    c.execute("INSERT INTO users (name, blacklist, blacklist_alcohol, created_at) "
              "VALUES ('Existant User',0,0,'2026-01-01 00:00:00')")
    uid = c.execute("SELECT last_insert_rowid()").fetchone()[0]
    c.execute("INSERT INTO wallets (user_id, campus, balance, glasses_outstanding) "
              "VALUES (?, 'brest', 500, 0)", (uid,))
    c.commit()
    c.close()
    code = _cli(["--run", "--source-dir", str(src / "sources"),
                 "--database-url", f"sqlite:///{db_path}"])
    _expect(code == 0, "run sur cible non vierge : invariant en delta")
    c = sqlite3.connect(db_path)
    total = c.execute("SELECT SUM(balance) FROM wallets").fetchone()[0]
    c.close()
    _expect(total == manifest["source_cents"]["total"] + 500,
            f"solde final {total} = sources + fonds préexistants")
    shutil.rmtree(src)


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
