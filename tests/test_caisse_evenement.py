"""Caisse équipe et articles d'événement.

Exécutable sans pytest : python -m tests.test_caisse_evenement

Couvre la vente des articles d'événement depuis la caisse classique (en plus
de la passerelle), la fenêtre de disponibilité, le catalogue rafraîchi et le
maintien du catalogue standard.
"""

import os
import re
import sys
import tempfile
from datetime import timedelta
from itertools import count
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="foyz_caisse_evenement_"))
os.environ["DATABASE_URL"] = (
    os.environ.get("FOYZ_TEST_DATABASE_URL") or f"sqlite:///{_TMP / 'app.db'}"
)
os.environ["UPLOAD_DIR"] = str(_TMP / "uploads")
os.environ["SECRET_KEY"] = "test-secret-key-0123456789abcdef0123456789abcdef"
os.environ["ADMIN_PASSWORD"] = "mot-de-passe-admin"
os.environ.pop("FLASK_ENV", None)

from werkzeug.security import generate_password_hash  # noqa: E402

from app import create_app  # noqa: E402
from app.extensions import db  # noqa: E402
from app.models import Article, Event, Transaction, User  # noqa: E402
from app.utils import utcnow  # noqa: E402

app = create_app()
_seq = count(1)


def _expect(cond, label):
    if not cond:
        raise AssertionError(f"échec : {label}")


def _csrf(html, field="csrf-token"):
    pattern = (
        r'name="csrf-token" content="([^"]+)"'
        if field == "csrf-token"
        else r'name="_csrf" value="([^"]+)"'
    )
    match = re.search(pattern, html)
    _expect(match is not None, f"jeton CSRF {field} présent")
    return match.group(1)


def _login(client, username, password="secret123", campus="brest"):
    token = _csrf(client.get("/connexion").get_data(as_text=True), "csrf-token")
    res = client.post(
        "/connexion",
        data={
            "username": username,
            "password": password,
            "campus": campus,
            "_csrf": token,
        },
    )
    _expect(res.status_code == 302, f"connexion {username} sur {campus} ({res.status_code})")
    return client


def _username(user_id):
    with app.app_context():
        return db.session.get(User, user_id).username


def _user(name, *, campus="brest", team=False, balance=50_000):
    n = next(_seq)
    with app.app_context():
        user = User(
            name=f"{name} {n}",
            username=f"caisse.evenement.{n}",
            password_hash=generate_password_hash("secret123"),
            team_status="mandat" if team else "etudiant",
            team_campus=campus if team else None,
        )
        db.session.add(user)
        db.session.flush()
        user.wallet(campus).balance = balance
        db.session.commit()
        return user.id


def _article(name, *, campus="brest", kind="biere", std=300, event_id=None):
    n = next(_seq)
    with app.app_context():
        article = Article(
            name=f"{name} {n}",
            article_type=kind,
            campus=campus,
            price_std=std,
            price_team=std,
            active=True,
            event_id=event_id,
        )
        db.session.add(article)
        db.session.commit()
        return article.id


def _event(name, *, campus="brest", starts=-1, ends=4, closed=False, allow_standard=False):
    with app.app_context():
        event = Event(
            name=name,
            campus=campus,
            starts_at=utcnow() + timedelta(hours=starts),
            ends_at=utcnow() + timedelta(hours=ends),
            token=f"caisse-event-{next(_seq)}",
            closed=closed,
            allow_standard_articles=allow_standard,
        )
        db.session.add(event)
        db.session.commit()
        return event.id


def test_team_payment_event_articles():
    mandate_id = _user("Mandat caisse", team=True)
    mandate = _login(app.test_client(), _username(mandate_id))
    student_id = _user("Caisse étudiant")

    standard = _article("Caisse standard", std=300)
    running = _event("Caisse événement en cours", starts=-1, ends=4, allow_standard=False)
    running_article = _article(
        "Caisse article en cours", kind="evenement", std=500, event_id=running
    )
    future = _event("Caisse événement futur", starts=48, ends=52)
    future_article = _article("Caisse article futur", kind="evenement", std=500, event_id=future)
    closed = _event("Caisse événement clôturé", starts=-1, ends=4, closed=True)
    closed_article = _article("Caisse article clôturé", kind="evenement", std=500, event_id=closed)
    ended = _event("Caisse événement terminé", starts=-5, ends=-1)
    ended_article = _article("Caisse article terminé", kind="evenement", std=500, event_id=ended)

    page = mandate.get("/equipe/paiement").get_data(as_text=True)
    _expect("Caisse article en cours" in page, "article de l'événement en cours affiché")
    _expect("Caisse standard" in page, "catalogue standard affiché")
    for hidden in ("Caisse article futur", "Caisse article clôturé", "Caisse article terminé"):
        _expect(hidden not in page, f"{hidden} masqué")

    live = mandate.get("/equipe/paiement/catalogue").get_json()
    _expect(live["ok"] is True, "catalogue live")
    listed = {article["id"]: article for article in live["catalog"]}
    _expect(
        running_article in listed and listed[running_article]["event"] is True,
        "événement listé",
    )
    _expect(standard in listed and listed[standard]["event"] is False, "standard listé")
    for hidden in (future_article, closed_article, ended_article):
        _expect(hidden not in listed, "article hors fenêtre absent du catalogue live")

    csrf = _csrf(page, "csrf-token")
    res = mandate.post(
        "/api/purchase",
        json={
            "items": [
                {"article_id": running_article, "quantity": 1},
                {"article_id": standard, "quantity": 1},
            ],
            "contributors": [student_id],
        },
        headers={"X-CSRFToken": csrf},
    )
    _expect(
        res.status_code == 200,
        f"vente événement + standard ({res.status_code}, {res.get_json()})",
    )
    payload = res.get_json()
    _expect(payload["total"] == 800, "total événement + standard")
    with app.app_context():
        transaction = db.session.get(Transaction, payload["transaction_id"])
        _expect(transaction.event_id == running, "transaction rattachée à l'événement")
        _expect(
            db.session.get(User, student_id).wallet("brest").balance == 50_000 - 800,
            "solde débité",
        )

    with app.app_context():
        db.session.get(Event, running).closed = True
        db.session.commit()
    live = mandate.get("/equipe/paiement/catalogue").get_json()
    _expect(
        running_article not in {article["id"] for article in live["catalog"]},
        "événement clôturé retiré",
    )

    for article_id in (running_article, future_article, ended_article, closed_article):
        res = mandate.post(
            "/api/purchase",
            json={
                "items": [{"article_id": article_id, "quantity": 1}],
                "contributors": [student_id],
            },
            headers={"X-CSRFToken": csrf},
        )
        _expect(res.status_code == 400, f"article hors fenêtre refusé ({article_id})")

    res = mandate.post(
        "/api/purchase",
        json={
            "items": [{"article_id": standard, "quantity": 1}],
            "contributors": [student_id],
        },
        headers={"X-CSRFToken": csrf},
    )
    _expect(res.status_code == 200, "catalogue standard toujours vendable")

    with app.app_context():
        standard_row = db.session.get(Article, standard)
        standard_row.price_std = 350
        db.session.commit()
    live = mandate.get("/equipe/paiement/catalogue").get_json()
    _expect(
        any(article["id"] == standard and article["std"] == 350 for article in live["catalog"]),
        "prix rafraîchi par la route live",
    )

    event_one = _event("Caisse multi un", starts=-1, ends=4)
    event_two = _event("Caisse multi deux", starts=-1, ends=4)
    article_one = _article("Caisse multi article un", kind="evenement", std=100, event_id=event_one)
    article_two = _article(
        "Caisse multi article deux", kind="evenement", std=100, event_id=event_two
    )
    live = mandate.get("/equipe/paiement/catalogue").get_json()
    listed = {article["id"] for article in live["catalog"]}
    _expect(article_one in listed and article_two in listed, "deux événements en cours listés")
    res = mandate.post(
        "/api/purchase",
        json={
            "items": [
                {"article_id": article_one, "quantity": 1},
                {"article_id": article_two, "quantity": 1},
            ],
            "contributors": [student_id],
        },
        headers={"X-CSRFToken": csrf},
    )
    _expect(res.status_code == 200, "panier multi-événements accepté")
    with app.app_context():
        transaction = db.session.get(Transaction, res.get_json()["transaction_id"])
        _expect(
            transaction.event_id is None,
            "panier multi-événements non rattaché à un seul",
        )


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
