"""Catalogue public des prix : articles classés par tendance (ventes récentes)
dans chaque catégorie, puis par nom pour ceux sans vente récente.

Exécutable sans pytest : python -m tests.test_catalogue_public
"""

import os
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="foyz_catalogue_"))
os.environ["DATABASE_URL"] = (
    os.environ.get("FOYZ_TEST_DATABASE_URL") or f"sqlite:///{_TMP / 'app.db'}"
)
os.environ["UPLOAD_DIR"] = str(_TMP / "uploads")
os.environ["SECRET_KEY"] = "test-secret-key-0123456789abcdef0123456789abcdef"
os.environ["ADMIN_PASSWORD"] = "mot-de-passe-admin"
os.environ.pop("FLASK_ENV", None)

from sqlalchemy import select  # noqa: E402

from app import create_app  # noqa: E402
from app.extensions import db  # noqa: E402
from app.models import Article, Transaction  # noqa: E402
from app.services import transactions as T  # noqa: E402

_state = {}


def _expect(cond, label):
    if not cond:
        raise AssertionError(f"échec : {label}")


def _sell(app, name, qty, campus="brest"):
    with app.app_context():
        article = db.session.scalars(
            select(Article).where(Article.name == name, Article.campus == campus)
        ).one()
        T.create_purchase(
            operator_label="t",
            campus=campus,
            items=[{"article_id": article.id, "quantity": qty}],
            direct=True,
            payment_method="cb",
        )


def _setup():
    if _state:
        return _state
    app = create_app()
    with app.app_context():
        specs = [
            ("Alpha snack", "snack", "brest"),
            ("Bravo snack", "snack", "brest"),
            ("Charlie snack", "snack", "brest"),
            ("Delta snack", "snack", "brest"),
            ("Aaa biere", "biere", "brest"),
            ("Zzz biere", "biere", "brest"),
            ("Aaa paris", "snack", "paris"),
            ("Bbb paris", "snack", "paris"),
        ]
        for name, kind, campus in specs:
            db.session.add(
                Article(name=name, article_type=kind, campus=campus, price_std=100, price_team=100)
            )
        db.session.commit()
    _sell(app, "Charlie snack", 5)
    _sell(app, "Bravo snack", 2)
    _sell(app, "Zzz biere", 3)
    _sell(app, "Bbb paris", 4, campus="paris")
    _state.update(app=app, client=app.test_client())
    return _state


def _order(campus="brest"):
    """Noms d'articles dans l'ordre d'affichage de la page publique."""
    html = _setup()["client"].get(f"/catalogue?campus={campus}").get_data(as_text=True)
    return re.findall(r"<td>([^<]+)</td>\s*<td class=\"text-muted\">", html)


def test_tendance_dans_chaque_categorie():
    order = _order()
    snacks = [n for n in order if n.endswith("snack")]
    _expect(
        snacks == ["Charlie snack", "Bravo snack", "Alpha snack", "Delta snack"],
        f"snacks par tendance puis par nom : {snacks}",
    )
    bieres = [n for n in order if n.endswith("biere")]
    _expect(bieres == ["Zzz biere", "Aaa biere"], f"bières par tendance : {bieres}")


def test_categories_gardent_leur_ordre():
    html = _setup()["client"].get("/catalogue?campus=brest").get_data(as_text=True)
    _expect(
        html.index("Zzz biere") < html.index("Charlie snack"),
        "les cartes de catégorie gardent leur ordre fixe (bière avant snacks)",
    )
    _expect("les plus vendus" in html, "phrase d'explication du classement")


def test_page_publique_sans_connexion():
    res = _setup()["client"].get("/catalogue?campus=brest")
    _expect(res.status_code == 200, f"page publique : {res.status_code}")


def test_campus_independants():
    _expect(_order("paris") == ["Bbb paris", "Aaa paris"], f"Paris : {_order('paris')}")
    _expect(not any(n.endswith("paris") for n in _order("brest")), "pas de mélange des campus")


def test_le_classement_suit_les_nouvelles_ventes():
    s = _setup()
    _sell(s["app"], "Alpha snack", 10)
    snacks = [n for n in _order() if n.endswith("snack")]
    _expect(snacks[0] == "Alpha snack", f"Alpha passe en tête après 10 ventes : {snacks}")


def test_vente_annulee_ne_compte_pas():
    s = _setup()
    with s["app"].app_context():
        article = db.session.scalars(select(Article).where(Article.name == "Delta snack")).one()
        T.create_purchase(
            operator_label="t",
            campus="brest",
            items=[{"article_id": article.id, "quantity": 50}],
            direct=True,
            payment_method="cb",
        )
        sale = db.session.scalars(select(Transaction).order_by(Transaction.id.desc())).first()
        first = next(n for n in _order() if n.endswith("snack"))
        _expect(first == "Delta snack", f"Delta en tête avec 50 ventes : {first}")
        T.cancel_transaction(sale, None, verified=True)
    snacks = [n for n in _order() if n.endswith("snack")]
    _expect(snacks[0] == "Alpha snack" and snacks[-1] == "Delta snack", f"annulée : {snacks}")


def main():
    # Ordre de définition (pas alphabétique) : les derniers tests modifient les
    # ventes, donc le classement, que les premiers supposent à l'état initial.
    tests = [
        (name, fn) for name, fn in globals().items() if name.startswith("test_") and callable(fn)
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
