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
from datetime import datetime
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
from app.models import Article, Event, User  # noqa: E402
from app.services import transactions as T  # noqa: E402

ADMIN_PASSWORD = "mot-de-passe-admin"
_client = None
_ids = {}


def _expect(cond, label):
    if not cond:
        raise AssertionError(f"échec : {label}")


def _article(name, kind, volume, price, campus="brest", **extra):
    article = Article(
        name=name,
        article_type=kind,
        volume_cl=volume,
        campus=campus,
        price_std=price,
        price_team=price // 2,
        active=True,
        **extra,
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
        c = _article("Zart C", "cidre", 25, 300)
        d = _article("Zart D", "snack", None, 100)
        _article("Zart E", "vin", 75, 900)
        _article("Zart P", "biere", 33, 400, campus="paris")
        _article("Zart T", "biere", 25, 300, is_tap=True, tap_number=42)
        event = Event(
            name="Zart Événement",
            campus="brest",
            starts_at=datetime(2026, 1, 1, 18, 0),
            ends_at=datetime(2026, 1, 2, 2, 0),
            token="zart-event-token",
        )
        db.session.add(event)
        db.session.flush()
        _article("Zart V", "evenement", 33, 400, event_id=event.id)
        buyer = User(
            name="Zart Acheteur",
            username="zart.acheteur",
            password_hash=generate_password_hash("secret123"),
        )
        db.session.add(buyer)
        db.session.flush()
        buyer.wallet("brest").balance = 1_000_000
        db.session.commit()
        _ids.update(A=a.id, B=b.id, C=c.id, D=d.id)
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


def test_articles_tireuse_et_evenement_exclus():
    """Les articles gérés dans Tireuses ou Événements ne polluent pas la liste."""
    page = _page("")
    _expect("Zart T" not in page, "article de tireuse absent de l'onglet Articles")
    _expect("Zart V" not in page, "article d'événement absent de l'onglet Articles")
    _expect("géré dans Tireuses" not in page, "renvoi vers l'onglet Tireuses retiré")
    _expect('<span class="badge bg-dark">tireuse</span>' not in page, "badge tireuse retiré")


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


def test_filtrage_instantane_et_fragments_json():
    """La liste se met à jour par fragments, sans bouton « Appliquer »."""
    client = _setup()
    page = client.get("/admin/articles?campus=brest").get_data(as_text=True)
    _expect("data-live-filter" in page, "formulaire relié au filtrage instantané")
    _expect('id="articles-rows"' in page, "lignes remplaçables")
    _expect('id="articles-footer"' in page, "pied de liste remplaçable")
    _expect("js/admin_filter.js" in page, "script de filtrage chargé")
    _expect('<i class="bi bi-funnel"></i> Appliquer' not in page, "bouton Appliquer retiré")

    res = client.get(
        "/admin/articles?campus=brest&q=Zart&type=biere&volume=25",
        headers={"X-Foyz-Partiel": "1"},
    )
    _expect(res.is_json, f"réponse partielle JSON ({res.status_code})")
    data = res.get_json()
    _expect("<html" not in data["rows_html"], "fragment de lignes seulement")
    _expect(
        "<td>Zart A" in data["rows_html"] and "<td>Zart B" not in data["rows_html"],
        "fragment filtré",
    )
    _expect("1 article" in data["footer_html"], "total du fragment à jour")

    res = client.get(
        "/admin/articles?campus=brest&q=Zart&type=biere&tri=prix_desc",
        headers={"X-Foyz-Partiel": "1"},
    )
    data = res.get_json()
    rows = data["rows_html"]
    _expect(
        "<td>Zart A" in rows
        and "<td>Zart B" in rows
        and rows.index("<td>Zart B") < rows.index("<td>Zart A"),
        "fragment trié par prix décroissant",
    )
    _expect("2 articles" in data["footer_html"], "compteur du fragment à jour")


def test_fiche_article_derniere_vente():
    """La fiche indique la date du dernier encaissement non annulé, s'il existe."""
    client = _setup()
    sold = client.get(f"/admin/articles/{_ids['A']}?campus=brest").get_data(as_text=True)
    _expect("Dernier encaissement" in sold, "ligne présente sur la fiche")
    _expect(
        re.search(r"<strong>\d{2}/\d{2}/\d{4} \d{2}:\d{2}</strong>", sold) is not None,
        "date affichée",
    )
    never = client.get(f"/admin/articles/{_ids['C']}?campus=brest").get_data(as_text=True)
    _expect("aucun enregistré" in never, "article jamais vendu")
    # Une vente annulée ne compte pas : article dédié, une seule vente puis annulation.
    with create_app().app_context():
        extra = _article("Dernier encaissement test", "snack", None, 100)
        buyer = db.session.query(User).filter_by(username="zart.acheteur").one()
        db.session.commit()
        sale = T.create_purchase(
            operator_label="test",
            campus="brest",
            items=[{"article_id": extra.id, "quantity": 1}],
            contributor_ids=[buyer.id],
        )
        extra_id = extra.id
        page = client.get(f"/admin/articles/{extra_id}?campus=brest").get_data(as_text=True)
        _expect("aucun enregistré" not in page, "vente non annulée comptée")
        T.cancel_transaction(sale, None, verified=True)
        db.session.commit()
    after = client.get(f"/admin/articles/{extra_id}?campus=brest").get_data(as_text=True)
    _expect("aucun enregistré" in after, "vente annulée ignorée")


def _form_token(html):
    match = re.search(r'name="_csrf" value="([^"]+)"', html)
    _expect(match is not None, "jeton CSRF présent")
    return match.group(1)


def test_filtres_conserves_apres_modification_et_suppression():
    """Modifier ou supprimer un article ramène sur la liste filtrée et triée."""
    client = _setup()
    args = "q=Zart&type=biere&volume=25&prix_max=9&tri=prix_desc"
    with create_app().app_context():
        extra = _article("Modif Suppr Test", "snack", None, 100)
        db.session.commit()
        extra_id = extra.id
    client.get(f"/admin/articles?campus=brest&{args}")

    def _same_view(location, label):
        for part in ("q=Zart", "type=biere", "volume=25", "prix_max=9", "tri=prix_desc"):
            _expect(part in location, f"{label} : {part} conservé ({location})")

    fiche = client.get(f"/admin/articles/{extra_id}?campus=brest").get_data(as_text=True)
    _expect("tri=prix_desc" in fiche, "lien Retour de la fiche conserve les filtres")
    token = _form_token(fiche)
    res = client.post(
        f"/admin/articles/{extra_id}",
        data={
            "_csrf": token,
            "name": "Modif Suppr Test 2",
            "article_type": "snack",
            "price_std": "1",
            "price_team": "1",
        },
    )
    _expect(res.status_code == 302, f"modification acceptée ({res.status_code})")
    _same_view(res.headers["Location"], "après modification")

    liste = client.get(res.headers["Location"]).get_data(as_text=True)
    token = _form_token(liste)
    res = client.post(f"/admin/articles/{extra_id}/supprimer", data={"_csrf": token})
    _expect(res.status_code == 302, f"suppression acceptée ({res.status_code})")
    _same_view(res.headers["Location"], "après suppression")

    # Une nouvelle consultation sans filtre remet la mémoire à zéro.
    client.get("/admin/articles?campus=brest")
    with create_app().app_context():
        extra = _article("Modif Suppr Test 3", "snack", None, 100)
        db.session.commit()
        extra_id = extra.id
    res = client.post(f"/admin/articles/{extra_id}/supprimer", data={"_csrf": token})
    _expect("tri=" not in res.headers["Location"], "liste vierge : aucun filtre restitué")


def test_degre_alcool_modifiable_et_efface_si_non_alcoolise():
    client = _setup()
    with create_app().app_context():
        article = _article("Degré test", "biere", 33, 500)
        db.session.commit()
        article_id = article.id

    fiche = client.get(f"/admin/articles/{article_id}?campus=brest").get_data(as_text=True)
    _expect('name="alcohol_degree"' in fiche, "champ degré présent sur la fiche article")
    _expect("Affiché dans le catalogue public" not in fiche, "commentaire du champ degré retiré")
    _expect("Prix standard (€, optionnel)" not in fiche, "prix standard rendu obligatoire")
    _expect("Prix équipe (€, optionnel)" not in fiche, "prix équipe rendu obligatoire")
    _expect(
        re.search(r'name="price_std"[^>]*\srequired', fiche) is not None,
        "champ prix standard marqué obligatoire",
    )
    _expect(
        re.search(r'name="price_team"[^>]*\srequired', fiche) is not None,
        "champ prix équipe marqué obligatoire",
    )
    token = _form_token(fiche)
    data = {
        "_csrf": token,
        "name": "Degré test",
        "article_type": "biere",
        "volume_cl": "33",
        "is_alcohol": "on",
        "alcohol_degree": "6,5",
        "price_std": "5.00",
        "price_team": "3.00",
    }
    res = client.post(f"/admin/articles/{article_id}?campus=brest", data=data)
    _expect(res.status_code == 302, f"degré enregistré ({res.status_code})")
    with create_app().app_context():
        _expect(
            db.session.get(Article, article_id).alcohol_degree == 6.5,
            "degré stocké avec la virgule décimale",
        )

    data.pop("is_alcohol")
    res = client.post(f"/admin/articles/{article_id}?campus=brest", data=data)
    _expect(res.status_code == 302, f"article non alcoolisé accepté ({res.status_code})")
    with create_app().app_context():
        _expect(
            db.session.get(Article, article_id).alcohol_degree is None,
            "degré effacé quand l'article n'est plus alcoolisé",
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
