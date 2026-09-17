"""Tests des services de caisse et des autorisations (phase 9, T-9.1).

Exécutable sans pytest : python -m tests.test_caisse

Couvre les chemins critiques non testés jusqu'ici : partage des montants,
prix équipe, découvert + mot de passe administrateur, blacklists, consignes,
rechargement/retrait/transfert, annulations, portée des articles
d'événement, validation (aucune entrée utilisateur ne doit produire un 500),
CSRF, autorisations API et mode lecture seule de l'autre campus.

Compatible SQLite et PostgreSQL : si FOYZ_TEST_DATABASE_URL est défini, la
suite s'exécute sur cette base (le schéma doit être migré au préalable).
"""

import itertools
import os
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="foyz_caisse_"))
os.environ["DATABASE_URL"] = (
    os.environ.get("FOYZ_TEST_DATABASE_URL") or f"sqlite:///{_TMP / 'app.db'}"
)
os.environ["UPLOAD_DIR"] = str(_TMP / "uploads")
os.environ["SECRET_KEY"] = "test-secret-key-0123456789abcdef0123456789abcdef"
os.environ["ADMIN_PASSWORD"] = "mot-de-passe-admin"
os.environ.pop("FLASK_ENV", None)

from datetime import timedelta  # noqa: E402

from sqlalchemy import func, select  # noqa: E402
from werkzeug.security import generate_password_hash  # noqa: E402

from app import create_app  # noqa: E402
from app.extensions import db  # noqa: E402
from app.models import Article, Event, Transaction, User  # noqa: E402
from app.services import transactions as T  # noqa: E402
from app.services.settings import set_setting  # noqa: E402
from app.utils import utcnow  # noqa: E402

ADMIN_PASSWORD = "mot-de-passe-admin"
_seq = itertools.count(1)


def _expect(cond, label):
    if not cond:
        raise AssertionError(f"échec : {label}")


def _refus(code, fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except T.OperationError as exc:
        _expect(exc.code == code, f"code {code} attendu, obtenu {exc.code} ({exc.message})")
        return exc
    raise AssertionError(f"échec : refus {code} attendu, aucune erreur levée")


def _csrf(html, field="_csrf"):
    pattern = (
        r'name="csrf-token" content="([^"]+)"'
        if field == "csrf-token"
        else r'name="_csrf" value="([^"]+)"'
    )
    match = re.search(pattern, html)
    _expect(match is not None, f"jeton CSRF {field} présent")
    return match.group(1)


def _login(client, username, password, campus="brest"):
    token = _csrf(client.get("/connexion").get_data(as_text=True))
    res = client.post(
        "/connexion",
        data={"username": username, "password": password, "campus": campus, "_csrf": token},
    )
    _expect(res.status_code == 302, f"connexion {username} sur {campus} ({res.status_code})")


def _user(
    campus="brest",
    *,
    team=False,
    balance=0,
    blacklist=False,
    blacklist_alcohol=False,
    password="secret123",
):
    n = next(_seq)
    user = User(
        name=f"Caisse Test {n}",
        username=f"caisse.test.{n}",
        password_hash=generate_password_hash(password),
        team_status="mandat" if team else "etudiant",
        team_campus=campus if team else None,
        blacklist=blacklist,
        blacklist_alcohol=blacklist_alcohol,
    )
    db.session.add(user)
    db.session.flush()
    user.wallet(campus).balance = balance
    db.session.commit()
    return user


def _article(
    name=None,
    *,
    kind="biere",
    alcohol=True,
    std=300,
    team=None,
    active=True,
    tap=False,
    volume_cl=None,
    keg_id=None,
    event_id=None,
    tap_number=None,
):
    article = Article(
        name=name or f"Article {next(_seq)}",
        article_type=kind,
        is_alcohol=alcohol,
        price_std_brest=std,
        price_std_paris=std,
        price_team_brest=std if team is None else team,
        price_team_paris=std if team is None else team,
        active=active,
        is_tap=tap,
        volume_cl=volume_cl,
        keg_id=keg_id,
        event_id=event_id,
        tap_number=tap_number,
    )
    db.session.add(article)
    db.session.commit()
    return article


def _balance(user_id, campus="brest"):
    return db.session.get(User, user_id).wallet(campus).balance


def _glasses(user_id, campus="brest"):
    return db.session.get(User, user_id).wallet(campus).glasses_outstanding


def _purchase_count():
    return db.session.scalar(select(func.count(Transaction.id)))


def test_achat_partage_exact_et_prix_equipe():
    app = create_app()
    with app.app_context():
        article = _article(std=300, team=250)
        team_a = _user(team=True, balance=1000)
        team_b = _user(team=True, balance=1000)
        t = T.create_purchase(
            operator_label="test",
            campus="brest",
            items=[{"article_id": article.id, "quantity": 1}],
            contributor_ids=[team_a.id, team_b.id],
        )
        _expect(t.total == 250, f"total au prix équipe (obtenu {t.total})")
        _expect(
            _balance(team_a.id) == 875 and _balance(team_b.id) == 875, "débit équilibré 125 / 125"
        )

        std_a = _user(balance=1000)
        std_b = _user(balance=1000)
        t2 = T.create_purchase(
            operator_label="test",
            campus="brest",
            items=[{"article_id": article.id, "quantity": 2}],
            contributor_ids=[std_a.id, std_b.id],
        )
        _expect(t2.total == 600, f"total au prix standard (obtenu {t2.total})")
        _expect(_balance(std_a.id) == 700 and _balance(std_b.id) == 700, "300 chacun")


def test_achat_arrondi_du_partage():
    app = create_app()
    with app.app_context():
        article = _article(std=100)
        users = [_user(balance=1000) for _ in range(3)]
        t = T.create_purchase(
            operator_label="test",
            campus="brest",
            items=[{"article_id": article.id, "quantity": 1}],
            contributor_ids=[u.id for u in users],
        )
        _expect(t.total == 100, f"total 100 (obtenu {t.total})")
        balances = [_balance(u.id) for u in users]
        debits = [1000 - b for b in balances]
        _expect(sum(debits) == 100, f"la somme des débits est exacte ({debits})")
        _expect(max(debits) - min(debits) <= 1, f"écart d'un centime au plus ({debits})")


def test_decouvert_mot_de_passe_admin_et_limite():
    app = create_app()
    with app.app_context():
        article = _article(std=300)
        user = _user(balance=100)
        before = _purchase_count()
        exc = _refus(
            "admin_password_required",
            T.create_purchase,
            operator_label="test",
            campus="brest",
            items=[{"article_id": article.id, "quantity": 1}],
            contributor_ids=[user.id],
        )
        _expect(
            user.display_name in exc.extra.get("negative_users", []),
            "le refus nomme l'étudiant concerné",
        )
        _expect(_purchase_count() == before, "aucune transaction écrite sans mot de passe")

        T.create_purchase(
            operator_label="test",
            campus="brest",
            items=[{"article_id": article.id, "quantity": 1}],
            contributor_ids=[user.id],
            admin_password=ADMIN_PASSWORD,
        )
        _expect(_balance(user.id) == -200, f"négatif autorisé -200 (obtenu {_balance(user.id)})")

        big = _article(std=1000)
        pauvre = _user(balance=0)
        _refus(
            "overdraft_limit",
            T.create_purchase,
            operator_label="test",
            campus="brest",
            items=[{"article_id": big.id, "quantity": 1}],
            contributor_ids=[pauvre.id],
            admin_password=ADMIN_PASSWORD,
        )
        _expect(_balance(pauvre.id) == 0, "solde intact après refus du découvert maximum")


def test_blacklists():
    app = create_app()
    with app.app_context():
        biere = _article(kind="biere", alcohol=True, std=300)
        soft = _article(kind="soft", alcohol=False, std=200)
        bloque = _user(balance=1000, blacklist=True)
        _refus(
            "blacklist",
            T.create_purchase,
            operator_label="test",
            campus="brest",
            items=[{"article_id": soft.id, "quantity": 1}],
            contributor_ids=[bloque.id],
        )
        alcool = _user(balance=1000, blacklist_alcohol=True)
        _refus(
            "alcohol",
            T.create_purchase,
            operator_label="test",
            campus="brest",
            items=[{"article_id": biere.id, "quantity": 1}],
            contributor_ids=[alcool.id],
        )
        t = T.create_purchase(
            operator_label="test",
            campus="brest",
            items=[{"article_id": soft.id, "quantity": 1}],
            contributor_ids=[alcool.id],
        )
        _expect(t.total == 200, "blacklist alcool : les articles sans alcool restent permis")


def test_consigne_aller_retour():
    app = create_app()
    with app.app_context():
        article = _article(std=300)
        user = _user(balance=1000)
        t = T.create_purchase(
            operator_label="test",
            campus="brest",
            items=[{"article_id": article.id, "quantity": 1}],
            contributor_ids=[user.id],
            deposit_glasses=2,
        )
        _expect(t.total == 500, f"total = article + 2 consignes (obtenu {t.total})")
        _expect(_glasses(user.id) == 2, "2 verres consignés")
        _expect(_balance(user.id) == 500, f"500 débités (obtenu {_balance(user.id)})")

        retour = T.return_glasses(operator_label="test", campus="brest", user=user, count=2)
        _expect(retour.total == 200 and retour.type == "consigne", "retour crédité")
        _expect(
            _glasses(user.id) == 0 and _balance(user.id) == 700, "verres rendus et solde crédité"
        )
        _refus(
            "invalid", T.return_glasses, operator_label="test", campus="brest", user=user, count=1
        )
        _refus(
            "invalid", T.return_glasses, operator_label="test", campus="brest", user=user, count="x"
        )

        set_setting("deposit_enabled", "0")
        db.session.commit()
        _refus(
            "invalid",
            T.create_purchase,
            operator_label="test",
            campus="brest",
            items=[{"article_id": article.id, "quantity": 1}],
            contributor_ids=[user.id],
            deposit_glasses=1,
        )
        set_setting("deposit_enabled", "1")
        db.session.commit()


def test_mouvements_rechargement_retrait_transfert():
    app = create_app()
    with app.app_context():
        user = _user(balance=500)
        t = T.create_reload(
            operator_label="test",
            campus="brest",
            user=user,
            amount_cents=1000,
            payment_method="cb",
        )
        _expect(t.total == 1000 and _balance(user.id) == 1500, "rechargement crédité")
        _refus(
            "invalid",
            T.create_reload,
            operator_label="test",
            campus="brest",
            user=user,
            amount_cents=0,
            payment_method="cb",
        )
        _refus(
            "invalid",
            T.create_reload,
            operator_label="test",
            campus="brest",
            user=user,
            amount_cents=100,
            payment_method="cheque",
        )
        _refus(
            "invalid",
            T.create_reload,
            operator_label="test",
            campus="brest",
            user=user,
            amount_cents="abc",
            payment_method="cb",
        )

        T.create_withdrawal(operator_label="test", campus="brest", user=user, amount_cents=500)
        _expect(_balance(user.id) == 1000, "retrait débité")
        _refus(
            "solde",
            T.create_withdrawal,
            operator_label="test",
            campus="brest",
            user=user,
            amount_cents=99999,
        )
        _refus(
            "invalid",
            T.create_withdrawal,
            operator_label="test",
            campus="brest",
            user=user,
            amount_cents="abc",
        )

        autre = _user(balance=0)
        t = T.create_transfer(
            operator_label="test", campus="brest", from_user=user, to_user=autre, amount_cents=400
        )
        _expect(_balance(user.id) == 600 and _balance(autre.id) == 400, "transfert équilibré")
        _refus(
            "invalid",
            T.create_transfer,
            operator_label="test",
            campus="brest",
            from_user=user,
            to_user=user,
            amount_cents=100,
        )
        _refus(
            "solde",
            T.create_transfer,
            operator_label="test",
            campus="brest",
            from_user=autre,
            to_user=user,
            amount_cents=99999,
        )
        _expect(t.type == "transfert", "type transfert")


def test_annulation_achat_consigne_et_direct():
    app = create_app()
    with app.app_context():
        article = _article(std=300)
        user = _user(balance=1000)
        t = T.create_purchase(
            operator_label="test",
            campus="brest",
            items=[{"article_id": article.id, "quantity": 1}],
            contributor_ids=[user.id],
            deposit_glasses=1,
        )
        _refus("admin_password_required", T.cancel_transaction, t, "mauvais")
        _expect(not t.cancelled, "l'annulation refusée ne marque rien")
        T.cancel_transaction(t, ADMIN_PASSWORD)
        _expect(t.cancelled, "transaction annulée")
        _expect(_balance(user.id) == 1000, f"solde restauré (obtenu {_balance(user.id)})")
        _expect(_glasses(user.id) == 0, "consigne restaurée")
        _refus("invalid", T.cancel_transaction, t, ADMIN_PASSWORD)

        direct = T.create_purchase(
            operator_label="test",
            campus="brest",
            direct=True,
            items=[{"article_id": article.id, "quantity": 1}],
            payment_method="cb",
        )
        T.cancel_transaction(direct, ADMIN_PASSWORD)
        _expect(
            direct.cancelled and _balance(user.id) == 1000,
            "annulation d'un paiement direct sans débit de portefeuille",
        )


def test_annulation_rechargement_retrait_transfert():
    app = create_app()
    with app.app_context():
        user = _user(balance=500)
        reload_t = T.create_reload(
            operator_label="test", campus="brest", user=user, amount_cents=1000, payment_method="cb"
        )
        T.cancel_transaction(reload_t, ADMIN_PASSWORD)
        _expect(_balance(user.id) == 500, f"rechargement annulé ({_balance(user.id)})")

        withdrawal = T.create_withdrawal(
            operator_label="test", campus="brest", user=user, amount_cents=200
        )
        T.cancel_transaction(withdrawal, ADMIN_PASSWORD)
        _expect(_balance(user.id) == 500, "retrait annulé")

        autre = _user(balance=0)
        transfer = T.create_transfer(
            operator_label="test", campus="brest", from_user=user, to_user=autre, amount_cents=300
        )
        T.cancel_transaction(transfer, ADMIN_PASSWORD)
        _expect(_balance(user.id) == 500 and _balance(autre.id) == 0, "transfert annulé")


def test_portee_des_articles_evenement():
    app = create_app()
    with app.app_context():
        now = utcnow()
        event = Event(
            name="Soirée test",
            campus="brest",
            starts_at=now - timedelta(hours=1),
            ends_at=now + timedelta(hours=2),
            token="jeton-evenement-caisse",
        )
        other = Event(
            name="Autre soirée",
            campus="brest",
            starts_at=now - timedelta(hours=1),
            ends_at=now + timedelta(hours=2),
            token="jeton-evenement-autre",
        )
        db.session.add_all([event, other])
        db.session.flush()
        event_article = _article(std=400, event_id=event.id)
        standard = _article(std=300)
        user = _user(balance=5000)

        _refus(
            "invalid",
            T.create_purchase,
            operator_label="test",
            campus="brest",
            items=[{"article_id": event_article.id, "quantity": 1}],
            contributor_ids=[user.id],
        )
        t = T.create_purchase(
            operator_label="test",
            campus="brest",
            event_id=event.id,
            items=[{"article_id": event_article.id, "quantity": 1}],
            contributor_ids=[user.id],
        )
        _expect(t.total == 400 and t.event_id == event.id, "article de l'événement vendu")
        t2 = T.create_purchase(
            operator_label="test",
            campus="brest",
            event_id=event.id,
            items=[{"article_id": standard.id, "quantity": 1}],
            contributor_ids=[user.id],
        )
        _expect(t2.total == 300, "catalogue standard disponible en passerelle")
        _refus(
            "invalid",
            T.create_purchase,
            operator_label="test",
            campus="brest",
            event_id=other.id,
            items=[{"article_id": event_article.id, "quantity": 1}],
            contributor_ids=[user.id],
        )


def test_validation_aucune_entree_ne_produit_un_500():
    app = create_app()
    with app.app_context():
        article = _article(std=300)
        user = _user(balance=1000)
        inactive = _article(std=300, active=False)
        base = {"operator_label": "test", "campus": "brest"}

        _refus("invalid", T.create_purchase, **base, items=[], contributor_ids=[user.id])
        _refus(
            "invalid",
            T.create_purchase,
            **base,
            items=[{"article_id": article.id, "quantity": 0}],
            contributor_ids=[user.id],
        )
        _refus(
            "invalid",
            T.create_purchase,
            **base,
            items=[{"article_id": article.id, "quantity": -3}],
            contributor_ids=[user.id],
        )
        _refus(
            "invalid",
            T.create_purchase,
            **base,
            items=[{"article_id": 999999, "quantity": 1}],
            contributor_ids=[user.id],
        )
        _refus(
            "invalid",
            T.create_purchase,
            **base,
            items=[{"article_id": inactive.id, "quantity": 1}],
            contributor_ids=[user.id],
        )
        _refus(
            "invalid",
            T.create_purchase,
            **base,
            items=[{"article_id": "x", "quantity": 1}],
            contributor_ids=[user.id],
        )
        _refus(
            "invalid", T.create_purchase, **base, items=[{"article_id": article.id, "quantity": 1}]
        )
        _refus(
            "invalid",
            T.create_purchase,
            **{**base, "campus": "lyon"},
            items=[{"article_id": article.id, "quantity": 1}],
            contributor_ids=[user.id],
        )
        _refus(
            "invalid",
            T.create_purchase,
            **base,
            items=[{"article_id": article.id, "quantity": 1}],
            contributor_ids=["abc"],
        )
        _refus(
            "invalid",
            T.create_purchase,
            **base,
            items=[{"article_id": article.id, "quantity": 1}],
            contributor_ids=[user.id],
            deposit_glasses="abc",
        )
        _refus(
            "invalid",
            T.create_purchase,
            **base,
            direct=True,
            items=[{"article_id": article.id, "quantity": 1}],
            payment_method="cheque",
        )
        gratuit = _article(std=0)
        _refus(
            "invalid",
            T.create_purchase,
            **base,
            items=[{"article_id": gratuit.id, "quantity": 1}],
            contributor_ids=[user.id],
        )


def test_api_authentification_et_csrf():
    app = create_app()
    client = app.test_client()
    _expect(client.get("/api/students").status_code == 401, "API sans session : 401")
    _expect(client.get("/api/wallet/1").status_code == 401, "portefeuille sans session : 401")
    token = _csrf(client.get("/connexion").get_data(as_text=True))
    res = client.post("/api/purchase", json={}, headers={"X-CSRFToken": token})
    _expect(res.status_code == 401, f"achat sans session : 401 (obtenu {res.status_code})")

    with app.app_context():
        user = _user(team=True, balance=1000)
        article = _article(std=300)
        login_name, user_id, article_id = user.username, user.id, article.id
    _login(client, login_name, "secret123")
    payload = {"items": [{"article_id": article_id, "quantity": 1}], "contributors": [user_id]}
    res = client.post("/api/purchase", json=payload)
    _expect(res.status_code == 400, f"CSRF manquant : 400 (obtenu {res.status_code})")
    csrf = _csrf(client.get("/equipe/paiement").get_data(as_text=True), "csrf-token")
    res = client.post("/api/purchase", json=payload, headers={"X-CSRFToken": "mauvais-jeton"})
    _expect(res.status_code == 400, "CSRF invalide : 400")
    res = client.post("/api/purchase", json=payload, headers={"X-CSRFToken": csrf})
    _expect(res.status_code == 200 and res.get_json()["ok"], "achat authentifié accepté")


def test_api_lecture_seule_autre_campus():
    app = create_app()
    with app.app_context():
        parisien = _user(campus="paris", team=True, balance=1000)
        article = _article(std=300)
        login_name, user_id, article_id = parisien.username, parisien.id, article.id
    client = app.test_client()
    _login(client, login_name, "secret123", campus="brest")
    csrf = _csrf(client.get("/equipe/paiement").get_data(as_text=True), "csrf-token")
    payload = {"items": [{"article_id": article_id, "quantity": 1}], "contributors": [user_id]}
    res = client.post("/api/purchase", json=payload, headers={"X-CSRFToken": csrf})
    _expect(
        res.status_code == 403, f"écriture sur l'autre campus refusée (obtenu {res.status_code})"
    )
    page = client.get("/equipe/paiement").get_data(as_text=True)
    _expect("lecture seule" in page, "badge lecture seule affiché")
    _expect(
        client.get("/api/students?campus=paris").status_code == 200,
        "lecture de l'autre campus autorisée",
    )
    res = client.post(
        "/equipe/rechargement",
        data={
            "user_id": user_id,
            "amount": "10",
            "method": "cb",
            "_csrf": csrf,
        },
    )
    _expect(res.status_code == 302, "opération d'écriture redirigée avec refus")

    client = app.test_client()
    _login(client, login_name, "secret123", campus="paris")
    csrf = _csrf(client.get("/equipe/paiement").get_data(as_text=True), "csrf-token")
    res = client.post("/api/purchase", json=payload, headers={"X-CSRFToken": csrf})
    _expect(
        res.status_code == 200 and res.get_json()["ok"],
        "écriture autorisée sur son campus d'appartenance",
    )
    with app.app_context():
        _expect(_balance(user_id, "paris") == 700, "solde paris débité une seule fois")


def test_pages_parametres_invalides():
    app = create_app()
    client = app.test_client()
    _login(client, "admin", ADMIN_PASSWORD)
    for path in (
        "/equipe/historique?page=abc&per_page=abc",
        "/equipe/statistiques?page=abc&per_page=abc&alimit=abc",
        "/equipe/tresorerie?year=abc&month=abc",
        "/admin/comptes?page=abc",
    ):
        res = client.get(path)
        _expect(res.status_code == 200, f"{path} -> 200 (obtenu {res.status_code})")
    res = client.get("/equipe/tresorerie/export/mensuel?year=abc&month=abc")
    _expect(res.status_code == 404, f"export invalide -> 404 (obtenu {res.status_code})")


def test_api_validation_400_pas_500():
    app = create_app()
    with app.app_context():
        user = _user(balance=1000)
        article = _article(std=300)
        ids = {"article": article.id, "user": user.id}
    client = app.test_client()
    _login(client, "admin", ADMIN_PASSWORD)
    csrf = _csrf(client.get("/equipe/paiement").get_data(as_text=True), "csrf-token")
    payloads = [
        {"items": [{"article_id": "x", "quantity": 1}], "contributors": [ids["user"]]},
        {"items": [{"article_id": ids["article"], "quantity": 1}], "contributors": ["abc"]},
        {
            "items": [{"article_id": ids["article"], "quantity": 1}],
            "contributors": [ids["user"]],
            "deposit_glasses": "abc",
        },
        {"items": "pas-une-liste", "contributors": [ids["user"]]},
        {
            "direct": True,
            "items": [{"article_id": ids["article"], "quantity": 1}],
            "payment_method": "cheque",
        },
    ]
    for payload in payloads:
        res = client.post("/api/purchase", json=payload, headers={"X-CSRFToken": csrf})
        _expect(
            res.status_code == 400, f"payload invalide -> 400 ({payload}, obtenu {res.status_code})"
        )
        _expect(res.get_json()["ok"] is False, "réponse ok=false")
    res = client.post(
        "/api/glasses/return", json={"user_id": "abc", "count": 1}, headers={"X-CSRFToken": csrf}
    )
    _expect(res.status_code == 404, f"utilisateur inconnu -> 404 (obtenu {res.status_code})")


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
