"""Tests de l'onglet Statistiques (onglets, périodes, comparaison, export).

Exécutable sans pytest : python -m tests.test_statistiques
"""

import json
import os
import re
import sys
import tempfile
from datetime import date, datetime, time as dt_time, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="foyz_stats_"))
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
from app.models import Contribution, Event, Transaction, TransactionLine, User  # noqa: E402
from app.services import stats as ST  # noqa: E402
from app.utils import PARIS_TZ, paris_to_utc  # noqa: E402

ADMIN_PASSWORD = "mot-de-passe-admin"
_state = {}


def _expect(cond, label):
    if not cond:
        raise AssertionError(f"échec : {label}")


def _csrf(html):
    match = re.search(r'name="_csrf" value="([^"]+)"', html)
    _expect(match is not None, "jeton CSRF présent")
    return match.group(1)


def _login(client):
    html = client.get("/connexion").get_data(as_text=True)
    res = client.post(
        "/connexion",
        data={
            "username": "stats.mandat",
            "password": "secret123",
            "campus": "brest",
            "_csrf": _csrf(html),
        },
    )
    _expect(res.status_code == 302, f"connexion mandat ({res.status_code})")


def _add_transaction(campus, ttype, day, lines, contributors=(), event_id=None, total=None):
    when = paris_to_utc(datetime.combine(day, dt_time(18, 0)))
    transaction = Transaction(
        campus=campus,
        type=ttype,
        created_at=when,
        event_id=event_id,
    )
    for user, amount in contributors:
        transaction.contributions.append(
            Contribution(
                user_id=user.id if user else None,
                campus=campus,
                amount=amount,
                balance_after=0,
            )
        )
    for name, article_type, quantity, unit_price in lines:
        transaction.lines.append(
            TransactionLine(
                article_name=name,
                article_type=article_type,
                quantity=quantity,
                unit_price=unit_price,
                line_total=quantity * unit_price,
            )
        )
    transaction.total = (
        total if total is not None else sum(line.line_total for line in transaction.lines)
    )
    db.session.add(transaction)
    return transaction


def _setup():
    if _state:
        return _state
    app = create_app()
    today = datetime.now(PARIS_TZ).date()
    days = {
        "d1": today - timedelta(days=20),
        "d2": today - timedelta(days=18),
        "d3": today - timedelta(days=16),
        "d4": today - timedelta(days=12),
        "d5": today - timedelta(days=10),
        "previous": today - timedelta(days=25),
        "blague": today - timedelta(days=100),
        "old": today - timedelta(days=500),
    }
    with app.app_context():
        mandat = User(
            name="Mandat Stats",
            username="stats.mandat",
            team_status="mandat",
            team_campus="brest",
            password_hash=generate_password_hash("secret123"),
        )
        alice = User(name="Alice", username="stats.alice", promotion=2027)
        bob = User(
            name="Bob",
            username="stats.bob",
            promotion=2026,
            team_status="mandat",
            team_campus="brest",
        )
        charlie = User(name="Charlie", username="stats.charlie", promotion=2027)
        db.session.add_all([mandat, alice, bob, charlie])
        db.session.flush()

        event = Event(
            name="Soirée test",
            campus="brest",
            starts_at=paris_to_utc(datetime.combine(days["d4"], dt_time(18, 0))),
            ends_at=paris_to_utc(datetime.combine(days["d4"], dt_time(23, 0))),
            token="stats-event-token",
            closed=False,
        )
        db.session.add(event)
        db.session.flush()

        # A : achat partagé Alice + Bob (Pinte 2 × 2,50 € + Chips 1 × 2,00 €).
        _add_transaction(
            "brest",
            "achat",
            days["d1"],
            [("Pinte", "biere", 2, 250), ("Chips", "snack", 1, 200)],
            [(alice, 350), (bob, 350)],
        )
        # B : Alice seule (Pinte 1 × 2,50 €).
        _add_transaction(
            "brest",
            "achat",
            days["d2"],
            [("Pinte", "biere", 1, 250)],
            [(alice, 250)],
        )
        # C : Paris, Charlie (Vin 1 × 4,00 €).
        _add_transaction(
            "paris",
            "achat",
            days["d3"],
            [("Vin", "vin", 1, 400)],
            [(charlie, 400)],
        )
        # D : vente de soirée, Bob (Pinte 3 × 2,50 €).
        _add_transaction(
            "brest",
            "achat",
            days["d4"],
            [("Pinte", "biere", 3, 250)],
            [(bob, 750)],
            event_id=event.id,
        )
        # E : paiement direct sans participant (Chips 2 × 1,00 €).
        _add_transaction(
            "brest",
            "direct",
            days["d5"],
            [("Chips", "snack", 2, 100)],
            [(None, 200)],
        )
        # Vente de la période précédente, pour la comparaison.
        _add_transaction(
            "brest",
            "achat",
            days["previous"],
            [("Pinte", "biere", 1, 250)],
            [(alice, 250)],
        )
        # Vente très ancienne, hors du plafond d'accès, pour « Tout l'historique ».
        _add_transaction(
            "brest",
            "achat",
            days["old"],
            [("Pinte", "biere", 1, 300)],
            [(alice, 300)],
        )
        # Article « Blague », masquable depuis les filtres.
        _add_transaction(
            "brest",
            "achat",
            days["blague"],
            [("Blague", "blague", 1, 100)],
            [(alice, 100)],
        )
        db.session.commit()
    _state.update(app=app, today=today, **days)
    return _state


def _client():
    app = _setup()["app"]
    client = app.test_client()
    _login(client)
    return client


def _params(**overrides):
    state = _setup()
    params = {
        "preset": "custom",
        "date_from": state["d1"].isoformat(),
        "date_to": state["d5"].isoformat(),
        "campus": "brest",
    }
    params.update(overrides)
    return params


def _page(client, **overrides):
    res = client.get("/equipe/statistiques", query_string=_params(**overrides))
    _expect(res.status_code == 200, f"page statistiques ({res.status_code})")
    return res.get_data(as_text=True)


def _chart_data(html):
    match = re.search(r'<script type="application/json" id="stats-data">(.*?)</script>', html, re.S)
    _expect(match is not None, "données du graphique présentes")
    return json.loads(match.group(1))


def test_dates_et_periodes():
    today = date(2026, 10, 27)
    period = ST.resolve_period("30j", today=today)
    _expect(
        (period["start"], period["end"]) == (date(2026, 9, 28), today),
        "30 jours inclusifs",
    )
    _expect(period["preset"] == "30j" and not period["note"], "30j sans note")
    _expect(
        ST.resolve_period("mois", today=today)["start"] == date(2026, 10, 1),
        "début du mois",
    )
    previous_month = ST.resolve_period("mois-1", today=today)
    _expect(
        (previous_month["start"], previous_month["end"]) == (date(2026, 9, 1), date(2026, 9, 30)),
        "mois dernier complet",
    )
    _expect(
        ST.resolve_period("6mois", today=today)["start"] == date(2026, 5, 1),
        "six mois calendaires",
    )
    _expect(
        ST.resolve_period("12mois", today=today)["start"] == date(2025, 11, 1),
        "douze mois calendaires",
    )
    _expect(
        ST.resolve_period("annee", today=today)["start"] == date(2026, 1, 1),
        "année civile",
    )
    _expect(
        ST.resolve_period("7j", today=today)["start"] == date(2026, 10, 21),
        "sept jours",
    )

    swapped = ST.resolve_period("custom", "2026-10-12", "2026-10-01", today=today)
    _expect(
        (swapped["start"], swapped["end"]) == (date(2026, 10, 1), date(2026, 10, 12)),
        "dates inversées remises dans l'ordre",
    )
    _expect("inversées" in swapped["note"], "note d'inversion")

    future = ST.resolve_period("custom", "2026-10-01", "2026-12-31", today=today)
    _expect(future["end"] == today, "fin future ramenée à aujourd'hui")

    invalid = ST.resolve_period("custom", "n'importe quoi", "aussi", today=today)
    _expect(invalid["preset"] == "30j" and "invalides" in invalid["note"], "dates invalides")

    previous = ST.previous_period(date(2026, 9, 28), today)
    _expect(
        (previous[0], previous[1]) == (date(2026, 8, 29), date(2026, 9, 27)),
        "période précédente de même durée",
    )
    _expect(
        ST.granularity_for(date(2026, 9, 28), today) == "jour",
        "agrégation journalière sur un mois",
    )
    _expect(
        ST.granularity_for(date(2026, 5, 1), today) == "semaine",
        "agrégation hebdomadaire sur six mois",
    )
    _expect(
        ST.granularity_for(date(2025, 11, 1), today) == "mois",
        "agrégation mensuelle sur un an",
    )

    _expect(
        ST.format_period(date(2026, 9, 28), today) == "28 sept. – 27 oct. 2026",
        "libellé de période",
    )
    _expect(
        ST.format_period(date(2026, 10, 1), date(2026, 10, 31)) == "1–31 oct. 2026",
        "libellé dans le même mois",
    )
    _expect(
        ST.format_period(date(2026, 12, 28), date(2027, 1, 5)) == "28 déc. 2026 – 5 janv. 2027",
        "libellé à cheval sur deux années",
    )
    _expect(
        ST._parse_date("2026-10-01") == paris_to_utc(datetime(2026, 10, 1)),
        "borne de début en heure de Paris",
    )
    _expect(
        ST._parse_date("2026-10-01", end_of_day=True)
        == paris_to_utc(datetime(2026, 10, 2)) - timedelta(microseconds=1),
        "borne de fin en heure de Paris",
    )


def test_series_totaux_et_filtres():
    state = _setup()
    app = state["app"]
    with app.app_context():
        filters = {
            "date_from": state["d1"].isoformat(),
            "date_to": state["d5"].isoformat(),
            "campus": "brest",
        }
        totals = ST.period_totals(filters)
        _expect(totals["revenue"] == 1900, f"recettes ({totals['revenue']})")
        _expect(totals["qty"] == 9, f"volume ({totals['qty']})")
        _expect(totals["transactions"] == 4, f"transactions ({totals['transactions']})")
        _expect(totals["students"] == 2, f"étudiants ({totals['students']})")
        _expect(totals["by_category"]["biere"] == {"qty": 6, "revenue": 1500}, "bière")

        series = ST.daily_series(filters, state["d1"], state["d5"])
        _expect(len(series["dates"]) == 11, "onze jours affichés")
        _expect(sum(series["revenue"]) == 1900, "somme de la série")
        _expect(series["revenue"][0] == 700 and series["revenue"][1] == 0, "jour vide à zéro")
        _expect(series["qty"][0] == 3, "volume du premier jour")
        _expect(series["transactions"][0] == 1, "transactions du premier jour")
        _expect(series["monthly"]["labels"], "mois présents")

        long_start = state["d1"] - timedelta(days=70)
        long_series = ST.daily_series(
            {**filters, "date_from": long_start.isoformat()}, long_start, state["d5"]
        )
        _expect(
            len(long_series["monthly"]["labels"]) >= 3,
            "série mensuelle sur plusieurs mois",
        )
        beer = next(item for item in long_series["monthly"]["series"] if item["type"] == "biere")
        # La période longue inclut aussi la vente de la période précédente.
        _expect(sum(beer["qty"]) == 7, "volume bière mensuel")

        sample = {
            "dates": [
                (date(2026, 10, 5) + timedelta(days=offset)).isoformat() for offset in range(10)
            ],
            "revenue": list(range(1, 11)),
            "qty": list(range(1, 11)),
            "transactions": [1] * 10,
        }
        weekly = ST.bucket_series(sample, date(2026, 10, 5), date(2026, 10, 14), "semaine")
        _expect(weekly["labels"] == ["5 oct.", "12 oct."], "semaines alignées sur lundi")
        _expect(weekly["revenue"] == [28, 27], "somme hebdomadaire")
        _expect(sum(weekly["transactions"]) == 10, "transactions hebdomadaires")
        monthly = ST.bucket_series(sample, date(2026, 10, 5), date(2026, 10, 14), "mois")
        _expect(monthly["labels"] == ["oct. 2026"], "mois regroupés")

        events = ST.period_totals({**filters, "events": "events"})
        _expect(events["revenue"] == 750 and events["transactions"] == 1, "soirées seulement")
        outside = ST.period_totals({**filters, "events": "hors_events"})
        _expect(outside["revenue"] == 1150 and outside["qty"] == 6, "hors soirée")

        beer_only = ST.period_totals({**filters, "category": "biere"})
        _expect(beer_only["revenue"] == 1500 and beer_only["qty"] == 6, "catégorie bière")

        paris = ST.period_totals({**filters, "campus": "paris"})
        _expect(paris["revenue"] == 400 and paris["students"] == 1, "campus Paris")

        promo = ST.period_totals({**filters, "promotion": "2027"})
        _expect(promo["revenue"] == 950 and promo["transactions"] == 2, "promotion 2027")

        team = ST.period_totals({**filters, "team_only": "team"})
        _expect(team["revenue"] == 1450 and team["transactions"] == 2, "équipe")

        # Les articles et étudiants filtrent de la même façon.
        articles = ST.articles_stats(filters, sort="nom")
        _expect([a["name"] for a in articles["rows"]] == ["Chips", "Pinte"], "tri par nom")
        page = ST.articles_stats(filters, sort="volume", page=1, per_page=1)
        _expect(page["total"] == 2 and page["pages"] == 2, "pagination articles")
        students = ST.students_stats(filters, sort="nom")
        _expect([s["name"] for s in students["rows"]] == ["Alice", "Bob"], "tri étudiants")
        _expect(students["rows"][0]["initials"] == "AL", "initiales")
        _expect(students["team_total"] == 1, "membres d'équipe")
        _expect(students["rows"][1]["spent"] == 1100, "dépense de Bob")
        _expect(students["rows"][0]["average"] == 300, "panier moyen d'Alice")

        _expect(ST.percent_delta(100, 50)["tone"] == "up", "delta positif")
        _expect(ST.percent_delta(50, 100)["tone"] == "down", "delta négatif")
        _expect(ST.percent_delta(0, 0)["tone"] == "none", "delta sans comparaison")


def test_tout_historique():
    state = _setup()
    app = state["app"]
    with app.app_context():
        period = ST.resolve_period("tout", campus="brest", today=state["today"])
        _expect(period["start"] == state["old"], "début = première transaction")
        _expect(period["end"] == state["today"], "fin = aujourd'hui")
        _expect(period["preset"] == "tout", "preset conservé")
        paris = ST.resolve_period("tout", campus="paris", today=state["today"])
        _expect(paris["start"] == state["d3"], "première transaction du campus")
        all_campuses = ST.resolve_period("tout", today=state["today"])
        _expect(all_campuses["start"] == state["old"], "première transaction globale")

        date_range = {
            "campus": "brest",
            "date_from": state["old"].isoformat(),
            "date_to": state["today"].isoformat(),
        }
        limited = ST.period_totals(date_range)
        _expect(limited["revenue"] == 2250, "plafond d'accès appliqué")
        full = ST.period_totals({**date_range, "all_time": "1"})
        _expect(full["revenue"] == 2550, "historique complet")
        _expect(full["qty"] == 12 and full["transactions"] == 7, "volumes complets")
        _expect(full["students"] == 2, "étudiants complets")

    client = _client()
    html = _page(client, preset="tout", date_from="", date_to="", sans_blagues="0")
    _expect('value="tout"' in html, "période proposée")
    _expect("25,50 €" in html, "recettes all-time")
    data = _chart_data(html)
    _expect(data["granularity"] == "mois", "agrégation mensuelle all-time")
    _expect(len(data["labels"]) >= 16, "mois depuis le début")
    _expect(sum(data["revenue"]) == 2550, "série all-time complète")
    _expect(len(data["monthly"]["labels"]) >= 16, "répartition mensuelle all-time")

    standard = _chart_data(_page(client, preset="30j"))
    _expect(sum(standard["revenue"]) == 2150, "période standard toujours plafonnée")


def test_masquer_blagues():
    state = _setup()
    client = _client()

    shown = _page(client, preset="tout", date_from="", date_to="", sans_blagues="0")
    _expect("25,50 €" in shown, "recettes avec blagues")
    _expect("Sans blagues" not in shown, "puce absente quand les blagues sont visibles")
    _expect(
        re.search(r'id="stats-sans-blagues"[^>]*checked', shown) is None,
        "case décochée",
    )
    _expect("sans_blagues=0" in shown, "désactivation conservée dans les liens")
    shown_data = _chart_data(shown)
    _expect(sum(shown_data["revenue"]) == 2550, "série avec blagues")
    _expect(sum(shown_data["transactions"]) == 7, "transactions avec blagues")

    # Sans paramètre, le filtre est actif par défaut.
    hidden = _page(client, preset="tout", date_from="", date_to="")
    _expect("24,50 €" in hidden, "recettes sans blagues par défaut")
    _expect("Sans blagues" in hidden, "chip sans blagues")
    _expect(
        re.search(r'id="stats-sans-blagues"[^>]*checked', hidden) is not None,
        "case cochée par défaut",
    )
    hidden_data = _chart_data(hidden)
    _expect(sum(hidden_data["revenue"]) == 2450, "série sans blagues")
    _expect(sum(hidden_data["transactions"]) == 6, "transactions sans blagues")

    # Le formulaire envoie 0 (valeur cachée) puis 1 (case cochée) : la case
    # doit rester prioritaire.
    both = client.get(
        "/equipe/statistiques",
        query_string="preset=tout&campus=brest&sans_blagues=0&sans_blagues=1",
    ).get_data(as_text=True)
    _expect("24,50 €" in both, "case cochée prioritaire sur la valeur cachée")

    start = state["today"] - timedelta(days=110)
    period = {
        "date_from": start.isoformat(),
        "date_to": state["today"].isoformat(),
    }
    include = _chart_data(_page(client, preset="custom", sans_blagues="0", **period))
    exclude = _chart_data(_page(client, preset="custom", **period))
    _expect(sum(include["revenue"]) == 2250, "période avec blague")
    _expect(sum(exclude["revenue"]) == 2150, "période sans blague par défaut")

    with state["app"].app_context():
        filters = {
            "campus": "brest",
            "date_from": state["old"].isoformat(),
            "date_to": state["today"].isoformat(),
            "all_time": "1",
        }
        full_categories = ST.category_totals(filters)
        _expect(
            full_categories["by_category"].get("blague", {}).get("revenue") == 100,
            "catégorie blague incluse",
        )
        hidden_categories = ST.category_totals({**filters, "sans_blagues": "1"})
        _expect("blague" not in hidden_categories["by_category"], "catégorie blague masquée")
        conflict = ST.period_totals({**filters, "category": "blague", "sans_blagues": "1"})
        _expect(
            conflict["revenue"] == 0
            and conflict["transactions"] == 0
            and conflict["students"] == 0,
            "catégorie blague masquée sans incohérence",
        )
        full_articles = ST.articles_stats(filters, per_page=0)
        _expect(
            any(article["type"] == "blague" for article in full_articles["rows"]), "article inclus"
        )
        hidden_articles = ST.articles_stats({**filters, "sans_blagues": "1"}, per_page=0)
        _expect(
            not any(article["type"] == "blague" for article in hidden_articles["rows"]),
            "article masqué",
        )

    export_args = {"preset": "tout", "campus": "brest", "vue": "categories"}
    full_csv = client.get(
        "/equipe/statistiques/export.csv",
        query_string={**export_args, "sans_blagues": "0"},
    )
    _expect("Blague" in full_csv.get_data(as_text=True), "export avec blague")
    default_csv = client.get("/equipe/statistiques/export.csv", query_string=export_args)
    _expect("Blague" not in default_csv.get_data(as_text=True), "export sans blague par défaut")
    hidden_csv = client.get(
        "/equipe/statistiques/export.csv",
        query_string={**export_args, "sans_blagues": "1"},
    )
    _expect("Blague" not in hidden_csv.get_data(as_text=True), "export sans blague")

    article_export = {
        "preset": "tout",
        "campus": "brest",
        "vue": "articles",
        "sans_blagues": "1",
    }
    article_csv = client.get("/equipe/statistiques/export.csv", query_string=article_export)
    _expect("Blague" not in article_csv.get_data(as_text=True), "export articles sans blague")


def test_page_kpis_et_graphiques():
    client = _client()
    html = _page(client)
    _expect("Statistiques" in html and "Recettes" in html, "titre et KPI")
    _expect("Panier moyen" in html and "acheteurs uniques" in html, "KPI complets")
    _expect("19,00 €" in html, "recettes affichées")
    _expect("4 transactions" in html, "nombre de transactions")
    _expect("vs période préc." in html, "comparaison KPI")
    _expect('id="stats-data"' in html, "données JSON")
    _expect("data-stats-filter" in html, "formulaire de filtres")
    _expect("data-auto-submit" in html, "application automatique des filtres")
    _expect(
        re.search(r'id="stats-sans-blagues"[^>]*checked', html) is not None,
        "blagues masquées par défaut",
    )
    _expect("chart-trend" in html, "graphique de tendance")
    _expect('aria-label="Vues des statistiques"' in html, "onglets accessibles")
    _expect(html.count("js/statistiques.js") == 1, "script des statistiques")
    _expect("new Chart(" not in html, "aucun graphique inline")

    data = _chart_data(html)
    _expect(len(data["dates"]) == 11, "jours dans le JSON")
    _expect(data["revenue"][0] == 700 and data["revenue"][1] == 0, "série complète")
    _expect(data["previous"] is None, "pas de comparaison par défaut")
    _expect(any(event["name"] == "Soirée test" for event in data["events"]), "marqueur de soirée")

    long_html = _page(client, preset="6mois")
    _expect('id="chart-monthly"' in long_html, "graphique mensuel sur longue période")
    _expect("Modes de consommation par mois" in long_html, "titre du graphique mensuel")
    long_data = _chart_data(long_html)
    _expect(long_data["granularity"] == "semaine", "agrégation hebdomadaire")
    _expect(len(long_data["labels"]) < 30, "tendance hebdomadaire compacte")
    _expect("Semaine par semaine" in long_html, "libellé de granularité")

    year_data = _chart_data(_page(client, preset="12mois"))
    _expect(year_data["granularity"] == "mois", "agrégation mensuelle sur un an")

    compare_html = _page(client, comparer="1")
    _expect("Période de comparaison" in compare_html, "libellé de comparaison")
    compare_data = _chart_data(compare_html)
    _expect(compare_data["previous"] is not None, "série précédente")
    _expect(sum(compare_data["previous"]["revenue"]) == 250, "recettes précédentes")


def test_onglets_articles_et_etudiants():
    client = _client()

    articles = _page(client, vue="articles")
    _expect("Classement des articles" in articles, "onglet articles")
    _expect('aria-sort="descending"' in articles, "tri accessible")
    _expect("Pinte" in articles and "Chips" in articles, "articles listés")
    _expect("Bière" in articles and "Snacks" in articles, "catégories lisibles")
    _expect('id="stats-data"' not in articles, "pas de graphique sur l'onglet articles")
    _expect("js/statistiques.js" in articles, "script des filtres présent")

    by_name = _page(client, vue="articles", tri="nom")
    _expect(by_name.index("Chips") < by_name.index("Pinte"), "tri alphabétique")
    by_revenue = _page(client, vue="articles", tri="revenu")
    _expect(by_revenue.index("Pinte") < by_revenue.index("Chips"), "tri par recettes")

    search = _page(client, vue="articles", aq="chip")
    _expect("Chips" in search and "Pinte" not in search, "recherche d'article")

    students = _page(client, vue="etudiants")
    _expect("Consommation par étudiant" in students, "onglet étudiants")
    _expect("Alice" in students and "Bob" in students, "étudiants du campus")
    _expect("Charlie" not in students, "campus de session par défaut")
    _expect("11,00 €" in students, "dépense de Bob")
    _expect("stats-initials" in students, "pastilles d'initiales")
    _expect("js/statistiques.js" in students, "script des filtres présent")

    all_campuses = _page(client, vue="etudiants", campus="tous")
    _expect("Charlie" in all_campuses, "les deux campus")

    by_name_students = _page(client, vue="etudiants", tri_etudiants="nom")
    _expect(
        by_name_students.index("Alice") < by_name_students.index("Bob"),
        "tri des étudiants par nom",
    )


def test_filtres_periodes_et_notes():
    client = _client()

    category = _page(client, category="biere")
    _expect("stats-chip" in category and "Retirer le filtre Bière" in category, "chip catégorie")
    _expect("category=biere" in category, "lien de catégorie conservé")

    events = _page(client, events="events")
    data = _chart_data(events)
    _expect(sum(data["revenue"]) == 750, "filtre soirées")

    inverted = _page(
        client,
        date_from=_setup()["d5"].isoformat(),
        date_to=_setup()["d1"].isoformat(),
    )
    _expect("inversées" in inverted, "note de dates inversées")

    invalid = _page(client, date_from="pas-une-date", date_to="non-plus")
    _expect("Dates invalides" in invalid, "note de dates invalides")

    event = _page(client, preset="evenement", date_from="", date_to="")
    _expect("Soirée test" in event, "événement dans l'en-tête")
    event_data = _chart_data(event)
    _expect(event_data["dates"] == [_setup()["d4"].isoformat()], "période du dernier événement")
    _expect(event_data["revenue"] == [750], "ventes du dernier événement")

    ignored = _page(
        client,
        category="inconnue",
        promotion="abc",
        team_only="autre",
        events="autre",
        preset="inconnu",
    )
    _expect("Statistiques" in ignored, "filtres invalides ignorés")


def test_exports_csv():
    client = _client()
    params = _params(vue="articles")
    res = client.get("/equipe/statistiques/export.csv", query_string=params)
    _expect(res.status_code == 200, f"export articles ({res.status_code})")
    _expect(res.mimetype == "text/csv", "type CSV")
    _expect("statistiques_articles_brest" in res.headers["Content-Disposition"], "nom du fichier")
    _expect(res.data.startswith(b"\xef\xbb\xbf"), "BOM UTF-8")
    text = res.get_data(as_text=True)
    _expect("Rang;Article;Catégorie" in text, "en-tête articles")
    _expect("Pinte" in text and "Chips" in text, "lignes articles")

    res = client.get("/equipe/statistiques/export.csv", query_string=_params(vue="etudiants"))
    text = res.get_data(as_text=True)
    _expect("Étudiant;Promotion;Profil" in text, "en-tête étudiants")
    _expect("Alice" in text and "Bob" in text, "lignes étudiants")

    res = client.get("/equipe/statistiques/export.csv", query_string=_params())
    text = res.get_data(as_text=True)
    _expect("Catégorie;Volume;Recettes" in text, "en-tête catégories")
    _expect("Bière" in text and "TOTAL" in text, "lignes catégories")

    res = client.get("/equipe/statistiques/export.csv", query_string=_params(vue="inconnue"))
    _expect(res.status_code == 200, "vue inconnue -> export catégories")
    _expect("Catégorie;Volume;Recettes" in res.get_data(as_text=True), "export par défaut")

    res = client.get("/equipe/statistiques/export.csv", query_string=_params(vue="serie"))
    text = res.get_data(as_text=True)
    _expect("Début;Période;Recettes (EUR);Articles;Transactions" in text, "en-tête série")
    _expect(_setup()["d1"].isoformat() in text, "date de la série")
    _expect("7.00" in text, "recettes de la série")


def test_acces_et_erreurs():
    app = _setup()["app"]
    anonymous = app.test_client()
    for path in ("/equipe/statistiques", "/equipe/statistiques/export.csv"):
        res = anonymous.get(path)
        _expect(res.status_code == 302, f"{path} redirige vers la connexion")
        _expect("/connexion" in res.headers["Location"], "redirection de connexion")

    client = _client()
    for query in (
        {"page": "abc", "per_page": "abc", "alimit": "abc"},
        {"date_from": "2026-01-01", "date_to": "2026-12-31", "q": "a"},
        {"promotion": "9" * 5000},
        {"category": "x" * 5000},
        {"q": "x" * 5000, "aq": "x" * 5000},
        {"preset": "custom", "date_from": "0001-01-01", "date_to": "0001-01-01"},
        {"preset": "custom", "date_from": "9999-12-31", "date_to": "9999-12-31"},
    ):
        res = client.get("/equipe/statistiques", query_string=query)
        _expect(res.status_code == 200, f"paramètres invalides ({res.status_code})")

    state = _setup()
    empty = _page(
        client,
        date_from=(state["today"] - timedelta(days=350)).isoformat(),
        date_to=(state["today"] - timedelta(days=340)).isoformat(),
    )
    _expect("Aucune vente sur cette période" in empty, "état vide")


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
