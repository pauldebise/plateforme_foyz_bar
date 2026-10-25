"""Récapitulatif de fin d'événement.

Exécutable sans pytest : python -m tests.test_recap_evenement

Couvre la disponibilité après la fin ou la clôture, les recettes hors
consignes, le détail par article, la fréquentation, les horaires, les volumes
et fûts, le détail par opérateur, les annulations, l'export CSV et
l'attribution des paniers multi-événements.
"""

import csv
import io
import os
import re
import sys
import tempfile
from datetime import timedelta
from html import unescape
from itertools import count
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="foyz_recap_evenement_"))
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
from app.models import Article, Event, Keg, Transaction, User  # noqa: E402
from app.services import transactions as T  # noqa: E402
from app.services.event_recap import build_recap, export_rows  # noqa: E402
from app.utils import utcnow  # noqa: E402

app = create_app()
_seq = count(1)
ADMIN_PASSWORD = "mot-de-passe-admin"


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


def _login(client, username, password, campus="brest"):
    token = _csrf(client.get("/connexion").get_data(as_text=True), "csrf-token")
    res = client.post(
        "/connexion",
        data={"username": username, "password": password, "campus": campus, "_csrf": token},
    )
    _expect(res.status_code == 302, f"connexion {username} sur {campus} ({res.status_code})")
    return client


def _user(name, *, campus="brest", balance=100_000, promotion=2027, team=False):
    n = next(_seq)
    with app.app_context():
        user = User(
            name=f"{name} {n}",
            username=f"recap.evenement.{n}",
            password_hash=generate_password_hash("secret123"),
            team_status="mandat" if team else "etudiant",
            team_campus=campus if team else None,
            promotion=promotion,
        )
        db.session.add(user)
        db.session.flush()
        user.wallet(campus).balance = balance
        db.session.commit()
        return user.id


def _keg(name, volume_l=30.0):
    n = next(_seq)
    with app.app_context():
        keg = Keg(name=f"{name} {n}", volume_l=volume_l)
        db.session.add(keg)
        db.session.commit()
        return keg.id


def _article(
    name,
    *,
    campus="brest",
    kind="evenement",
    std=500,
    team=None,
    event_id=None,
    volume_cl=None,
    keg_id=None,
    is_alcohol=False,
):
    n = next(_seq)
    with app.app_context():
        article = Article(
            name=f"{name} {n}",
            article_type=kind,
            campus=campus,
            price_std=std,
            price_team=std if team is None else team,
            active=True,
            event_id=event_id,
            volume_cl=volume_cl,
            keg_id=keg_id,
            is_alcohol=is_alcohol,
        )
        db.session.add(article)
        db.session.commit()
        return article.id


def _event(name, *, campus="brest", starts=-4, ends=1, closed=False, allow_standard=True):
    with app.app_context():
        event = Event(
            name=name,
            campus=campus,
            starts_at=utcnow() + timedelta(hours=starts),
            ends_at=utcnow() + timedelta(hours=ends),
            token=f"recap-event-{next(_seq)}",
            closed=closed,
            allow_standard_articles=allow_standard,
        )
        db.session.add(event)
        db.session.commit()
        return event.id


def _finish(event_id):
    with app.app_context():
        event = db.session.get(Event, event_id)
        event.ends_at = utcnow() - timedelta(minutes=5)
        db.session.commit()


def _purchase(**kwargs):
    with app.app_context():
        transaction = T.create_purchase(**kwargs)
        return transaction.id


def test_recap_complet_et_export():
    buyer_id = _user("Acheteur récap")
    keg_id = _keg("Fût récap", 30.0)
    event_id = _event("Soirée récap")
    beer_id = _article(
        "Bière événement récap",
        kind="biere",
        std=500,
        event_id=event_id,
        volume_cl=25,
        keg_id=keg_id,
        is_alcohol=True,
    )
    snack_id = _article("Snack événement récap", kind="snack", std=200, event_id=event_id)
    standard_id = _article(
        "Bouteille standard récap", kind="biere", std=300, volume_cl=33, is_alcohol=True
    )
    outside_id = _article("Article hors événement récap", kind="snack", std=300)

    gateway_tx = _purchase(
        operator_label="Passerelle · Soirée récap",
        campus="brest",
        items=[
            {"article_id": beer_id, "quantity": 2},
            {"article_id": standard_id, "quantity": 1},
            {"article_id": snack_id, "quantity": 1},
        ],
        contributor_ids=[buyer_id],
        deposit_glasses=2,
        event_id=event_id,
        gateway=True,
    )
    direct_tx = _purchase(
        operator_label="Alice",
        campus="brest",
        items=[{"article_id": beer_id, "quantity": 1}],
        direct=True,
        payment_method="cb",
        event_id=event_id,
    )
    team_tx = _purchase(
        operator_label="Bob",
        campus="brest",
        items=[
            {"article_id": beer_id, "quantity": 1},
            {"article_id": standard_id, "quantity": 1},
        ],
        contributor_ids=[buyer_id],
        team_sale=True,
    )
    cancelled_tx = _purchase(
        operator_label="Alice",
        campus="brest",
        items=[{"article_id": beer_id, "quantity": 1}],
        contributor_ids=[buyer_id],
        event_id=event_id,
    )
    outside_tx = _purchase(
        operator_label="Alice",
        campus="brest",
        items=[{"article_id": outside_id, "quantity": 1}],
        contributor_ids=[buyer_id],
    )
    with app.app_context():
        T.cancel_transaction(db.session.get(Transaction, cancelled_tx), ADMIN_PASSWORD)
        for tx_id in (gateway_tx, direct_tx, team_tx):
            transaction = db.session.get(Transaction, tx_id)
            _expect(
                transaction.event_id == event_id, f"transaction {tx_id} rattachée à l'événement"
            )
        _expect(
            db.session.get(Transaction, outside_tx).event_id is None,
            "vente standard hors événement non rattachée",
        )
        _expect(
            db.session.get(Transaction, cancelled_tx).cancelled,
            "transaction annulée",
        )
    _finish(event_id)

    client = _login(app.test_client(), "admin", ADMIN_PASSWORD)
    page = client.get(f"/admin/evenements/{event_id}/recap")
    _expect(page.status_code == 200, f"page récap ({page.status_code})")
    html = page.get_data(as_text=True)
    _expect("built-in method" not in html, "aucune méthode Python affichée par erreur")
    for marker in (
        "Recette",
        "Détail des ventes par article",
        "Fréquentation et horaires",
        "Détail par opérateur",
        "Journal des ventes",
        "Rechercher un nom…",
        "Bière événement récap",
        "Snack événement récap",
        "Bouteille standard récap",
        "Passerelle",
        "Alice",
        "Bob",
        "Annulée",
        "28,00 €",
    ):
        _expect(marker in html, f"marqueur « {marker} » présent")
    for removed in (
        "Annulations exclues",
        "Répartition par catégorie",
        "Volumes et fûts",
        "Définitions.",
    ):
        _expect(removed not in html, f"bloc retiré « {removed} » absent")

    with app.app_context():
        event = db.session.get(Event, event_id)
        recap = build_recap(event)
        buyer_name = db.session.get(User, buyer_id).name
    summary = recap["summary"]
    _expect(summary["sales_count"] == 3, "trois ventes actives")
    _expect(summary["sold_items"] == 7, "sept articles vendus")
    _expect(summary["revenue"] == 2800, "recettes hors consignes")
    _expect(summary["average_basket"] == 933, "panier moyen arrondi")
    _expect("deposits" not in summary, "plus de total avec consignes")
    _expect("collected" not in summary, "plus de total encaissé")
    _expect("cancelled_count" not in summary, "plus de carte annulations")
    _expect(recap["buyers"]["unique"] == 1, "un acheteur unique")
    _expect(recap["buyers"]["top"][0]["spent"] == 2500, "montant du top acheteur")
    _expect(recap["buyers"]["promotions"][0]["promotion"] == 2027, "promotion de l'acheteur")
    _expect(len(recap["operators"]) == 3, "trois opérateurs")
    _expect(recap["peak"]["sales"] == 3, "trois ventes à l'heure de pointe")
    _expect(recap["transactions"]["total"] == 4, "quatre ventes au journal")
    _expect(recap["transactions"]["pages"] == 1, "une page de journal")
    _expect("categories" not in recap, "plus de répartition par catégorie")
    _expect("volumes" not in recap, "plus de volumes")

    with app.app_context():
        event = db.session.get(Event, event_id)
        paged = build_recap(event, page=9, per_page=2)
        by_article = build_recap(event, search="snack evenement")
        by_buyer = build_recap(event, search=buyer_name)
        by_operator = build_recap(event, search="bob")
        by_accent = build_recap(event, search="biere evenement")
        no_result = build_recap(event, search="introuvable")
    _expect(paged["transactions"]["pages"] == 2, "deux pages avec deux lignes")
    _expect(paged["transactions"]["page"] == 2, "page demandée ramenée à la dernière")
    _expect(len(paged["transactions"]["rows"]) == 2, "deux lignes sur la dernière page")
    _expect(by_article["transactions"]["total"] == 1, "recherche par nom d'article")
    _expect(by_buyer["transactions"]["total"] == 3, "recherche par nom d'acheteur")
    _expect(by_operator["transactions"]["total"] == 1, "recherche par opérateur")
    _expect(by_accent["transactions"]["total"] == 4, "recherche sans accents")
    _expect(no_result["transactions"]["total"] == 0, "recherche sans résultat")
    _expect(no_result["transactions"]["rows"] == [], "journal vide sans résultat")

    searched_page = client.get(
        f"/admin/evenements/{event_id}/recap", query_string={"q": "snack evenement"}
    )
    searched_html = searched_page.get_data(as_text=True)
    _expect('value="snack evenement"' in searched_html, "recherche conservée dans le champ")
    _expect("1 résultat" in searched_html, "compteur de résultats")
    empty_search = client.get(
        f"/admin/evenements/{event_id}/recap", query_string={"q": "zzz introuvable"}
    )
    _expect(
        "Aucune vente ne correspond à la recherche." in empty_search.get_data(as_text=True),
        "message de recherche vide",
    )

    csv_response = client.get(f"/admin/evenements/{event_id}/recap.csv")
    _expect(csv_response.status_code == 200, f"export CSV ({csv_response.status_code})")
    _expect(csv_response.mimetype == "text/csv", "type CSV")
    body = csv_response.data.decode("utf-8-sig")
    _expect(body.startswith("Date;N° transaction;"), "en-tête CSV")
    _expect("Annulée" in body, "annulation dans le CSV")
    _expect("Article hors événement récap" not in body, "vente hors événement exclue du CSV")
    rows = list(csv.reader(io.StringIO(body), delimiter=";"))
    _expect(rows[0][0] == "Date", "première colonne Date")
    _expect(len(rows) == 8, f"en-tête + sept lignes de vente ({len(rows)})")
    _expect(any(row[5] == "Annulée" for row in rows[1:]), "statut annulé dans les lignes")

    # Le récap n'apparaît qu'après la fin ou la clôture.
    upcoming_id = _event("Soirée récap à venir", starts=2, ends=4)
    redirect = client.get(f"/admin/evenements/{upcoming_id}/recap")
    _expect(redirect.status_code == 302, "récap indisponible avant la fin")
    followed = client.get(f"/admin/evenements/{upcoming_id}/recap", follow_redirects=True)
    _expect(
        "Le récap est disponible une fois l'événement terminé ou clôturé."
        in unescape(followed.get_data(as_text=True)),
        "message d'indisponibilité",
    )
    csv_redirect = client.get(f"/admin/evenements/{upcoming_id}/recap.csv")
    _expect(csv_redirect.status_code == 302, "export indisponible avant la fin")
    with app.app_context():
        event = db.session.get(Event, upcoming_id)
        event.closed = True
        db.session.commit()
    closed_page = client.get(f"/admin/evenements/{upcoming_id}/recap")
    _expect(closed_page.status_code == 200, "récap disponible après clôture")
    _expect(
        "Aucune vente rattachée à cet événement." in closed_page.get_data(as_text=True),
        "récap vide rendu",
    )
    empty_csv = client.get(f"/admin/evenements/{upcoming_id}/recap.csv")
    _expect(empty_csv.status_code == 200, "export vide disponible après clôture")
    empty_rows = list(csv.reader(io.StringIO(empty_csv.data.decode("utf-8-sig")), delimiter=";"))
    _expect(len(empty_rows) == 1, "export vide : en-tête seulement")


def test_panier_multi_evenements():
    buyer_id = _user("Acheteur multi récap")
    first_id = _event("Récap multi un", starts=-1, ends=4)
    second_id = _event("Récap multi deux", starts=-1, ends=4)
    first_article = _article("Article récap multi un", kind="evenement", std=400, event_id=first_id)
    second_article = _article(
        "Article récap multi deux", kind="evenement", std=700, event_id=second_id
    )
    tx_id = _purchase(
        operator_label="Caisse multi",
        campus="brest",
        items=[
            {"article_id": first_article, "quantity": 2},
            {"article_id": second_article, "quantity": 1},
        ],
        contributor_ids=[buyer_id],
        team_sale=True,
    )
    with app.app_context():
        transaction = db.session.get(Transaction, tx_id)
        _expect(transaction.event_id is None, "panier multi non rattaché")
        first_recap = build_recap(db.session.get(Event, first_id))
        second_recap = build_recap(db.session.get(Event, second_id))
    _expect(first_recap["summary"]["revenue"] == 800, "recettes du premier événement")
    _expect(first_recap["summary"]["sold_items"] == 2, "deux articles du premier événement")
    _expect(len(first_recap["articles"]) == 1, "seul le premier article est détaillé")
    _expect(
        first_recap["articles"][0]["name"].startswith("Article récap multi un"),
        "nom du premier article",
    )
    _expect(second_recap["summary"]["revenue"] == 700, "recettes du second événement")
    _expect(second_recap["summary"]["sold_items"] == 1, "un article du second événement")


def test_export_lignes_et_operateurs():
    buyer_id = _user("Acheteur export")
    event_id = _event("Soirée export récap")
    article_id = _article("Article export récap", std=450, event_id=event_id)
    _purchase(
        operator_label="Passerelle · Soirée export récap",
        campus="brest",
        items=[{"article_id": article_id, "quantity": 1}],
        contributor_ids=[buyer_id],
        event_id=event_id,
        gateway=True,
    )
    _finish(event_id)
    with app.app_context():
        event = db.session.get(Event, event_id)
        header, rows = export_rows(event)
    _expect(header[2] == "Canal", "colonne Canal")
    _expect(len(rows) == 1, "une ligne exportée")
    _expect(rows[0][2] == "Passerelle", "canal passerelle")
    _expect(rows[0][5] == "Validée", "statut validé")
    _expect(rows[0][9] == "1", "quantité exportée")


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
