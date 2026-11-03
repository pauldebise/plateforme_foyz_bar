"""Paniers multi-soirées : statistiques et récapitulatifs cohérents.

Un panier contenant des articles de plusieurs soirées n'a pas de
``Transaction.event_id``. Il doit néanmoins compter dans le filtre « Soirées
uniquement » et ses acheteurs doivent être répartis entre les récaps au prorata
de la part de chaque soirée, sans double comptage.
"""

import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="foyz_multi_evenements_"))
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
from app.models import Article, Event, User  # noqa: E402
from app.services import stats as ST  # noqa: E402
from app.services import transactions as T  # noqa: E402
from app.services.event_recap import build_recap  # noqa: E402
from app.utils import PARIS_TZ, utcnow  # noqa: E402

_events = 0


def _expect(cond, label):
    if not cond:
        raise AssertionError(f"échec : {label}")


def _event(name):
    global _events
    _events += 1
    event = Event(
        name=name,
        campus="brest",
        starts_at=utcnow() - timedelta(hours=1),
        ends_at=utcnow() + timedelta(hours=3),
        token=f"multi-evenement-{_events}",
    )
    db.session.add(event)
    db.session.flush()
    return event


def _article(name, price, *, event_id=None):
    article = Article(
        name=name,
        article_type="biere",
        campus="brest",
        price_std=price,
        price_team=price,
        event_id=event_id,
        active=True,
    )
    db.session.add(article)
    db.session.flush()
    return article


def _user(name, promotion, balance=10_000):
    user = User(
        name=name,
        username=f"multi.{promotion}.{name}".lower().replace(" ", "."),
        promotion=promotion,
        password_hash=generate_password_hash("secret123"),
    )
    db.session.add(user)
    db.session.flush()
    user.wallet("brest").balance = balance
    return user


def test_panier_multi_evenements_stats_et_recaps():
    """Le panier mixte compte en soirée et ses acheteurs sont proratisés."""
    app = create_app()
    today = datetime.now(PARIS_TZ).date()
    with app.app_context():
        first = _event("Multi stats un")
        second = _event("Multi stats deux")
        first_article = _article("Article multi un", 400, event_id=first.id)
        second_article = _article("Article multi deux", 700, event_id=second.id)
        standard = _article("Article standard multi", 300)
        alice = _user("Alice Multi", 2027)
        bob = _user("Bob Multi", 2026)
        db.session.commit()

        transaction = T.create_purchase(
            operator_label="Caisse multi",
            campus="brest",
            items=[
                {"article_id": first_article.id, "quantity": 2},
                {"article_id": second_article.id, "quantity": 1},
                {"article_id": standard.id, "quantity": 1},
            ],
            contributor_ids=[alice.id, bob.id],
            deposit_glasses=2,
            team_sale=True,
        )
        T.create_purchase(
            operator_label="Caisse standard",
            campus="brest",
            items=[{"article_id": standard.id, "quantity": 1}],
            contributor_ids=[alice.id],
        )
        _expect(transaction.event_id is None, "panier multi-soirées non rattaché")
        _expect(transaction.total == 2000, "total du panier avec consignes")
        first_id, second_id = first.id, second.id

    filters = {
        "campus": "brest",
        "date_from": (today - timedelta(days=1)).isoformat(),
        "date_to": (today + timedelta(days=1)).isoformat(),
    }
    with app.app_context():
        all_totals = ST.period_totals(filters)
        _expect(all_totals["revenue"] == 2100, "toutes les ventes de la période")
        _expect(all_totals["transactions"] == 2, "deux transactions au total")

        event_totals = ST.period_totals({**filters, "events": "events"})
        _expect(
            event_totals["revenue"] == 1800,
            f"panier multi compté en entier en soirée ({event_totals['revenue']})",
        )
        _expect(event_totals["qty"] == 4, "les quatre articles du panier multi comptent")
        _expect(event_totals["transactions"] == 1, "une seule vente de soirée")
        _expect(event_totals["students"] == 2, "les deux acheteurs du panier multi")

        outside_totals = ST.period_totals({**filters, "events": "hors_events"})
        _expect(
            outside_totals["revenue"] == 300,
            f"seule la vente standard reste hors soirée ({outside_totals['revenue']})",
        )
        _expect(outside_totals["qty"] == 1, "un article standard hors soirée")
        _expect(outside_totals["transactions"] == 1, "une vente standard hors soirée")

        # L'onglet étudiants a son propre filtre : il doit suivre la même règle.
        event_students = ST.students_stats({**filters, "events": "events"})
        _expect(event_students["total"] == 2, "deux étudiants dans les ventes de soirée")
        outside_students = ST.students_stats({**filters, "events": "hors_events"})
        _expect(outside_students["total"] == 1, "un étudiant dans les ventes hors soirée")

        first_recap = build_recap(db.session.get(Event, first_id))
        second_recap = build_recap(db.session.get(Event, second_id))

    for recap, revenue, spent in (
        (first_recap, 800, 400),
        (second_recap, 700, 350),
    ):
        _expect(recap["summary"]["revenue"] == revenue, "recette de la soirée")
        _expect(recap["summary"]["sales_count"] == 1, "une vente rattachée")
        _expect(recap["buyers"]["unique"] == 2, "deux acheteurs identifiés")
        _expect(
            sorted(row["spent"] for row in recap["buyers"]["top"]) == [spent, spent],
            f"contribution proratisée par acheteur ({recap['buyers']['top']})",
        )
        _expect(
            sum(row["spent"] for row in recap["buyers"]["top"]) == revenue,
            "la somme des acheteurs correspond à la recette de la soirée",
        )
        _expect(
            all(row["nb"] == 1 for row in recap["buyers"]["top"]),
            "une vente comptée par acheteur",
        )
        _expect(
            sorted(row["spent"] for row in recap["buyers"]["promotions"]) == [spent, spent],
            "montants proratisés par promotion",
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
