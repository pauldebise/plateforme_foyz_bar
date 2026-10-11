"""Tests des filtres et tris de l'onglet Articles (/admin/articles).

Exécutable sans pytest : python -m tests.test_articles_filtres

Couvre : recherche, filtres type / volume / prix, tri par nom, prix, volume et
tendances (ventes récentes, même classement que la caisse), combinaison des
critères, isolation par campus, paramètres invalides ignorés.
"""

import os
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="foyz_articles_filtres_"))
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
from app.models import Article, User  # noqa: E402
from app.services import transactions as T  # noqa: E402

ADMIN_PASSWORD = "mot-de-passe-admin"
_client = None


def _expect(cond, label):
    if not cond:
        raise AssertionError(f"échec : {label}")


def _article(name, kind, volume, price, campus="brest"):
    article = Article(
        name=name,
        article_type=kind,
        volume_cl=volume,
        campus=campus,
        price_std=price,
        price_team=price // 2,
        active=True,
    )
    db.session.add(article)
    db.session.flush()
    return article


def _setup():
    global _client
    if _client is not None:
        return _client
    app = create_app()
    with app.app_context():
        a = _article("Zart A", "biere", 25, 300)
        b = _article("Zart B", "biere", 50, 500)
        _article("Zart C", "cidre", 25, 300)
        d = _article("Zart D", "snack", None, 100)
        _article("Zart E", "vin", 75, 900)
        _article("Zart P", "biere", 33, 400, campus="paris")
        buyer = User(
            name="Zart Acheteur",
            username="zart.acheteur",
            password_hash=generate_password_hash("secret123"),
        )
        db.session.add(buyer)
        db.session.flush()
        buyer.wallet("brest").balance = 1_000_000
        db.session.commit()
        for article, qty in ((a, 2), (b, 7), (d, 3)):
            T.create_purchase(
                operator_label="test",
                campus="brest",
                items=[{"article_id": article.id, "quantity": qty}],
                contributor_ids=[buyer.id],
            )
    client = app.test_client()
    html = client.get("/connexion").get_data(as_text=True)
    token = re.search(r'name="_csrf" value="([^"]+)"', html).group(1)
    res = client.post(
        "/connexion",
        data={"username": "admin", "password": ADMIN_PASSWORD, "campus": "brest", "_csrf": token},
    )
    _expect(res.status_code == 302, "connexion administrateur")
    _client = client
    return client


def _page(query):
    res = _setup().get(f"/admin/articles?campus=brest&q=Zart&{query}")
    _expect(res.status_code == 200, f"page servie pour « {query} » ({res.status_code})")
    return res.get_data(as_text=True)


def _names(query):
    return [m.split()[-1] for m in re.findall(r"<td>(Zart [A-E])", _page(query))]


def test_sans_filtre_et_sans_fuite_entre_campus():
    _expect(_names("") == list("ABCDE"), f"ordre par nom ({_names('')})")
    _expect("Zart P" not in _page(""), "article de Paris absent du catalogue de Brest")
    paris = _setup().get("/admin/articles?q=Zart&campus=paris").get_data(as_text=True)
    _expect("Zart P" in paris and "Zart A" not in paris, "catalogue de Paris isolé")


def test_filtres_type_volume_prix():
    _expect(_names("type=biere") == list("AB"), "type bière")
    _expect(_names("type=cidre") == ["C"], "type cidre")
    _expect(_names("volume=25") == list("AC"), "volume 25 cl")
    _expect(_names("volume=aucun") == ["D"], "sans volume")
    _expect(_names("prix_min=3") == list("ABCE"), "prix min 3 €")
    _expect(_names("prix_max=3") == list("ACD"), "prix max 3 €")
    _expect(_names("prix_min=3&prix_max=5") == list("ABC"), "prix entre 3 et 5 €")
    _expect(_names("prix_min=2,5&prix_max=3,00") == list("AC"), "virgule décimale acceptée")


def test_tris_nom_prix_volume():
    _expect(_names("tri=prix_asc") == list("DACBE"), "prix croissant")
    _expect(_names("tri=prix_desc") == list("EBACD"), "prix décroissant")
    _expect(_names("tri=volume_asc") == list("ACBED"), "volume croissant, sans volume en dernier")
    _expect(
        _names("tri=volume_desc") == list("EBACD"), "volume décroissant, sans volume en dernier"
    )


def test_tri_tendances():
    _expect(_names("tri=tendance_desc") == list("BDACE"), "plus vendus d'abord")
    _expect(_names("tri=tendance_asc") == list("CEADB"), "moins vendus d'abord")
    page = _page("tri=tendance_desc")
    _expect("<strong>7</strong>" in page and "<strong>3</strong>" in page, "ventes affichées")


def test_combinaison_des_criteres():
    _expect(_names("type=biere&volume=25") == ["A"], "type + volume")
    _expect(_names("volume=25&tri=tendance_desc") == list("AC"), "volume + tendances")
    _expect(
        _names("type=biere&prix_min=1&tri=tendance_desc") == list("BA"), "type + prix + tendances"
    )
    _expect(_names("prix_max=3&tri=prix_desc") == list("ACD"), "prix max + tri prix")
    _expect(_names("type=snack&volume=25") == [], "combinaison sans résultat")
    html = (
        _setup()
        .get("/admin/articles?campus=brest&q=Zart+B&type=biere&tri=prix_desc")
        .get_data(as_text=True)
    )
    _expect("<td>Zart B" in html and "<td>Zart A" not in html, "recherche + filtres")


def test_parametres_invalides_ignores():
    _expect(
        _names("type=xx&volume=abc&prix_min=abc&prix_max=-3&tri=zz") == list("ABCDE"),
        "valeurs ignorées",
    )
    _expect(_names("volume=%27%3B--&prix_min=1e999") == list("ABCDE"), "injection ignorée")


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
