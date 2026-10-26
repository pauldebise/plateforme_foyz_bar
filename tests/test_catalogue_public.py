"""Catalogue public des prix : articles classés par tendance (ventes récentes)
dans chaque catégorie, puis par nom pour ceux sans vente récente ; carte des
10 articles les plus vendus en tête ; barre de recherche.

Exécutable sans pytest : python -m tests.test_catalogue_public
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import mock

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

CATALOGUE_JS = ROOT / "app" / "static" / "js" / "catalogue.js"
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
        specs += [(f"Vin {i:02d}", "vin", "brest") for i in range(1, 9)]
        for name, kind, campus in specs:
            db.session.add(
                Article(
                    name=name,
                    article_type=kind,
                    campus=campus,
                    price_std=100,
                    price_team=100,
                    alcohol_degree=6.5 if name == "Zzz biere" else None,
                )
            )
        # Très vendus mais jamais affichés au public : privé, inactif, type masqué.
        for name, kind, private in (
            ("Prive hit", "snack", True),
            ("Inactif hit", "snack", False),
            ("Blague hit", "blague", False),
        ):
            db.session.add(
                Article(
                    name=name,
                    article_type=kind,
                    campus="brest",
                    price_std=100,
                    price_team=100,
                    is_private=private,
                )
            )
        db.session.commit()
    for i in range(1, 9):
        _sell(app, f"Vin {i:02d}", 31 - i)  # 30 … 23 : tête du classement
    for name in ("Prive hit", "Inactif hit", "Blague hit"):
        _sell(app, name, 100)
    with app.app_context():
        inactive = db.session.scalars(select(Article).where(Article.name == "Inactif hit")).one()
        inactive.active = False
        db.session.commit()
    _sell(app, "Charlie snack", 5)
    _sell(app, "Bravo snack", 2)
    _sell(app, "Zzz biere", 3)
    _sell(app, "Bbb paris", 4, campus="paris")
    _state.update(app=app, client=app.test_client())
    return _state


def _cards(campus="brest"):
    """Page publique : {identifiant de carte: noms d'articles, dans l'ordre}.
    La carte « plus vendus » a l'identifiant « catalogue-top »."""
    html = _setup()["client"].get(f"/catalogue?campus={campus}").get_data(as_text=True)
    cards = {}
    for part in re.split(r'<div class="card mb-4[^"]*" id="', html)[1:]:
        names = re.findall(r'<td class="catalogue-name">([^<]+?)\s*(?:<span|</td>)', part)
        cards[part[: part.index('"')]] = names
    return cards


def _order(campus="brest"):
    """Noms d'articles des catégories, dans l'ordre d'affichage (hors carte
    « plus vendus »)."""
    return [n for cid, names in _cards(campus).items() if cid != "catalogue-top" for n in names]


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
        html.index('id="cat-biere"') < html.index('id="cat-vin"') < html.index('id="cat-snack"'),
        "les cartes de catégorie gardent leur ordre fixe (bière, vin, snacks)",
    )
    _expect("Prix standards" not in html, "titre de page simplifié en « Prix »")
    _expect(
        "les plus vendus ces derniers temps" not in html,
        "phrase d'explication du classement retirée",
    )


def test_degre_alcool_dans_le_catalogue():
    html = _setup()["client"].get("/catalogue?campus=brest").get_data(as_text=True)
    beer = html[html.index('id="cat-biere"') : html.index('id="cat-vin"')]
    snack = html[html.index('id="cat-snack"') :]
    top = html[html.index('id="catalogue-top"') : html.index('id="cat-biere"')]
    _expect("<th>Degré</th>" in beer, "colonne Degré présente pour les bières renseignées")
    _expect("6.5°" in beer, "degré de l'article affiché")
    _expect("6.5°" in top, "degré affiché dans la carte des plus vendus")
    aaa = beer[beer.index("Aaa biere") :].split("</tr>", 1)[0]
    _expect("—" in aaa, "article sans degré : cellule vide")
    _expect("<th>Degré</th>" not in snack, "colonne absente quand aucun degré n'est renseigné")


def test_libelles_carte_catalogue_accueil():
    s = _setup()
    accueil = s["client"].get("/").get_data(as_text=True)
    _expect("Le bar en un clin d'œil" not in accueil, "ancien titre de carte retiré")
    _expect(
        '<i class="bi bi-cup-straw"></i> Catalogue' in accueil,
        "carte renommée en « Catalogue »",
    )
    _expect(
        "Consultez le catalogue complet et les prix de chaque campus :" in accueil,
        "phrase du catalogue simplifiée",
    )
    _expect("<strong>prix standards</strong>" not in accueil, "prix sans gras")
    _expect("monnaie virtuelle" not in accueil, "commentaire sur la monnaie virtuelle retiré")


def test_carte_des_plus_vendus():
    html = _setup()["client"].get("/catalogue?campus=brest").get_data(as_text=True)
    cards = _cards()
    expected = [f"Vin {i:02d}" for i in range(1, 9)] + ["Charlie snack", "Zzz biere"]
    _expect(cards.get("catalogue-top") == expected, f"top 10 : {cards.get('catalogue-top')}")
    _expect("Bravo snack" not in cards["catalogue-top"], "le 11e n'est pas dans la carte")
    _expect(
        html.index('id="catalogue-top"') < html.index('id="cat-biere"'),
        "la carte est au-dessus des catégories",
    )
    _expect("Les 10 plus vendus" in html, "titre de la carte")
    _expect("Charlie snack" in cards["cat-snack"], "les catégories listent toujours tout")


def test_top_sans_articles_non_publics():
    top = _cards()["catalogue-top"]
    page = " ".join(n for names in _cards().values() for n in names)
    for hidden in ("Prive hit", "Inactif hit", "Blague hit"):
        _expect(hidden not in top and hidden not in page, f"{hidden} jamais affiché au public")


def test_top_limite_aux_articles_vendus():
    cards = _cards("paris")
    _expect(cards.get("catalogue-top") == ["Bbb paris"], f"Paris : {cards.get('catalogue-top')}")
    s = _setup()
    with mock.patch("app.routes.public.top_article_ids", return_value=[]):
        html = s["client"].get("/catalogue?campus=brest").get_data(as_text=True)
    _expect('id="catalogue-top"' not in html, "aucune vente : pas de carte")
    _expect('id="cat-snack"' in html, "les catégories restent affichées")


def test_barre_de_recherche():
    s = _setup()
    html = s["client"].get("/catalogue?campus=brest").get_data(as_text=True)
    _expect('id="catalogue-search"' in html, "champ de recherche présent")
    _expect(re.search(r'id="catalogue-search-box"[^>]*\shidden', html), "masqué sans JavaScript")
    _expect(re.search(r'<script src="/static/js/catalogue\.js[^"]*"', html), "script chargé")
    _expect('data-search="Charlie snack Snacks"' in html, "texte de recherche des lignes")
    _expect(
        s["client"].get("/static/js/catalogue.js").status_code == 200, "script servi (CSP : site)"
    )
    js = CATALOGUE_JS.read_text(encoding="utf-8")
    node = shutil.which("node")
    if not node:
        print("  (node absent : exécution de fold() ignorée)")
        return
    script = js[js.index("const FOLD_EXTRA") : js.index("const top =")] + (
        "const cas = [['Bière','biere'],['BIÈRE','biere'],['Crêpe','crepe'],['Cœur','coeur']];"
        "for (const [a, b] of cas) { if (fold(a) !== b) { console.log('KO ' + a); process.exit(1); } }"
        "console.log('ok');"
    )
    res = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=30)
    _expect(res.returncode == 0 and "ok" in res.stdout, f"fold() : {res.stdout}{res.stderr}")


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
    top = _cards()["catalogue-top"]
    _expect("Alpha snack" in top and "Zzz biere" not in top, f"la carte suit aussi : {top}")


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
