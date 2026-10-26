"""Tests des filtres et tris de l'onglet Comptes (/admin/comptes).

Exécutable sans pytest : python -m tests.test_comptes_filtres

Couvre : filtres promotion, statut équipe, blacklist, recherche, tri par solde
Brest/Paris (comptes sans portefeuille comptés pour 0), combinaison des
critères, paramètres invalides ignorés, filtres conservés par la pagination.
"""

import os
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="foyz_comptes_filtres_"))
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
from app.models import User  # noqa: E402

ADMIN_PASSWORD = "mot-de-passe-admin"
_app = None
_client = None


def _expect(cond, label):
    if not cond:
        raise AssertionError(f"échec : {label}")


def _make(name, *, promo=None, team=None, brest=None, paris=None, bl=False, alc=False):
    user = User(
        name=name,
        username=name.lower().replace(" ", "."),
        password_hash=generate_password_hash("secret123"),
        promotion=promo,
        team_status=team,
        team_campus="brest" if team else None,
        blacklist=bl,
        blacklist_alcohol=alc,
    )
    db.session.add(user)
    db.session.flush()
    if brest is not None:
        user.wallet("brest").balance = brest
    if paris is not None:
        user.wallet("paris").balance = paris
    return user


def _setup():
    global _app, _client
    if _client is not None:
        return _client
    _app = create_app()
    with _app.app_context():
        _make("Zfiltre A", promo=2027, brest=-500)
        _make("Zfiltre B", promo=2027, team="mandat", brest=1000, paris=-200, bl=True)
        _make("Zfiltre C", promo=2028, team="ancien", brest=300, paris=700, alc=True)
        _make("Zfiltre D", brest=50)
        _make("Zfiltre E", promo=2028, brest=-9000, paris=5000, bl=True, alc=True)
        for i in range(55):
            _make(f"Zpag {i:02d}", promo=2031, brest=i * 10)
        db.session.commit()
    client = _app.test_client()
    html = client.get("/connexion").get_data(as_text=True)
    token = re.search(r'name="_csrf" value="([^"]+)"', html).group(1)
    res = client.post(
        "/connexion",
        data={"username": "admin", "password": ADMIN_PASSWORD, "campus": "brest", "_csrf": token},
    )
    _expect(res.status_code == 302, "connexion administrateur")
    _client = client
    return client


def _names(query, prefix="Zfiltre"):
    res = _setup().get(f"/admin/comptes?q={prefix}&{query}")
    _expect(res.status_code == 200, f"page servie pour « {query} » ({res.status_code})")
    html = res.get_data(as_text=True)
    return [m.split()[-1] for m in re.findall(rf"<strong>({prefix} [^<]+)</strong>", html)]


def test_sans_filtre_tri_par_nom():
    _expect(_names("") == list("ABCDE"), f"ordre par nom ({_names('')})")


def test_filtre_promotion():
    _expect(_names("promo=2027") == list("AB"), "promo 2027")
    _expect(_names("promo=2028") == list("CE"), "promo 2028")
    _expect(_names("promo=aucune") == ["D"], "sans promotion")


def test_filtre_equipe():
    _expect(_names("equipe=membre") == list("BC"), "équipe (mandat ou ancien)")
    _expect(_names("equipe=mandat") == ["B"], "mandat en cours")
    _expect(_names("equipe=ancien") == ["C"], "anciens")
    _expect(_names("equipe=aucun") == list("ADE"), "non membres")


def test_filtre_blacklist():
    _expect(_names("restriction=oui") == list("BCE"), "au moins une restriction")
    _expect(_names("restriction=blacklist") == list("BE"), "blacklist")
    _expect(_names("restriction=alcool") == list("CE"), "blacklist alcool")
    _expect(_names("restriction=aucune") == list("AD"), "aucune restriction")


def test_tri_par_solde():
    _expect(_names("tri=brest_asc") == list("EADCB"), "Brest croissant")
    _expect(_names("tri=brest_desc") == list("BCDAE"), "Brest décroissant")
    # A et D n'ont pas de portefeuille Paris : comptés pour 0, départagés par nom.
    _expect(_names("tri=paris_asc") == list("BADCE"), "Paris croissant")
    _expect(_names("tri=paris_desc") == list("ECADB"), "Paris décroissant")


def test_combinaison_des_criteres():
    _expect(
        _names("promo=2028&restriction=oui&tri=brest_asc") == list("EC"), "promo + blacklist + tri"
    )
    _expect(_names("equipe=aucun&tri=paris_desc") == list("EAD"), "non membres + tri Paris")
    _expect(_names("promo=2027&equipe=membre&restriction=blacklist") == ["B"], "trois filtres")
    _expect(_names("promo=2027&equipe=ancien") == [], "combinaison sans résultat")
    res = _setup().get("/admin/comptes?q=Zfiltre+C&promo=2028&equipe=ancien")
    _expect("<strong>Zfiltre C</strong>" in res.get_data(as_text=True), "recherche + filtres")


def test_parametres_invalides_ignores():
    _expect(
        _names("promo=abc&equipe=xx&restriction=yy&tri=zz") == list("ABCDE"), "valeurs ignorées"
    )
    _expect(_names("promo=%27%3B--&tri=brest_asc%27") == list("ABCDE"), "injection ignorée")


def test_pagination_conserve_filtres_et_tri():
    client = _setup()
    args = "q=Zpag&promo=2031&tri=brest_desc"
    page1 = client.get(f"/admin/comptes?{args}").get_data(as_text=True)
    names1 = re.findall(r"<strong>(Zpag \d+)</strong>", page1)
    _expect(len(names1) == 50 and names1[0] == "Zpag 54", f"page 1 triée ({names1[:2]})")
    _expect("55 comptes" in page1, "total affiché")
    _expect(
        "page=2" in page1 and "tri=brest_desc" in page1.split("page=2")[1][:200], "tri conservé"
    )
    page2 = client.get(f"/admin/comptes?{args}&page=2").get_data(as_text=True)
    names2 = re.findall(r"<strong>(Zpag \d+)</strong>", page2)
    _expect(names2 == [f"Zpag {i:02d}" for i in range(4, -1, -1)], f"page 2 ({names2})")


def test_filtrage_instantane_et_fragments_json():
    """La liste se met à jour par fragments, sans bouton « Appliquer »."""
    client = _setup()
    page = client.get("/admin/comptes").get_data(as_text=True)
    _expect("data-live-filter" in page, "formulaire relié au filtrage instantané")
    _expect('id="comptes-rows"' in page, "lignes remplaçables")
    _expect('id="comptes-footer"' in page, "pied de liste remplaçable")
    _expect("js/admin_filter.js" in page, "script de filtrage chargé")
    _expect('<i class="bi bi-funnel"></i> Appliquer' not in page, "bouton Appliquer retiré")

    res = client.get(
        "/admin/comptes?q=Zfiltre+B&tri=brest_desc",
        headers={"X-Foyz-Partiel": "1"},
    )
    _expect(res.is_json, f"réponse partielle JSON ({res.status_code})")
    data = res.get_json()
    _expect("<html" not in data["rows_html"], "fragment de lignes seulement")
    _expect(
        "Zfiltre B" in data["rows_html"] and "Zfiltre A" not in data["rows_html"],
        "fragment filtré et trié",
    )
    _expect("1 compte" in data["footer_html"], "total du fragment à jour")

    res = client.get(
        "/admin/comptes?q=Zpag&tri=brest_desc&page=2",
        headers={"X-Foyz-Partiel": "1"},
    )
    data = res.get_json()
    names = re.findall(r"<strong>(Zpag \d+)</strong>", data["rows_html"])
    _expect(names == [f"Zpag {i:02d}" for i in range(4, -1, -1)], f"page 2 en fragment ({names})")
    _expect("page=2" in data["footer_html"], "pagination du fragment conservée")


def test_filtres_conserves_apres_suppression_et_retour():
    """Retour à la liste et suppression d'un compte conservent filtres, tri et page."""
    client = _setup()
    args = "q=Zpag&promo=2031&tri=brest_desc&page=2"
    with _app.app_context():
        victim = _make("Zpag Suppr", promo=2031, brest=0)
        db.session.commit()
        victim_id = victim.id
    client.get(f"/admin/comptes?{args}")
    fiche = client.get(f"/admin/comptes/{victim_id}").get_data(as_text=True)
    _expect("Retour à la liste" in fiche, "bouton de retour présent")
    for part in ("q=Zpag", "promo=2031", "tri=brest_desc", "page=2"):
        _expect(part in fiche, f"retour conserve {part}")
    token = re.search(r'name="_csrf" value="([^"]+)"', fiche).group(1)
    res = client.post(
        f"/admin/comptes/{victim_id}/supprimer",
        data={"_csrf": token, "admin_password": ADMIN_PASSWORD},
    )
    _expect(res.status_code == 302, f"suppression acceptée ({res.status_code})")
    location = res.headers["Location"]
    for part in ("q=Zpag", "promo=2031", "tri=brest_desc"):
        _expect(part in location, f"après suppression : {part} conservé ({location})")
    with _app.app_context():
        _expect(db.session.get(User, victim_id) is None, "compte supprimé")
    client.get("/admin/comptes")
    fiche = client.get("/admin/comptes/1").get_data(as_text=True)
    _expect("tri=brest_desc" not in fiche, "liste vierge : aucun filtre restitué")


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
