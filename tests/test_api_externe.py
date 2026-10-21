"""Tests de l'API externe et de la caisse avec les articles d'événement.

Exécutable sans pytest : python -m tests.test_api_externe

Couvre :
- onglet API réservé au compte admin, création/édition/révocation des jetons ;
- authentification Bearer, portées, limite d'une requête par seconde ;
- catalogue et agrégats de consommation sans données nominatives ;
- écriture par lot des prix standards (articles, événements, fûts) ;
- vente des articles d'événement depuis la caisse équipe, pendant la fenêtre
  de l'événement uniquement, et rafraîchissement du catalogue.
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

_TMP = Path(tempfile.mkdtemp(prefix="foyz_api_externe_"))
os.environ["DATABASE_URL"] = (
    os.environ.get("FOYZ_TEST_DATABASE_URL") or f"sqlite:///{_TMP / 'app.db'}"
)
os.environ["UPLOAD_DIR"] = str(_TMP / "uploads")
os.environ["SECRET_KEY"] = "test-secret-key-0123456789abcdef0123456789abcdef"
os.environ["ADMIN_PASSWORD"] = "mot-de-passe-admin"
os.environ.pop("FLASK_ENV", None)

from sqlalchemy import select  # noqa: E402
from werkzeug.security import generate_password_hash  # noqa: E402

from app import create_app  # noqa: E402
from app.extensions import db  # noqa: E402
from app.models import (  # noqa: E402
    ApiToken,
    Article,
    AuditLog,
    Event,
    Keg,
    KegPrice,
    Tap,
    Transaction,
    User,
)
from app.services import api_tokens as AT  # noqa: E402
from app.services import catalog as C  # noqa: E402
from app.services import transactions as T  # noqa: E402
from app.utils import to_paris, utcnow  # noqa: E402

ADMIN_PASSWORD = "mot-de-passe-admin"
app = create_app()
_seq = count(1)


def _expect(cond, label):
    if not cond:
        raise AssertionError(f"échec : {label}")


def _csrf(html, field="_csrf"):
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


def _admin_client():
    return _login(app.test_client(), "admin", ADMIN_PASSWORD)


def _user(name, *, campus="brest", team=False, balance=10_000):
    n = next(_seq)
    with app.app_context():
        user = User(
            name=f"{name} {n}",
            username=f"api.test.{n}",
            password_hash=generate_password_hash("secret123"),
            team_status="mandat" if team else "etudiant",
            team_campus=campus if team else None,
        )
        db.session.add(user)
        db.session.flush()
        user.wallet(campus).balance = balance
        db.session.commit()
        return user.id


def _article(
    name,
    *,
    campus="brest",
    kind="biere",
    std=300,
    team=None,
    active=True,
    volume_cl=None,
    event_id=None,
    tap=False,
    keg_id=None,
    tap_number=None,
):
    n = next(_seq)
    with app.app_context():
        article = Article(
            name=f"{name} {n}",
            article_type=kind,
            campus=campus,
            price_std=std,
            price_team=std if team is None else team,
            active=active,
            volume_cl=volume_cl,
            event_id=event_id,
            is_tap=tap,
            keg_id=keg_id,
            tap_number=tap_number,
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
            token=f"event-api-{next(_seq)}",
            closed=closed,
            allow_standard_articles=allow_standard,
        )
        db.session.add(event)
        db.session.commit()
        return event.id


def _keg(
    name,
    *,
    campus="brest",
    prices=(200, 400, 300),
    team_prices=(180, 360, 270),
    mounted=True,
):
    with app.app_context():
        keg = Keg(name=name, volume_l=30, remaining_l=20, active=True)
        db.session.add(keg)
        db.session.flush()
        row = keg.price_row(campus)
        row.price_half_std, row.price_pint_std, row.price_pot_std = prices
        row.price_half_team, row.price_pint_team, row.price_pot_team = team_prices
        if mounted:
            tap = Tap(number=next(_seq), campus=campus, keg_id=keg.id)
            db.session.add(tap)
            db.session.commit()
            C.assign_keg(tap, keg)
        else:
            db.session.commit()
        return keg.id


def _tap_article_ids(keg_id):
    with app.app_context():
        return {
            article.volume_cl: article.id
            for article in db.session.scalars(
                select(Article).where(Article.keg_id == keg_id, Article.is_tap.is_(True))
            )
        }


def _local(dt):
    return to_paris(dt).strftime("%Y-%m-%dT%H:%M")


def _token_form_data(
    *,
    label,
    campus,
    article_ids=(),
    keg_ids=(),
    read=True,
    write=True,
    expires_hours=4,
    history_from_hours=-1,
    history_to_hours=2,
):
    now = utcnow()
    data = {
        "label": label,
        "campus": campus,
        "expires_at": _local(now + timedelta(hours=expires_hours)),
        "history_from": _local(now + timedelta(hours=history_from_hours)),
        "history_to": _local(now + timedelta(hours=history_to_hours)),
        "article_ids": [str(value) for value in article_ids],
        "keg_ids": [str(value) for value in keg_ids],
    }
    if read:
        data["can_read_consumption"] = "on"
    if write:
        data["can_write_prices"] = "on"
    return data


def _post_token_form(client, url, **kwargs):
    page = client.get("/admin/api/nouveau").get_data(as_text=True)
    data = _token_form_data(**kwargs)
    data["_csrf"] = _csrf(page)
    return client.post(url, data=data)


def _create_token(client, **kwargs):
    res = _post_token_form(client, "/admin/api/nouveau", **kwargs)
    _expect(res.status_code == 200, f"création du jeton ({res.status_code})")
    match = re.search(r'id="new-api-token"[^>]*value="([^"]+)"', res.get_data(as_text=True))
    _expect(match is not None, "jeton affiché une seule fois après création")
    raw = match.group(1)
    _expect(raw.startswith("fz_"), "préfixe du jeton")
    return raw


def _token_id(raw):
    with app.app_context():
        return (
            db.session.scalars(select(ApiToken).where(ApiToken.token_hash == AT.hash_token(raw)))
            .one()
            .id
        )


def _reset_rate(raw):
    with app.app_context():
        token = db.session.scalars(
            select(ApiToken).where(ApiToken.token_hash == AT.hash_token(raw))
        ).one()
        token.last_request_at = None
        db.session.commit()


def _get(client, raw, path, **kwargs):
    _reset_rate(raw)
    return client.get(path, headers={"Authorization": f"Bearer {raw}"}, **kwargs)


def _patch(client, raw, path, **kwargs):
    _reset_rate(raw)
    return client.patch(path, headers={"Authorization": f"Bearer {raw}"}, **kwargs)


def _edit_token(
    client,
    token_id,
    *,
    label,
    article_ids=(),
    keg_ids=(),
    read=True,
    write=True,
    expires_hours=6,
    history_from_hours=-2,
    history_to_hours=2,
):
    page = client.get(f"/admin/api/{token_id}").get_data(as_text=True)
    data = _token_form_data(
        label=label,
        campus="brest",
        article_ids=article_ids,
        keg_ids=keg_ids,
        read=read,
        write=write,
        expires_hours=expires_hours,
        history_from_hours=history_from_hours,
        history_to_hours=history_to_hours,
    )
    data["_csrf"] = _csrf(page)
    return client.post(f"/admin/api/{token_id}", data=data)


def _revoke(client, token_id):
    page = client.get("/admin/api").get_data(as_text=True)
    return client.post(f"/admin/api/{token_id}/revoquer", data={"_csrf": _csrf(page)})


def test_admin_only_and_token_lifecycle():
    mandate_id = _user("Mandat API", team=True)
    mandate = _login(app.test_client(), _username(mandate_id))
    _expect(mandate.get("/admin/api").status_code == 403, "onglet API interdit aux mandats")
    _expect(
        mandate.get("/admin/api/nouveau").status_code == 403,
        "création de jeton interdite aux mandats",
    )
    page = mandate.get("/equipe/paiement").get_data(as_text=True)
    _expect('href="/admin/api"' not in page, "lien API absent du menu des mandats")

    admin = _admin_client()
    _expect(admin.get("/admin/api").status_code == 200, "onglet API accessible à l'admin")
    page = admin.get("/admin/api").get_data(as_text=True)
    _expect('href="/admin/api"' in page, "lien API présent pour l'admin")
    _expect("guide-api-externe.txt" in page, "guide tiers lié depuis l'onglet")
    guide = admin.get("/static/guide-api-externe.txt")
    _expect(guide.status_code == 200, "guide tiers téléchargeable")
    _expect(guide.content_type.startswith("text/plain"), "guide en texte brut")
    _expect(
        "Authorization: Bearer" in guide.get_data(as_text=True),
        "guide décrit l'authentification",
    )

    article_a = _article("Jeton article A", std=400)
    article_b = _article("Jeton article B", std=350)
    event_id = _event("Événement jeton", starts=-1, ends=4)
    event_article = _article(
        "Jeton article événement", kind="evenement", std=500, event_id=event_id
    )
    keg_id = _keg("Jeton fût")

    raw = _create_token(
        admin,
        label="Jeton principal",
        campus="brest",
        article_ids=[article_a, event_article],
        keg_ids=[keg_id],
    )
    with app.app_context():
        token = db.session.scalars(
            select(ApiToken).where(ApiToken.token_hash == AT.hash_token(raw))
        ).one()
        _expect(token.token_hash != raw, "jeton jamais stocké en clair")
        _expect(token.label == "Jeton principal", "libellé conservé")
        _expect(token.campus == "brest", "campus du jeton")
        _expect(len(token.article_links) == 2, "deux articles sélectionnés")
        _expect(len(token.keg_links) == 1, "un fût sélectionné")
        _expect(
            article_b not in {link.article_id for link in token.article_links}, "B hors sélection"
        )

    listing = admin.get("/admin/api").get_data(as_text=True)
    _expect(raw not in listing, "jeton non réaffiché dans la liste")
    _expect("Jeton principal" in listing, "jeton listé")

    token_id = _token_id(raw)
    res = _revoke(admin, token_id)
    _expect(res.status_code == 302, "révocation acceptée")
    res = admin.get("/api/v1/catalog", headers={"Authorization": f"Bearer {raw}"})
    _expect(res.status_code == 401, "jeton révoqué refusé")


def test_api_auth_rate_limit_and_scopes():
    admin = _admin_client()
    article_a = _article("API auth A", std=400)
    article_b = _article("API auth B", std=350)
    raw = _create_token(admin, label="Jeton auth", campus="brest", article_ids=[article_a])
    client = app.test_client()

    _expect(client.get("/api/v1/catalog").status_code == 401, "sans jeton : 401")
    _expect(
        client.get("/api/v1/catalog", headers={"Authorization": "Bearer inconnu"}).status_code
        == 401,
        "jeton inconnu : 401",
    )

    res = _get(client, raw, "/api/v1/catalog")
    _expect(res.status_code == 200, f"catalogue autorisé ({res.status_code})")
    _expect(res.headers.get("Cache-Control") == "no-store, private", "API non mise en cache")
    ids = {article["id"] for article in res.get_json()["articles"]}
    _expect(article_a in ids and article_b not in ids, "seuls les articles du jeton")

    _reset_rate(raw)
    first = client.get("/api/v1/catalog", headers={"Authorization": f"Bearer {raw}"})
    second = client.get("/api/v1/catalog", headers={"Authorization": f"Bearer {raw}"})
    _expect(first.status_code == 200, "premier appel accepté")
    _expect(
        second.status_code == 429,
        f"deuxième appel dans la même seconde refusé ({second.status_code})",
    )
    _expect(second.headers.get("Retry-After") == "1", "en-tête Retry-After")
    _reset_rate(raw)
    _expect(
        client.get("/api/v1/catalog", headers={"Authorization": f"Bearer {raw}"}).status_code
        == 200,
        "après une seconde, de nouveau accepté",
    )

    read_raw = _create_token(
        admin,
        label="Jeton lecture",
        campus="brest",
        article_ids=[article_a],
        read=True,
        write=False,
    )
    res = _get(client, read_raw, "/api/v1/consumption")
    _expect(res.status_code == 200, "lecture seule : consommation autorisée")
    res = _patch(
        client,
        read_raw,
        "/api/v1/prices",
        json={"articles": [{"article_id": article_a, "price_std_cents": 450}]},
    )
    _expect(res.status_code == 403, "lecture seule : écriture refusée")

    write_raw = _create_token(
        admin,
        label="Jeton écriture",
        campus="brest",
        article_ids=[article_a],
        read=False,
        write=True,
    )
    res = _get(client, write_raw, "/api/v1/consumption")
    _expect(res.status_code == 403, "écriture seule : lecture refusée")
    res = _patch(
        client,
        write_raw,
        "/api/v1/prices",
        json={"articles": [{"article_id": article_a, "price_std_cents": 450}]},
    )
    _expect(res.status_code == 200, "écriture seule : prix accepté")

    res = _get(client, raw, "/api/v1/inconnu")
    _expect(res.status_code == 404 and res.get_json()["code"] == "not_found", "404 JSON")
    res = client.get("/api/v1")
    _expect(res.status_code == 404 and res.get_json()["code"] == "not_found", "base API 404 JSON")
    _reset_rate(raw)
    res = client.post("/api/v1/catalog", headers={"Authorization": f"Bearer {raw}"})
    _expect(res.status_code == 405 and res.get_json()["code"] == "method_not_allowed", "405 JSON")

    with app.app_context():
        token = db.session.scalars(
            select(ApiToken).where(ApiToken.token_hash == AT.hash_token(raw))
        ).one()
        token.expires_at = utcnow() - timedelta(minutes=1)
        db.session.commit()
    _expect(
        client.get("/api/v1/catalog", headers={"Authorization": f"Bearer {raw}"}).status_code
        == 401,
        "jeton expiré refusé",
    )


def test_consumption_aggregates_and_privacy():
    admin = _admin_client()
    article_a = _article("Conso A", std=400, team=350, volume_cl=50)
    article_b = _article("Conso B", std=300)
    event_id = _event("Conso événement", starts=-1, ends=4)
    event_article = _article(
        "Conso événement article", kind="evenement", std=500, event_id=event_id
    )
    keg_id = _keg("Conso fût")
    demi_id = _tap_article_ids(keg_id)[25]
    student_id = _user("Conso Étudiant", balance=20_000)

    raw = _create_token(
        admin,
        label="Jeton conso",
        campus="brest",
        article_ids=[article_a, event_article],
        keg_ids=[keg_id],
        history_from_hours=-1,
        history_to_hours=2,
        expires_hours=4,
    )

    with app.app_context():
        t1 = T.create_purchase(
            operator_label="test",
            campus="brest",
            items=[
                {"article_id": article_a, "quantity": 2},
                {"article_id": event_article, "quantity": 1},
            ],
            contributor_ids=[student_id],
            team_sale=True,
        )
        _expect(t1.event_id == event_id, "vente caisse rattachée à l'événement")
        T.create_purchase(
            operator_label="test",
            campus="brest",
            items=[{"article_id": demi_id, "quantity": 1}],
            contributor_ids=[student_id],
            team_sale=True,
        )
        T.create_purchase(
            operator_label="test",
            campus="brest",
            items=[{"article_id": article_b, "quantity": 1}],
            contributor_ids=[student_id],
            team_sale=True,
        )
        cancelled = T.create_purchase(
            operator_label="test",
            campus="brest",
            items=[{"article_id": article_a, "quantity": 1}],
            contributor_ids=[student_id],
            team_sale=True,
        )
        T.cancel_transaction(db.session.get(Transaction, cancelled.id), None, verified=True)

    client = app.test_client()
    res = _get(client, raw, "/api/v1/consumption")
    _expect(res.status_code == 200, f"consommation lue ({res.status_code})")
    body = res.get_json()
    groups = {row.get("article_id"): row for row in body["groups"]}
    _expect(groups[article_a]["quantity"] == 2, "quantité A")
    _expect(groups[article_a]["revenue_cents"] == 800, "recette A")
    _expect(groups[article_a]["volume_ml"] == 1000, "volume A en millilitres")
    _expect(groups[event_article]["quantity"] == 1, "quantité événement")
    _expect(groups[demi_id]["quantity"] == 1, "quantité demi")
    _expect(groups[demi_id]["volume_ml"] == 250, "volume demi en millilitres")
    _expect(article_b not in groups, "article non sélectionné exclu")
    _expect(body["totals"] == {"quantity": 4, "revenue_cents": 1500, "volume_ml": 1250}, "totaux")
    _expect("Conso Étudiant" not in res.get_data(as_text=True), "aucun nom d'étudiant")
    keg_row = body["kegs"][0]
    _expect(abs(keg_row["remaining_l"] - 19.75) < 0.01, "litres restants du fût")

    res = _get(client, raw, "/api/v1/consumption", query_string={"group_by": "category"})
    categories = {row["category"]: row for row in res.get_json()["groups"]}
    _expect(categories["biere"]["quantity"] == 3, "catégorie bière")
    _expect(categories["biere"]["revenue_cents"] == 1000, "recette bière")
    _expect(categories["evenement"]["quantity"] == 1, "catégorie événement")

    res = _get(client, raw, "/api/v1/consumption", query_string={"bucket_seconds": "3600"})
    series = res.get_json()["series"]
    _expect(sum(row["quantity"] for row in series) == 4, "série : quantités")
    _expect(sum(row["revenue_cents"] for row in series) == 1500, "série : recettes")

    now = utcnow()
    res = _get(
        client,
        raw,
        "/api/v1/consumption",
        query_string={
            "from": AT.iso_utc(now - timedelta(minutes=30)),
            "to": AT.iso_utc(now + timedelta(minutes=30)),
        },
    )
    _expect(res.status_code == 200 and res.get_json()["totals"]["quantity"] == 4, "sous-fenêtre")

    res = _get(
        client,
        raw,
        "/api/v1/consumption",
        query_string={"from": AT.iso_utc(now - timedelta(hours=3))},
    )
    _expect(res.status_code == 400, "hors des bornes : refusé")
    res = _get(client, raw, "/api/v1/consumption", query_string={"group_by": "x"})
    _expect(res.status_code == 400, "group_by invalide")
    res = _get(client, raw, "/api/v1/consumption", query_string={"bucket_seconds": "abc"})
    _expect(res.status_code == 400, "bucket invalide")


def test_price_updates_and_audit():
    admin = _admin_client()
    article_a = _article("Prix A", std=400, team=350)
    article_b = _article("Prix B", std=300)
    event_id = _event("Prix événement", starts=-1, ends=4)
    event_article = _article(
        "Prix événement article", kind="evenement", std=500, team=450, event_id=event_id
    )
    keg_id = _keg("Prix fût")
    raw = _create_token(
        admin,
        label="Jeton prix",
        campus="brest",
        article_ids=[article_a, event_article],
        keg_ids=[keg_id],
        read=True,
        write=True,
    )
    client = app.test_client()

    res = _patch(
        client,
        raw,
        "/api/v1/prices",
        json={
            "articles": [
                {"article_id": article_a, "price_std_cents": 450},
                {"article_id": event_article, "price_std_cents": 550},
            ],
            "kegs": [
                {
                    "keg_id": keg_id,
                    "price_half_std_cents": 250,
                    "price_pint_std_cents": 450,
                    "price_pot_std_cents": 350,
                }
            ],
        },
    )
    _expect(res.status_code == 200, f"lot de prix accepté ({res.status_code}, {res.get_json()})")

    with app.app_context():
        a = db.session.get(Article, article_a)
        ea = db.session.get(Article, event_article)
        _expect(a.price_std == 450 and a.price_team == 350, "prix A standard seul")
        _expect(ea.price_std == 550 and ea.price_team == 450, "prix événement standard seul")
        row = db.session.scalars(
            select(KegPrice).where(KegPrice.keg_id == keg_id, KegPrice.campus == "brest")
        ).one()
        _expect(
            (row.price_half_std, row.price_pint_std, row.price_pot_std) == (250, 450, 350),
            "prix du fût",
        )
        _expect(
            (row.price_half_team, row.price_pint_team, row.price_pot_team) == (180, 360, 270),
            "tarif équipe du fût inchangé",
        )
        taps = {
            article.volume_cl: article
            for article in db.session.scalars(
                select(Article).where(Article.keg_id == keg_id, Article.is_tap.is_(True))
            )
        }
        _expect(taps[25].price_std == 250, "demi mis à jour")
        _expect(taps[50].price_std == 450, "pinte mise à jour")
        _expect(taps[33].price_std == 350, "pot mis à jour")
        _expect(
            all(article.price_team in (180, 360, 270) for article in taps.values()),
            "tarif équipe des tireuses inchangé",
        )
        logs = db.session.scalars(
            select(AuditLog).where(AuditLog.action.in_(("api.prix_article", "api.prix_fut")))
        ).all()
        _expect(
            any("Jeton API" in log.actor and "Jeton prix" in log.actor for log in logs),
            "audit des prix avec le libellé du jeton",
        )

    with app.app_context():
        a = db.session.get(Article, article_a)
        a.price_std = 400
        db.session.commit()
    res = _patch(
        client,
        raw,
        "/api/v1/prices",
        json={
            "articles": [
                {"article_id": article_a, "price_std_cents": 450},
                {"article_id": article_b, "price_std_cents": 450},
            ]
        },
    )
    _expect(res.status_code == 403, "article hors périmètre refusé")
    with app.app_context():
        _expect(db.session.get(Article, article_a).price_std == 400, "lot atomique : A inchangé")

    for bad in (10_001, -1, "x", None):
        data = {"articles": [{"article_id": article_a, "price_std_cents": bad}]}
        res = _patch(client, raw, "/api/v1/prices", json=data)
        _expect(res.status_code == 400, f"prix invalide refusé ({bad})")
    res = _patch(client, raw, "/api/v1/prices", json={"articles": []})
    _expect(res.status_code == 400, "lot vide refusé")
    res = _patch(
        client,
        raw,
        "/api/v1/prices",
        json={
            "articles": [
                {"article_id": article_a, "price_std_cents": 410},
                {"article_id": article_a, "price_std_cents": 420},
            ]
        },
    )
    _expect(res.status_code == 400, "doublon refusé")

    read_raw = _create_token(
        admin,
        label="Jeton prix lecture",
        campus="brest",
        article_ids=[article_a],
        read=True,
        write=False,
    )
    res = _patch(
        client,
        read_raw,
        "/api/v1/prices",
        json={"articles": [{"article_id": article_a, "price_std_cents": 450}]},
    )
    _expect(res.status_code == 403, "jeton lecture seule refusé en écriture")


def test_edit_token_with_detached_keg():
    admin = _admin_client()
    keg_id = _keg("Édition fût détaché")
    raw = _create_token(admin, label="Jeton fût détaché", campus="brest", keg_ids=[keg_id])
    token_id = _token_id(raw)
    with app.app_context():
        tap = db.session.scalars(select(Tap).where(Tap.keg_id == keg_id)).first()
        C.detach_keg(tap)

    page = admin.get(f"/admin/api/{token_id}").get_data(as_text=True)
    _expect(f'id="keg-{keg_id}-brest"' in page, "fût détaché toujours visible")
    _expect(
        re.search(f'id="keg-{keg_id}-brest"[^>]*checked', page, re.S) is not None,
        "fût détaché toujours coché",
    )
    res = _edit_token(
        admin,
        token_id,
        label="Jeton fût détaché modifié",
        keg_ids=[keg_id],
    )
    _expect(res.status_code == 302, "modification avec fût détaché acceptée")
    with app.app_context():
        token = db.session.get(ApiToken, token_id)
        _expect({link.keg_id for link in token.keg_links} == {keg_id}, "fût conservé")


def test_unmounted_keg_scope():
    admin = _admin_client()
    keg_id = _keg("Fût non monté principal", mounted=False)
    page = admin.get("/admin/api/nouveau").get_data(as_text=True)
    _expect("Fût non monté principal" in page, "fût non monté proposé")
    _expect(f'id="keg-{keg_id}-brest"' in page, "case du fût non monté")
    _expect("non monté" in page, "mention non monté")

    raw = _create_token(admin, label="Jeton fût non monté", campus="brest", keg_ids=[keg_id])
    client = app.test_client()
    body = _get(client, raw, "/api/v1/catalog").get_json()
    _expect(any(keg["id"] == keg_id for keg in body["kegs"]), "fût non monté accessible")
    _expect(not body["articles"], "aucun article de tireuse avant montage")

    res = _patch(
        client,
        raw,
        "/api/v1/prices",
        json={
            "kegs": [
                {
                    "keg_id": keg_id,
                    "price_half_std_cents": 260,
                    "price_pint_std_cents": 470,
                    "price_pot_std_cents": 360,
                }
            ]
        },
    )
    _expect(res.status_code == 200, "prix du fût non monté acceptés")
    with app.app_context():
        row = db.session.scalars(
            select(KegPrice).where(KegPrice.keg_id == keg_id, KegPrice.campus == "brest")
        ).one()
        _expect(
            (row.price_half_std, row.price_pint_std, row.price_pot_std) == (260, 470, 360),
            "prix du fût non monté enregistrés",
        )
        _expect(
            not db.session.scalars(
                select(Article).where(Article.keg_id == keg_id, Article.is_tap.is_(True))
            ).all(),
            "pas encore d'articles de tireuse",
        )
        tap = Tap(number=next(_seq), campus="brest", keg_id=keg_id)
        db.session.add(tap)
        db.session.commit()
        C.assign_keg(tap, db.session.get(Keg, keg_id))
    with app.app_context():
        taps = {
            article.volume_cl: article
            for article in db.session.scalars(
                select(Article).where(Article.keg_id == keg_id, Article.is_tap.is_(True))
            )
        }
        _expect(
            (taps[25].price_std, taps[50].price_std, taps[33].price_std) == (260, 470, 360),
            "prix appliqués au montage",
        )

    # Modification de la portée à la volée : ajout d'un second fût non monté.
    second = _keg("Fût non monté secondaire", mounted=False)
    token_id = _token_id(raw)
    res = _edit_token(
        admin,
        token_id,
        label="Jeton fût non monté étendu",
        keg_ids=[keg_id, second],
    )
    _expect(res.status_code == 302, "portée modifiée à la volée")
    body = _get(client, raw, "/api/v1/catalog").get_json()
    keg_ids = {keg["id"] for keg in body["kegs"]}
    _expect({keg_id, second} <= keg_ids, "les deux fûts sont accessibles")


def test_token_edit_and_revocation_immediate():
    admin = _admin_client()
    article_a = _article("Édition A", std=400)
    article_b = _article("Édition B", std=350)
    raw = _create_token(
        admin,
        label="Jeton édition",
        campus="brest",
        article_ids=[article_a],
    )
    token_id = _token_id(raw)
    client = app.test_client()

    body = _get(client, raw, "/api/v1/catalog").get_json()
    _expect(
        {article["id"] for article in body["articles"]} == {article_a},
        "sélection initiale",
    )

    res = _edit_token(
        admin,
        token_id,
        label="Jeton édition modifié",
        article_ids=[article_b],
        expires_hours=6,
        history_from_hours=-2,
        history_to_hours=3,
    )
    _expect(res.status_code == 302, "modification acceptée")
    body = _get(client, raw, "/api/v1/catalog").get_json()
    _expect(
        {article["id"] for article in body["articles"]} == {article_b},
        "nouvelle sélection immédiate",
    )
    _expect(body["token"]["label"] == "Jeton édition modifié", "libellé modifié")
    _expect(
        "Jeton édition modifié" in admin.get("/admin/api").get_data(as_text=True), "liste à jour"
    )

    with app.app_context():
        token = db.session.get(ApiToken, token_id)
        token.expires_at = utcnow() - timedelta(minutes=1)
        db.session.commit()
    _expect(
        client.get("/api/v1/catalog", headers={"Authorization": f"Bearer {raw}"}).status_code
        == 401,
        "jeton expiré refusé",
    )

    res = _edit_token(
        admin,
        token_id,
        label="Jeton édition prolongé",
        article_ids=[article_b],
        expires_hours=6,
        history_from_hours=-2,
        history_to_hours=3,
    )
    _expect(res.status_code == 302, "prolongation acceptée")
    _expect(_get(client, raw, "/api/v1/catalog").status_code == 200, "même jeton réactivé")

    _revoke(admin, token_id)
    _expect(
        client.get("/api/v1/catalog", headers={"Authorization": f"Bearer {raw}"}).status_code
        == 401,
        "jeton révoqué refusé",
    )
    res = _edit_token(
        admin,
        token_id,
        label="Tentative révoquée",
        article_ids=[article_a],
        expires_hours=6,
        history_from_hours=-2,
        history_to_hours=3,
    )
    _expect(res.status_code == 302, "édition d'un jeton révoqué refusée")
    with app.app_context():
        _expect(
            db.session.get(ApiToken, token_id).label == "Jeton édition prolongé",
            "jeton révoqué inchangé",
        )


def test_token_validation_errors():
    admin = _admin_client()
    article_brest = _article("Validation Brest", std=400)
    article_paris = _article("Validation Paris", campus="paris", std=400)
    keg_brest = _keg("Validation fût Brest")
    keg_paris = _keg("Validation fût Paris", campus="paris")
    tap_article = _tap_article_ids(keg_brest)[25]

    def count_tokens(label):
        with app.app_context():
            return len(db.session.scalars(select(ApiToken).where(ApiToken.label == label)).all())

    cases = [
        ("Validation sans droit", dict(campus="brest", read=False, write=False)),
        ("Validation label vide", dict(campus="brest", label="")),
        (
            "Validation bornes",
            dict(
                campus="brest",
                expires_hours=4,
                history_from_hours=-1,
                history_to_hours=5,
            ),
        ),
        ("Validation campus", dict(campus="brest", article_ids=[article_paris])),
        ("Validation tireuse", dict(campus="brest", article_ids=[tap_article])),
        ("Validation fût campus", dict(campus="brest", keg_ids=[keg_paris])),
        ("Validation expiration", dict(campus="brest", expires_hours=-1)),
    ]
    for label, values in cases:
        params = {"label": label, **values}
        data = _token_form_data(**params)
        page = admin.get("/admin/api/nouveau").get_data(as_text=True)
        data["_csrf"] = _csrf(page)
        res = admin.post("/admin/api/nouveau", data=data)
        _expect(res.status_code == 302, f"{label} : formulaire refusé")
        _expect(count_tokens(label) == 0, f"{label} : aucun jeton créé")

    raw = _create_token(
        admin,
        label="Validation valide",
        campus="brest",
        article_ids=[article_brest],
        keg_ids=[keg_brest],
    )
    _expect(raw.startswith("fz_"), "jeton valide créé")


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
