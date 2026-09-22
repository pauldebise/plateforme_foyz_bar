"""Tests de la passerelle événement (phase 2 du plan d'action).

Exécutable sans pytest : python -m tests.test_gateway

Couvre :
- T-2.1 : recherche étudiants et portefeuille accessibles en session
  passerelle, sur le campus de l'événement uniquement ; autres endpoints
  /api/* refusés ;
- T-2.2 : catalogue standard inclus dans la page passerelle et encaissement
  mixte (article événement + article standard) ;
- T-2.3 : sortie de passerelle -> session vidée et retour à la connexion.
"""

import json
import os
import re
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="foyz_gateway_"))
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

TOKEN_PREFIX = "jeton-pass-0123456789"


def _expect(cond, label):
    if not cond:
        raise AssertionError(f"échec : {label}")


def _csrf(html):
    match = re.search(r'name="csrf-token" content="([^"]+)"', html)
    _expect(match is not None, "jeton CSRF présent dans la page")
    return match.group(1)


def _seed(app, suffix):
    with app.app_context():
        ev = Event(
            name="Soirée test",
            campus="paris",
            starts_at=utcnow() - timedelta(hours=1),
            ends_at=utcnow() + timedelta(hours=6),
            token=f"{TOKEN_PREFIX}-{suffix}",
        )
        db.session.add(ev)
        db.session.flush()
        event_article = Article(
            name="Cocktail événement",
            article_type="evenement",
            is_alcohol=True,
            event_id=ev.id,
            campus="paris",
            price_std=300,
            active=True,
        )
        standard = Article(
            name="Pinte standard",
            article_type="biere",
            is_alcohol=True,
            campus="paris",
            price_std=200,
            active=True,
        )
        brest_only = Article(
            name="Snack Brest",
            article_type="snack",
            campus="brest",
            price_std=150,
            active=True,
        )
        user = User(
            name="Élève Paris",
            username=f"eleve.paris{suffix}",
            password_hash=generate_password_hash("secret123"),
        )
        db.session.add_all([event_article, standard, brest_only, user])
        db.session.flush()
        user.wallet("paris").balance = 1000
        user.wallet("brest").balance = 5000
        db.session.commit()
        return {
            "token": f"{TOKEN_PREFIX}-{suffix}",
            "event": ev.id,
            "event_article": event_article.id,
            "standard": standard.id,
            "brest_only": brest_only.id,
            "user": user.id,
        }


def test_gateway_full_parcours():
    app = create_app()
    ids = _seed(app, "a")
    client = app.test_client()

    _expect(client.get("/api/students?q=x").status_code == 401, "API refusée sans session")

    page = client.get(f"/passerelle/{ids['token']}")
    _expect(page.status_code == 200, f"passerelle ouverte ({page.status_code})")
    html = page.get_data(as_text=True)
    token = _csrf(html)
    _expect(
        "Cocktail" in html and "Pinte standard" in html,
        "les deux catalogues sont servis sur la page",
    )
    match = re.search(
        r'<script type="application/json" id="catalog-data">(.*?)</script>', html, re.S
    )
    catalog = json.loads(match.group(1))
    flags = {a["name"]: a["event"] for a in catalog}
    _expect(flags.get("Cocktail événement") is True, "article événement marqué event")
    _expect(flags.get("Pinte standard") is False, "article standard inclus")
    _expect("Snack Brest" not in flags, "article absent du campus exclu")

    res = client.get("/api/students?q=eleve&campus=brest")
    _expect(res.status_code == 200, f"recherche étudiants autorisée ({res.status_code})")
    students = res.get_json()
    _expect(
        any(s["id"] == ids["user"] and s["balance"] == 1000 for s in students),
        "campus de l'événement imposé (solde Paris)",
    )

    res = client.get(f"/api/wallet/{ids['user']}?campus=brest")
    _expect(res.get_json()["balance"] == 1000, "portefeuille lu sur le campus de l'événement")

    for path in ("/api/stats", "/api/treasury"):
        _expect(client.get(path).status_code == 403, f"{path} refusé en passerelle")
    res = client.post("/api/purchase", json={}, headers={"X-CSRFToken": token})
    _expect(res.status_code == 403, "écriture API refusée en passerelle")

    res = client.post(
        f"/passerelle/{ids['token']}/encaisser",
        json={
            "items": [
                {"article_id": ids["event_article"], "quantity": 1},
                {"article_id": ids["standard"], "quantity": 2},
            ],
            "contributors": [ids["user"]],
        },
        headers={"X-CSRFToken": token},
    )
    _expect(res.status_code == 200, f"encaissement mixte accepté ({res.status_code})")
    payload = res.get_json()
    _expect(
        payload["ok"] and payload["total"] == 300 + 2 * 200,
        f"total mixte correct ({payload})",
    )
    with app.app_context():
        t = db.session.get(Transaction, payload["transaction_id"])
        _expect(
            t.event_id == ids["event"] and t.campus == "paris",
            "transaction rattachée à l'événement",
        )
        user = db.session.get(User, ids["user"])
        _expect(user.wallet("paris").balance == 1000 - 700, "solde Paris débité")
        _expect(user.wallet("brest").balance == 5000, "solde Brest intact")

    res = client.post(f"/passerelle/{ids['token']}/quitter", data={"_csrf": token})
    _expect(
        res.status_code == 302 and "/connexion" in res.headers["Location"],
        f"sortie vers la connexion équipe ({res.headers.get('Location')})",
    )
    _expect(client.get("/api/students?q=x").status_code == 401, "session passerelle vidée")
    res = client.get("/equipe/paiement")
    _expect(
        res.status_code == 302 and "/connexion" in res.headers["Location"],
        "interface équipe de nouveau protégée",
    )


def test_gateway_rate_limits_encaissements():
    app = create_app()
    ids = _seed(app, "rl")
    client = app.test_client()
    html = client.get(f"/passerelle/{ids['token']}").get_data(as_text=True)
    token = _csrf(html)
    from app.routes.gateway import _pay_limiter

    statuses = [
        client.post(
            f"/passerelle/{ids['token']}/encaisser",
            json={"items": []},
            headers={"X-CSRFToken": token},
        ).status_code
        for _ in range(_pay_limiter.max_requests + 1)
    ]
    _expect(
        all(s != 429 for s in statuses[:-1]),
        "les encaissements sous le seuil restent traités",
    )
    _expect(
        statuses[-1] == 429,
        f"au-delà du seuil, l'encaissement passerelle est refusé ({statuses[-1]})",
    )


def test_gateway_refuses_foreign_article():
    app = create_app()
    ids = _seed(app, "b")
    client = app.test_client()
    html = client.get(f"/passerelle/{ids['token']}").get_data(as_text=True)
    token = _csrf(html)
    res = client.post(
        f"/passerelle/{ids['token']}/encaisser",
        json={
            "items": [{"article_id": ids["brest_only"], "quantity": 1}],
            "contributors": [ids["user"]],
        },
        headers={"X-CSRFToken": token},
    )
    _expect(res.status_code == 400, f"article hors campus refusé ({res.status_code})")
    _expect("indisponible" in res.get_json()["error"], "message d'article indisponible")


def test_gateway_standard_toggle_and_live_catalogue():
    """Le réglage « articles standards » s'applique en direct, côté affichage
    (route /catalogue sans rechargement) comme côté encaissement."""
    app = create_app()
    ids = _seed(app, "std")
    client = app.test_client()
    html = client.get(f"/passerelle/{ids['token']}").get_data(as_text=True)
    token = _csrf(html)

    live = client.get(f"/passerelle/{ids['token']}/catalogue").get_json()
    _expect(live["running"] is True, "catalogue servi en direct")
    _expect(live["allow_standard"] is True, "réglage standard par défaut")
    _expect(
        any(a["id"] == ids["standard"] for a in live["catalog"]),
        "article standard présent par défaut",
    )

    with app.app_context():
        ev = db.session.get(Event, ids["event"])
        ev.allow_standard_articles = False
        db.session.commit()

    live = client.get(f"/passerelle/{ids['token']}/catalogue").get_json()
    _expect(live["allow_standard"] is False, "réglage mis à jour en direct")
    _expect(
        all(a["event"] for a in live["catalog"]),
        "seuls les articles de l'événement restent affichés",
    )

    res = client.post(
        f"/passerelle/{ids['token']}/encaisser",
        json={
            "items": [{"article_id": ids["standard"], "quantity": 1}],
            "contributors": [ids["user"]],
        },
        headers={"X-CSRFToken": token},
    )
    _expect(res.status_code == 400, f"standard refusé réglage désactivé ({res.status_code})")

    res = client.post(
        f"/passerelle/{ids['token']}/encaisser",
        json={
            "items": [{"article_id": ids["event_article"], "quantity": 1}],
            "contributors": [ids["user"]],
        },
        headers={"X-CSRFToken": token},
    )
    _expect(res.status_code == 200, "article événement toujours encaissable")


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
