"""Tests de sécurité de la caisse et de la passerelle (plan de bascule, B1 à B5).

Exécutable sans pytest : python -m tests.test_securite_caisse

Couvre :
- B1 : échecs et succès du mot de passe administrateur au journal d'audit, sans
  jamais bloquer un campus, `admin_password` refusé sur la passerelle ;
- B2 : plafonds de la passerelle, informations réduites de /api/students et
  /api/wallet, recherche de 3 caractères minimum, validité bornée, jeton
  masqué des journaux, page passerelle absente du cache du Service Worker ;
- B3 : redirections `next` (liste d'URL piégées) ;
- B4 : annulation concurrente (un seul remboursement) ;
- B5 : parseur de montants unique (0,29 -> 29 centimes, 1e999 -> refus) et
  types JSON validés (jamais de 500).

Compatible SQLite et PostgreSQL (FOYZ_TEST_DATABASE_URL).
"""

import itertools
import os
import re
import sys
import tempfile
import threading
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="foyz_secu_"))
os.environ["DATABASE_URL"] = (
    os.environ.get("FOYZ_TEST_DATABASE_URL") or f"sqlite:///{_TMP / 'app.db'}"
)
os.environ["UPLOAD_DIR"] = str(_TMP / "uploads")
os.environ["SECRET_KEY"] = "test-secret-key-0123456789abcdef0123456789abcdef"
os.environ["ADMIN_PASSWORD"] = "mot-de-passe-admin"
os.environ.pop("FLASK_ENV", None)

from sqlalchemy import func, select  # noqa: E402
from werkzeug.security import generate_password_hash  # noqa: E402

from app import create_app  # noqa: E402
from app.extensions import db  # noqa: E402
from app.models import Article, AuditLog, Event, Transaction, User  # noqa: E402
from app.services import adminpw  # noqa: E402
from app.services import transactions as T  # noqa: E402
from app.utils import is_safe_target, parse_amount, redact_secrets, utcnow  # noqa: E402

ADMIN_PASSWORD = "mot-de-passe-admin"
FAILURES = 12
_seq = itertools.count(1)


def _expect(cond, label):
    if not cond:
        raise AssertionError(f"échec : {label}")


def _csrf_form(html):
    match = re.search(r'name="_csrf" value="([^"]+)"', html)
    _expect(match is not None, "jeton CSRF de formulaire présent")
    return match.group(1)


def _csrf_meta(html):
    match = re.search(r'name="csrf-token" content="([^"]+)"', html)
    _expect(match is not None, "jeton CSRF (meta) présent")
    return match.group(1)


def _login(client, username, password="secret123", campus="brest", query=""):
    token = _csrf_form(client.get("/connexion").get_data(as_text=True))
    return client.post(
        f"/connexion{query}",
        data={"username": username, "password": password, "campus": campus, "_csrf": token},
    )


def _user(campus="brest", *, team=False, balance=0):
    n = next(_seq)
    user = User(
        name=f"Secu Test {n}",
        username=f"secu.test.{n}",
        password_hash=generate_password_hash("secret123"),
        team_status="mandat" if team else "etudiant",
        team_campus=campus if team else None,
    )
    db.session.add(user)
    db.session.flush()
    user.wallet(campus).balance = balance
    db.session.commit()
    return user


def _article(price=100, campus="brest", event_id=None):
    n = next(_seq)
    article = Article(
        name=f"Article secu {n}",
        article_type="snack",
        campus=campus,
        price_std=price,
        price_team=price // 2,
        event_id=event_id,
        active=True,
    )
    db.session.add(article)
    db.session.commit()
    return article


def _event(campus="brest", *, starts=-1, ends=6):
    n = next(_seq)
    ev = Event(
        name=f"Soirée secu {n}",
        campus=campus,
        starts_at=utcnow() + timedelta(hours=starts),
        ends_at=utcnow() + timedelta(hours=ends),
        token=f"jeton-secu-0123456789-{n}",
    )
    db.session.add(ev)
    db.session.commit()
    return ev


def _team_client(app):
    with app.app_context():
        member = _user(team=True)
        username = member.username
    client = app.test_client()
    res = _login(client, username)
    _expect(res.status_code == 302, f"connexion équipe ({res.status_code})")
    csrf = _csrf_meta(client.get("/equipe/paiement").get_data(as_text=True))
    return client, csrf


def _reset_audit(app):
    """Les suites partagent une base : on repart d'un journal vide (le limiteur
    d'échecs s'appuie sur lui)."""
    with app.app_context():
        db.session.query(AuditLog).delete()
        db.session.commit()


def _audit_count(action):
    return db.session.scalar(select(func.count(AuditLog.id)).where(AuditLog.action == action))


# --------------------------------------------------------------------------- B1


def test_b1_echecs_journalises_sans_jamais_bloquer():
    app = create_app()
    _reset_audit(app)
    client, csrf = _team_client(app)
    with app.app_context():
        student = _user(balance=-100)
        article = _article(100)
        student_id, article_id = student.id, article.id

    def buy(password, key):
        return client.post(
            "/api/purchase",
            json={
                "items": [{"article_id": article_id, "quantity": 1}],
                "contributors": [student_id],
                "admin_password": password,
                "idempotency_key": key,
            },
            headers={"X-CSRFToken": csrf},
        )

    res = buy("", "b1-vide")
    _expect(res.get_json()["code"] == "admin_password_required", "sans mot de passe : requis")
    with app.app_context():
        _expect(_audit_count(adminpw.ACTION_FAILURE) == 0, "mot de passe absent : pas un échec")

    for i in range(FAILURES):
        res = buy("mauvais-mot-de-passe", f"b1-echec-{i}")
        _expect(
            res.status_code == 400 and res.get_json()["code"] == "admin_password_required",
            f"échec {i + 1} refusé ({res.get_json()})",
        )
    with app.app_context():
        _expect(
            _audit_count(adminpw.ACTION_FAILURE) == FAILURES,
            "chaque échec figure au journal d'audit",
        )
        failure = db.session.scalars(
            select(AuditLog).where(AuditLog.action == adminpw.ACTION_FAILURE)
        ).first()
        _expect(failure.user_id is not None and failure.campus == "brest", "échec attribué")
        _expect(failure.ip != "", "échec journalisé avec l'IP")

    # Jamais de blocage : le bon mot de passe passe, quel que soit le nombre d'échecs.
    res = buy(ADMIN_PASSWORD, "b1-ok")
    _expect(
        res.status_code == 200 and res.get_json()["ok"],
        f"le campus n'est jamais bloqué ({res.get_json()})",
    )
    with app.app_context():
        success = db.session.scalars(
            select(AuditLog).where(AuditLog.action == adminpw.ACTION_SUCCESS)
        ).one()
        _expect(success.user_id is not None, "succès attribué à l'opérateur")
        _expect("à découvert" in success.details, "succès : opération protégée décrite")
        _expect(db.session.get(User, student_id).wallet("brest").balance == -200, "achat passé")


def test_b1_service_ne_bloque_jamais():
    app = create_app()
    _reset_audit(app)
    with app.app_context():
        for _ in range(FAILURES):
            _expect(not adminpw.verify("faux", "brest", "test"), "mauvais mot de passe refusé")
        _expect(adminpw.verify(ADMIN_PASSWORD, "brest", "test"), "Brest jamais bloqué")
        _expect(adminpw.verify(ADMIN_PASSWORD, "paris", "test"), "Paris non affecté")
        _expect(_audit_count(adminpw.ACTION_FAILURE) == FAILURES, "échecs journalisés")
        _expect(_audit_count(adminpw.ACTION_SUCCESS) == 2, "succès journalisés")


def test_b1_annulation_historique_toujours_possible_apres_echecs():
    app = create_app()
    _reset_audit(app)
    client, csrf = _team_client(app)
    with app.app_context():
        student = _user(balance=1000)
        article = _article(100)
        t = T.create_purchase(
            operator_label="test",
            campus="brest",
            items=[{"article_id": article.id, "quantity": 1}],
            contributor_ids=[student.id],
        )
        tid = t.id
    page = client.get("/equipe/historique").get_data(as_text=True)
    form_csrf = _csrf_form(page) if 'name="_csrf"' in page else csrf
    for _ in range(FAILURES):
        client.post(
            "/equipe/historique/annuler",
            data={"transaction_id": str(tid), "admin_password": "faux", "_csrf": form_csrf},
        )
    with app.app_context():
        _expect(db.session.get(Transaction, tid).cancelled is False, "échecs : pas d'annulation")
        _expect(_audit_count(adminpw.ACTION_FAILURE) == FAILURES, "échecs journalisés")
    client.post(
        "/equipe/historique/annuler",
        data={"transaction_id": str(tid), "admin_password": ADMIN_PASSWORD, "_csrf": form_csrf},
        follow_redirects=True,
    )
    with app.app_context():
        _expect(db.session.get(Transaction, tid).cancelled is True, "bon mot de passe : annulée")
        _expect(_audit_count(adminpw.ACTION_SUCCESS) == 1, "succès journalisé")


def test_b1_passerelle_refuse_admin_password():
    app = create_app()
    _reset_audit(app)
    with app.app_context():
        ev = _event()
        article = _article(100, event_id=ev.id)
        student = _user(balance=-100)
        token, article_id, student_id = ev.token, article.id, student.id
    client = app.test_client()
    csrf = _csrf_meta(client.get(f"/passerelle/{token}").get_data(as_text=True))
    payload = {
        "items": [{"article_id": article_id, "quantity": 1}],
        "contributors": [student_id],
    }
    res = client.post(
        f"/passerelle/{token}/encaisser",
        json={**payload, "admin_password": ADMIN_PASSWORD},
        headers={"X-CSRFToken": csrf},
    )
    _expect(
        res.status_code == 400 and res.get_json()["code"] == "admin_password_refused",
        f"admin_password refusé sur la passerelle ({res.get_json()})",
    )
    res = client.post(f"/passerelle/{token}/encaisser", json=payload, headers={"X-CSRFToken": csrf})
    _expect(
        res.status_code == 400 and res.get_json()["code"] == "gateway_negative_balance",
        f"compte à découvert : pas de contournement par la passerelle ({res.get_json()})",
    )
    with app.app_context():
        _expect(_audit_count(adminpw.ACTION_FAILURE) == 0, "la passerelle ne teste aucun secret")
        _expect(_audit_count(adminpw.ACTION_SUCCESS) == 0, "la passerelle n'ouvre aucun succès")
        _expect(db.session.get(User, student_id).wallet("brest").balance == -100, "solde intact")


# --------------------------------------------------------------------------- B2


def test_b2_plafonds_passerelle():
    app = create_app()
    with app.app_context():
        ev = _event()
        cheap = _article(100, event_id=ev.id)
        pricey = _article(2000, event_id=ev.id)
        student = _user(balance=100_000)
        token, cheap_id, pricey_id, student_id = ev.token, cheap.id, pricey.id, student.id
    client = app.test_client()
    csrf = _csrf_meta(client.get(f"/passerelle/{token}").get_data(as_text=True))

    def pay(items, **extra):
        return client.post(
            f"/passerelle/{token}/encaisser",
            json={"items": items, "contributors": [student_id], **extra},
            headers={"X-CSRFToken": csrf},
        )

    res = pay([{"article_id": cheap_id, "quantity": T.GATEWAY_MAX_LINE_QUANTITY + 1}])
    _expect(res.status_code == 400, f"quantité au-delà du plafond refusée ({res.status_code})")
    res = pay([{"article_id": cheap_id, "quantity": 10}, {"article_id": cheap_id, "quantity": 11}])
    _expect(res.status_code == 400, "quantités cumulées d'un même article plafonnées")
    res = pay([{"article_id": pricey_id, "quantity": 8}])
    _expect(
        res.status_code == 400 and res.get_json()["code"] == "invalid",
        f"montant au-delà du plafond refusé (16 € x 8, {res.get_json()})",
    )
    res = pay([{"article_id": cheap_id, "quantity": T.GATEWAY_MAX_LINE_QUANTITY}])
    _expect(res.status_code == 200 and res.get_json()["ok"], "achat dans les bornes accepté")
    res = pay([{"article_id": cheap_id, "quantity": 1}], idempotency_key="x" * 65)
    _expect(res.status_code == 400, "clé d'idempotence trop longue refusée")
    with app.app_context():
        balance = db.session.get(User, student_id).wallet("brest").balance
        _expect(balance == 100_000 - 100 * T.GATEWAY_MAX_LINE_QUANTITY, "un seul achat débité")


def test_b2_api_passerelle_reduite():
    app = create_app()
    with app.app_context():
        ev = _event()
        article = _article(300, event_id=ev.id)
        member = _user(team=True, balance=5000)
        member.name = "Equipier Passerelle"
        db.session.commit()
        token, article_id, member_id = ev.token, article.id, member.id
    client = app.test_client()
    csrf = _csrf_meta(client.get(f"/passerelle/{token}").get_data(as_text=True))

    _expect(client.get("/api/students?q=eq").get_json() == [], "recherche < 3 caractères vide")
    results = client.get("/api/students?q=equipier").get_json()
    found = [s for s in results if s["id"] == member_id]
    _expect(len(found) == 1, "recherche d'au moins 3 caractères fonctionnelle")
    _expect("username" not in found[0], "pas d'identifiant de connexion en passerelle")
    _expect(found[0]["is_team"] is True, "statut équipe exposé : il détermine le tarif affiché")
    _expect("balance" in found[0] and "blacklist" in found[0], "champs utiles conservés")
    wallet = client.get(f"/api/wallet/{member_id}").get_json()
    _expect("username" not in wallet and wallet["is_team"] is True, "portefeuille passerelle")

    res = client.post(
        f"/passerelle/{token}/encaisser",
        json={
            "items": [{"article_id": article_id, "quantity": 1}],
            "contributors": [member_id],
        },
        headers={"X-CSRFToken": csrf},
    )
    _expect(res.status_code == 200 and res.get_json()["total"] == 150, "tarif équipe en passerelle")

    from app.routes.api import _gateway_wallet_limiter

    statuses = {client.get(f"/api/wallet/{member_id}").status_code for _ in range(40)}
    _expect(429 in statuses, "limiteur dédié sur /api/wallet en passerelle")
    _gateway_wallet_limiter._hits.clear()

    # Hors passerelle, l'équipe garde les champs complets.
    team_client, _ = _team_client(app)
    full = team_client.get("/api/students?q=Equipier").get_json()
    _expect(
        any("username" in s and "is_team" in s for s in full),
        "l'équipe conserve les champs complets",
    )


def test_b2_passerelle_tarif_equipe_seulement_si_tous_equipe():
    app = create_app()
    with app.app_context():
        ev = _event()
        article = _article(300, event_id=ev.id)
        member = _user(team=True, balance=5000)
        other = _user(balance=5000)
        token, article_id, member_id, other_id = ev.token, article.id, member.id, other.id
    client = app.test_client()
    csrf = _csrf_meta(client.get(f"/passerelle/{token}").get_data(as_text=True))

    def pay(contributors):
        res = client.post(
            f"/passerelle/{token}/encaisser",
            json={
                "items": [{"article_id": article_id, "quantity": 2}],
                "contributors": contributors,
            },
            headers={"X-CSRFToken": csrf},
        )
        _expect(res.status_code == 200 and res.get_json()["ok"], f"encaissement ({res.get_json()})")
        return res.get_json()["total"]

    _expect(pay([member_id]) == 300, "membre de l'équipe seul : 2 x 1,50 € (tarif équipe)")
    _expect(pay([other_id]) == 600, "étudiant : tarif public")
    _expect(pay([member_id, other_id]) == 600, "équipe + étudiant : tarif public (comme en caisse)")


def test_b2_validite_passerelle_bornee():
    app = create_app()
    with app.app_context():
        long_ago = _event(starts=-100, ends=50)
        _expect(not long_ago.is_running, "fin lointaine : validité plafonnée à 72 h après le début")
        normal = _event(starts=-2, ends=8)
        _expect(normal.is_running, "événement ordinaire accessible")
        _expect(normal.gateway_deadline == normal.ends_at, "fin saisie respectée sous le plafond")


def test_b2_jeton_absent_des_journaux():
    token = "SECRET-token_0123456789abcdef"
    _expect(token not in redact_secrets(f"/passerelle/{token}/encaisser?q=1"), "chemin masqué")
    _expect(
        redact_secrets(f"/passerelle/{token}/encaisser") == "/passerelle/***/encaisser",
        "masque ciblé sur le jeton",
    )
    _expect(redact_secrets("/equipe/paiement") == "/equipe/paiement", "autres chemins intacts")
    from app.routes.gateway import token_ref

    _expect(token not in token_ref(token) and len(token_ref(token)) == 8, "référence opaque")
    from app.gunicorn_logging import MaskingLogger

    _expect(MaskingLogger is not None, "journal d'accès Gunicorn masquant disponible")


def test_b2_service_worker_retire():
    """L'ancien service worker ne cache plus rien : il ne reste qu'une pierre
    tombale qui purge ses caches et se désenregistre sur les anciens postes."""
    body = (ROOT / "app" / "static" / "js" / "sw.js").read_text(encoding="utf-8")
    _expect("respondWith" not in body, "plus d'interception des requêtes")
    _expect("caches.open" not in body, "plus aucune mise en cache (pages sensibles incluses)")
    _expect(
        "caches.delete" in body and "unregister" in body,
        "purge des caches et désenregistrement",
    )


# --------------------------------------------------------------------------- B3


def test_b3_redirections_piegees():
    pieges = [
        "/\\evil.example",
        "//evil.example",
        "https://evil.example",
        "http://evil.example/x",
        "\\\\evil.example",
        "%5Cevil.example",
        "/%5Cevil.example",
        "/\t/evil.example",
        "/\n/evil.example",
        "/\r/evil.example",
        "/%09/evil.example",
        "/%2F/evil.example",
        "javascript:alert(1)",
        "evil.example",
        "",
        None,
    ]
    for url in pieges:
        _expect(not is_safe_target(url), f"URL piégée refusée : {url!r}")
    for url in ("/", "/equipe/paiement", "/admin/comptes?q=a&page=2", "/passerelle/abc"):
        _expect(is_safe_target(url), f"chemin interne accepté : {url!r}")


def test_b3_connexion_et_consigne_ignorent_next_pieges():
    app = create_app()
    with app.app_context():
        member = _user(team=True)
        student = _user(balance=0)
        username, student_id = member.username, student.id
    client = app.test_client()
    res = _login(client, username, query="?next=%2F%5Cevil.example")
    _expect(res.status_code == 302, "connexion acceptée")
    _expect("evil" not in res.headers["Location"], f"next piégé ignoré ({res.headers['Location']})")
    csrf = _csrf_meta(client.get("/equipe/paiement").get_data(as_text=True))
    for piege in ("/\\evil.example", "//evil.example", "https://evil.example"):
        res = client.post(
            "/equipe/consignes/retour",
            data={"user_id": str(student_id), "count": "1", "next": piege, "_csrf": csrf},
        )
        _expect(res.status_code == 302, "consigne : redirection")
        _expect(
            "evil" not in res.headers["Location"],
            f"consigne : next piégé ignoré ({piege} -> {res.headers['Location']})",
        )
    res = client.post(
        "/equipe/consignes/retour",
        data={
            "user_id": str(student_id),
            "count": "1",
            "next": "/equipe/historique",
            "_csrf": csrf,
        },
    )
    _expect(res.headers["Location"].endswith("/equipe/historique"), "next interne conservé")


# --------------------------------------------------------------------------- B4


def test_b4_double_annulation_sequentielle():
    app = create_app()
    _reset_audit(app)
    with app.app_context():
        student = _user(balance=1000)
        article = _article(100)
        t = T.create_purchase(
            operator_label="test",
            campus="brest",
            items=[{"article_id": article.id, "quantity": 1}],
            contributor_ids=[student.id],
        )
        student_id, tid = student.id, t.id
        T.cancel_transaction(t, ADMIN_PASSWORD)
        try:
            T.cancel_transaction(db.session.get(Transaction, tid), ADMIN_PASSWORD)
            raise AssertionError("échec : la seconde annulation devrait être refusée")
        except T.OperationError as exc:
            _expect(exc.code == "invalid", "seconde annulation refusée")
        _expect(
            db.session.get(User, student_id).wallet("brest").balance == 1000, "un remboursement"
        )


def test_b4_annulations_concurrentes_un_seul_remboursement():
    app = create_app()
    with app.app_context():
        student = _user(balance=1000)
        article = _article(100)
        t = T.create_purchase(
            operator_label="test",
            campus="brest",
            items=[{"article_id": article.id, "quantity": 1}],
            contributor_ids=[student.id],
        )
        student_id, tid = student.id, t.id
        _expect(db.session.get(User, student_id).wallet("brest").balance == 900, "achat débité")

    count = 6
    barrier = threading.Barrier(count)
    results, lock = [], threading.Lock()

    def worker():
        with app.app_context():
            barrier.wait()
            try:
                T.cancel_transaction(db.session.get(Transaction, tid), None, verified=True)
                outcome = "ok"
            except T.OperationError as exc:
                outcome = f"refus:{exc.code}"
            except Exception as exc:
                outcome = f"erreur:{exc!r}"
            with lock:
                results.append(outcome)

    threads = [threading.Thread(target=worker) for _ in range(count)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    _expect(results.count("ok") == 1, f"une seule annulation aboutit ({results})")
    _expect(
        all(r in ("ok", "refus:invalid") for r in results),
        f"les autres sont refusées proprement ({results})",
    )
    with app.app_context():
        balance = db.session.get(User, student_id).wallet("brest").balance
        _expect(balance == 1000, f"un seul remboursement (solde {balance}, attendu 1000)")
        _expect(db.session.get(Transaction, tid).cancelled is True, "transaction annulée")


# --------------------------------------------------------------------------- B5


def test_b5_parseur_montants():
    _expect(parse_amount("0,29") == 29, "0,29 -> 29 centimes")
    _expect(parse_amount("4,35") == 435, "4,35 -> 435 centimes")
    _expect(parse_amount("12.50") == 1250, "point décimal accepté")
    _expect(parse_amount(20) == 2000, "entier JSON accepté")
    for bad in (
        "1e999",
        "nan",
        "inf",
        "-1",
        "0",
        "0,004",
        "abc",
        "",
        None,
        True,
        "9" * 40,
        "5000,01",
    ):
        try:
            parse_amount(bad)
        except ValueError:
            continue
        raise AssertionError(f"échec : {bad!r} devrait être refusé")
    _expect(parse_amount("5000") == 500_000, "plafond inclusif")


def test_b5_formulaires_equipe_sans_500():
    app = create_app()
    client, _ = _team_client(app)
    with app.app_context():
        student = _user(balance=0)
        other = _user(balance=1000)
        student_id, other_id = student.id, other.id

    def post(path, **fields):
        token = _csrf_form(client.get(path).get_data(as_text=True))
        return client.post(path, data={**fields, "_csrf": token}, follow_redirects=True)

    def balance(uid):
        with app.app_context():
            db.session.expire_all()
            return db.session.get(User, uid).wallet("brest").balance

    res = post("/equipe/rechargement", user_id=student_id, amount="0,29", method="cb")
    _expect(res.status_code == 200 and balance(student_id) == 29, "rechargement 0,29 -> 29")
    res = post("/equipe/rechargement", user_id=student_id, amount="4,35", method="cb")
    _expect(balance(student_id) == 29 + 435, "rechargement 4,35 -> 435 (pas 434)")
    for bad in ("1e999", "nan", "-5", "0", "5001", "abc"):
        res = post("/equipe/rechargement", user_id=student_id, amount=bad, method="cb")
        _expect(res.status_code == 200, f"rechargement {bad!r} : 200 avec message, pas de 500")
    _expect(balance(student_id) == 29 + 435, "aucune saisie invalide n'a crédité le compte")

    res = post("/equipe/retrait", user_id=student_id, amount="0,29")
    _expect(res.status_code == 200 and balance(student_id) == 435, "retrait 0,29 -> 29")
    res = post("/equipe/retrait", user_id=student_id, amount="1e999")
    _expect(res.status_code == 200 and balance(student_id) == 435, "retrait 1e999 : refus")

    res = post("/equipe/transfert", from_id=other_id, to_id=student_id, amount="0,29")
    _expect(balance(student_id) == 435 + 29 and balance(other_id) == 1000 - 29, "transfert 0,29")
    res = post(
        "/equipe/transfert",
        from_id=[other_id, student_id],
        to_id=student_id,
        amount="2,00",
    )
    _expect(
        balance(student_id) == 464 + 100 and balance(other_id) == 971 - 100,
        "transfert avec un compte à la fois donneur et receveur",
    )
    res = post("/equipe/transfert", from_id=other_id, to_id=student_id, amount="1e999")
    _expect(res.status_code == 200 and balance(other_id) == 971 - 100, "transfert 1e999 : refus")


def test_b5_api_types_json_valides():
    app = create_app()
    client, csrf = _team_client(app)
    with app.app_context():
        student = _user(balance=1000)
        article = _article(100)
        student_id, article_id = student.id, article.id
    headers = {"X-CSRFToken": csrf}
    base = {"items": [{"article_id": article_id, "quantity": 1}], "contributors": [student_id]}
    mauvais = [
        {**base, "items": 5},
        {**base, "items": "abc"},
        {**base, "items": [5]},
        {**base, "items": [{"article_id": "x", "quantity": 1}]},
        {**base, "items": [{"article_id": article_id, "quantity": 10**30}]},
        {**base, "items": [{"article_id": article_id, "quantity": T.MAX_LINE_QUANTITY + 1}]},
        {**base, "items": [{"article_id": True, "quantity": 1}]},
        {**base, "contributors": 7},
        {**base, "contributors": "abc"},
        {**base, "contributors": [{"a": 1}]},
        {**base, "contributors": list(range(T.MAX_CONTRIBUTORS + 1))},
        {**base, "deposit_glasses": 10**30},
        {**base, "deposit_glasses": T.MAX_GLASSES + 1},
        {**base, "deposit_glasses": "abc"},
        {**base, "idempotency_key": "k" * (T.MAX_IDEMPOTENCY_KEY_LENGTH + 1)},
        {**base, "idempotency_key": 12345},
    ]
    for payload in mauvais:
        res = client.post("/api/purchase", json=payload, headers=headers)
        _expect(
            res.status_code == 400,
            f"payload invalide -> 400, pas 500 : {payload} ({res.status_code})",
        )
    for raw in ("[1, 2]", "42", '"texte"', "Infinity"):
        res = client.post(
            "/api/purchase", data=raw, content_type="application/json", headers=headers
        )
        _expect(res.status_code == 400, f"corps JSON {raw} -> 400 ({res.status_code})")
    res = client.post(
        "/api/glasses/return",
        data='{"user_id": Infinity, "count": 1}',
        content_type="application/json",
        headers=headers,
    )
    _expect(res.status_code in (400, 404), f"user_id infini -> pas de 500 ({res.status_code})")
    res = client.post("/api/purchase", json=base, headers=headers)
    _expect(res.status_code == 200 and res.get_json()["ok"], "payload valide accepté")


def test_operateur_au_surnom_tres_long():
    """Les surnoms importés peuvent atteindre ~245 caractères : le libellé de
    l'opérateur (nom + surnom) dépasse alors VARCHAR(255). PostgreSQL rejette
    (SQLite tronque en silence) : la caisse ne doit jamais répondre 500."""
    app = create_app()
    with app.app_context():
        member = _user(team=True, balance=5000)
        member.nickname = "x" * 245
        student = _user(balance=5000)
        student.wallet("brest").glasses_outstanding = 1
        other = _user(balance=5000)
        article = _article(100)
        db.session.commit()
        username = member.username
        student_id, other_id, article_id = student.id, other.id, article.id
        label = member.display_name
        _expect(len(label) > 255, f"libellé d'opérateur trop long pour la colonne ({len(label)})")
    client = app.test_client()
    _expect(_login(client, username).status_code == 302, "connexion de l'opérateur")
    csrf = _csrf_meta(client.get("/equipe/paiement").get_data(as_text=True))
    res = client.post(
        "/api/purchase",
        json={
            "items": [{"article_id": article_id, "quantity": 1}],
            "contributors": [student_id],
            "idempotency_key": "surnom-long-achat",
        },
        headers={"X-CSRFToken": csrf},
    )
    _expect(
        res.status_code == 200 and res.get_json()["ok"],
        f"achat par l'opérateur ({res.status_code})",
    )
    with app.app_context():
        student = db.session.get(User, student_id)
        other = db.session.get(User, other_id)
        for t in (
            T.create_reload(
                operator_label=label,
                campus="brest",
                user=student,
                amount_cents=500,
                payment_method="especes",
            ),
            T.create_withdrawal(
                operator_label=label, campus="brest", user=student, amount_cents=100
            ),
            T.create_transfer(
                operator_label=label,
                campus="brest",
                from_users=[student],
                to_users=[other],
                amount_cents=100,
            ),
            T.return_glasses(operator_label=label, campus="brest", user=student, count=1),
        ):
            _expect(len(t.operator_label) == 255, "libellé tronqué à la longueur de la colonne")
            _expect(t.operator_label == label[:255], "début du libellé conservé")


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
