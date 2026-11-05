"""Mode mono-campus (option A) : Brest seul, Paris archivé.

Exécutable sans pytest : python -m tests.test_single_campus

FOYZ_ACTIVE_CAMPUSES est forcé à « brest » avant l'import de l'application.
La suite vérifie la configuration, les accès, les pages publiques, les écrans
d'administration, les statistiques, l'API et la passerelle, ainsi que l'absence
de création de données Paris à l'affichage.
"""

import os
import re
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Le package tests a posé « brest,paris » par défaut : ce scénario l'écrase
# avant tout import de app.
os.environ["FOYZ_ACTIVE_CAMPUSES"] = "brest"

_TMP = Path(tempfile.mkdtemp(prefix="foyz_single_campus_"))
os.environ["DATABASE_URL"] = (
    os.environ.get("FOYZ_TEST_DATABASE_URL") or f"sqlite:///{_TMP / 'app.db'}"
)
os.environ["UPLOAD_DIR"] = str(_TMP / "uploads")
os.environ["SECRET_KEY"] = "test-secret-key-0123456789abcdef0123456789abcdef"
os.environ["ADMIN_PASSWORD"] = "mot-de-passe-admin"
os.environ.pop("FLASK_ENV", None)

from werkzeug.security import generate_password_hash  # noqa: E402

from app import create_app  # noqa: E402
from app.campus import (  # noqa: E402
    CAMPUS_LABELS,
    CAMPUSSES,
    DEFAULT_CAMPUS,
    parse_active_campuses,
)
from app.extensions import db  # noqa: E402
from app.models import (  # noqa: E402
    ApiToken,
    Article,
    AuditLog,
    Event,
    Keg,
    KegPrice,
    Setting,
    Transaction,
    TransactionLine,
    User,
    Wallet,
)
from app.services import api_tokens as AT  # noqa: E402
from app.services import stats as ST  # noqa: E402
from app.services import transactions as T  # noqa: E402
from app.services.treasury import treasury  # noqa: E402
from app.utils import utcnow  # noqa: E402

ADMIN_PASSWORD = "mot-de-passe-admin"
PARIS_TOKEN = "fz_paris_token_for_single_campus_test"
_state = {}


def _expect(cond, label):
    if not cond:
        raise AssertionError(f"échec : {label}")


def _csrf(html):
    match = re.search(r'name="csrf-token" content="([^"]+)"', html)
    _expect(match is not None, "jeton CSRF présent")
    return match.group(1)


def _login(client, username, password, campus="brest"):
    html = client.get("/connexion").get_data(as_text=True)
    return client.post(
        "/connexion",
        data={
            "username": username,
            "password": password,
            "campus": campus,
            "_csrf": _csrf(html),
        },
    )


def _setup():
    if _state:
        return _state
    app = create_app()
    with app.app_context():
        student = User(
            name="Mono Etudiant",
            username="mono.etudiant",
            promotion=2027,
            password_hash=generate_password_hash("secret123"),
        )
        paris_team = User(
            name="Mono Paris",
            username="mono.paris",
            password_hash=generate_password_hash("secret123"),
            team_status="mandat",
            team_campus="paris",
        )
        db.session.add_all([student, paris_team])
        db.session.flush()
        student.wallet("brest").balance = 5_000
        db.session.add(Wallet(user=student, campus="paris", balance=250))

        brest_article = Article(
            name="Mono Brest", article_type="biere", campus="brest", price_std=250
        )
        paris_article = Article(
            name="Mono Paris", article_type="biere", campus="paris", price_std=300
        )
        db.session.add_all([brest_article, paris_article])
        db.session.flush()

        keg = Keg(name="Mono Fut", alcohol_degree=5.0, volume_l=30)
        db.session.add(keg)
        db.session.flush()
        db.session.add_all(
            [
                KegPrice(
                    keg=keg,
                    campus="paris",
                    price_half_std=100,
                    price_pint_std=200,
                    price_pot_std=150,
                ),
                KegPrice(
                    keg=keg,
                    campus="brest",
                    price_half_std=200,
                    price_pint_std=400,
                    price_pot_std=300,
                ),
            ]
        )

        brest_event = Event(
            name="Mono Soiree Brest",
            campus="brest",
            starts_at=utcnow() - timedelta(hours=1),
            ends_at=utcnow() + timedelta(hours=1),
            token="mono-brest-token",
        )
        paris_event = Event(
            name="Mono Soiree Paris",
            campus="paris",
            starts_at=utcnow() - timedelta(hours=1),
            ends_at=utcnow() + timedelta(hours=1),
            token="mono-paris-token",
        )
        db.session.add_all([brest_event, paris_event])

        paris_token = ApiToken(
            label="Mono jeton Paris",
            token_hash=AT.hash_token(PARIS_TOKEN),
            token_prefix=PARIS_TOKEN[:12],
            campus="paris",
            history_from=utcnow() - timedelta(days=1),
            history_to=utcnow() + timedelta(days=1),
            expires_at=utcnow() + timedelta(days=1),
        )
        db.session.add(paris_token)

        brest_sale = Transaction(type="direct", campus="brest", total=1_234, payment_method="cb")
        paris_sale = Transaction(type="direct", campus="paris", total=9_999, payment_method="cb")
        db.session.add_all(
            [
                brest_sale,
                paris_sale,
                Transaction(type="rechargement", campus="brest", total=500, payment_method="cb"),
                Transaction(type="rechargement", campus="paris", total=700, payment_method="cb"),
                AuditLog(
                    actor="Ancienne equipe",
                    campus="paris",
                    action="compte.modification",
                    target="Archive Paris",
                ),
            ]
        )
        db.session.flush()
        db.session.add_all(
            [
                TransactionLine(
                    transaction_id=brest_sale.id,
                    article_id=brest_article.id,
                    article_name=brest_article.name,
                    article_type="biere",
                    quantity=2,
                    unit_price=617,
                    line_total=1_234,
                ),
                TransactionLine(
                    transaction_id=paris_sale.id,
                    article_id=paris_article.id,
                    article_name=paris_article.name,
                    article_type="biere",
                    quantity=3,
                    unit_price=3_333,
                    line_total=9_999,
                ),
            ]
        )
        db.session.commit()

        admin_password_paris = db.session.get(Setting, "admin_password_hash_paris")
        state = {
            "app": app,
            "student_id": student.id,
            "paris_team_id": paris_team.id,
            "brest_article_id": brest_article.id,
            "paris_article_id": paris_article.id,
            "brest_event_id": brest_event.id,
            "paris_event_id": paris_event.id,
            "keg_id": keg.id,
            "admin_password_paris": admin_password_paris,
        }
    client = app.test_client()
    res = _login(client, "admin", ADMIN_PASSWORD, campus="paris")
    _expect(res.status_code == 302, f"connexion admin ({res.status_code})")
    state["client"] = client
    _state.update(state)
    return _state


def test_configuration_et_parsing():
    _expect(CAMPUSSES == {"brest": "Brest"}, f"campus actifs ({CAMPUSSES})")
    _expect(CAMPUS_LABELS.get("paris") == "Paris", "Paris reste libellable")
    _expect(DEFAULT_CAMPUS == "brest", "campus par défaut")
    _expect(parse_active_campuses(" Brest , brest ") == ("brest",), "casse et doublons")
    for raw in ("", "lyon", "brest,lyon", ",,,"):
        try:
            parse_active_campuses(raw)
        except RuntimeError:
            continue
        raise AssertionError(f"valeur invalide acceptée : {raw!r}")


def test_init_db_ne_touche_pas_au_mot_de_passe_paris():
    _expect(_setup()["admin_password_paris"] is None, "aucun hash admin Paris créé")


def test_connexion_mono_campus():
    state = _setup()
    html = state["client"].get("/connexion").get_data(as_text=True)
    _expect('value="paris"' not in html, "aucun choix Paris")
    _expect("Brest" in html, "campus Brest affiché")
    with state["client"].session_transaction() as sess:
        _expect(sess.get("campus") == "brest", "campus de session Brest")


def test_membre_equipe_paris_refuse():
    state = _setup()
    client = state["app"].test_client()
    res = _login(client, "mono.paris", "secret123")
    _expect(res.status_code == 401, f"connexion Paris refusée ({res.status_code})")
    body = res.get_data(as_text=True)
    _expect("désactivé" in body, "message de campus désactivé")
    with client.session_transaction() as sess:
        _expect("user_id" not in sess, "aucune session ouverte")


def test_pages_publiques_sans_paris():
    client = _setup()["client"]
    home = client.get("/").get_data(as_text=True)
    _expect("Mono Soiree Brest" in home, "événement Brest affiché")
    _expect("Mono Soiree Paris" not in home, "événement Paris masqué")

    catalogue = client.get("/catalogue").get_data(as_text=True)
    _expect("Mono Brest" in catalogue, "article Brest affiché")
    _expect("Mono Paris" not in catalogue, "article Paris masqué")

    res = client.get("/catalogue?campus=paris")
    _expect(res.status_code == 302, f"campus Paris redirigé ({res.status_code})")
    _expect("campus=brest" in res.headers["Location"], "redirection vers Brest")

    reglement = client.get("/reglement").get_data(as_text=True)
    _expect("Paris" not in reglement, "aucun règlement Paris")


def test_admin_ignore_le_campus_paris():
    client = _setup()["client"]
    articles = client.get("/admin/articles?campus=paris").get_data(as_text=True)
    _expect("Mono Brest" in articles, "catalogue Brest servi")
    _expect("Mono Paris" not in articles, "catalogue Paris absent")
    comptes = client.get("/admin/comptes").get_data(as_text=True)
    _expect("Solde Brest" in comptes, "colonne Brest")
    _expect("Solde Paris" not in comptes, "aucune colonne Paris")


def test_affichage_ne_cree_aucune_donnee_paris():
    state = _setup()
    client = state["client"]
    with state["app"].app_context():
        before_wallets = db.session.query(Wallet).filter_by(campus="paris").count()
        before_prices = db.session.query(KegPrice).filter_by(campus="paris").count()

    client.get("/admin/comptes")
    client.get(f"/admin/comptes/{state['student_id']}")
    keg_page = client.get(f"/admin/tireuses/kegs/{state['keg_id']}")
    _expect(keg_page.status_code == 200, f"formulaire fût servi ({keg_page.status_code})")

    with state["app"].app_context():
        after_wallets = db.session.query(Wallet).filter_by(campus="paris").count()
        after_prices = db.session.query(KegPrice).filter_by(campus="paris").count()
        _expect(after_wallets == before_wallets, "aucun portefeuille Paris créé")
        _expect(after_prices == before_prices, "aucun prix de fût Paris créé")

    res = client.post(
        f"/admin/tireuses/kegs/{state['keg_id']}",
        data={
            "_csrf": _csrf(keg_page.get_data(as_text=True)),
            "name": "Mono Fut",
            "alcohol_degree": "5",
            "volume_l": "30",
            "brest_half_std": "2.00",
            "brest_pint_std": "4.00",
            "brest_pot_std": "3.00",
            "brest_half_team": "1.80",
            "brest_pint_team": "3.60",
            "brest_pot_team": "2.70",
        },
    )
    _expect(res.status_code == 302, f"fût Brest enregistré ({res.status_code})")
    with state["app"].app_context():
        keg = db.session.get(Keg, state["keg_id"])
        paris_row = next(price for price in keg.prices if price.campus == "paris")
        _expect(
            (paris_row.price_half_std, paris_row.price_pint_std, paris_row.price_pot_std)
            == (100, 200, 150),
            "prix Paris inchangés par une modification Brest",
        )

    res = client.post(
        f"/admin/tireuses/kegs/{state['keg_id']}/supprimer",
        data={"_csrf": _csrf(keg_page.get_data(as_text=True))},
    )
    _expect(res.status_code == 302, f"suppression refusée sans erreur ({res.status_code})")
    with state["app"].app_context():
        _expect(db.session.get(Keg, state["keg_id"]) is not None, "fût Paris conservé")


def test_creation_de_compte_un_seul_portefeuille():
    state = _setup()
    client = state["client"]
    html = client.get("/admin/comptes").get_data(as_text=True)
    res = client.post(
        "/admin/comptes/nouveau",
        data={
            "_csrf": _csrf(html),
            "name": "Mono Nouveau",
            "username": "mono.nouveau",
        },
    )
    _expect(res.status_code == 302, f"création acceptée ({res.status_code})")
    with state["app"].app_context():
        user = db.session.query(User).filter_by(username="mono.nouveau").one()
        _expect({w.campus for w in user.wallets} == {"brest"}, "portefeuille Brest seul")


def test_statistiques_et_historique_scope_brest():
    state = _setup()
    with state["app"].app_context():
        totals = ST.category_totals({"all_time": "1", "campus": ""})
        _expect(totals["grand_total"]["revenue"] == 1_234, "recettes Brest seules")
        visible = T.visible_transactions().all()
        _expect({t.campus for t in visible} == {"brest"}, "historique limité à Brest")
        try:
            T.create_purchase(
                operator_label="test",
                campus="paris",
                items=[{"article_id": state["paris_article_id"], "quantity": 1}],
                direct=True,
                payment_method="cb",
            )
        except T.OperationError:
            pass
        else:
            raise AssertionError("vente Paris acceptée")


def test_tresorerie_et_api():
    state = _setup()
    client = state["client"]
    with state["app"].app_context():
        report = treasury("paris")
        _expect(sum(e["reloads_total"] for e in report) == 500, "trésorerie repliée sur Brest")
    page = client.get("/equipe/tresorerie?campus=paris")
    _expect(page.status_code == 200, "page trésorerie servie")
    _expect("Suivi trésorerie — Brest" in page.get_data(as_text=True), "libellé Brest")
    _expect(client.get("/api/treasury?campus=paris").status_code == 404, "API trésorerie Paris")


def test_jetons_api_paris_refuses():
    state = _setup()
    with state["app"].app_context():
        _expect(AT.resolve_token(PARIS_TOKEN) is None, "jeton Paris non résolu")
        try:
            AT.validate_token_fields(
                "paris",
                "Jeton interdit",
                utcnow() + timedelta(days=1),
                utcnow(),
                utcnow() + timedelta(hours=1),
                (True, False, False, False),
            )
        except ValueError:
            pass
        else:
            raise AssertionError("campus Paris accepté pour un jeton")
    client = state["app"].test_client()
    res = client.get("/api/v1/catalog", headers={"Authorization": f"Bearer {PARIS_TOKEN}"})
    _expect(res.status_code == 401, f"API Paris refusée ({res.status_code})")

    form = state["client"].get("/admin/api/nouveau")
    _expect(form.status_code == 200, "formulaire jeton servi")
    _expect('value="paris"' not in form.get_data(as_text=True), "aucune option Paris")


def test_passerelle_paris_refusee():
    client = _setup()["app"].test_client()
    _expect(client.get("/passerelle/mono-paris-token").status_code == 404, "page passerelle")
    html = client.get("/connexion").get_data(as_text=True)
    res = client.post(
        "/passerelle/mono-paris-token/encaisser",
        json={"items": []},
        headers={"X-CSRFToken": _csrf(html)},
    )
    _expect(res.status_code == 404, f"encaissement passerelle ({res.status_code})")


def test_libelles_historiques_sans_erreur():
    state = _setup()
    client = state["client"]
    journal = client.get("/admin/journal?tab=actions")
    _expect(journal.status_code == 200, f"journal ({journal.status_code})")
    _expect("Paris" in journal.get_data(as_text=True), "libellé Paris historique")
    fiche = client.get(f"/admin/comptes/{state['student_id']}")
    _expect(fiche.status_code == 200, f"fiche compte ({fiche.status_code})")
    html = fiche.get_data(as_text=True)
    _expect("campus désactivé" in html and "Paris" in html, "portefeuille Paris signalé")


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
