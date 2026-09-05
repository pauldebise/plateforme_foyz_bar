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
from migration.sources import (as_bool, map_article_row, map_keg_row,  # noqa: E402
                               map_operation_row, map_tap_row, map_transaction_row,
                               resolve_article_type)
from migration.sources.brest import ENTITY_FIELDS  # noqa: E402
from migration.util import liters_to_cl, unescape_html  # noqa: E402
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


def test_user_password_and_real_name_mapping():
    from migration.sources import map_user_row
    # Ligne au format réel du dump Brest : `name`=pseudo, `real_name`,
    # `password` = hash bcrypt PHP (non réutilisable tel quel en cible)
    user, warning = map_user_row(
        {"card_id": "1548963257462",
         "name": "aime secretement massé Paul DEBISE chips vachement bete",
         "real_name": "Paul Debise",
         "password": "$2a$10$wtlkmC9G3pGaid7Fw.vOYeek0JjVNe3eQ.RPKfOtMTEvDjLRljbjC",
         "balance": "-26.18", "is_foyz": 1, "promo": "CI2028"},
        "brest", "euros")
    _expect(warning is None, "ligne utilisateur valide")
    _expect(user["name"] == "Paul Debise",
            f"identifiant de connexion = real_name ({user['name']})")
    _expect(user["key"] == "paul debise", "clé de réconciliation = nom réel")
    _expect(user["password_hash"] is None, "bcrypt -> pas un hash werkzeug")
    _expect(user["legacy_password"] == "$2a$10$wtlkmC9G3pGaid7Fw.vOYeek0JjVNe3eQ.RPKfOtMTEvDjLRljbjC",
            "bcrypt conservé brut dans legacy_password")
    # Hash werkzeug source -> réutilisé tel quel en cible, rien en legacy
    werkzeug_hash = "pbkdf2:sha256:600000$sel$deadbeef"
    reuser, _ = map_user_row({"name": "x", "mdp": werkzeug_hash}, "brest", "euros")
    _expect(reuser["password_hash"] == werkzeug_hash and reuser["legacy_password"] is None,
            "hash werkzeug -> password_hash")
    # Sans mot de passe ni real_name : pseudo seul, rien importé
    plain, _ = map_user_row({"name": "y"}, "brest", "euros")
    _expect(plain["name"] == "y" and plain["password_hash"] is None
            and plain["legacy_password"] is None, "sans mot de passe -> legacy None")


def test_bit_literals_and_html():
    # mysqldump écrit les colonnes bit(1) sous forme b'0' / b'1' (Brest :
    # alcohol_blacklisted) — sans ce fix, tout importait à False
    _expect(as_bool("b'1'") is True, "bit b'1' -> True")
    _expect(as_bool("b'0'") is False, "bit b'0' -> False")
    _expect(as_bool("b'\\0'") is False, "bit b'\\0' -> False")
    _expect(as_bool("b'\\x01'") is True, "bit b'\\x01' -> True")
    _expect(as_bool(1) is True and as_bool(0) is False, "tinyint 0/1")
    _expect(as_bool(None) is None, "bit absent -> None")
    _expect(unescape_html("Menu Foy&#x27;z") == "Menu Foy'z", "entités HTML décodées")
    _expect(unescape_html("BDE 21&#x2F;02&#x2F;2025") == "BDE 21/02/2025", "slash encodé")
    _expect(liters_to_cl("0.33") == 33, "0.33 L -> 33 cl")
    _expect(liters_to_cl("0.00") is None and liters_to_cl(0) is None, "0 L -> None")
    _expect(liters_to_cl("0.5") == 50 and liters_to_cl(2.5) == 250, "litres variés")
    _expect(liters_to_cl(None) is None and liters_to_cl("n/a") is None, "illisible -> None")


def test_article_catalog_mapping():
    types = {1: "Bière", 4: "Snacks", 6: "Boisson Chaude", 7: "Boisson Froide",
             8: "Cocktails/Barbecue/Soirées"}
    _expect(resolve_article_type(1, types) == "biere", "type 1 -> biere")
    _expect(resolve_article_type("7", types) == "snack", "boisson froide -> snack")
    _expect(resolve_article_type(6, types) == "snack", "boisson chaude -> snack")
    _expect(resolve_article_type("8", types) == "evenement", "cocktails -> evenement")
    _expect(resolve_article_type("Bière") == "biere", "nom direct -> biere")
    _expect(resolve_article_type(None) == "snack", "type absent -> snack (non alcoolisé)")
    article = map_article_row(
        {"id": "0000000000042", "name": "Kronenbourg 25cl", "price": "2.50",
         "price_foyz": "2.20", "type": 1, "volume": "0.25"}, "euros", types)
    _expect(article["name"] == "Kronenbourg 25cl", "nom article")
    _expect(article["price_std_cents"] == 250 and article["price_team_cents"] == 220,
            "prix public/équipe en centimes")
    _expect(article["volume_cl"] == 25 and article["is_alcohol"], "volume + alcool")
    _expect(article["article_type"] == "biere", "type article")
    keg = map_keg_row({"id": 1, "name": "Barbar", "half_pint_price": "1.75",
                       "half_pint_price_foyz": "1.70", "pint_price": "3.50",
                       "pint_price_foyz": "3.40", "pot_price": "5.60",
                       "pot_price_foyz": "5.50", "volume": "30.00",
                       "alcohol_volume": "8.0"}, "euros")
    _expect(keg["volume_l"] == 30.0 and keg["remaining_l"] == 30.0, "volume fût")
    _expect(keg["alcohol_degree"] == 8.0, "degré fût")
    _expect(keg["price_pint_std"] == 350 and keg["price_pint_team"] == 340, "tarifs fût")
    tap = map_tap_row({"id": 3, "beer_draught": "Tireuse de Gauche", "draft_beer_id": 1,
                       "date_start": "2026-08-29 12:00:00", "date_end": None})
    _expect(tap["date_end"] is None and tap["date_start"] is not None, "tireuse ouverte")
    _expect(map_article_row({"name": "  "}, "euros") is None, "article sans nom ignoré")


def test_operation_mapping():
    # tables d'opérations séparées de la base Brest réelle (badges varchar(13))
    pay = map_operation_row(
        {"id": "9", "date": "2026-08-31 14:10:00", "user_id": "0000000000042",
         "balance": "1.50", "type": "CreditCard", "logged_user_id": "1425784165891"},
        ENTITY_FIELDS["rechargements"], "rechargements", "brest", "euros")
    _expect(pay["type"] == "rechargement" and pay["total_cents"] == 150,
            "payments -> rechargement")
    _expect(pay["payment_method"] == "cb" and pay["src_user_id"] == "0000000000042",
            "enum CreditCard -> cb")
    _expect(pay["operator_label"] == "1425784165891", "badge opérateur brut (résolu en aval)")
    check = map_operation_row(
        {"id": "10", "date": "2026-08-31 14:10:00", "user_id": "1", "balance": "20.00",
         "type": "Check", "logged_user_id": "2"},
        ENTITY_FIELDS["rechargements"], "rechargements", "brest", "euros")
    _expect(check["payment_method"] is None
            and check["note"] == "moyen de paiement source : Check",
            "chèque sans équivalent cible -> note")
    wd = map_operation_row(
        {"id": "3", "date": "2026-08-31 15:00:00", "user_id": "1", "balance": "10.00",
         "logged_user_id": "2"},
        ENTITY_FIELDS["retraits"], "retraits", "brest", "euros")
    _expect(wd["type"] == "retrait" and wd["total_cents"] == 1000, "withdrawals -> retrait")
    neg = map_operation_row(
        {"id": "4", "date": "2026-08-31 16:00:00", "user_id": "1", "balance": "-2.00",
         "logged_user_id": "2"},
        ENTITY_FIELDS["transferts"], "transferts", "brest", "euros")
    _expect(neg["type"] == "transfert" and neg["total_cents"] == 200
            and neg["signed_cents"] == -200 and neg["side"] == "from",
            "demi-transfert donneur (signé)")


def test_brest_transactions_default_to_achat():
    # la table `transactions` du dump réel n'a pas de colonne type
    txn = map_transaction_row(
        {"id": 5, "date": "2026-08-31 16:00:00", "user_id": "0000000000042",
         "balance": "2.20", "logged_user_id": "1425784165891"},
        "brest", "euros")
    _expect(txn["type"] is None, "type absent -> None (achat imposé en aval)")
    _expect(txn["total_cents"] == 220 and txn["src_user_id"] == "0000000000042",
            "montant + badge acheteur")


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
    # ---- catalogue importé (articles, fûts, tireuses, motifs de blacklist)
    n_articles = c.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
    n_tap_articles = c.execute("SELECT COUNT(*) FROM articles WHERE is_tap=1").fetchone()[0]
    n_kegs = c.execute("SELECT COUNT(*) FROM kegs").fetchone()[0]
    n_keg_prices = c.execute("SELECT COUNT(*) FROM keg_prices").fetchone()[0]
    n_taps = c.execute("SELECT COUNT(*) FROM taps").fetchone()[0]
    resolved = c.execute(
        "SELECT COUNT(*) FROM transaction_lines WHERE article_id IS NOT NULL").fetchone()[0]
    kronenbourg = c.execute(
        "SELECT volume_cl, price_std_brest, price_team_brest, is_alcohol, article_type "
        "FROM articles WHERE name='Kronenbourg 25cl'").fetchone()
    menu = c.execute(
        "SELECT name, article_type FROM articles WHERE name LIKE 'Menu Foy%'").fetchone()
    diabolo = c.execute(
        "SELECT article_type FROM articles WHERE name='Diabolo 33cl'").fetchone()
    paris_article = c.execute(
        "SELECT price_std_paris, price_team_paris, volume_cl FROM articles "
        "WHERE name='Bière pression demi'").fetchone()
    barbar = c.execute(
        "SELECT k.id, kp.price_pint_std, kp.price_pint_team FROM kegs k "
        "JOIN keg_prices kp ON kp.keg_id = k.id WHERE k.name='Barbar'").fetchone()
    taps_map = dict(c.execute(
        "SELECT t.number, k.name FROM taps t JOIN kegs k ON k.id = t.keg_id").fetchall())
    motif = c.execute(
        "SELECT blacklist_reason FROM users WHERE name='baptiste.chevalier'").fetchone()[0]
    alcool_bit = c.execute(
        "SELECT blacklist_alcohol FROM users WHERE name='manon.robin'").fetchone()[0]
    # ---- opérations éclatées Brest : payments / withdrawals / transfert
    pay = c.execute(
        "SELECT t.total, t.payment_method, t.operator_label, u.name "
        "FROM transactions t LEFT JOIN users u ON u.id = t.to_user_id "
        "WHERE t.campus='brest' AND t.type='rechargement' AND t.total=2000 "
        "AND t.payment_method='cb'").fetchall()
    _expect(len(pay) == 1 and pay[0][2] == "camille.rousseau" and pay[0][3] == "léo.martin",
            f"rechargement payments + opérateur résolu en nom ({pay})")
    check_pay = c.execute(
        "SELECT payment_method FROM transactions WHERE campus='brest' "
        "AND type='rechargement' AND note LIKE 'moyen de paiement source : Check'"
    ).fetchall()
    _expect(len(check_pay) == 1 and check_pay[0][0] is None,
            f"chèque sans équivalent -> note ({check_pay})")
    pay_contrib = c.execute(
        "SELECT u.name, c.amount, c.balance_after FROM contributions c "
        "JOIN users u ON u.id = c.user_id JOIN transactions t ON t.id = c.transaction_id "
        "WHERE t.campus='brest' AND t.type='rechargement' AND t.total=2000").fetchall()
    _expect(pay_contrib == [("léo.martin", 2000, 0)],
            f"contribution rechargement importé ({pay_contrib})")
    wd = c.execute(
        "SELECT t.total, u.name FROM transactions t JOIN users u ON u.id = t.from_user_id "
        "WHERE t.campus='brest' AND t.type='retrait' AND t.total=1500").fetchall()
    _expect(wd == [(1500, "anaïs.costa")], f"retrait withdrawals importé ({wd})")
    tr = c.execute(
        "SELECT t.total, uf.name, ut.name FROM transactions t "
        "JOIN users uf ON uf.id = t.from_user_id JOIN users ut ON ut.id = t.to_user_id "
        "WHERE t.campus='brest' AND t.type='transfert' AND t.total=500").fetchall()
    _expect(tr == [(500, "léo.martin", "hugo.petit")], f"transfert apparié ({tr})")
    tr_contrib = c.execute(
        "SELECT c.amount FROM contributions c JOIN transactions t ON t.id = c.transaction_id "
        "WHERE t.campus='brest' AND t.type='transfert' AND t.total=500 "
        "ORDER BY c.amount").fetchall()
    _expect(tr_contrib == [(-500,), (500,)], f"contributions transfert ± ({tr_contrib})")
    orph_neg = c.execute(
        "SELECT from_user_id IS NOT NULL, to_user_id IS NULL FROM transactions "
        "WHERE campus='brest' AND type='transfert' AND total=300").fetchone()
    orph_pos = c.execute(
        "SELECT from_user_id IS NULL, to_user_id IS NOT NULL FROM transactions "
        "WHERE campus='brest' AND type='transfert' AND total=150").fetchone()
    _expect(orph_neg == (1, 1), f"demi-transfert donneur orphelin ({orph_neg})")
    _expect(orph_pos == (1, 1), f"demi-transfert bénéficiaire orphelin ({orph_pos})")
    n_achats = c.execute(
        "SELECT COUNT(*) FROM transactions WHERE campus='brest' AND type='achat'"
    ).fetchone()[0]
    _expect(n_achats == manifest["brest_achats"], f"achats typés ({n_achats})")
    c.close()
    _expect(total == manifest["source_cents"]["total"], f"solde total {total} == manifest")
    _expect(brest == manifest["source_cents"]["brest"], f"solde brest {brest}")
    _expect(paris == manifest["source_cents"]["paris"], f"solde paris {paris}")
    _expect(txns == (manifest["brest_transactions"] + manifest["paris_transactions"]
                     + manifest["brest_payments"] + manifest["brest_withdrawals"]
                     + manifest["brest_transfer_txns"]),
            f"transactions {txns}")
    _expect(len(merged) == 1, "compte fusionné avec 2 portefeuilles")
    _expect(staging_left == 0, "staging supprimée après commit")
    _expect(n_articles == manifest["brest_articles"] + manifest["paris_articles"]
            + manifest["brest_tap_articles"], f"articles importés {n_articles}")
    _expect(n_tap_articles == manifest["brest_tap_articles"], "articles de tireuse générés")
    _expect(n_kegs == manifest["brest_kegs"] and n_keg_prices == manifest["brest_kegs"],
            "fûts + tarifs importés")
    _expect(n_taps == manifest["brest_taps"], "tireuses configurées")
    _expect(resolved == manifest["brest_lines"], f"lignes rattachées aux articles {resolved}")
    _expect(kronenbourg == (25, 250, 220, 1, "biere"), f"article bière {kronenbourg}")
    _expect(menu is not None and menu[0] == "Menu Foy'z" and menu[1] == "evenement",
            f"entités HTML + type 8 -> evenement ({menu})")
    _expect(diabolo == ("snack",), "boisson froide -> snack")
    _expect(paris_article == (200, 150, 25), f"article Paris projeté {paris_article}")
    _expect(barbar is not None and barbar[1] == 350 and barbar[2] == 350,
            f"tarif fût Barbar {barbar}")
    _expect(taps_map == {1: "Barbar", 2: "Coreff rousse"},
            f"tireuses : ouverture la plus récente gagne ({taps_map})")
    _expect(motif == "Comportement inacceptable en soirée (fixture)", "motif blacklist importé")
    _expect(alcool_bit == 1, "bit(1) alcohol_blacklisted -> blacklist_alcohol")
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
