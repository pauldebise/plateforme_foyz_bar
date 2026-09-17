"""Tests d'internationalisation (P10-7).

Exécutable sans pytest : python -m tests.test_i18n

Couvre :
- français par défaut, bascule FR/EN mémorisée en session ;
- négociation via Accept-Language sans session ;
- traductions effectives sur les pages publiques (accueil, prix, règles,
  connexion, erreurs) et catalogues compilés présents ;
- interface équipe/administration inchangée (français) ;
- retour de bascule limité au site (Referer externe ignoré) et 404 langue
  inconnue.
"""

import os
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="foyz_i18n_"))
os.environ["DATABASE_URL"] = (
    os.environ.get("FOYZ_TEST_DATABASE_URL") or f"sqlite:///{_TMP / 'app.db'}"
)
os.environ["UPLOAD_DIR"] = str(_TMP / "uploads")
os.environ["SECRET_KEY"] = "test-secret-key-0123456789abcdef0123456789abcdef"
os.environ["ADMIN_PASSWORD"] = "mot-de-passe-admin"
os.environ.pop("FLASK_ENV", None)

from app import create_app  # noqa: E402
from app.extensions import db  # noqa: E402
from app.models import Article  # noqa: E402

ADMIN_PASSWORD = "mot-de-passe-admin"
MO_FILE = ROOT / "translations" / "en" / "LC_MESSAGES" / "messages.mo"


def _expect(cond, label):
    if not cond:
        raise AssertionError(f"échec : {label}")


def _csrf(client, path="/connexion"):
    html = client.get(path).get_data(as_text=True)
    match = re.search(r'name="_csrf" value="([^"]+)"', html)
    _expect(match is not None, f"jeton CSRF présent sur {path}")
    return match.group(1)


def _seed_article():
    app = create_app()
    with app.app_context():
        if db.session.query(Article).filter(Article.name == "Article i18n").first() is None:
            db.session.add(
                Article(
                    name="Article i18n",
                    article_type="biere",
                    is_alcohol=True,
                    price_std_brest=250,
                    price_std_paris=250,
                    active=True,
                )
            )
            db.session.commit()
    return app


def test_catalogues_compiles():
    _expect(MO_FILE.is_file() and MO_FILE.stat().st_size > 0, "catalogue anglais compilé (.mo)")
    pou = ROOT / "translations" / "en" / "LC_MESSAGES" / "messages.po"
    _expect(pou.is_file(), "source .po versionnée")
    _expect((ROOT / "babel.cfg").is_file(), "configuration d'extraction présente")


def test_francais_par_defaut():
    app = _seed_article()
    client = app.test_client()
    html = client.get("/").get_data(as_text=True)
    _expect('<html lang="fr">' in html, "langue française par défaut")
    _expect("Accueil" in html, "navigation française")
    _expect("Accueil" in html and "Home" not in html, "pas de fuite anglaise")


def test_bascule_en_session():
    app = _seed_article()
    client = app.test_client()
    res = client.get("/langue/en")
    _expect(res.status_code == 302, "bascule en redirection")
    html = client.get("/").get_data(as_text=True)
    _expect('<html lang="en">' in html, "anglais actif")
    _expect("Home" in html and "Useful links" in html, "navigation traduite")

    catalogue = client.get("/catalogue").get_data(as_text=True)
    for marker in ("Standard prices", "Item", "Brest price"):
        _expect(marker in catalogue, f"catalogue traduit : {marker}")
    login = client.get("/connexion").get_data(as_text=True)
    _expect("Log in" in login and "Username" in login, "connexion traduite")
    reglement = client.get("/reglement").get_data(as_text=True)
    _expect("Rules of procedure" in reglement, "règlement traduit")

    _expect(client.get("/langue/en").status_code == 302, "bascule conservée")
    _expect(client.get("/langue/de").status_code == 404, "langue inconnue refusée")


def test_accept_language_sans_session():
    app = _seed_article()
    client = app.test_client()
    html = client.get("/", headers={"Accept-Language": "en-GB,en;q=0.9,fr;q=0.5"}).get_data(
        as_text=True
    )
    _expect('<html lang="en">' in html, "négociation Accept-Language")
    html_fr = client.get("/", headers={"Accept-Language": "fr-FR,fr;q=0.9"}).get_data(as_text=True)
    _expect('<html lang="fr">' in html_fr, "préférence française respectée")


def test_referer_externe_ignore():
    app = _seed_article()
    client = app.test_client()
    res = client.get("/langue/en", headers={"Referer": "https://exemple-malveillant.test/phish"})
    _expect(res.status_code == 302, "redirection")
    _expect(res.headers["Location"] == "/", f"retour à l'accueil ({res.headers['Location']})")
    res = client.get("/langue/en", headers={"Referer": "http://localhost/catalogue"})
    _expect(res.headers["Location"] in ("/catalogue", "http://localhost/catalogue"), "retour local")


def test_interfaces_internes_inchangees():
    app = _seed_article()
    client = app.test_client()
    token = _csrf(client)
    res = client.post(
        "/connexion",
        data={"username": "admin", "password": ADMIN_PASSWORD, "campus": "brest", "_csrf": token},
    )
    _expect(res.status_code == 302, "connexion administrateur")
    client.get("/langue/en")
    payment = client.get("/equipe/paiement").get_data(as_text=True)
    _expect("Paiement" in payment, "interface équipe en français")
    _expect("Encaisser" in payment, "boutons caisse en français")


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
