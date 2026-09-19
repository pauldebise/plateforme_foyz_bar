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

from migration import audit, cleaner, detect, logfilter, util  # noqa: E402
from migration.cli import _safe_error  # noqa: E402
from migration.errors import AccountingError, MigrationError, SourceError  # noqa: E402
from migration.parsing.sqlstream import iter_business_rows  # noqa: E402
from migration.sources import (  # noqa: E402
    as_bool,
    map_article_row,
    map_keg_row,
    map_operation_row,
    map_tap_row,
    map_transaction_row,
    map_user_row,
    merge_key,
    normalize_birth_date,
    resolve_article_type,
)
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
    _expect(
        util.parse_dt("2026-08-31 23:30:00").isoformat() == "2026-08-31T21:30:00",
        "naive Paris -> UTC",
    )
    _expect(
        util.parse_dt("2026-08-31T23:30:00+02:00").isoformat() == "2026-08-31T21:30:00",
        "ISO tz -> UTC",
    )


def test_slug_username():
    cases = {
        "Paul Debise": "paul.debise",
        "Marie Claire Dupont": "marie.claire.dupont",
        "  ÉLÈVE Dupont ": "eleve.dupont",
        "Jean-Luc Picard": "jean.luc.picard",
        "O'Brien": "obrien",
        "a  b\tc": "a.b.c",
    }
    for raw, expected in cases.items():
        _expect(util.slug_username(raw) == expected, f"slug {raw!r} -> {expected}")
    _expect(util.slug_username("") is None, "slug vide -> None")
    _expect(util.slug_username(None) is None, "slug None -> None")
    _expect(len(util.slug_username("x" * 100)) == 64, "slug borné à 64")
    # miroir app <-> migration : le login et l'import doivent produire les
    # mêmes identifiants
    from app.utils import slug_username as app_slug

    for raw, expected in cases.items():
        _expect(app_slug(raw) == expected, f"miroir app slug {raw!r}")


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
    result = audit.AuditResult(
        source={"brest": 100, "paris": 50},
        initial=audit.TargetTotals(total=0),
        final=audit.TargetTotals(total=150, brest=100, paris=50),
    )
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
        {
            "card_id": "1548963257462",
            "name": "aime secretement massé Paul DEBISE chips vachement bete",
            "real_name": "Paul Debise",
            "password": "$2a$10$wtlkmC9G3pGaid7Fw.vOYeek0JjVNe3eQ.RPKfOtMTEvDjLRljbjC",
            "balance": "-26.18",
            "is_foyz": 1,
            "promo": "CI2028",
        },
        "brest",
        "euros",
    )
    _expect(warning is None, "ligne utilisateur valide")
    _expect(user["name"] == "Paul Debise", f"nom réel affiché ({user['name']})")
    _expect(
        user["nickname"] == "aime secretement massé Paul DEBISE chips vachement bete",
        f"pseudo conservé en surnom ({user['nickname']})",
    )
    _expect(
        user["username"] == "paul.debise",
        f"identifiant de connexion = slug du nom réel ({user['username']})",
    )
    _expect(user["key"] == "paul debise", "clé de réconciliation = nom réel")
    _expect(user["password_hash"] is None, "bcrypt -> pas un hash werkzeug")
    _expect(
        user["legacy_password"] == "$2a$10$wtlkmC9G3pGaid7Fw.vOYeek0JjVNe3eQ.RPKfOtMTEvDjLRljbjC",
        "bcrypt conservé brut dans legacy_password",
    )
    # Hash werkzeug source -> réutilisé tel quel en cible, rien en legacy ;
    # pseudo confondu avec le nom réel -> pas de surnom redondant
    werkzeug_hash = "pbkdf2:sha256:600000$sel$deadbeef"
    reuser, _ = map_user_row({"name": "x", "mdp": werkzeug_hash}, "brest", "euros")
    _expect(
        reuser["name"] == "x" and reuser["nickname"] is None,
        "pseudo seul : nom = pseudo, pas de surnom",
    )
    _expect(reuser["username"] == "x", "identifiant = pseudo slughé")
    _expect(
        reuser["password_hash"] == werkzeug_hash and reuser["legacy_password"] is None,
        "hash werkzeug -> password_hash",
    )
    # Sans mot de passe ni real_name : pseudo seul, rien importé
    plain, _ = map_user_row({"name": "y"}, "brest", "euros")
    _expect(
        plain["name"] == "y"
        and plain["password_hash"] is None
        and plain["legacy_password"] is None,
        "sans mot de passe -> legacy None",
    )
    # prénom + nom séparés (Paris) : nom réel composé, pseudo conservé
    paris, _ = map_user_row(
        {"prenom": "Marie", "nom": "Le Goff", "pseudo": "marie.legoff"}, "paris", "euros"
    )
    _expect(paris["name"] == "Marie Le Goff", f"prénom+nom -> nom réel ({paris['name']})")
    _expect(paris["nickname"] == "marie.legoff", "pseudo Paris conservé en surnom")
    _expect(paris["username"] == "marie.le.goff", f"identifiant multi-mots ({paris['username']})")


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
    types = {
        1: "Bière",
        4: "Snacks",
        6: "Boisson Chaude",
        7: "Boisson Froide",
        8: "Cocktails/Barbecue/Soirées",
    }
    _expect(resolve_article_type(1, types) == "biere", "type 1 -> biere")
    _expect(resolve_article_type("7", types) == "snack", "boisson froide -> snack")
    _expect(resolve_article_type(6, types) == "snack", "boisson chaude -> snack")
    _expect(resolve_article_type("8", types) == "evenement", "cocktails -> evenement")
    _expect(resolve_article_type("Bière") == "biere", "nom direct -> biere")
    _expect(resolve_article_type(None) == "snack", "type absent -> snack (non alcoolisé)")
    article = map_article_row(
        {
            "id": "0000000000042",
            "name": "Kronenbourg 25cl",
            "price": "2.50",
            "price_foyz": "2.20",
            "type": 1,
            "volume": "0.25",
        },
        "euros",
        types,
    )
    _expect(article["name"] == "Kronenbourg 25cl", "nom article")
    _expect(
        article["price_std_cents"] == 250 and article["price_team_cents"] == 220,
        "prix public/équipe en centimes",
    )
    _expect(article["volume_cl"] == 25 and article["is_alcohol"], "volume + alcool")
    _expect(article["article_type"] == "biere", "type article")
    keg = map_keg_row(
        {
            "id": 1,
            "name": "Barbar",
            "half_pint_price": "1.75",
            "half_pint_price_foyz": "1.70",
            "pint_price": "3.50",
            "pint_price_foyz": "3.40",
            "pot_price": "5.60",
            "pot_price_foyz": "5.50",
            "volume": "30.00",
            "alcohol_volume": "8.0",
        },
        "euros",
    )
    _expect(keg["volume_l"] == 30.0 and keg["remaining_l"] == 30.0, "volume fût")
    _expect(keg["alcohol_degree"] == 8.0, "degré fût")
    _expect(keg["price_pint_std"] == 350 and keg["price_pint_team"] == 340, "tarifs fût")
    tap = map_tap_row(
        {
            "id": 3,
            "beer_draught": "Tireuse de Gauche",
            "draft_beer_id": 1,
            "date_start": "2026-08-29 12:00:00",
            "date_end": None,
        }
    )
    _expect(tap["date_end"] is None and tap["date_start"] is not None, "tireuse ouverte")
    _expect(map_article_row({"name": "  "}, "euros") is None, "article sans nom ignoré")


def test_operation_mapping():
    # tables d'opérations séparées de la base Brest réelle (badges varchar(13))
    pay = map_operation_row(
        {
            "id": "9",
            "date": "2026-08-31 14:10:00",
            "user_id": "0000000000042",
            "balance": "1.50",
            "type": "CreditCard",
            "logged_user_id": "1425784165891",
        },
        ENTITY_FIELDS["rechargements"],
        "rechargements",
        "brest",
        "euros",
    )
    _expect(pay["type"] == "rechargement" and pay["total_cents"] == 150, "payments -> rechargement")
    _expect(
        pay["payment_method"] == "cb" and pay["src_user_id"] == "0000000000042",
        "enum CreditCard -> cb",
    )
    _expect(pay["operator_label"] == "1425784165891", "badge opérateur brut (résolu en aval)")
    check = map_operation_row(
        {
            "id": "10",
            "date": "2026-08-31 14:10:00",
            "user_id": "1",
            "balance": "20.00",
            "type": "Check",
            "logged_user_id": "2",
        },
        ENTITY_FIELDS["rechargements"],
        "rechargements",
        "brest",
        "euros",
    )
    _expect(
        check["payment_method"] is None and check["note"] == "moyen de paiement source : Check",
        "chèque sans équivalent cible -> note",
    )
    wd = map_operation_row(
        {
            "id": "3",
            "date": "2026-08-31 15:00:00",
            "user_id": "1",
            "balance": "10.00",
            "logged_user_id": "2",
        },
        ENTITY_FIELDS["retraits"],
        "retraits",
        "brest",
        "euros",
    )
    _expect(wd["type"] == "retrait" and wd["total_cents"] == 1000, "withdrawals -> retrait")
    neg = map_operation_row(
        {
            "id": "4",
            "date": "2026-08-31 16:00:00",
            "user_id": "1",
            "balance": "-2.00",
            "logged_user_id": "2",
        },
        ENTITY_FIELDS["transferts"],
        "transferts",
        "brest",
        "euros",
    )
    _expect(
        neg["type"] == "transfert"
        and neg["total_cents"] == 200
        and neg["signed_cents"] == -200
        and neg["side"] == "from",
        "demi-transfert donneur (signé)",
    )


def test_brest_transactions_default_to_achat():
    # la table `transactions` du dump réel n'a pas de colonne type
    txn = map_transaction_row(
        {
            "id": 5,
            "date": "2026-08-31 16:00:00",
            "user_id": "0000000000042",
            "balance": "2.20",
            "logged_user_id": "1425784165891",
        },
        "brest",
        "euros",
    )
    _expect(txn["type"] is None, "type absent -> None (achat imposé en aval)")
    _expect(
        txn["total_cents"] == 220 and txn["src_user_id"] == "0000000000042",
        "montant + badge acheteur",
    )


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
    _prepare(src / "sources")
    _fresh_db(db_path)
    files_before = sorted(p.name for p in (src / "sources").iterdir())
    code = _cli(
        [
            "--dry-run",
            "--source-dir",
            str(src / "sources"),
            "--database-url",
            f"sqlite:///{db_path}",
        ]
    )
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
    code = _cli(
        ["--run", "--source-dir", str(src / "sources"), "--database-url", f"sqlite:///{db_path}"]
    )
    _expect(code == 0, "run exit 0")
    source_cents = manifest["source_cents"]
    _expect(
        source_cents["total"] == source_cents["brest"] + source_cents["paris"]
        and source_cents["total"] > 0,
        "manifest lisible (total source = brest + paris)",
    )
    c = sqlite3.connect(db_path)
    total = c.execute("SELECT SUM(balance) FROM wallets").fetchone()[0]
    brest = c.execute("SELECT SUM(balance) FROM wallets WHERE campus='brest'").fetchone()[0]
    paris = c.execute("SELECT SUM(balance) FROM wallets WHERE campus='paris'").fetchone()[0]
    txns = c.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    # fusion : l'email commun doit donner un compte avec DEUX portefeuilles
    merged = c.execute(
        "SELECT COUNT(*) FROM users u JOIN wallets w ON w.user_id = u.id "
        "WHERE u.username LIKE 'marie%' GROUP BY u.id HAVING COUNT(*) = 2"
    ).fetchall()
    # identifiants : username = slug du nom réel, surnom = pseudo source
    # (Brest prioritaire à la fusion)
    leo = c.execute("SELECT name, nickname FROM users WHERE username = 'leo.martin'").fetchone()
    marie = c.execute("SELECT nickname FROM users WHERE username = 'marie.le.goff'").fetchone()
    usernames = [r[0] for r in c.execute("SELECT username FROM users").fetchall()]
    users_with_login = [u for u in usernames if u]
    _expect(len(users_with_login) == len(set(users_with_login)), "identifiants uniques")
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
        "SELECT COUNT(*) FROM transaction_lines WHERE article_id IS NOT NULL"
    ).fetchone()[0]
    kronenbourg = c.execute(
        "SELECT volume_cl, price_std, price_team, is_alcohol, article_type, campus "
        "FROM articles WHERE name='Kronenbourg 25cl'"
    ).fetchone()
    menu = c.execute(
        "SELECT name, article_type FROM articles WHERE name LIKE 'Menu Foy%'"
    ).fetchone()
    diabolo = c.execute("SELECT article_type FROM articles WHERE name='Diabolo 33cl'").fetchone()
    paris_article = c.execute(
        "SELECT price_std, price_team, volume_cl, campus FROM articles "
        "WHERE name='Bière pression demi'"
    ).fetchone()
    barbar = c.execute(
        "SELECT k.id, kp.price_pint_std, kp.price_pint_team FROM kegs k "
        "JOIN keg_prices kp ON kp.keg_id = k.id WHERE k.name='Barbar'"
    ).fetchone()
    taps_map = dict(
        c.execute("SELECT t.number, k.name FROM taps t JOIN kegs k ON k.id = t.keg_id").fetchall()
    )
    motif = c.execute(
        "SELECT blacklist_reason FROM users WHERE username='baptiste.chevalier'"
    ).fetchone()[0]
    alcool_bit = c.execute(
        "SELECT blacklist_alcohol FROM users WHERE username='manon.robin'"
    ).fetchone()[0]
    # ---- opérations éclatées Brest : payments / withdrawals / transfert
    pay = c.execute(
        "SELECT t.total, t.payment_method, t.operator_label, u.name "
        "FROM transactions t LEFT JOIN users u ON u.id = t.to_user_id "
        "WHERE t.campus='brest' AND t.type='rechargement' AND t.total=2000 "
        "AND t.payment_method='cb'"
    ).fetchall()
    _expect(
        len(pay) == 1
        and pay[0][2] == "Camille Rousseau (camille.rousseau)"
        and pay[0][3] == "Léo Martin",
        f"rechargement payments + opérateur résolu en nom long ({pay})",
    )
    check_pay = c.execute(
        "SELECT payment_method FROM transactions WHERE campus='brest' "
        "AND type='rechargement' AND note LIKE 'moyen de paiement source : Check'"
    ).fetchall()
    _expect(
        len(check_pay) == 1 and check_pay[0][0] is None,
        f"chèque sans équivalent -> note ({check_pay})",
    )
    pay_contrib = c.execute(
        "SELECT u.name, c.amount, c.balance_after FROM contributions c "
        "JOIN users u ON u.id = c.user_id JOIN transactions t ON t.id = c.transaction_id "
        "WHERE t.campus='brest' AND t.type='rechargement' AND t.total=2000"
    ).fetchall()
    _expect(
        pay_contrib == [("Léo Martin", 2000, 0)],
        f"contribution rechargement importé ({pay_contrib})",
    )
    wd = c.execute(
        "SELECT t.total, u.name FROM transactions t JOIN users u ON u.id = t.from_user_id "
        "WHERE t.campus='brest' AND t.type='retrait' AND t.total=1500"
    ).fetchall()
    _expect(wd == [(1500, "Anaïs Costa")], f"retrait withdrawals importé ({wd})")
    tr = c.execute(
        "SELECT t.total, uf.name, ut.name FROM transactions t "
        "JOIN users uf ON uf.id = t.from_user_id JOIN users ut ON ut.id = t.to_user_id "
        "WHERE t.campus='brest' AND t.type='transfert' AND t.total=500"
    ).fetchall()
    _expect(tr == [(500, "Léo Martin", "Hugo Petit")], f"transfert apparié ({tr})")
    tr_contrib = c.execute(
        "SELECT c.amount FROM contributions c JOIN transactions t ON t.id = c.transaction_id "
        "WHERE t.campus='brest' AND t.type='transfert' AND t.total=500 "
        "ORDER BY c.amount"
    ).fetchall()
    _expect(tr_contrib == [(-500,), (500,)], f"contributions transfert ± ({tr_contrib})")
    orph_neg = c.execute(
        "SELECT from_user_id IS NOT NULL, to_user_id IS NULL FROM transactions "
        "WHERE campus='brest' AND type='transfert' AND total=300"
    ).fetchone()
    orph_pos = c.execute(
        "SELECT from_user_id IS NULL, to_user_id IS NOT NULL FROM transactions "
        "WHERE campus='brest' AND type='transfert' AND total=150"
    ).fetchone()
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
    _expect(
        txns
        == (
            manifest["brest_transactions"]
            + manifest["paris_transactions"]
            + manifest["brest_payments"]
            + manifest["brest_withdrawals"]
            + manifest["brest_transfer_txns"]
        ),
        f"transactions {txns}",
    )
    _expect(len(merged) == 1, "compte fusionné avec 2 portefeuilles")
    _expect(leo == ("Léo Martin", "léo.martin"), f"nom réel + surnom importés ({leo})")
    _expect(marie == ("marie.le goff",), f"surnom Brest prioritaire à la fusion ({marie})")
    _expect(staging_left == 0, "staging supprimée après commit")
    _expect(
        n_articles
        == manifest["brest_articles"] + manifest["paris_articles"] + manifest["brest_tap_articles"],
        f"articles importés {n_articles}",
    )
    _expect(n_tap_articles == manifest["brest_tap_articles"], "articles de tireuse générés")
    _expect(
        n_kegs == manifest["brest_kegs"] and n_keg_prices == manifest["brest_kegs"],
        "fûts + tarifs importés",
    )
    _expect(n_taps == manifest["brest_taps"], "tireuses configurées")
    _expect(resolved == manifest["brest_lines"], f"lignes rattachées aux articles {resolved}")
    _expect(kronenbourg == (25, 250, 220, 1, "biere", "brest"), f"article bière {kronenbourg}")
    _expect(
        menu is not None and menu[0] == "Menu Foy'z" and menu[1] == "evenement",
        f"entités HTML + type 8 -> evenement ({menu})",
    )
    _expect(diabolo == ("snack",), "boisson froide -> snack")
    _expect(paris_article == (200, 150, 25, "paris"), f"article Paris projeté {paris_article}")
    _expect(
        barbar is not None and barbar[1] == 350 and barbar[2] == 350, f"tarif fût Barbar {barbar}"
    )
    _expect(
        taps_map == {1: "Barbar", 2: "Coreff rousse"},
        f"tireuses : ouverture la plus récente gagne ({taps_map})",
    )
    _expect(motif == "Comportement inacceptable en soirée (fixture)", "motif blacklist importé")
    _expect(alcool_bit == 1, "bit(1) alcohol_blacklisted -> blacklist_alcohol")
    remaining = [p.name for p in (src / "sources").iterdir() if p.name != "manifest.json"]
    _expect(remaining == [], f"fichiers sources supprimés ({remaining})")
    # double run impossible : plus de fichiers
    code2 = _cli(
        ["--run", "--source-dir", str(src / "sources"), "--database-url", f"sqlite:///{db_path}"]
    )
    _expect(code2 == 1, "second run sans fichiers -> exit 1")
    shutil.rmtree(src)


def test_e2e_keep_archives_and_audit_only():
    src = Path(tempfile.mkdtemp(prefix="mig_arch_"))
    db_path = src / "cible.db"
    _prepare(src / "sources")
    _fresh_db(db_path)
    code = _cli(
        [
            "--run",
            "--keep-archives",
            "--source-dir",
            str(src / "sources"),
            "--database-url",
            f"sqlite:///{db_path}",
        ]
    )
    _expect(code == 0, "run --keep-archives exit 0")
    archives = list((src / "sources" / "archives").rglob("*.sql")) + list(
        (src / "sources" / "archives").rglob("*.json")
    )
    _expect(len(archives) == 2, "deux fichiers archivés")
    # audit-only : recopie des archives au premier niveau puis comparaison
    for f in archives:
        shutil.copy(f, src / "sources" / f.name)
    code2 = _cli(
        [
            "--audit-only",
            "--source-dir",
            str(src / "sources"),
            "--database-url",
            f"sqlite:///{db_path}",
        ]
    )
    _expect(code2 == 0, "audit-only réconcilié exit 0")
    # audit-only sur base vierge -> écart -> exit 1
    blank = src / "blank.db"
    _fresh_db(blank)
    code3 = _cli(
        [
            "--audit-only",
            "--source-dir",
            str(src / "sources"),
            "--database-url",
            f"sqlite:///{blank}",
        ]
    )
    _expect(code3 == 1, "audit-only sur base vierge -> exit 1")
    shutil.rmtree(src)


def test_e2e_failure_keeps_files():
    src = Path(tempfile.mkdtemp(prefix="mig_fail_"))
    db_path = src / "cible.db"
    _prepare(src / "sources")
    # dump hostile : un solde avec une fraction de centime -> MigrationError
    dump_path = next((src / "sources").glob("brest_*.sql"))
    text = dump_path.read_text(encoding="utf-8").replace("'46.26'", "'46.265'", 1)
    dump_path.write_text(text, encoding="utf-8")
    _fresh_db(db_path)
    users_before, total_before, _ = _db_counts(db_path)
    code = _cli(
        ["--run", "--source-dir", str(src / "sources"), "--database-url", f"sqlite:///{db_path}"]
    )
    _expect(code == 1, "écart/erreur -> exit 1")
    users_after, total_after, staging_left = _db_counts(db_path)
    _expect(
        (users_after, total_after) == (users_before, total_before),
        f"base inchangée après échec ({users_after}, {total_after})",
    )
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
    c.execute(
        "INSERT INTO users (username, name, blacklist, blacklist_alcohol, created_at) "
        "VALUES ('existant.user', 'Existant User',0,0,'2026-01-01 00:00:00')"
    )
    uid = c.execute("SELECT last_insert_rowid()").fetchone()[0]
    c.execute(
        "INSERT INTO wallets (user_id, campus, balance, glasses_outstanding) "
        "VALUES (?, 'brest', 500, 0)",
        (uid,),
    )
    c.commit()
    c.close()
    code = _cli(
        ["--run", "--source-dir", str(src / "sources"), "--database-url", f"sqlite:///{db_path}"]
    )
    _expect(code == 0, "run sur cible non vierge : invariant en delta")
    c = sqlite3.connect(db_path)
    total = c.execute("SELECT SUM(balance) FROM wallets").fetchone()[0]
    c.close()
    _expect(
        total == manifest["source_cents"]["total"] + 500,
        f"solde final {total} = sources + fonds préexistants",
    )
    shutil.rmtree(src)


# ------------------------------------------------------- phase 5 (T-5.1 → T-5.4)

_BREST_USERS_DDL = """
CREATE TABLE `users` (
  `card_id` varchar(13) NOT NULL,
  `name` varchar(255) NOT NULL,
  `real_name` varchar(255) DEFAULT '',
  `password` varchar(255) NOT NULL,
  `balance` decimal(10,2) NOT NULL DEFAULT 0.00,
  `blacklisted` tinyint(1) NOT NULL DEFAULT 0,
  `is_foyz` tinyint(1) NOT NULL DEFAULT 0,
  `promo` varchar(32) NOT NULL DEFAULT 'Autre',
  `disabled` tinyint(1) NOT NULL DEFAULT 0,
  `registration` datetime NOT NULL,
  `ecocups` int(11) NOT NULL DEFAULT 0,
  `blacklist_reason` varchar(255) NOT NULL DEFAULT '',
  `alcohol_blacklisted` bit(1) NOT NULL DEFAULT b'0'
) ENGINE=InnoDB;
"""


def _sql(value):
    if value is None:
        return "NULL"
    return "'" + str(value).replace("\\", "\\\\").replace("'", "\\'") + "'"


def _write_brest(path, users, transfert=None):
    """Dump Brest minimal et réaliste (schéma réel : pas d'email, `disabled`)."""
    cols = (
        "card_id",
        "name",
        "real_name",
        "password",
        "balance",
        "blacklisted",
        "is_foyz",
        "promo",
        "disabled",
        "registration",
        "ecocups",
        "blacklist_reason",
        "alcohol_blacklisted",
    )
    tuples = []
    for u in users:
        tuples.append("(" + ",".join(_sql(u.get(c)) for c in cols) + ")")
    dump = _BREST_USERS_DDL
    quoted = ",".join(f"`{c}`" for c in cols)
    dump += f"INSERT INTO `users` ({quoted}) VALUES " + ",".join(tuples) + ";\n"
    if transfert is not None:
        dump += (
            "CREATE TABLE `transfert` (`id` int(11), `date` datetime, "
            "`user_id` varchar(13), `balance` decimal(10,2), `logged_user_id` varchar(13));\n"
        )
        rows = ",".join("(" + ",".join(_sql(v) for v in r) + ")" for r in transfert)
        dump += (
            "INSERT INTO `transfert` (`id`,`date`,`user_id`,`balance`,`logged_user_id`) "
            "VALUES " + rows + ";\n"
        )
    Path(path).write_text(dump, encoding="utf-8")


def _user(card, name, balance, disabled=0, real_name=None, registration="2025-09-01 12:00:00"):
    return {
        "card_id": card,
        "name": name,
        "real_name": real_name if real_name is not None else name,
        "password": "5f4dcc3b5aa765d61d8327deb882cf99",
        "balance": balance,
        "blacklisted": 0,
        "is_foyz": 0,
        "promo": "CI2028",
        "disabled": disabled,
        "registration": registration,
        "ecocups": 0,
        "blacklist_reason": "",
        "alcohol_blacklisted": "b'0'",
    }


def test_p5_mapping_gap_blocks_and_control():
    # collision de clé : SERRURIER Ninon (10,00) et Serrurier Ninon (5,00)
    src = Path(tempfile.mkdtemp(prefix="p5_gap_"))
    sdir = src / "sources"
    sdir.mkdir()
    _write_brest(
        sdir / "brest_lot.sql",
        [
            _user("0041", "SERRURIER Ninon", "10.00"),
            _user("9999", "Serrurier Ninon", "5.00"),
        ],
    )
    db_path = src / "cible.db"
    _fresh_db(db_path)
    code = _cli(["--run", "--source-dir", str(sdir), "--database-url", f"sqlite:///{db_path}"])
    _expect(code == 1, "écart de mapping -> exit 1 (bloque)")
    users, total, staging = _db_counts(db_path)
    _expect((users, total, staging) == (0, 0, 0), f"base intacte après blocage ({users},{total})")
    _expect((sdir / "brest_lot.sql").exists(), "fichier source conservé après échec")
    # contrôle : noms distincts -> écart nul -> succès
    src2 = Path(tempfile.mkdtemp(prefix="p5_ok_"))
    sdir2 = src2 / "sources"
    sdir2.mkdir()
    _write_brest(
        sdir2 / "brest_lot.sql",
        [
            _user("0041", "SERRURIER Ninon", "10.00"),
            _user("9999", "Autre Personne", "5.00"),
        ],
    )
    db2 = src2 / "cible.db"
    _fresh_db(db2)
    code2 = _cli(["--run", "--source-dir", str(sdir2), "--database-url", f"sqlite:///{db2}"])
    _expect(code2 == 0, "absence de collision -> exit 0")
    shutil.rmtree(src)
    shutil.rmtree(src2)


def test_p5_collision_auto_fusion():
    """Fusions automatiques de collisions : solde nul ou compte de plus de 3 ans.

    - au plus une occurrence avec solde non nul -> fusion (rien à attribuer à
      tort, même s'il s'agit d'un homonyme) ;
    - au moins une occurrence créée il y a plus de 3 ans -> fusion ;
    - sinon (soldes réels des deux côtés, comptes récents) -> toujours bloquant.
    """
    # cas fusionnables : run OK, un compte par clé, solde = somme des occurrences
    src = Path(tempfile.mkdtemp(prefix="p5_fusion_"))
    sdir = src / "sources"
    sdir.mkdir()
    _write_brest(
        sdir / "brest_lot.sql",
        [
            _user("0041", "Dupont Alice", "10.00"),  # + carte vide -> fusion (solde nul)
            _user("9999", "Dupont Alice", "0.00"),
            _user("0042", "Ancien Marcel", "4.00", registration="2019-05-01 08:00:00"),
            _user("8888", "Ancien Marcel", "2.00"),  # compte ancien -> fusion
        ],
    )
    db_path = src / "cible.db"
    _fresh_db(db_path)
    code = _cli(["--run", "--source-dir", str(sdir), "--database-url", f"sqlite:///{db_path}"])
    _expect(code == 0, "collisions fusionnables -> exit 0")
    users, total, staging = _db_counts(db_path)
    _expect((users, total, staging) == (2, 1600, 0), f"comptes fusionnés créés ({users},{total})")
    c = sqlite3.connect(db_path)
    balances = {
        name: bal
        for name, bal in c.execute(
            "SELECT u.name, w.balance FROM users u JOIN wallets w ON w.user_id = u.id"
        ).fetchall()
    }
    c.close()
    _expect(balances.get("Dupont Alice") == 1000, f"fusion solde nul : 10 + 0 ({balances})")
    _expect(balances.get("Ancien Marcel") == 600, f"fusion compte ancien : 4 + 2 ({balances})")
    shutil.rmtree(src)

    # cas toujours bloquant : soldes réels des deux côtés, comptes récents
    src2 = Path(tempfile.mkdtemp(prefix="p5_bloq_"))
    sdir2 = src2 / "sources"
    sdir2.mkdir()
    _write_brest(
        sdir2 / "brest_lot.sql",
        [
            _user("0043", "Ambigu Recent", "3.00"),
            _user("7777", "Ambigu Recent", "1.00"),
        ],
    )
    db2 = src2 / "cible.db"
    _fresh_db(db2)
    code2 = _cli(["--run", "--source-dir", str(sdir2), "--database-url", f"sqlite:///{db2}"])
    _expect(code2 == 1, "soldes réels des deux côtés sur comptes récents -> exit 1")
    users2, total2, _ = _db_counts(db2)
    _expect((users2, total2) == (0, 0), f"base intacte après blocage ({users2},{total2})")
    shutil.rmtree(src2)

    # trois occurrences dont une à 0 : les deux soldes non nuls et récents
    # ne déclenchent aucune règle -> bloquant
    src3 = Path(tempfile.mkdtemp(prefix="p5_triple_"))
    sdir3 = src3 / "sources"
    sdir3.mkdir()
    _write_brest(
        sdir3 / "brest_lot.sql",
        [
            _user("0044", "Triple Cas", "5.00"),
            _user("6666", "Triple Cas", "0.00"),
            _user("5555", "Triple Cas", "2.00"),
        ],
    )
    db3 = src3 / "cible.db"
    _fresh_db(db3)
    code3 = _cli(["--run", "--source-dir", str(sdir3), "--database-url", f"sqlite:///{db3}"])
    _expect(code3 == 1, "deux soldes non nuls récents -> toujours bloquant")
    shutil.rmtree(src3)


def test_p5_brest_rejects_non_sql():
    with tempfile.TemporaryDirectory() as d:
        Path(d, "brest_export.json").write_text("[]")
        try:
            detect.scan(d, create=False)
            raise AssertionError("brest_*.json accepté à tort (R7)")
        except SourceError:
            pass


def test_p5_replay_refused_and_force():
    src = Path(tempfile.mkdtemp(prefix="p5_replay_"))
    sdir = src / "sources"
    sdir.mkdir()
    _write_brest(sdir / "brest_lot.sql", [_user("0041", "Unique Nom", "7.00")])
    db_path = src / "cible.db"
    _fresh_db(db_path)
    code = _cli(
        [
            "--run",
            "--keep-archives",
            "--source-dir",
            str(sdir),
            "--database-url",
            f"sqlite:///{db_path}",
        ]
    )
    _expect(code == 0, "première bascule OK")
    # le même lot (restauré depuis archives) ne doit pas être rejoué
    archived = next((sdir / "archives").rglob("brest_lot.sql"))
    shutil.copy(archived, sdir / "brest_lot.sql")
    before = _db_counts(db_path)
    code2 = _cli(["--run", "--source-dir", str(sdir), "--database-url", f"sqlite:///{db_path}"])
    _expect(code2 == 1, "rejeu du même lot refusé")
    after = _db_counts(db_path)
    _expect(before == after, "rejeu refusé : base inchangée")
    shutil.rmtree(src)


def test_p5_dispose_refuses_unconsumed():
    src = Path(tempfile.mkdtemp(prefix="p5_dispose_"))
    (src / "brest_lot.sql").write_text("")
    files = detect.scan(src, create=False)
    try:
        cleaner.dispose(files, source_dir=src, consumed=set())
        raise AssertionError("nettoyage autorisé sur fichier non consommé (R7)")
    except SourceError:
        pass
    _expect((src / "brest_lot.sql").exists(), "fichier non consommé préservé")
    shutil.rmtree(src)


def test_p5_safe_error_redacts_hashes():
    bcrypt = "$2a$10$wtlkmC9G3pGaid7Fw.vOYeek0JjVNe3eQ.RPKfOtMTEvDjLRljbjC"
    werkzeug = "pbkdf2:sha256:600000$salt$deadbeefdeadbeefdeadbeefdeadbeef"
    md5 = "5f4dcc3b5aa765d61d8327deb882cf99"
    text = _safe_error(RuntimeError(f"password_hash={bcrypt} legacy={werkzeug} hash={md5}"))
    _expect(
        bcrypt not in text and werkzeug not in text and md5 not in text,
        f"hashs expurgés du message ({text})",
    )
    _expect("<redacted>" in text, "marqueur de redaction présent")


def test_p5_engine_hides_parameters():
    from sqlalchemy import create_engine, text
    from sqlalchemy.exc import StatementError

    db_path = Path(tempfile.mkdtemp(prefix="p5_engine_")) / "e.db"
    engine = create_engine(f"sqlite:///{db_path}", hide_parameters=True)
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE t (x INTEGER UNIQUE)"))
        conn.execute(text("INSERT INTO t (x) VALUES (1)"))
    secret = "$2a$10$wtlkmC9G3pGaid7Fw.vOYeek0JjVNe3eQ.RPKfOtMTEvDjLRljbjC"
    try:
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO t (x) VALUES (:hash)"), {"hash": 1})
            conn.execute(text("INSERT INTO t (x) VALUES (:hash)"), {"hash": 1})
        raise AssertionError("violation d'unicité non levée")
    except StatementError as exc:
        rendered = _safe_error(exc)
        _expect(secret not in rendered, f"paramètre lié non exposé ({rendered})")
    shutil.rmtree(db_path.parent)


def test_p5_merge_key_and_birth_date():
    _expect(normalize_birth_date("1998-05-12") == "1998-05-12", "date ISO")
    _expect(normalize_birth_date("1998-05-12 00:00:00") == "1998-05-12", "datetime -> date")
    _expect(normalize_birth_date("illisible") is None, "date illisible -> None")
    _expect(normalize_birth_date(None) is None, "date absente")
    _expect(merge_key("Paul Debise", None, None) == "paul debise", "clé nom")
    _expect(
        merge_key("Paul Debise", None, "1998-05-12") == "paul debise|1998-05-12",
        "clé nom+naissance",
    )
    _expect(merge_key("Paul Debise", "P@x.fr", "1998-05-12") == "p@x.fr", "email dominant")
    # homonymes discriminés par la date de naissance
    a, _ = map_user_row(
        {"real_name": "Paul Debise", "date_naissance": "1998-05-12", "balance": "1.00"},
        "brest",
        "euros",
    )
    b, _ = map_user_row(
        {"real_name": "Paul Debise", "date_naissance": "1999-01-01", "balance": "1.00"},
        "brest",
        "euros",
    )
    _expect(a["key"] != b["key"], "homonymes -> clés distinctes")


def test_p5_disabled_imported():
    src = Path(tempfile.mkdtemp(prefix="p5_disabled_"))
    sdir = src / "sources"
    sdir.mkdir()
    _write_brest(
        sdir / "brest_lot.sql",
        [
            _user("0041", "Actif Compte", "1.00", disabled=0),
            _user("0042", "Desactive Compte", "2.00", disabled=1),
        ],
    )
    db_path = src / "cible.db"
    _fresh_db(db_path)
    code = _cli(["--run", "--source-dir", str(sdir), "--database-url", f"sqlite:///{db_path}"])
    _expect(code == 0, "bascule avec compte désactivé")
    c = sqlite3.connect(db_path)
    actif = c.execute("SELECT disabled FROM users WHERE name='Actif Compte'").fetchone()[0]
    off = c.execute("SELECT disabled FROM users WHERE name='Desactive Compte'").fetchone()[0]
    c.close()
    _expect((actif, off) == (0, 1), f"état disabled conservé ({actif},{off})")
    shutil.rmtree(src)


def test_p5_merge_without_email():
    src = Path(tempfile.mkdtemp(prefix="p5_merge_"))
    sdir = src / "sources"
    sdir.mkdir()
    _write_brest(
        sdir / "brest_lot.sql",
        [
            _user("0041", "Léo Martin", "10.00"),
            _user("0042", "Hugo Petit", "3.00"),
        ],
    )
    # Paris sans email : la fusion doit se faire sur le nom normalisé (R6)
    (sdir / "paris_export.json").write_text(
        json.dumps(
            {
                "etudiants": [
                    {"prenom": "Léo", "nom": "Martin", "promotion": 2026, "solde": "4,50"},
                    {"prenom": "Sarah", "nom": "Bernard", "promotion": 2026, "solde": "2,00"},
                ]
            }
        ),
        encoding="utf-8",
    )
    db_path = src / "cible.db"
    _fresh_db(db_path)
    code = _cli(["--run", "--source-dir", str(sdir), "--database-url", f"sqlite:///{db_path}"])
    _expect(code == 0, "bascule sans email OK")
    c = sqlite3.connect(db_path)
    merged = c.execute(
        "SELECT COUNT(*) FROM users u JOIN wallets w ON w.user_id = u.id "
        "WHERE u.name='Léo Martin' GROUP BY u.id HAVING COUNT(*) = 2"
    ).fetchall()
    total = c.execute("SELECT SUM(balance) FROM wallets").fetchone()[0]
    c.close()
    _expect(len(merged) == 1, "fusion inter-campus sans email (nom normalisé)")
    _expect(total == 1000 + 300 + 450 + 200, f"tous les soldes projetés ({total})")
    shutil.rmtree(src)


def test_paris_csv_clients_and_articles():
    # Export caisse Paris réel (18/09) : CP1252, décimales à la virgule,
    # table source déduite du nom de fichier, doublons de nom (soldes cumulés),
    # familles -> types cibles, articles hors vente inactifs.
    from migration.etl import SourceReader
    from migration.parsing.files import table_from_filename

    _expect(table_from_filename("paris_clients_1809.csv") == "clients", "table déduite clients")
    _expect(table_from_filename("paris_articles_1809.csv") == "articles", "table déduite articles")

    clients = (
        "Nom;Adresse;Code postal;Ville;Solde\r\n"
        '"MARTIN Léo";1 rue Vavy;29200;Brest;"12,40"\r\n'
        '"MARTIN Léo";;;;3,00\r\n'
        '"BERNARD Sarah";;;;0\r\n'
        ";;;;0,00\r\n"
        '"PRINTEMPS Élodie";;;;-2,05\r\n'
    )
    articles = (
        "Référence;Libellé;Famille;Imprimante;Taux TVA;Taux TVA à emporter;"
        "Tarif de base;Clavier;Poids ouvert;Prix ouvert;Code barre\r\n"
        '1;TsingTao;"Bières Bouteilles";;0,200;;1,50;;Non;Non;\r\n'
        '2;Merlot;"Vins/alcools";;0,200;;1,00;;Non;Non;\r\n'
        '3;Café;"Boissons chaudes";;0,200;;0,50;;Non;Non;\r\n'
        '4;Duvel;"A ne pas ouvrir";;0,200;;2,00;;Non;Non;\r\n'
        '5;ne pas utiliser;"Bières Bouteilles";;0,200;;;;Non;Non;\r\n'
    )
    with tempfile.TemporaryDirectory() as d:
        src = Path(d) / "sources"
        src.mkdir()
        (src / "paris_clients_1809.csv").write_bytes(clients.encode("cp1252"))
        (src / "paris_articles_1809.csv").write_bytes(articles.encode("cp1252"))
        files = detect.scan(src, create=False)
        _expect(
            sorted(f.path.name for f in files)
            == ["paris_articles_1809.csv", "paris_clients_1809.csv"],
            "CSV Paris détectés",
        )
        reader = SourceReader(files, "euros", 100, conn=None)
        users, seen, warnings, raw_total = {}, {}, 0, 0
        for campus, _filename, user, warning, raw in reader.iter_users():
            _expect(campus == "paris", "campus paris")
            raw_total += raw
            if warning:
                warnings += 1
            if user is not None:
                seen[user["name"]] = seen.get(user["name"], 0) + 1
                users.setdefault(user["name"], user)
        _expect(len(users) == 3 and warnings == 1, f"3 comptes + 1 ligne sans nom ({users})")
        _expect(seen["MARTIN Léo"] == 2, "les deux lignes du doublon sont mappées")
        leo = users["MARTIN Léo"]
        _expect(
            leo["balance_cents"] == 1240 and leo["key"] == "martin leo",
            f"clé de réconciliation = nom normalisé ({leo['key']})",
        )
        _expect(users["PRINTEMPS Élodie"]["balance_cents"] == -205, "CP1252 + virgule + négatif")
        _expect(raw_total == 1335, f"somme brute toutes lignes ({raw_total})")

        catalog = {}
        for entity, campus, obj in reader.iter_catalog():
            _expect(entity == "articles" and campus == "paris", "catalogue paris")
            catalog[obj["name"]] = obj
        tsingtao = catalog["TsingTao"]
        _expect(tsingtao["src_id"] == "1", "Référence -> src_id")
        _expect(
            tsingtao["article_type"] == "biere" and tsingtao["is_alcohol"],
            "famille Bières Bouteilles -> biere (contrôle alcool)",
        )
        _expect(
            tsingtao["price_std_cents"] == 150 and tsingtao["price_team_cents"] == 150,
            "Tarif de base -> prix public, prix équipe = prix public",
        )
        _expect(catalog["Merlot"]["article_type"] == "vin", "Vins/alcools -> vin")
        _expect(catalog["Café"]["article_type"] == "snack", "Boissons chaudes -> snack")
        _expect(catalog["Duvel"]["active"] is False, "famille « A ne pas ouvrir » -> inactif")
        _expect(catalog["ne pas utiliser"]["active"] is False, "libellé hors vente -> inactif")


def test_e2e_paris_csv_run():
    src = Path(tempfile.mkdtemp(prefix="mig_csv_"))
    db_path = src / "cible.db"
    sdir = src / "sources"
    sdir.mkdir()
    _write_brest(sdir / "brest_lot.sql", [_user("0041", "Léo Martin", "10.00")])
    clients = (
        "Nom;Adresse;Code postal;Ville;Solde\r\n"
        '"Léo Martin";;;;12,40\r\n'
        '"LEO MARTIN";;;;3,00\r\n'
        '"BERNARD Sarah";;;;0\r\n'
        ";;;;0,00\r\n"
    )
    articles = (
        "Référence;Libellé;Famille;Imprimante;Taux TVA;Taux TVA à emporter;"
        "Tarif de base;Clavier;Poids ouvert;Prix ouvert;Code barre\r\n"
        '1;TsingTao;"Bières Bouteilles";;0,200;;1,50;;Non;Non;\r\n'
        '4;Duvel;"A ne pas ouvrir";;0,200;;2,00;;Non;Non;\r\n'
    )
    (sdir / "paris_clients_1809.csv").write_bytes(clients.encode("cp1252"))
    (sdir / "paris_articles_1809.csv").write_bytes(articles.encode("cp1252"))
    _fresh_db(db_path)
    code = _cli(["--run", "--source-dir", str(sdir), "--database-url", f"sqlite:///{db_path}"])
    _expect(code == 0, "run avec CSV Paris OK")
    c = sqlite3.connect(db_path)
    # fusion inter-campus sur le nom normalisé : Léo Martin (Brest) +
    # MARTIN Léo (Paris, doublon CSV cumulé) -> un compte, deux portefeuilles
    leo_id = c.execute("SELECT id FROM users WHERE username='leo.martin'").fetchone()[0]
    wallets = dict(
        c.execute("SELECT campus, balance FROM wallets WHERE user_id=?", (leo_id,)).fetchall()
    )
    _expect(wallets == {"brest": 1000, "paris": 1540}, f"fusion + doublon cumulé ({wallets})")
    sarah = c.execute(
        "SELECT COUNT(*) FROM wallets w JOIN users u ON u.id = w.user_id "
        "WHERE u.name='BERNARD Sarah' AND w.balance=0 AND w.campus='paris'"
    ).fetchone()[0]
    _expect(sarah == 1, "compte Paris à solde nul créé")
    tsingtao = c.execute(
        "SELECT price_std, price_team, article_type, is_alcohol, active, campus "
        "FROM articles WHERE name='TsingTao'"
    ).fetchone()
    _expect(tsingtao == (150, 150, "biere", 1, 1, "paris"), f"article CSV projeté ({tsingtao})")
    duvel = c.execute(
        "SELECT active, price_std, campus FROM articles WHERE name='Duvel'"
    ).fetchone()
    _expect(duvel == (0, 200, "paris"), f"article hors vente importé inactif ({duvel})")
    c.close()
    remaining = [p.name for p in sdir.iterdir()]
    _expect(remaining == [], f"sources supprimées après run ({remaining})")
    shutil.rmtree(src)


def test_p5_transfer_ambiguity_still_pairs():
    src = Path(tempfile.mkdtemp(prefix="p5_tr_"))
    sdir = src / "sources"
    sdir.mkdir()
    users = [
        _user("0041", "Donneur Un", "0.00"),
        _user("0042", "Donneur Deux", "0.00"),
        _user("0043", "Benef Un", "0.00"),
        _user("0044", "Benef Deux", "0.00"),
    ]
    transfert = [
        (1, "2026-09-03 10:00:00", "0042", "-5.00", "0041"),
        (2, "2026-09-03 10:00:00", "0041", "-5.00", "0041"),
        (3, "2026-09-03 10:00:00", "0043", "5.00", "0041"),
        (4, "2026-09-03 10:00:00", "0044", "5.00", "0041"),
    ]
    _write_brest(sdir / "brest_lot.sql", users, transfert=transfert)
    db_path = src / "cible.db"
    _fresh_db(db_path)
    code = _cli(["--run", "--source-dir", str(sdir), "--database-url", f"sqlite:///{db_path}"])
    _expect(code == 0, "transferts ambigus migrés")
    c = sqlite3.connect(db_path)
    n = c.execute("SELECT COUNT(*) FROM transactions WHERE type='transfert'").fetchone()[0]
    paired = c.execute(
        "SELECT COUNT(*) FROM transactions WHERE type='transfert' "
        "AND from_user_id IS NOT NULL AND to_user_id IS NOT NULL"
    ).fetchone()[0]
    c.close()
    _expect(n == 2 and paired == 2, f"2 transferts appariés ({n},{paired})")
    shutil.rmtree(src)


def main():
    tests = [
        (name, fn)
        for name, fn in sorted(globals().items())
        if name.startswith("test_") and callable(fn)
    ]
    failures = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  OK   {name}")
        except Exception as exc:
            failures += 1
            print(f"  FAIL {name}: {exc}")
    print(f"\n{len(tests) - failures}/{len(tests)} tests OK")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
